"""Launch-level singleton checks use unique disposable socket namespaces only."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid

import pytest


@pytest.fixture
def instance(monkeypatch):
    from modelark import instance
    monkeypatch.setattr(instance, "_ADDRESS", "\0modelark-test-instance-" + uuid.uuid4().hex)
    return instance


def test_rejects_even_second_launch_in_same_process(instance):
    with instance.launch():
        with pytest.raises(SystemExit, match="ModelArk is already running"):
            with instance.launch():
                pytest.fail("second instance accepted")
    with instance.launch():
        pass


def test_error_releases_instance(instance):
    with pytest.raises(RuntimeError, match="boom"):
        with instance.launch():
            raise RuntimeError("boom")
    with instance.launch():
        pass


def test_cli_parser_and_command_errors_release(instance, monkeypatch):
    from modelark import cli
    with pytest.raises(SystemExit):
        cli.main(["not-a-command"])

    def fail(args):
        raise RuntimeError("command failed")

    monkeypatch.setattr(cli, "cmd_query", fail)
    with pytest.raises(RuntimeError, match="command failed"):
        cli.main(["query", "SELECT 1"])
    with instance.launch():
        pass


def test_direct_portal_error_releases(instance, monkeypatch):
    from modelark.web import server

    def fail(**kwargs):
        raise RuntimeError("portal failed")

    monkeypatch.setattr(server, "_serve", fail)
    with pytest.raises(RuntimeError, match="portal failed"):
        server.serve(open_browser=False)
    with instance.launch():
        pass


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["query", "SELECT 1"],
                                  ["--data-dir", "/tmp/other-modelark", "--state-dir", "/tmp/other-state",
                                   "--config", "/tmp/other-config", "serve", "--port", "12345"]])
def test_busy_cli_precedes_parser_config_and_command(instance, monkeypatch, argv):
    from modelark import cli
    for target in ("argparse.ArgumentParser", "db.configure", "wishlist.configure", "db.connect"):
        obj = cli
        parts = target.split(".")
        for part in parts[:-1]:
            obj = getattr(obj, part)
        monkeypatch.setattr(obj, parts[-1], lambda *a, **k: pytest.fail("launch side effect"))
    with instance.launch():
        with pytest.raises(SystemExit, match="ModelArk is already running"):
            cli.main(argv)


def test_busy_direct_portal_precedes_logging_and_bootstrap(instance, monkeypatch):
    from modelark.web import server
    monkeypatch.setattr(server.telemetry, "configure", lambda **k: pytest.fail("logging"))
    monkeypatch.setattr(server.data, "conn", lambda: pytest.fail("database"))
    monkeypatch.setattr(server.plan, "bootstrap", lambda *a: pytest.fail("bootstrap"))
    with instance.launch():
        with pytest.raises(SystemExit, match="ModelArk is already running"):
            server.serve(open_browser=False)


def test_cli_to_portal_uses_one_explicit_launch_permit(instance, monkeypatch):
    from modelark import cli
    from modelark.web import server
    called = []

    def serving(**kwargs):
        with pytest.raises(SystemExit, match="ModelArk is already running"):
            with instance.launch():
                pytest.fail("portal launch not retained")
        called.append(kwargs)

    monkeypatch.setattr(server, "_serve", serving)
    cli.main(["serve", "--no-open", "--port", "12345"])
    assert called == [{"port": 12345, "open_browser": False, "resume": False, "host": "127.0.0.1"}]
    with instance.launch():
        pass


def test_portal_permit_is_single_use_and_not_valid_after_release(instance, monkeypatch):
    from modelark.web import server
    monkeypatch.setattr(server, "_serve", lambda **k: None)
    with instance.launch() as permit:
        server._serve_in_instance(permit)
        with pytest.raises(SystemExit, match="launch permit"):
            server._serve_in_instance(permit)
    with pytest.raises(SystemExit, match="launch permit"):
        server._serve_in_instance(permit)


def test_fork_child_retains_kernel_ownership_until_it_exits(instance):
    ready_read, ready_write = os.pipe()
    stop_read, stop_write = os.pipe()
    child = None
    try:
        with instance.launch():
            child = os.fork()
            if child == 0:
                os.close(ready_read)
                os.close(stop_write)
                os.write(ready_write, b"ready")
                os.read(stop_read, 1)
                os._exit(0)
            os.close(ready_write)
            ready_write = -1
            os.close(stop_read)
            stop_read = -1
            assert os.read(ready_read, 5) == b"ready"
        with pytest.raises(SystemExit, match="ModelArk is already running"):
            with instance.launch():
                pytest.fail("retained child descriptor ignored")
        os.write(stop_write, b"x")
        assert os.waitpid(child, 0)[1] == 0
        child = None
        with instance.launch():
            pass
    finally:
        if child:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        for fd in (ready_read, ready_write, stop_read, stop_write):
            if fd >= 0:
                os.close(fd)


def test_cold_process_rejection_and_sigkill_release(instance):
    code = (
        "import sys;from modelark import instance;"
        "instance._ADDRESS='\\0'+sys.argv[1];"
        "guard=instance.launch();guard.__enter__();"
        "print('ready',flush=True);sys.stdin.read()"
    )
    child = subprocess.Popen([sys.executable, "-c", code, instance._ADDRESS[1:]],
                             cwd=Path(__file__).parents[1], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline() == "ready\n"
        with pytest.raises(SystemExit, match="ModelArk is already running"):
            with instance.launch():
                pytest.fail("cold process ignored")
        child.kill()
        child.wait()
        with instance.launch():
            pass
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate()
