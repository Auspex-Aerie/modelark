"""Physical reader-floor compatibility does not imply a schema/identity migration."""
import sqlite3
from pathlib import Path

import pytest

from modelark.catalog_versions import MAX_SUPPORTED_CATALOG_VERSION, SUPPORTED_CATALOG_VERSIONS
from modelark.core import db


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "STATE_DIR", tmp_path / "state")
    con = db.connect(_bootstrapping=True)
    con.execute("INSERT INTO drives(drive_label,fs_uuid,serial) VALUES('saved','fs','canonical')")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    return db.DB_PATH


def snapshot(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as con:
        return (con.execute("PRAGMA user_version").fetchone()[0],
                con.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall(),
                con.execute("SELECT * FROM drives").fetchall(),
                con.execute("SELECT * FROM planner_state").fetchall())


def stamp(path, version):
    with sqlite3.connect(path) as con:
        con.execute(f"PRAGMA user_version={version}")


def install_publication(path):
    from modelark import publication_store as store
    from modelark.proposal import graph_write
    with sqlite3.connect(path, isolation_level=None, timeout=30) as con:
        con.execute("PRAGMA busy_timeout=30000")
        graph_write(con, lambda c: store._install_schema(
            c, library_id="11111111-1111-4111-8111-111111111111",
            map_uuid="22222222-2222-4222-8222-222222222222"))
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with sqlite3.connect(path, isolation_level=None, timeout=30) as con:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise RuntimeError(f"publication fixture journal_mode={mode!r}")
    for suffix in ("-wal", "-shm"):
        leftover = path.with_name(path.name + suffix)
        leftover.unlink(missing_ok=True)


@pytest.mark.parametrize("read_only", [True, False])
def test_qualified_v9_open_preserves_schema_history_and_pending_diagnostics(catalog, read_only):
    from test_publication_store import pending
    install_publication(catalog)
    with sqlite3.connect(catalog, isolation_level=None) as con:
        pending(con, label="saved")
        before = tuple(con.iterdump())
    before_bytes = catalog.read_bytes()
    con = db.connect(read_only=read_only)
    try:
        # Normal writable bootstrap recreates views; SQL dump ordering is not
        # part of schema/history identity.
        assert sorted(con.iterdump()) == sorted(before)
        assert con.execute("PRAGMA user_version").fetchone() == (9,)
        assert con.execute("SELECT state FROM publication_operations").fetchone() == ("PREPARED",)
    finally:
        con.close()
    if read_only:
        assert catalog.read_bytes() == before_bytes


@pytest.mark.parametrize("corruption", ["bare", "partial", "definition", "downgraded", "identity"])
@pytest.mark.parametrize("entry", [
    "read_only", "writable", "migration", "clone_validator", "provenance", "schema_migrations",
])
def test_invalid_publication_contract_refuses_before_schema_or_journal_writes(catalog, corruption, entry):
    if corruption != "bare":
        install_publication(catalog)
    with sqlite3.connect(catalog, isolation_level=None, timeout=30) as con:
        con.execute("PRAGMA busy_timeout=30000")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute({
            "bare": "PRAGMA user_version=9",
            "partial": "DROP TABLE publication_files",
            "definition": "ALTER TABLE publication_operations ADD COLUMN unqualified TEXT",
            "downgraded": "PRAGMA user_version=8",
            "identity": "DELETE FROM publication_library",
        }[corruption])
    before = catalog.read_bytes()
    with pytest.raises(RuntimeError, match="Catalog publication contract refused"):
        if entry in {"read_only", "writable"}:
            db.connect(read_only=entry == "read_only")
        elif entry == "migration":
            db.migrate_existing_catalog(backup_existing=False)
        else:
            with sqlite3.connect(catalog, isolation_level=None) as con:
                if entry == "clone_validator":
                    db._validate_migrated_clone(con)
                elif entry == "schema_migrations":
                    db.apply_schema_migrations(con, backup_existing=False)
                else:
                    db._migrate_provenance_v7(con, backup_existing=False)
    assert catalog.read_bytes() == before
    assert not catalog.with_name(catalog.name + "-wal").exists()


def test_qualified_v9_explicit_schema_ladder_preserves_floor(catalog):
    install_publication(catalog)
    before = snapshot(catalog)
    con = db.migrate_existing_catalog(backup_existing=False)
    try:
        db._validate_migrated_clone(con)
    finally:
        con.close()
    assert snapshot(catalog) == before


def test_historical_v8_numeric_gate_remains_a_distinct_contract(catalog, monkeypatch):
    # Reproduce the old gate, not an assertion that the current binary still has
    # the v8 ceiling. Accepted native qualification artifacts remain historical.
    install_publication(catalog)
    monkeypatch.setattr(db, "MAX_SUPPORTED_CATALOG_VERSION", 8)
    before = catalog.read_bytes()
    with pytest.raises(RuntimeError, match="newer than this ModelArk build"):
        db.connect(read_only=True)
    assert catalog.read_bytes() == before


@pytest.mark.parametrize("version", [7, 8])
@pytest.mark.parametrize("read_only", [True, False])
def test_normal_open_preserves_physical_version_schema_and_identity(catalog, version, read_only):
    stamp(catalog, version)
    before = snapshot(catalog)
    con = db.connect(read_only=read_only)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == version
        assert con.execute("SELECT serial FROM drives").fetchone()[0] == "canonical"
        if read_only:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                con.execute("UPDATE drives SET serial='changed'")
    finally:
        con.close()
    assert snapshot(catalog) == before


def test_bootstrap_and_provenance_layout_stay_separate_from_reader_ceiling(catalog):
    assert db._SCHEMA_VERSION == 7
    assert snapshot(catalog)[0] == 7
    assert SUPPORTED_CATALOG_VERSIONS == {7, 8, 9}
    assert MAX_SUPPORTED_CATALOG_VERSION == 9


@pytest.mark.parametrize("version", [0, 5, 6, 10, 99])
@pytest.mark.parametrize("read_only", [True, False])
def test_unsupported_normal_open_is_byte_preserving(catalog, version, read_only):
    stamp(catalog, version)
    before = catalog.read_bytes()
    with pytest.raises(RuntimeError, match="clone-first|newer"):
        db.connect(read_only=read_only)
    assert catalog.read_bytes() == before
    assert snapshot(catalog)[0] == version


@pytest.mark.parametrize("version", [7, 8])
def test_explicit_schema_ladder_cannot_raise_or_lower_supported_floor(catalog, version):
    stamp(catalog, version)
    before = snapshot(catalog)
    con = db.migrate_existing_catalog(backup_existing=False)
    con.close()
    assert snapshot(catalog) == before


def test_explicit_ladder_rejects_future_before_journal_mode_mutation(catalog):
    with sqlite3.connect(catalog) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA user_version=10")
    before = catalog.read_bytes()
    with pytest.raises(RuntimeError, match="newer"):
        db.migrate_existing_catalog(backup_existing=False)
    assert catalog.read_bytes() == before


@pytest.mark.parametrize("version", [7, 8])
def test_clone_validator_accepts_closed_compatible_versions(catalog, version):
    stamp(catalog, version)
    with sqlite3.connect(catalog) as con:
        db._validate_migrated_clone(con)


def test_clone_validator_refuses_future_layout_lookalike(catalog):
    stamp(catalog, 10)
    with sqlite3.connect(catalog) as con:
        with pytest.raises(RuntimeError, match="newer"):
            db._validate_migrated_clone(con)


def test_remigration_refuses_future_snapshot_without_touching_source(catalog, tmp_path):
    stamp(catalog, 10)
    before = catalog.read_bytes()
    with pytest.raises(RuntimeError, match="newer"):
        db._remigrate_snapshot_to_expected(catalog, tmp_path / "remigrate")
    assert catalog.read_bytes() == before


def test_provenance_helper_never_downgrades_malformed_v8(catalog):
    stamp(catalog, 8)
    con = sqlite3.connect(catalog, isolation_level=None)
    try:
        con.execute("ALTER TABLE archived RENAME COLUMN orig_sha256_provenance TO unknown_provenance")
        before = snapshot(catalog)
        with pytest.raises(RuntimeError, match="missing orig_sha256_provenance"):
            db._migrate_provenance_v7(con, backup_existing=False)
        assert con.execute("PRAGMA user_version").fetchone()[0] == 8
        assert snapshot(catalog) == before
    finally:
        con.close()


@pytest.mark.parametrize("version", [7, 8])
def test_clone_rehearsal_and_remigration_preserve_reader_floor(catalog, tmp_path, version):
    stamp(catalog, version)
    before = snapshot(catalog)
    bundle = db._read_sqlite_bundle_bytes(catalog)
    report = db.rehearse_provenance_migration(
        catalog.parent, tmp_path / "rehearsal", run_id="reader-floor")
    assert report["status"] == "ok"
    assert report["source_user_version"] == report["clone_user_version"] == version
    assert db._read_sqlite_bundle_bytes(catalog) == bundle
    assert snapshot(Path(report["clone_catalog_path"])) == before
    expected = db._remigrate_snapshot_to_expected(
        Path(report["snapshot_path"]), tmp_path / "expected")
    assert snapshot(expected) == before
