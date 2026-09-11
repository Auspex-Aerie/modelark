"""Real pipes/children with synthetic protocols; no live sources or destinations."""
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from modelark import codec_supervisor as cs, streamznn as sz
from modelark.codec_resources import CodecMemoryPolicy, CodecResourceRefusal

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guard")
POLICY = CodecMemoryPolicy(8 << 30, 0)


def runtime():
    return {"version": "modelark.codec-runtime.v1",
            "startup": "isolated-no-site-guard-then-site.v1",
            "python": "test", "executable": "/test/python", "prefix": "/test",
            "base_prefix": "/base", "address_space_bytes": [8 << 30, 8 << 30],
            "parent_death_signal": 9,
            "zipnn": {"distribution_version": "test", "module": "/test/zipnn.py"},
            "torch": {"distribution_version": "test", "module": "/test/torch.py"}}


def header(original=4, stored=36):
    value = bytearray(32)
    value[:4] = b"ZN\x00\x05"
    value[5], value[8], value[15] = 10, 1, 1
    value[16:24] = original.to_bytes(8, "little")
    value[24:32] = stored.to_bytes(8, "little")
    return bytes(value)


@pytest.fixture(autouse=True)
def synthetic_admission(monkeypatch):
    # Transport tests do not qualify host RAM. The qualification CLI samples
    # real headroom. The real child AS guard is NOT patched or disabled here.
    monkeypatch.setattr(cs, "available_memory", lambda: {"available_bytes": 10 << 30})


def decode(source=None, **kwargs):
    defaults = dict(policy=POLICY, max_stored_bytes=1 << 20,
                    max_decoded_bytes=1 << 20, remaining_bytes=1 << 20)
    return cs.guarded_zipnn_frame(source if source is not None else io.BytesIO(header() + b"abcd"),
                                  **{**defaults, **kwargs})


def launch_fake(monkeypatch, body, *, add_metadata=True):
    children = []
    def launch(request, output):
        program = ("import os,sys,signal,time,json\n"
                   "from modelark.codec_process import bind_parent\n"
                   "bind_parent(int(sys.argv[2]))\n"
                   "output=int(sys.argv[1])\n")
        if add_metadata:
            program += f"metadata={json.dumps(runtime()).encode()!r}\n" + '''
real_write=os.write
metadata_sent=False
def write(fd, data):
    global metadata_sent
    if fd==output and data[:1]==b'O' and not metadata_sent:
        real_write(fd,b'M'+len(metadata).to_bytes(8,'little')+metadata)
        metadata_sent=True
    return real_write(fd,data)
os.write=write
'''
        program += body
        proc = subprocess.Popen([sys.executable, "-c", program, str(output), str(os.getpid())],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, close_fds=True,
                                pass_fds=(output,), bufsize=0)
        children.append(proc)
        return proc
    monkeypatch.setattr(cs, "_launch", launch)
    return children


def assert_reaped(children):
    assert children
    for child in children:
        assert child.poll() is not None
        assert child.stdin.closed and child.stdout.closed
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)


def legacy(source=None, **kwargs):
    return cs.guarded_legacy_stream(io.BytesIO(b"abcd") if source is None else source,
                                    stored_bytes=4, **kwargs)


def legacy_metadata():
    data = json.dumps(runtime()).encode()
    return f"os.write(output, {b'M' + len(data).to_bytes(8, 'little') + data!r})\n"


def test_legacy_full_duplex_bounded_output_and_exact_completion(monkeypatch):
    children = launch_fake(monkeypatch, legacy_metadata() + '''
os.write(output,b'D'+(2).to_bytes(8,'little')+b'ab')
assert sys.stdin.buffer.read()==b'abcd'
os.write(output,b'D'+(2).to_bytes(8,'little')+b'cd')
os.write(output,b'E'+(4).to_bytes(8,'little'))
''', add_metadata=False)
    evidence = []
    with legacy(on_complete=evidence.append) as parts:
        assert b"".join(parts) == b"abcd"
    assert len(evidence) == 1
    assert_reaped(children)


@pytest.mark.parametrize("body,kind", [
    ("os.write(output,b'D'+(65537).to_bytes(8,'little'))", "INVALID"),
    ("os.write(output,b'D'+bytes(8))", "INVALID"),
    ("os.write(output,b'D'+(4).to_bytes(8,'little')+b'ab')", "WORKER_FAILED"),
    ("os.write(output,b'D'+(2).to_bytes(8,'little')+b'ab')", "WORKER_FAILED"),
    ("os.write(output,b'E'+(4).to_bytes(8,'little'))", "INVALID"),
    ("os.write(output,b'E'+bytes(8)+b'x')", "INVALID"),
    ("os.write(output,b'E'+bytes(8));sys.exit(2)", "WORKER_FAILED"),
    ("os.write(output,b'D'+(2).to_bytes(8,'little')+b'ab'+b'R'+(3).to_bytes(8,'little')+b'ram')", "RESOURCE"),
    ("os.write(output,b'I'+(3).to_bytes(8,'little')+b'bad')", "CODEC_INVALID"),
    ("os.write(output,b'U'+(3).to_bytes(8,'little')+b'dep')", "UNSUPPORTED"),
])
def test_legacy_protocol_never_certifies_partial_or_unavailable(monkeypatch, body, kind):
    children = launch_fake(monkeypatch, legacy_metadata() + "sys.stdin.buffer.read()\n" + body,
                           add_metadata=False)
    evidence = []
    with pytest.raises(cs.WorkerRefusal) as caught:
        with legacy(on_complete=evidence.append) as parts:
            list(parts)
    assert caught.value.kind == kind
    assert evidence == []
    assert_reaped(children)


def test_legacy_destination_error_reaps_and_preserves_identity(monkeypatch):
    children = launch_fake(monkeypatch, legacy_metadata() + '''
sys.stdin.buffer.read()
while True: os.write(output,b'D'+(65536).to_bytes(8,'little')+bytes(65536))
''', add_metadata=False)
    failure = OSError(errno.ENOSPC, "destination full")
    with pytest.raises(OSError) as caught:
        with legacy() as parts:
            assert next(parts)
            raise failure
    assert caught.value is failure
    assert_reaped(children)


@pytest.mark.parametrize("phase", ["input", "compute", "output"])
def test_legacy_cancel_kills_reaps_before_return(monkeypatch, phase):
    body = {"input": "while True: time.sleep(.02)",
            "compute": "sys.stdin.buffer.read()\nwhile True: time.sleep(.02)",
            "output": "sys.stdin.buffer.read()\nwhile True: os.write(output,b'D'+(65536).to_bytes(8,'little')+bytes(65536))"}[phase]
    children = launch_fake(monkeypatch, legacy_metadata() + body, add_metadata=False)
    failure, started = RuntimeError("operator stop"), time.monotonic()
    def check():
        if time.monotonic() - started > .25:
            raise failure
    with pytest.raises(RuntimeError) as caught:
        with cs.guarded_legacy_stream(io.BytesIO(bytes(1 << 20)), stored_bytes=1 << 20,
                                     check=check) as parts:
            list(parts)
    assert caught.value is failure
    assert_reaped(children)


@pytest.mark.parametrize("failure", [BrokenPipeError(), MemoryError()])
@pytest.mark.parametrize("fail_at", [1, 2, 3])
def test_legacy_worker_never_appends_error_after_partial_packet(monkeypatch, failure, fail_at):
    from modelark import codec_worker as worker, codec_legacy
    from modelark.artifact_policy import qualified_policy
    monkeypatch.setattr(worker, "initialize_worker", lambda *a: None)
    monkeypatch.setattr(worker, "runtime_record", runtime)
    monkeypatch.setattr(codec_legacy, "chunks", lambda source, *a: (b"abcd",))
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": io.BytesIO(b"")})())
    request = {"version": "modelark.codec-worker.v3-legacy", "stored_bytes": 0,
               "dtype": "bfloat16", "policy": qualified_policy().memory.to_record()}
    sent = []
    def send(fd, data):
        sent.append(data)
        if len(sent) == fail_at:
            raise failure
    monkeypatch.setattr(worker, "_send", send)
    assert worker.main(["worker", "99", str(os.getpid()), json.dumps(request)]) == 1
    assert [s[:1] for s in sent] == [b"M", b"D", b"E"][:fail_at]


def test_success_short_input_and_diagnostic_flood(monkeypatch):
    children = launch_fake(monkeypatch, '''
data=sys.stdin.buffer.read()
for _ in range(64):
    os.write(1,b'x'*65536)
    os.write(2,b'y'*65536)
for piece in [b'O', (4).to_bytes(8,'little'), b'ab', b'cd']:
    os.write(output,piece)
''')
    class Short(io.BytesIO):
        def read(self, size):
            assert 0 < size <= cs.IO_BYTES
            return super().read(min(size, 3))
    source = Short(header() + b"abcd" + b"next frame")
    checks = []
    with decode(source, check=lambda: checks.append(source.tell())) as parts:
        assert b"".join(parts) == b"abcd"
    assert source.tell() == 36 and not source.closed
    assert len(checks) > 20
    assert_reaped(children)


@pytest.mark.parametrize("body,kind", [
    ("sys.stdin.buffer.read(); os.abort()", "WORKER_FAILED"),
    ("sys.stdin.buffer.read(); os.write(output,b'O'+(4).to_bytes(8,'little')+b'ab')", "WORKER_FAILED"),
    ("sys.stdin.buffer.read(); os.write(output,b'O'+(5).to_bytes(8,'little'))", "INVALID"),
    ("sys.stdin.buffer.read(); os.write(output,b'X'+bytes(8))", "INVALID"),
    ("os.write(output,b'R'+(4097).to_bytes(8,'little'))", "INVALID"),
    ("os.write(output,b'R'+(3).to_bytes(8,'little')+b'ram')", "RESOURCE"),
    ("sys.stdin.buffer.read(); os.write(output,b'O'+(4).to_bytes(8,'little')+b'abcde')", "INVALID"),
    ("sys.stdin.buffer.read(); os.write(output,b'O'+(4).to_bytes(8,'little')+b'abcd'); sys.exit(2)", "WORKER_FAILED"),
])
def test_untrusted_worker_protocol_and_crash(monkeypatch, body, kind):
    children = launch_fake(monkeypatch, "import resource\nresource.setrlimit(resource.RLIMIT_CORE,(0,0))\n" + body)
    with pytest.raises(cs.WorkerRefusal) as caught:
        with decode() as parts:
            list(parts)
    assert caught.value.kind == kind
    assert_reaped(children)


@pytest.mark.parametrize("phase", ["input", "compute", "output"])
def test_cancel_kills_reaps_and_preserves_exception(monkeypatch, phase):
    body = {
        "input": "while True: time.sleep(.02)",
        "compute": "sys.stdin.buffer.read()\nwhile True: time.sleep(.02)",
        "output": "sys.stdin.buffer.read()\nos.write(output,b'O'+(1<<20).to_bytes(8,'little'))\n"
                  "while True: os.write(output,b'x'*4096); time.sleep(.02)",
    }[phase]
    children = launch_fake(monkeypatch, body)
    problem = RuntimeError("stop or detached source")
    start = time.monotonic()
    checks = 0
    def check():
        nonlocal checks
        checks += 1
        if time.monotonic() - start > .25:
            raise problem
    source = io.BytesIO(header(original=1 << 20, stored=1 << 20) + bytes((1 << 20) - 32))
    with pytest.raises(RuntimeError) as caught:
        with decode(source, check=check) as parts:
            for _ in parts:
                pass
    assert caught.value is problem
    assert time.monotonic() - start < 3
    assert checks > 4
    assert not source.closed
    assert_reaped(children)


def test_consumer_enospc_reaps_before_outer_fence_exit(monkeypatch):
    children = launch_fake(monkeypatch, "sys.stdin.buffer.read()\nos.write(output,b'O'+(1<<20).to_bytes(8,'little'))\n"
                           "while True: os.write(output,b'x'*65536)")
    failure = OSError(errno.ENOSPC, "destination full")
    with pytest.raises(OSError) as caught:
        try:
            with decode(io.BytesIO(header(original=1 << 20) + b"abcd")) as parts:
                assert next(parts)
                raise failure
        finally:
            assert_reaped(children)  # Models source fence release ordering.
    assert caught.value is failure


def test_source_error_identity_and_truncation(monkeypatch):
    children = launch_fake(monkeypatch, "sys.stdin.buffer.read()")
    problem = OSError(errno.EIO, "archive read failed")
    class Broken(io.BytesIO):
        def read(self, size):
            if self.tell() >= 32:
                raise problem
            return super().read(size)
    with pytest.raises(OSError) as caught:
        with decode(Broken(header() + b"abcd")) as parts:
            list(parts)
    assert caught.value is problem
    with pytest.raises(sz.StreamZnnError, match="truncated"):
        with decode(io.BytesIO(header() + b"ab")) as parts:
            list(parts)
    assert_reaped(children)


def test_header_and_resource_refusal_precede_spawn_payload(monkeypatch):
    monkeypatch.setattr(cs, "_launch", lambda *a: pytest.fail("must not spawn"))
    source = io.BytesIO(header(original=2 << 20) + b"abcd")
    with pytest.raises(sz.DecodeLimitExceeded):
        with decode(source):
            pytest.fail("must not yield")
    assert source.tell() == 32
    monkeypatch.setattr(cs, "available_memory", lambda: {"available_bytes": 1})
    source = io.BytesIO(header() + b"abcd")
    with pytest.raises(CodecResourceRefusal):
        with decode(source):
            pytest.fail("must not yield")
    assert source.tell() == 32


def test_no_authority_fds_inherited(monkeypatch, tmp_path):
    secret = tmp_path / "archive-fence-catalog-destination"
    with secret.open("wb") as authority:
        os.set_inheritable(authority.fileno(), True)
        children = launch_fake(monkeypatch, f'''
assert all(os.readlink('/proc/self/fd/'+name) != {str(secret)!r}
           for name in os.listdir('/proc/self/fd') if os.path.exists('/proc/self/fd/'+name))
sys.stdin.buffer.read()
os.write(output,b'O'+(4).to_bytes(8,'little')+b'abcd')
''')
        with decode() as parts:
            assert b"".join(parts) == b"abcd"
        assert_reaped(children)


def test_actual_launch_does_not_inherit_unrelated_fd(monkeypatch, tmp_path):
    launch = cs._launch
    processes = []
    def capture(request, output):
        proc = launch(request, output)
        processes.append(proc)
        return proc
    monkeypatch.setattr(cs, "_launch", capture)
    path = tmp_path / "authority"
    with path.open("wb") as authority:
        os.set_inheritable(authority.fileno(), True)
        # Worker waits for stdin. Examine its own FD table after exec, not a fake
        # launcher. No source bytes are sent until next(parts).
        with decode():
            proc = processes[0]
            deadline = time.monotonic() + 3
            while True:
                fdroot = Path(f"/proc/{proc.pid}/fd")
                links = []
                for entry in fdroot.iterdir():
                    try:
                        links.append(os.readlink(entry))
                    except FileNotFoundError:
                        pass
                assert str(path) not in links
                if "modelark.codec_worker" in Path(f"/proc/{proc.pid}/cmdline").read_bytes().decode():
                    break
                assert time.monotonic() < deadline
                time.sleep(.01)
    assert_reaped(processes)


def test_real_worker_guard_refuses_before_native_import():
    with pytest.raises(cs.WorkerRefusal) as caught:
        with decode(policy=CodecMemoryPolicy(32 << 20, 0)) as parts:
            list(parts)
    assert caught.value.kind in {"RESOURCE", "INVALID", "WORKER_FAILED", "UNSUPPORTED"}
    # Native dependencies may raise ImportError after guard-denied mappings;
    # the explicit unavailable-dependency result is also fail-closed. None of
    # these paths may retry unguarded or certify success.


def test_real_writer_frame_decodes_hash_and_closes_without_source_ownership():
    zipnn = pytest.importorskip("zipnn")
    original = b"\x00\x3f\x80\x3f" * (1 << 18)
    # ZipNN reorders its input in-place, even if handed immutable Python bytes.
    # Give it an owned copy so the expected original remains an independent oracle.
    stored = zipnn.ZipNN(input_format="byte", bytearray_dtype="bfloat16", threads=1).compress(bytearray(original))
    source = io.BytesIO(stored)
    evidence = []
    with decode(source, max_stored_bytes=len(stored), max_decoded_bytes=len(original),
                remaining_bytes=len(original), on_complete=evidence.append) as parts:
        digest = hashlib.sha256()
        total = 0
        for block in parts:
            assert 0 < len(block) <= cs.IO_BYTES
            total += len(block)
            digest.update(block)
    assert total == len(original)
    assert digest.digest() == hashlib.sha256(original).digest()
    assert not source.closed
    assert len(evidence) == 1
    assert evidence[0]["admission"] == {"available_bytes": 10 << 30}
    actual = evidence[0]["worker"]
    assert actual["executable"] == sys.executable
    assert actual["prefix"] == sys.prefix
    assert actual["zipnn"]["module"] == str(Path(zipnn.__file__).resolve())
    assert actual["address_space_bytes"] == [POLICY.address_space_bytes] * 2


def test_worker_origin_is_not_resolved_from_untrusted_cwd(monkeypatch, tmp_path):
    fake = tmp_path / "modelark"
    fake.mkdir()
    (fake / "__init__.py").write_text("")
    (fake / "codec_worker.py").write_text(
        "import os,sys\nsys.stdin.buffer.read()\n"
        "os.write(int(sys.argv[1]),b'O'+(4).to_bytes(8,'little')+b'fake')\n")
    monkeypatch.chdir(tmp_path)
    # A fake worker certifies malformed data; the package actually imported by
    # the parent must be launched and refuse it instead, under its real guard.
    with pytest.raises(cs.WorkerRefusal):
        with decode() as parts:
            list(parts)


@pytest.mark.parametrize("phase", ["input", "compute", "output"])
def test_parent_death_kills_worker(phase):
    # This subprocess becomes a subreaper solely inside the test, allowing it
    # to reap the orphan itself instead of relying on the host PID 1 behavior.
    program = r'''
import ctypes, io, os, signal, subprocess, sys, time
from modelark import codec_supervisor as cs
from modelark.codec_resources import CodecMemoryPolicy
assert ctypes.CDLL(None).prctl(36,1,0,0,0)==0
r,w=os.pipe()
parent=os.fork()
if parent==0:
    os.close(r)
    cs.available_memory=lambda: {"available_bytes": 10<<30}
    real=cs._launch
    def launch(request,output):
        phase=sys.argv[1]
        if phase=='input':
            proc=real(request,output)
        else:
            body="import ctypes,os,sys\nfrom modelark.codec_process import bind_parent\nbind_parent(int(sys.argv[2]))\nsys.stdin.buffer.read()\n"
            if phase=='compute':
                body+="ctypes.CDLL(None).pause()\n"
            else:
                body+="import json\nm=sys.argv[3].encode()\nos.write(int(sys.argv[1]),b'M'+len(m).to_bytes(8,'little')+m)\nos.write(int(sys.argv[1]),b'O'+(1<<20).to_bytes(8,'little'))\nwhile True: os.write(int(sys.argv[1]),b'x'*65536)\n"
            proc=subprocess.Popen([sys.executable,'-c',body,str(output),str(os.getpid()),sys.argv[2]],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
                close_fds=True,pass_fds=(output,),bufsize=0)
        os.write(w,str(proc.pid).encode()+b'\n')
        return proc
    cs._launch=launch
    h=bytearray(32); h[:4]=b'ZN\x00\x05'; h[5]=10; h[8]=h[15]=1
    size=1<<20
    h[16:24]=size.to_bytes(8,'little'); h[24:32]=(36).to_bytes(8,'little')
    with cs.guarded_zipnn_frame(io.BytesIO(h+b'abcd'),policy=CodecMemoryPolicy(8<<30,0),
             max_stored_bytes=36,max_decoded_bytes=size,remaining_bytes=size) as parts:
        if sys.argv[1]!='input':
            next(parts)
        while True: time.sleep(.01)
os.close(w)
worker=int(os.read(r,100).strip()); os.close(r)
time.sleep(.2)
os.kill(parent,signal.SIGKILL)
os.waitpid(parent,0)
pid,status=os.waitpid(worker,0)
assert pid==worker and os.WIFSIGNALED(status) and os.WTERMSIG(status)==signal.SIGKILL
print('parent-death-contained')
'''
    result = subprocess.run([sys.executable, "-c", program, phase, json.dumps(runtime())], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "parent-death-contained"


def test_launch_failure_closes_pipes(monkeypatch):
    before = len(os.listdir("/proc/self/fd"))
    failure = OSError("cannot spawn")
    def fail(*args):
        raise failure
    monkeypatch.setattr(cs, "_launch", fail)
    with pytest.raises(OSError) as caught:
        with decode():
            pass
    assert caught.value is failure
    assert len(os.listdir("/proc/self/fd")) == before


def test_worker_request_is_closed_world():
    from modelark.codec_worker import run
    with pytest.raises(ValueError, match="fields"):
        run({"version": "modelark.codec-worker.v2", "source_path": "/unauthorized"}, None)
    assert json.loads(json.dumps(POLICY.to_record())) == POLICY.to_record()


@pytest.mark.parametrize("failure", [BrokenPipeError(), MemoryError()])
@pytest.mark.parametrize("fail_at", [1, 2, 3, 4])
def test_worker_never_appends_error_after_success_header(monkeypatch, failure, fail_at):
    from modelark import codec_worker as worker
    monkeypatch.setattr(worker, "initialize_worker", lambda policy, pid: None)
    monkeypatch.setattr(worker, "_policy", lambda request: POLICY)
    monkeypatch.setattr(worker, "run", lambda request, source: b"abcd")
    monkeypatch.setattr(worker, "runtime_record", runtime)
    sent = []
    def send(fd, data):
        sent.append(data)
        if len(sent) >= fail_at:
            raise failure
    monkeypatch.setattr(worker, "_send", send)
    assert worker.main(["worker", "99", str(os.getpid()), '{"version":"modelark.codec-worker.v2"}']) == 1
    metadata = json.dumps(runtime(), separators=(",", ":")).encode()
    assert sent == [b"M" + len(metadata).to_bytes(8, "little"), metadata,
                    b"O" + (4).to_bytes(8, "little"), b"abcd"][:fail_at]


def test_parent_guard_failure_is_not_reported_as_ram(monkeypatch):
    from modelark import codec_worker as worker
    def fail(policy, pid):
        raise worker.WorkerInitializationError("no parent guard")
    monkeypatch.setattr(worker, "initialize_worker", fail)
    monkeypatch.setattr(worker, "_policy", lambda request: POLICY)
    sent = []
    monkeypatch.setattr(worker, "_send", lambda fd, data: sent.append(data))
    assert worker.main(["worker", "99", str(os.getpid()), "{}"]) == 1
    assert len(sent) == 1 and sent[0][:1] == b"I"
    assert sent[0][9:] == b"codec worker initialization failed"


@pytest.mark.parametrize("payload", [
    b"", b"{}", b"[]", b"null", b"\xff", b"[" * 1500 + b"]" * 1500,
    json.dumps({**runtime(), "address_space_bytes": [1, 1]}).encode(),
    json.dumps({**runtime(), "parent_death_signal": True}).encode(),
    json.dumps({**runtime(), "extra": "not permitted"}).encode(),
    json.dumps({**runtime(), "zipnn": {"distribution_version": "x", "module": "relative"}}).encode(),
])
def test_malformed_metadata_never_reaches_original_output(monkeypatch, payload):
    children = launch_fake(monkeypatch,
        f"sys.stdin.buffer.read(); m={payload!r}\n"
        "os.write(output,b'M'+len(m).to_bytes(8,'little')+m+b'O'+(4).to_bytes(8,'little')+b'abcd')",
        add_metadata=False)
    completed = []
    yielded = []
    with pytest.raises(cs.WorkerRefusal) as caught:
        with decode(on_complete=completed.append) as parts:
            for piece in parts:
                yielded.append(piece)
    assert caught.value.kind == "INVALID"
    assert not completed and not yielded
    assert_reaped(children)


@pytest.mark.parametrize("suffix", [
    "b'M'+(8193).to_bytes(8,'little')",
    "b'M'+(2).to_bytes(8,'little')+b'{'",
    "b'O'+(4).to_bytes(8,'little')+b'abcd'",  # missing metadata
    "frame",  # no original response
    "frame+frame",  # repeated metadata
    "frame+b'R'+(3).to_bytes(8,'little')+b'ram'",  # error after metadata
])
def test_metadata_protocol_phase_failures(monkeypatch, suffix):
    body = (f"sys.stdin.buffer.read(); m={json.dumps(runtime()).encode()!r}\n"
            "frame=b'M'+len(m).to_bytes(8,'little')+m\n"
            f"os.write(output,{suffix})")
    children = launch_fake(monkeypatch, body, add_metadata=False)
    completed = []
    with pytest.raises(cs.WorkerRefusal):
        with decode(on_complete=completed.append) as parts:
            list(parts)
    assert not completed
    assert_reaped(children)


@pytest.mark.parametrize("finish", ["close", "nonzero", "success"])
def test_completion_evidence_requires_exhaustion_and_success(monkeypatch, finish):
    children = launch_fake(monkeypatch,
        "sys.stdin.buffer.read(); os.write(output,b'O'+(4).to_bytes(8,'little')+b'abcd')\n"
        + ("sys.exit(2)" if finish == "nonzero" else ""))
    sample = {"available_bytes": 10 << 30, "observations": {"sample": "this launch only"}}
    monkeypatch.setattr(cs, "available_memory", lambda: sample)
    completed = []
    def consume():
        with decode(on_complete=completed.append) as parts:
            assert next(parts) == b"abcd"
            assert not completed
            if finish != "close":
                assert list(parts) == []
    if finish == "nonzero":
        with pytest.raises(cs.WorkerRefusal):
            consume()
    else:
        consume()
    if finish == "success":
        assert completed == [{"admission": sample, "worker": runtime()}]
        assert completed[0]["admission"] is sample
    else:
        assert not completed
    assert_reaped(children)


@pytest.mark.parametrize("closed", [[], [0], [1], [2], [0, 1], [0, 2], [1, 2], [0, 1, 2]])
def test_real_launch_with_closed_standard_descriptors(closed):
    # Close only disposable process descriptors; preserve a private result FD.
    program = r'''
import io, os, sys
from modelark import codec_supervisor as cs
from modelark.codec_resources import CodecMemoryPolicy
result=os.dup(1)
assert result>2
for fd in map(int, sys.argv[1:]): os.close(fd)
cs.available_memory=lambda: {"available_bytes": 10<<30}
real_launch=cs._launch
def mismatch(request, writer):
    # Force a deterministic header refusal before any native import/allocation;
    # this test qualifies the real launch/FD path, not malformed codec behavior.
    return real_launch({**request, 'stored_bytes':request['stored_bytes']+1}, writer)
cs._launch=mismatch
h=bytearray(32); h[:4]=b'ZN\x00\x05'; h[5]=10; h[8]=h[15]=1
h[16:24]=(4).to_bytes(8,'little'); h[24:32]=(36).to_bytes(8,'little')
try:
    with cs.guarded_zipnn_frame(io.BytesIO(h+b'abcd'),policy=CodecMemoryPolicy(8<<30,0),
            max_stored_bytes=36,max_decoded_bytes=4,remaining_bytes=4) as parts:
        list(parts)
except cs.WorkerRefusal as exc:
    # Only the actual worker's typed refusal proves the result pipe survived.
    assert exc.kind=='INVALID', exc
else:
    raise AssertionError('malformed frame accepted')
os.write(result,b'closed-stdio-contained')
os._exit(0)
'''
    result = subprocess.run([sys.executable, "-c", program, *map(str, closed)], capture_output=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"closed-stdio-contained"


def test_descriptor_normalization_failure_closes_original_pipe(monkeypatch):
    import fcntl
    real_pipe = os.pipe
    pipes = []
    def pipe():
        result = real_pipe()
        pipes.extend(result)
        return result
    # Force this branch without changing the process's real stdio descriptors.
    class LowDescriptor(int):
        def __lt__(self, other):
            return other == 3
    def low_pipe():
        read, write = pipe()
        return read, LowDescriptor(write)
    failure = OSError(errno.EMFILE, "no descriptors")
    def fail(*args):
        raise failure
    monkeypatch.setattr(cs.os, "pipe", low_pipe)
    monkeypatch.setattr(fcntl, "fcntl", fail)
    with pytest.raises(OSError) as caught:
        with decode():
            pass
    assert caught.value is failure
    for fd in pipes:
        with pytest.raises(OSError):
            os.fstat(fd)
