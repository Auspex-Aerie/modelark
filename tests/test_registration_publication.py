"""Registration IO, final catalog CAS and legacy/new map exclusion boundaries."""
import json
import sqlite3
import sys

import pytest

from modelark import drive_fence, drive_lifecycle, proposal, register, registration_publication
from modelark.core import db
from test_def029_gate2_contracts import _catalog, _device, _mounted_topology, _preview


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path / "catalog-state")
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    connection = _catalog()
    yield connection
    connection.close()


def _apply(con, prepare):
    preview = _preview(con)
    return drive_lifecycle.register_new_identity(
        con, _device(), _mounted_topology(),
        expected_binding=preview["registration_binding"],
        confirmation=preview["confirmation"], prepare_archive=prepare,
    )


def _prepared(kwargs):
    return {"archive_path": kwargs["archive_path"], "annex_uuid": "NEW-ANNEX-UUID"}


def test_preparation_has_no_sql_transaction_and_holds_controller(con):
    def prepare(**kwargs):
        assert not con.in_transaction
        with pytest.raises(drive_fence.FenceUnavailable), drive_fence.hold_controller(
            db.DB_PATH, blocking=False,
        ):
            pytest.fail("preparation must retain controller exclusion")
        return _prepared(kwargs)

    assert _apply(con, prepare)["planner_revision"] == 12


def test_final_cas_rejects_changed_revision_without_erasing_preparation(con):
    retained = []

    def prepare(**kwargs):
        assert not con.in_transaction
        retained.append(_prepared(kwargs))
        # Another legitimate writer can run outside this connection's write TX.
        proposal.graph_write(con, lambda c: proposal.GraphResult(proven_noop=False))
        return retained[-1]

    with pytest.raises(proposal.Refusal) as refused:
        _apply(con, prepare)
    assert refused.value.code == "DRIVE_REGISTRATION_PREVIEW_STALE"
    assert retained
    assert con.execute("SELECT count(*) FROM drives").fetchone()[0] == 7
    assert drive_lifecycle.planner_revision(con) == 12


def test_controller_refuses_lock_acquisition_inside_transaction(con, monkeypatch):
    con.execute("BEGIN")
    monkeypatch.setattr(drive_fence, "hold_controller", lambda *a, **k: pytest.fail("no wait in TX"))
    with pytest.raises(RuntimeError, match="inside a SQLite transaction"):
        with registration_publication.controller(con):
            pytest.fail("unreachable")
    con.rollback()


def _map(tmp_path):
    path = tmp_path / "map"
    path.mkdir()
    register._run("git", "-C", str(path), "init", "-q")
    register._run("git", "-C", str(path), "config", "annex.uuid",
                  "2b2f8d5c-a06f-498e-ad3b-a8b879d36f20")
    return path


def test_map_lock_matches_central_publisher_and_survives_in_child(con, tmp_path):
    path = _map(tmp_path)
    with registration_publication.controller(con), registration_publication.map_write(path):
        with pytest.raises(drive_fence.FenceUnavailable), drive_fence.hold_map(
            "2b2f8d5c-a06f-498e-ad3b-a8b879d36f20", blocking=False,
        ):
            pytest.fail("another catalog must contend on the same map UUID")
        fds = registration_publication.child_fds()
        assert len(fds) == 2
        result = register._run(
            sys.executable, "-c",
            "import json, os, sys; print(json.dumps([os.fstat(int(fd)).st_ino for fd in sys.argv[1:]]))",
            *map(str, fds),
        )
        assert len(json.loads(result.stdout)) == 2
    assert registration_publication.child_fds() == ()


def test_map_lock_requires_controller(con, tmp_path):
    with pytest.raises(RuntimeError, match="controller before map"):
        with registration_publication.map_write(_map(tmp_path)):
            pytest.fail("unreachable")


def test_map_identity_change_after_preparation_is_not_success(con, tmp_path):
    path = _map(tmp_path)
    with registration_publication.controller(con):
        with pytest.raises(RuntimeError, match="changed during preparation"):
            with registration_publication.map_write(path):
                register._run("git", "-C", str(path), "config", "annex.uuid",
                              "30c39918-c559-4f9f-99d1-a7f0c9c02a9f")


def test_nested_controller_cannot_switch_catalog(con, tmp_path):
    other = sqlite3.connect(tmp_path / "different.sqlite", isolation_level=None)
    try:
        with registration_publication.controller(con):
            with pytest.raises(RuntimeError, match="scope mismatch"):
                with registration_publication.controller(other):
                    pytest.fail("unreachable")
    finally:
        other.close()


LIBRARY = "11111111-1111-4111-8111-111111111111"
MAP = "22222222-2222-4222-8222-222222222222"


def _v9_map(tmp_path):
    path = tmp_path / "map"
    path.mkdir()
    register._run("git", "-C", str(path), "init", "-q")
    register._run("git", "-C", str(path), "config", "user.name", "ModelArk")
    register._run("git", "-C", str(path), "config", "user.email", "publication@modelark.invalid")
    register._run("git", "-C", str(path), "config", "annex.uuid", MAP)
    register._run("git", "-C", str(path), "config", "annex.version", "8")
    (path / "README.md").write_text("map\n")
    register._run("git", "-C", str(path), "add", "README.md")
    register._run("git", "-C", str(path), "commit", "-qm", "init")
    register._save_library_root(path)
    return path


@pytest.fixture
def v9(tmp_path, monkeypatch):
    from modelark import publication_store

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path / "catalog-state")
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    source = _catalog()
    target = sqlite3.connect(tmp_path / "catalog.sqlite", isolation_level=None)
    source.backup(target)
    source.close()
    target.execute("PRAGMA user_version=8")
    proposal.graph_write(target, lambda c: publication_store._install_schema(
        c, library_id=LIBRARY, map_uuid=MAP))
    _v9_map(tmp_path)
    yield target
    target.close()


def test_v9_registration_uses_setup_adapter_and_closes(v9, tmp_path):
    result = _apply(v9, lambda **kw: _prepared(kw))
    assert result["annex_uuid"] == "NEW-ANNEX-UUID"
    assert v9.execute("SELECT count(*) FROM drives").fetchone()[0] == 8
    assert v9.execute("SELECT kind,state FROM publication_operations").fetchone() == ("registration", "CLOSED")
    assert not (tmp_path / "must-not-create").exists()


def test_v9_ensure_library_qualifies_matching_map(v9, tmp_path):
    root = tmp_path / "map"
    assert register.ensure_library(root) == root
    assert v9.execute("SELECT kind,state FROM publication_operations").fetchone() == ("registration", "CLOSED")


def test_v9_registration_without_adapter_still_refuses(con):
    from modelark import publication_store

    con.execute("PRAGMA user_version=8")
    proposal.graph_write(con, lambda c: publication_store._install_schema(
        c, library_id=LIBRARY, map_uuid=MAP))
    with pytest.raises(proposal.Refusal) as refused:
        registration_publication.require_legacy_registration(con)
    assert refused.value.code == "REGISTRATION_PUBLICATION_ADAPTER_REQUIRED"


def test_final_cas_checks_annex_collision_discovered_during_preparation(con):
    def prepare(**kwargs):
        con.execute("UPDATE drives SET annex_uuid='NEW-ANNEX-UUID' WHERE drive_label='drive-00'")
        return _prepared(kwargs)

    with pytest.raises(proposal.Refusal) as refused:
        _apply(con, prepare)
    assert refused.value.code == "DRIVE_REGISTRATION_IDENTITY_COLLISION"
    assert refused.value.evidence["annex_uuid"] == "NEW-ANNEX-UUID"
    assert con.execute("SELECT count(*) FROM drives").fetchone()[0] == 7
    assert drive_lifecycle.planner_revision(con) == 11


def test_ensure_library_bootstrap_overlaps_uuid_exclusion_and_reuses_without_annex(
    con, tmp_path, monkeypatch,
):
    path = tmp_path / "new-map"
    original = register._git
    commands = []

    def run(repo, *args, **kwargs):
        commands.append(args)
        if args[:2] == ("annex", "init"):
            assert len(registration_publication.child_fds()) == 2
        if args[:2] == ("annex", "numcopies"):
            assert len(registration_publication.child_fds()) == 3
            with pytest.raises(drive_fence.FenceUnavailable), drive_fence.hold_controller(
                registration_publication.bootstrap_lock_identity(path), blocking=False,
            ):
                pytest.fail("bootstrap must overlap acquired UUID exclusion")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(register, "_git", run)
    assert register.ensure_library(path) == path
    assert (path / "README.md").exists()
    assert (path / ".gitattributes").exists()
    identity = register._git(path, "config", "--local", "--get", "annex.uuid")
    commands.clear()
    assert register.ensure_library(path) == path
    assert not any(command[0] == "annex" for command in commands)
    assert register._git(path, "config", "--local", "--get", "annex.uuid") == identity
    assert json.loads((db.CATALOG_DIR / "library.json").read_text())["library_root"] == str(path)


@pytest.mark.parametrize("kind", ["unrelated-directory", "uninitialized-git", "uuid-without-head"])
def test_ensure_library_never_adopts_occupied_or_incomplete_namespace(
    con, tmp_path, monkeypatch, kind,
):
    path = tmp_path / "occupied-map"
    path.mkdir()
    marker = path / "keep.txt"
    marker.write_text("operator bytes")
    if kind != "unrelated-directory":
        register._run("git", "-C", str(path), "init", "-q")
    if kind == "uuid-without-head":
        register._git(path, "config", "annex.uuid", "2b2f8d5c-a06f-498e-ad3b-a8b879d36f20")
    original = register._git

    def read_only(repo, *args, **kwargs):
        assert args[0] in {"config", "rev-parse"}
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(register, "_git", read_only)
    with pytest.raises(RuntimeError, match="occupied or incomplete"):
        register.ensure_library(path)
    assert marker.read_text() == "operator bytes"
    assert not (path / ".gitattributes").exists()
    assert not (db.CATALOG_DIR / "library.json").exists()


def test_bootstrap_identity_is_canonical_and_map_lock_cannot_precede_it(con, tmp_path):
    path = _map(tmp_path)
    assert registration_publication.bootstrap_lock_identity(path) == (
        registration_publication.bootstrap_lock_identity(path / "unused" / "..")
    )
    with registration_publication.controller(con), registration_publication.map_write(path):
        with pytest.raises(RuntimeError, match="bootstrap must precede"):
            with registration_publication.bootstrap(path):
                pytest.fail("reverse lock order")


def test_bootstrap_cannot_run_after_owning_connection_enters_transaction(con, tmp_path):
    with registration_publication.controller(con):
        con.execute("BEGIN")
        try:
            with pytest.raises(RuntimeError, match="inside a SQLite transaction"):
                register.ensure_library(tmp_path / "not-created")
        finally:
            con.rollback()
    assert not (tmp_path / "not-created").exists()


def test_standalone_ensure_library_refuses_v9_before_map_or_config_mutation(
    tmp_path, monkeypatch,
):
    from modelark import publication_store

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path / "catalog-state")
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    connection = db.connect()
    try:
        proposal.graph_write(connection, lambda c: publication_store._install_schema(
            c, library_id="11111111-1111-4111-8111-111111111111",
            map_uuid="22222222-2222-4222-8222-222222222222",
        ))
    finally:
        connection.close()
    target = tmp_path / "must-not-create"
    with pytest.raises(proposal.Refusal) as refused:
        register.ensure_library(target)
    assert refused.value.code == "PUBLICATION_MAP_ROOT_MISSING"
    assert not target.exists()
    assert not (db.CATALOG_DIR / "library.json").exists()
