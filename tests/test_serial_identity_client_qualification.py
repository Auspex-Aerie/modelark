"""Harness guardrails independent of retained wheel artifacts or live services."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import qualify_serial_identity_clients as qualification


def test_worker_command_is_isolated_and_disables_bytecode():
    args = {"operation": "hold", "case": "/tmp/disposable"}
    command = qualification._command(args)
    assert command[:3] == [sys.executable, "-I", "-B"]
    assert Path(command[3]).resolve() == Path(qualification.__file__).resolve()
    assert command[4] == "--worker" and json.loads(command[5]) == args


def test_holder_exception_still_releases_and_reaps_process(tmp_path, monkeypatch):
    receipt = tmp_path / "release.txt"
    program = (
        "import json,sys; from pathlib import Path; "
        "print(json.dumps({'outcome':'holding','count':1}),flush=True); "
        "assert sys.stdin.readline().strip()=='release'; "
        "Path(sys.argv[1]).write_text('released'); "
        "print(json.dumps({'outcome':'released'}),flush=True)"
    )
    monkeypatch.setattr(qualification, "_command", lambda args: [
        sys.executable, "-I", "-B", "-c", program, str(receipt)])
    with pytest.raises(ValueError, match="consumer failed"):
        with qualification._holder({"operation": "hold", "case": str(tmp_path)}):
            raise ValueError("consumer failed")
    assert receipt.read_text() == "released"


def test_holder_requires_explicit_release_confirmation(tmp_path, monkeypatch):
    program = (
        "import json,sys; print(json.dumps({'outcome':'holding'}),flush=True); "
        "sys.stdin.readline()"
    )
    monkeypatch.setattr(qualification, "_command", lambda args: [sys.executable, "-I", "-B", "-c", program])
    with pytest.raises(RuntimeError, match="did not confirm descriptor release"):
        with qualification._holder({"operation": "hold", "case": str(tmp_path)}):
            pass


def test_import_origin_guard_rejects_nonwheel_modelark_module(tmp_path):
    program = """
import importlib.util,sys,types
from pathlib import Path
spec=importlib.util.spec_from_file_location('qualification',sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
sys.modules['modelark.foreign']=types.SimpleNamespace(__file__=sys.argv[2])
try:
    module._origins(Path(sys.argv[3]))
except ValueError:
    pass
else:
    raise AssertionError('foreign package import accepted')
"""
    result = subprocess.run([sys.executable, "-I", "-B", "-c", program, qualification.__file__,
                             str(tmp_path / "foreign.py"), str(tmp_path / "wheel")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
