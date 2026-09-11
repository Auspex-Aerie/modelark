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


def launch_fake(monkeypatch, body):
    children = []
    def launch(request, output):
        program = ("import os,sys,signal,time,json\n"
                   "from modelark.codec_worker import bind_parent\n"
                   "bind_parent(int(sys.argv[2]))\n"
                   "output=int(sys.argv[1])\n" + body)
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
    assert caught.value.kind in {"RESOURCE", "INVALID", "WORKER_FAILED"}
    # Native dependencies may raise ImportError after guard-denied mappings;
    # none of these failure paths may retry unguarded or certify success.


def test_real_writer_frame_decodes_hash_and_closes_without_source_ownership():
    zipnn = pytest.importorskip("zipnn")
    original = b"\x00\x3f\x80\x3f" * (1 << 18)
    # ZipNN reorders its input in-place, even if handed immutable Python bytes.
    # Give it an owned copy so the expected original remains an independent oracle.
    stored = zipnn.ZipNN(input_format="byte", bytearray_dtype="bfloat16", threads=1).compress(bytearray(original))
    source = io.BytesIO(stored)
    with decode(source, max_stored_bytes=len(stored), max_decoded_bytes=len(original),
                remaining_bytes=len(original)) as parts:
        digest = hashlib.sha256()
        total = 0
        for block in parts:
            assert 0 < len(block) <= cs.IO_BYTES
            total += len(block)
            digest.update(block)
    assert total == len(original)
    assert digest.digest() == hashlib.sha256(original).digest()
    assert not source.closed


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
            body="import ctypes,os,sys\nfrom modelark.codec_worker import bind_parent\nbind_parent(int(sys.argv[2]))\nsys.stdin.buffer.read()\n"
            if phase=='compute':
                body+="ctypes.CDLL(None).pause()\n"
            else:
                body+="os.write(int(sys.argv[1]),b'O'+(1<<20).to_bytes(8,'little'))\nwhile True: os.write(int(sys.argv[1]),b'x'*65536)\n"
            proc=subprocess.Popen([sys.executable,'-c',body,str(output),str(os.getpid())],
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
    result = subprocess.run([sys.executable, "-c", program, phase], capture_output=True, text=True)
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
        run({"version": "modelark.codec-worker.v1", "source_path": "/unauthorized"}, None, None)
    assert json.loads(json.dumps(POLICY.to_record())) == POLICY.to_record()
