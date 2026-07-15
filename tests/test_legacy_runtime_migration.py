"""Legacy runtime cutover tooling is backup-first, non-overwriting, and fixture-only."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import SkipTest

from modelark.core import db

_SPEC = importlib.util.spec_from_file_location(
    "migrate_legacy_runtime", Path(__file__).parents[1] / "scripts" / "migrate_legacy_runtime.py")
assert _SPEC and _SPEC.loader
MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(MIGRATION)


def _source(root: Path) -> Path:
    source = root / "legacy-data"
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        db.configure(source, source / "state")
        con = db.connect(_bootstrapping=True)
        con.execute("INSERT INTO models(repo_id,status,numcopies) VALUES ('org/model','archived',1)")
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
                    "VALUES ('org/model','nested/model.safetensors',10,'safetensors','bf16')")
        con.execute("INSERT INTO drives(drive_label,role,raid_backed) "
                    "VALUES ('drive-01','primary',false)")
        con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
                    "compressed) VALUES ('org/model','nested/model.safetensors',"
                    "'model.safetensors.znn','nested/model.safetensors.znn','drive-01',true)")
        # Shape the source like the pre-v2 operator catalog. The cutover must migrate the staged
        # copy, never the live source, and preserve compressed -> compression_aware semantics.
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("DROP INDEX IF EXISTS idx_plans_one_active")
        con.execute("""
            CREATE TABLE plans__v1 (
                plan_id VARCHAR PRIMARY KEY NOT NULL,
                name VARCHAR, annex_root VARCHAR,
                provisioning VARCHAR NOT NULL DEFAULT 'uncompressed'
                    CHECK (provisioning IN ('uncompressed','compressed')),
                status VARCHAR NOT NULL DEFAULT 'active',
                is_active BOOLEAN NOT NULL DEFAULT false,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                notes VARCHAR
            )
        """)
        con.execute(
            "INSERT INTO plans__v1(plan_id,name,provisioning,status,is_active) "
            "VALUES ('ark','Archive','compressed','active',true)"
        )
        con.execute("DROP TABLE plans")
        con.execute("ALTER TABLE plans__v1 RENAME TO plans")
        con.execute("CREATE UNIQUE INDEX idx_plans_one_active ON plans(is_active) WHERE is_active=1")
        con.execute("PRAGMA user_version=1")
        con.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old
    (source / "library.json").write_text('{"library_root": "/synthetic/library"}\n')
    return source


def test_default_inspection_creates_nothing(tmp_path):
    source = _source(tmp_path)
    destination, backups = tmp_path / "new-data", tmp_path / "backups"
    report = MIGRATION.inspect(source, destination, backups)
    assert report["mode"] == "inspection-only" and report["source_kind"] == "sqlite"
    assert report["source"]["integrity_check"] == "ok"
    assert report["source"]["tables"]["archived"] == 1
    assert not destination.exists() and not backups.exists()


def test_multiple_catalogs_require_an_explicit_source_kind(tmp_path):
    source = _source(tmp_path)
    stale_duckdb = source / "catalog.duckdb"
    stale_duckdb.write_bytes(b"obsolete catalog sentinel")
    destination, backups = tmp_path / "new-data", tmp_path / "backups"

    try:
        MIGRATION.inspect(source, destination, backups)
        raise AssertionError("ambiguous catalogs must be refused")
    except RuntimeError as exc:
        assert "both catalog.sqlite and catalog.duckdb exist" in str(exc)

    report = MIGRATION.inspect(source, destination, backups, source_kind="sqlite")
    assert report["source_kind"] == "sqlite"
    assert report["source_selection"] == "explicit"
    assert report["source_catalog"] == str(source / "catalog.sqlite")
    assert stale_duckdb.read_bytes() == b"obsolete catalog sentinel"
    assert not destination.exists() and not backups.exists()


def test_explicit_source_kind_must_exist(tmp_path):
    source = _source(tmp_path)
    try:
        MIGRATION.inspect(
            source, tmp_path / "new-data", tmp_path / "backups", source_kind="duckdb")
        raise AssertionError("a missing explicitly selected catalog must be refused")
    except RuntimeError as exc:
        assert "explicit duckdb source requested" in str(exc)


def test_execute_backs_up_migrates_validates_and_publishes(tmp_path):
    source = _source(tmp_path)
    destination, backups = tmp_path / "new-data", tmp_path / "backups"
    report = MIGRATION.execute(source, destination, backups, "MODELARK-STOPPED", "fixture")
    assert report["status"] == "published" and report["row_count_mismatches"] == {}
    assert (destination / "catalog.sqlite").is_file()
    assert json.loads((destination / "library.json").read_text())["library_root"] == \
        "/synthetic/library"
    con = sqlite3.connect(str(destination / "catalog.sqlite"))
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("SELECT stored_relpath FROM archived").fetchone()[0] == \
        "nested/model.safetensors.znn"
    assert con.execute("SELECT plan_id,is_active FROM plans").fetchone() == ("ark", 1)
    assert con.execute("SELECT capacity_mode FROM plans").fetchone()[0] == "compression_aware"
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    assert con.execute("SELECT drive_label FROM plan_drives").fetchone()[0] == "drive-01"
    con.close()
    run = backups / "modelark-migration-fixture"
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["status"] == "published" and manifest["destination_sha256"]
    assert (run / "catalog.sqlite.snapshot").is_file()
    assert (run / "raw-source" / "catalog.sqlite").is_file()
    assert (destination / "migration-manifest.json").is_file()


def test_execute_records_explicit_source_selection_without_touching_other_catalog(tmp_path):
    source = _source(tmp_path)
    stale_duckdb = source / "catalog.duckdb"
    stale_duckdb.write_bytes(b"obsolete catalog sentinel")
    destination, backups = tmp_path / "new-data", tmp_path / "backups"

    report = MIGRATION.execute(
        source, destination, backups, "MODELARK-STOPPED", "explicit-sqlite",
        source_kind="sqlite",
    )
    assert report["status"] == "published"
    assert report["source_kind"] == "sqlite"
    assert report["source_selection"] == "explicit"
    assert stale_duckdb.read_bytes() == b"obsolete catalog sentinel"
    manifest = json.loads(
        (backups / "modelark-migration-explicit-sqlite" / "manifest.json").read_text())
    assert manifest["source_selection"] == "explicit"


def test_execution_refuses_confirmation_busy_source_and_existing_destination(tmp_path):
    source = _source(tmp_path)
    destination, backups = tmp_path / "new-data", tmp_path / "backups"
    try:
        MIGRATION.execute(source, destination, backups, "yes", "bad-confirm")
        raise AssertionError("weak confirmation must be refused")
    except RuntimeError as exc:
        assert "MODELARK-STOPPED" in str(exc)

    writer = sqlite3.connect(str(source / "catalog.sqlite"), isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    try:
        try:
            MIGRATION.execute(source, destination, backups, "MODELARK-STOPPED", "busy")
            raise AssertionError("active writer must be refused")
        except RuntimeError as exc:
            assert "writer may still be running" in str(exc)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert not destination.exists()
    failed = json.loads((backups / "modelark-migration-busy" / "manifest.json").read_text())
    assert failed["status"] == "failed"

    destination.mkdir()
    try:
        MIGRATION.execute(source, destination, backups, "MODELARK-STOPPED", "exists")
        raise AssertionError("existing destination must never be overwritten")
    except RuntimeError as exc:
        assert "refusing to overwrite" in str(exc)


def test_backup_root_must_not_create_the_destination_as_a_side_effect(tmp_path):
    source = _source(tmp_path)
    destination = tmp_path / "new-data"
    backup_inside_destination = destination / "backups"
    try:
        MIGRATION.inspect(source, destination, backup_inside_destination)
        raise AssertionError("nested backup root must be refused")
    except RuntimeError as exc:
        assert "outside the destination data directory" in str(exc)
    assert not destination.exists()


def test_stopped_duckdb_source_uses_the_same_backup_first_pipeline(tmp_path):
    try:
        import duckdb
    except ImportError as exc:
        raise SkipTest("DuckDB migration extra is not installed") from exc
    source = tmp_path / "legacy-duckdb"
    source.mkdir()
    con = duckdb.connect(str(source / "catalog.duckdb"))
    statements = [
        "CREATE TABLE models(repo_id VARCHAR, status VARCHAR)",
        "CREATE TABLE files(repo_id VARCHAR, rfilename VARCHAR, size_bytes BIGINT, format VARCHAR)",
        "CREATE TABLE drives(drive_label VARCHAR, role VARCHAR, raid_backed BOOLEAN)",
        "CREATE TABLE replicas(repo_id VARCHAR, rfilename VARCHAR, drive_label VARCHAR)",
        "CREATE TABLE verifications(repo_id VARCHAR)",
        "CREATE TABLE selection(repo_id VARCHAR)",
        "CREATE TABLE archived(repo_id VARCHAR, rfilename VARCHAR, stored_name VARCHAR, "
        "drive_label VARCHAR, compressed BOOLEAN)",
        "CREATE TABLE fetch_events(repo_id VARCHAR, outcome VARCHAR)",
    ]
    for statement in statements:
        con.execute(statement)
    con.execute("INSERT INTO models VALUES ('org/duck','verified')")
    con.execute("INSERT INTO files VALUES ('org/duck','model.safetensors',10,'safetensors')")
    con.execute("INSERT INTO drives VALUES ('drive-02','primary',false)")
    con.execute("INSERT INTO archived VALUES "
                "('org/duck','model.safetensors','model.safetensors.znn','drive-02',true)")
    con.close()

    destination, backups = tmp_path / "duck-next", tmp_path / "duck-backups"
    report = MIGRATION.execute(source, destination, backups, "MODELARK-STOPPED", "duck-fixture")
    assert report["status"] == "published" and report["source_kind"] == "duckdb"
    assert report["destination"]["duckdb_import"]["models"] == {
        "src": 1, "dst": 1, "dropped_cols": []}
    out = sqlite3.connect(str(destination / "catalog.sqlite"))
    assert out.execute("SELECT status FROM models").fetchone()[0] == "inspected"
    assert out.execute("PRAGMA foreign_key_check").fetchall() == []
    out.close()
    assert (backups / "modelark-migration-duck-fixture" / "raw-source" /
            "catalog.duckdb").is_file()


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as td:
                try:
                    fn(Path(td))
                    print(f"ok  {name}")
                except SkipTest as exc:
                    print(f"skip  {name}: {exc}")
    print("all passed")
