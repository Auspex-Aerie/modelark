"""Pinned native reader tests on disposable repos and synthetic attachment rows."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from modelark import publication_locks, publication_native as native, publication_tree
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.publication_policy import PublicationRefused
from test_publication_lifecycle import MAP
from test_publication_lifecycle import con as publication_connection  # noqa: F401
from test_publication_tree import repo as git_repository  # noqa: F401


@pytest.fixture(name="con")
def _connection(request):
    return request.getfixturevalue("publication_connection")


@pytest.fixture
def prepared(request, con):
    for path, digest in native.TOOLS.items():
        if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            pytest.skip("requires installed qualified native tools")
    archive, git = request.getfixturevalue("git_repository")
    git("annex", "init", "--version=8", "--quiet", "native-profile-test")
    git("config", "annex.backend", "SHA256")
    identity = git("config", "annex.uuid").decode().strip()
    fingerprint = identity_fingerprint_v1(fs_uuid="d0-fs", annex_uuid=identity, serial=None,
                                           filesystem_capacity_bytes=10**12)
    con.execute("UPDATE drives SET annex_uuid=?,identity_fingerprint=? WHERE drive_label='d0'",
                [identity, fingerprint])
    return archive, git


def test_actual_qualified_reader_feeds_tree_factory_and_cannot_mutate(con, prepared):
    archive, git = prepared
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            before = (archive / ".git/index").read_bytes()
            snapshot = publication_tree.capture(reader.tree, read_git=reader.read, require_scope=scope.require_io)
            assert snapshot.head_oid == git("rev-parse", "HEAD").decode().strip()
            assert (archive / ".git/index").read_bytes() == before
            assert json.loads(json.dumps(reader.profile.record())) == reader.profile.record()
            for args in (("config", "core.fsmonitor", "evil"), ("annex", "init"),
                         ("cat-file", "--filters", snapshot.head_oid), ("fetch", "origin"),
                         ("-c", "core.hooksPath=evil", "status"), ("write-tree",)):
                with pytest.raises(PublicationRefused, match="COMMAND_UNQUALIFIED"):
                    reader.read(*args)
        with pytest.raises(PublicationRefused, match="SCOPE_CLOSED"):
            reader.read("symbolic-ref", "HEAD")


@pytest.mark.parametrize("key,value", [
    ("filter.annex.clean", "touch SHOULDNTRUN"),
    ("filter.annex.process", "touch SHOULDNTRUN"),
    ("core.fsmonitor", "touch SHOULDNTRUN"),
    ("core.hooksPath", "hostile-hooks"),
    ("annex.version", "9"),
    ("include.path", "/does/not/exist"),
    ("extensions.worktreeConfig", "true"),
    ("remote.origin.url", "ext::touch SHOULDNTRUN"),
])
def test_unqualified_config_is_refused_before_any_annex_or_filter_execution(con, prepared, monkeypatch, key, value):
    archive, git = prepared
    git("config", key, value)
    observed = []
    original = native._bounded_process
    def process(argv, **kwargs):
        observed.append(argv)
        return original(argv, **kwargs)
    monkeypatch.setattr(native, "_bounded_process", process)
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused):
            native.QualifiedRepository(scope, archive, drive_label="d0")
    assert not (archive / "SHOULDNTRUN").exists()
    assert all("annex" not in args and "check-attr" not in args for args in observed)


def test_native_environment_isolated_and_config_changes_refuse(con, prepared, monkeypatch):
    archive, git = prepared
    monkeypatch.setenv("GIT_DIR", "/not/the/repository")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "touch SHOULDNTRUN")
    monkeypatch.setenv("PATH", "/not/the/qualified/tools")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            assert reader.read("symbolic-ref", "HEAD") == b"refs/heads/main\n"
            git("config", "user.name", "changed after binding")
            with pytest.raises(PublicationRefused, match="PROFILE_CHANGED"):
                reader.read("symbolic-ref", "HEAD")


def test_attributes_are_measured_not_inferred_from_neutral_name(con, prepared):
    archive, git = prepared
    path = "org/repo/__modelark_payload_v1__/p-test.blob"
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            with pytest.raises(PublicationRefused, match="ATTRIBUTES_UNQUALIFIED"):
                reader.attributes(path)
        (archive / ".git/info/attributes").write_text(
            "org/repo/__modelark_payload_v1__/** filter=annex -text -ident "
            "!working-tree-encoding !eol annex.largefiles=anything annex.backend=SHA256\n")
        with native.QualifiedRepository(scope, archive, drive_label="d0") as reader:
            assert reader.attributes(path)["filter"] == "annex"
            key = "SHA256-s4--" + hashlib.sha256(b"data").hexdigest()
            obj = reader.object_path(key)
            assert obj == git("annex", "examinekey", "--format=${objectpath}", key).decode().strip()


@pytest.mark.parametrize("path", [".git/commondir", ".git/shallow", ".git/info/grafts",
                                  ".git/objects/info/alternates"])
def test_external_git_routing_refused_without_consuming_it(con, prepared, path):
    archive, _ = prepared
    (archive / path).write_text("/not/a/qualified/object/store\n")
    with publication_locks.hold(con, ["d0"], map_uuid=MAP) as scope:
        with pytest.raises(PublicationRefused, match="ROUTING_UNQUALIFIED"):
            native.QualifiedRepository(scope, archive, drive_label="d0")


def test_native_reader_requires_actual_scope_not_noop_callback(prepared):
    archive, _ = prepared
    with pytest.raises(PublicationRefused, match="AUTHORITY_MISSING"):
        native.QualifiedRepository(lambda: None, archive, drive_label="d0")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_process_output_is_bounded_while_reading_not_after_communicate(tmp_path, stream):
    command = [sys.executable, "-c", f"import sys; sys.{stream}.buffer.write(b'x' * 1000000)"]
    with pytest.raises(PublicationRefused, match="OUTPUT_LIMIT"):
        native._bounded_process(command, cwd=tmp_path, pass_fds=(), environment={}, limit=1024)


def test_bounded_process_drains_input_and_both_output_streams(tmp_path):
    command = [sys.executable, "-c", "import sys; a=sys.stdin.buffer.read(); "
               "sys.stderr.write('diagnostic'); sys.stdout.buffer.write(a)"]
    assert native._bounded_process(command, cwd=tmp_path, pass_fds=(), environment={},
                                   limit=1000000, input_data=b"x" * 200000) == b"x" * 200000


def test_bounded_process_failure_is_not_success(tmp_path):
    with pytest.raises(PublicationRefused, match="NATIVE_COMMAND_FAILED"):
        native._bounded_process([sys.executable, "-c", "raise SystemExit(3)"], cwd=tmp_path,
                                pass_fds=(), environment={}, limit=1024)


def test_bounded_process_deadline_kills_a_silent_child(tmp_path):
    with pytest.raises(PublicationRefused, match="COMMAND_DEADLINE"):
        native._bounded_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                cwd=tmp_path, pass_fds=(), environment={}, limit=1024, deadline=1)


def test_bounded_process_deadline_kills_owned_descendants(tmp_path):
    script = tmp_path / "stall.py"
    marker = tmp_path / "child.pid"
    script.write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                         stdout=sys.stdout, stderr=sys.stderr)\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "raise SystemExit(0)\n"
    )
    with pytest.raises(PublicationRefused, match="COMMAND_DEADLINE"):
        native._bounded_process([sys.executable, str(script)], cwd=tmp_path, pass_fds=(),
                                environment={}, limit=1024, deadline=1)
    pid = int(marker.read_text())
    until = time.monotonic() + 2
    while time.monotonic() < until:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        raise AssertionError(f"descendant {pid} survived timeout cleanup")


def test_owned_session_members_exclude_a_recycled_leader_starttime():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        starttime = native._proc_starttime(proc.pid)
        assert proc.pid in native._owned_session_members(proc.pid, starttime)
        assert proc.pid not in native._owned_session_members(proc.pid, starttime - 1)
        assert proc.pid not in native._owned_session_members(proc.pid, starttime + 1)
    finally:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def test_deadline_reap_does_not_killpg_the_raw_child_pid(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("killpg must not run on the timeout path")
    monkeypatch.setattr(os, "killpg", boom)
    with pytest.raises(PublicationRefused, match="COMMAND_DEADLINE"):
        native._bounded_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                cwd=tmp_path, pass_fds=(), environment={}, limit=1024, deadline=1)


def _wait_dead(pid, seconds=2):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.kill(pid, 9)
    except ProcessLookupError:
        return
    raise AssertionError(f"pid {pid} survived timeout cleanup")


def test_deadline_reaps_setsid_and_forking_descendants(tmp_path):
    script = tmp_path / "escape.py"
    marker = tmp_path / "pids"
    script.write_text(
        "import os, sys, time\n"
        f"marker = {str(marker)!r}\n"
        "def note(tag):\n"
        "    with open(marker, 'a') as fh:\n"
        "        fh.write(f'{tag} {os.getpid()}\\n')\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    os.setsid()\n"
        "    note('setsid')\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "note('leader')\n"
        "while True:\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        note('fork')\n"
        "        time.sleep(30)\n"
        "        os._exit(0)\n"
        "    time.sleep(0.02)\n"
    )
    with pytest.raises(PublicationRefused, match="COMMAND_DEADLINE"):
        native._bounded_process([sys.executable, str(script)], cwd=tmp_path, pass_fds=(),
                                environment={}, limit=1024, deadline=1)
    pids = []
    if marker.exists():
        pids = [int(line.split()[1]) for line in marker.read_text().splitlines() if line.strip()]
    assert pids, "escape script must record descendant pids"
    for pid in pids:
        _wait_dead(pid)
