"""Disposable venv startup proofs; no host hooks, settings or live data changed."""
import json
import io
import os
import subprocess
import sys
import time
import venv

import pytest

from modelark.codec_process import isolated_command
from modelark import codec_supervisor as cs
from modelark.codec_resources import CodecMemoryPolicy

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux guard")


@pytest.fixture
def hooked_venv(tmp_path):
    target = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(target)
    packages = target / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    record = tmp_path / "hooks.jsonl"
    (packages / "modelark_probe.py").write_text(
        "import ctypes,json,os,resource,signal,sys\n"
        "sig=ctypes.c_int()\n"
        "assert ctypes.CDLL(None).prctl(2,ctypes.byref(sig),0,0,0)==0\n"
        f"with open({str(record)!r},'a') as f:\n"
        " f.write(json.dumps({'as':resource.getrlimit(resource.RLIMIT_AS),"
        "'signal':sig.value,'prefix':sys.prefix,'pid':os.getpid()})+'\\n')\n")
    (packages / "zz_modelark_probe.pth").write_text("import modelark_probe\n")
    return target, packages, record


@pytest.mark.parametrize("module", ["modelark.codec_worker", "scripts.qualify_codec_resources"])
def test_both_workers_guard_before_site_hooks_and_restore_venv(hooked_venv, tmp_path, module, monkeypatch):
    target, packages, record = hooked_venv
    monkeypatch.setattr(sys, "executable", str(target / "bin/python"))
    policy = {"version": "modelark.codec-memory.v1", "address_space_bytes": 128 << 20, "reserve_bytes": 0}
    if module == "modelark.codec_worker":
        request = {"version": "modelark.codec-worker.v2", "policy": policy,
                   "stored_bytes": 36, "original_bytes": 4}
        read, write = os.pipe()
        try:
            result = subprocess.run(isolated_command(module, write, os.getpid(), json.dumps(request)),
                                    input=b"", capture_output=True, pass_fds=(write,))
        finally:
            os.close(write)
        response = os.read(read, 8192)
        os.close(read)
        assert result.returncode == 1
        assert response[:1] == b"I", result.stderr
    else:
        request = tmp_path / "request.json"
        output = tmp_path / "result.json"
        request.write_text(json.dumps({"policy": policy, "phase": "allocation-refusal", "result": str(output)}))
        result = subprocess.run(isolated_command(module, "--worker", request, "--parent-pid", os.getpid()),
                                capture_output=True)
        assert result.returncode == 0, result.stderr
        assert json.loads(output.read_text())["allocation_refused"]
    records = [json.loads(line) for line in record.read_text().splitlines()]
    assert records
    assert all(r["as"] == [128 << 20] * 2 and r["signal"] == 9 for r in records)
    assert all(r["prefix"] == str(target) for r in records)


def test_parent_death_during_actual_site_hook(hooked_venv):
    target, packages, record = hooked_venv
    # Hook announces that it has reached guarded site initialization, then blocks
    # in libc, so no Python cooperation can make the lifetime test pass.
    with (packages / "modelark_probe.py").open("a") as handle:
        handle.write("ctypes.CDLL(None).pause()\n")
    program = r'''
import ctypes, json, os, signal, subprocess, sys, time
from pathlib import Path
from modelark.codec_process import isolated_command
assert ctypes.CDLL(None).prctl(36,1,0,0,0)==0
parent=os.fork()
if parent==0:
    sys.executable=sys.argv[1]
    r,w=os.pipe()
    request={"version":"modelark.codec-worker.v2","stored_bytes":36,"original_bytes":4,
             "policy":{"version":"modelark.codec-memory.v1","address_space_bytes":128<<20,"reserve_bytes":0}}
    proc=subprocess.Popen(isolated_command('modelark.codec_worker',w,os.getpid(),json.dumps(request)),
          stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,pass_fds=(w,))
    proc.wait()
    os._exit(2)
worker=None
try:
    deadline=time.monotonic()+10
    marker=Path(sys.argv[2])
    while not marker.exists():
        assert time.monotonic()<deadline, 'site hook never started'
        time.sleep(.01)
    # File existence alone is not the synchronization signal; wait for its line.
    while not marker.read_text().endswith('\n'):
        assert time.monotonic()<deadline
        time.sleep(.01)
    worker=json.loads(marker.read_text().splitlines()[0])['pid']
finally:
    os.kill(parent,signal.SIGKILL)
    os.waitpid(parent,0)
if worker is not None:
    pid,status=os.waitpid(worker,0)
    assert pid==worker and os.WIFSIGNALED(status) and os.WTERMSIG(status)==signal.SIGKILL
print('startup-parent-death-contained')
'''
    result = subprocess.run([sys.executable, "-c", program, str(target / "bin/python"), str(record)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "startup-parent-death-contained"


def test_cancellation_during_actual_site_hook(hooked_venv, monkeypatch):
    target, packages, record = hooked_venv
    with (packages / "modelark_probe.py").open("a") as handle:
        handle.write("ctypes.CDLL(None).pause()\n")
    monkeypatch.setattr(sys, "executable", str(target / "bin/python"))
    monkeypatch.setattr(cs, "available_memory", lambda: {"available_bytes": 10 << 30})
    children = []
    real = cs._launch
    def launch(request, output):
        child = real(request, output)
        children.append(child)
        return child
    monkeypatch.setattr(cs, "_launch", launch)
    failure = RuntimeError("operator stop during startup")
    deadline = time.monotonic() + 10
    def check():
        if record.exists() and record.read_text().endswith("\n"):
            raise failure
        assert time.monotonic() < deadline, "site hook never started"
    header = bytearray(32)
    header[:4] = b"ZN\x00\x05"
    header[5], header[8], header[15] = 10, 1, 1
    header[16:24], header[24:32] = (4).to_bytes(8, "little"), (36).to_bytes(8, "little")
    with pytest.raises(RuntimeError) as caught:
        with cs.guarded_zipnn_frame(io.BytesIO(header + b"abcd"), policy=CodecMemoryPolicy(128 << 20, 0),
                                    max_stored_bytes=36, max_decoded_bytes=4, remaining_bytes=4,
                                    check=check) as parts:
            list(parts)
    assert caught.value is failure
    assert len(children) == 1
    child = children[0]
    assert child.poll() is not None and child.stdin.closed and child.stdout.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
