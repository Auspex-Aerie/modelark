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
    assert SUPPORTED_CATALOG_VERSIONS == {7, 8}
    assert MAX_SUPPORTED_CATALOG_VERSION == 8


@pytest.mark.parametrize("version", [0, 5, 6, 9, 99])
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
        con.execute("PRAGMA user_version=9")
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
    stamp(catalog, 9)
    with sqlite3.connect(catalog) as con:
        with pytest.raises(RuntimeError, match="expected 7 or 8"):
            db._validate_migrated_clone(con)


def test_remigration_refuses_future_snapshot_without_touching_source(catalog, tmp_path):
    stamp(catalog, 9)
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
