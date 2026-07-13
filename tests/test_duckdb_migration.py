"""The optional DuckDB importer closes and cleans up on both success and constraint failure."""
from __future__ import annotations

import sqlite3
import importlib.util
from pathlib import Path

import duckdb

from modelark.core import db

_SPEC = importlib.util.spec_from_file_location(
    "migrate_duckdb_to_sqlite", Path(__file__).parents[1] / "scripts" / "migrate_duckdb_to_sqlite.py")
assert _SPEC and _SPEC.loader
_MIGRATOR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATOR)
migrate = _MIGRATOR.migrate


_TABLES = (
    "CREATE TABLE models(repo_id VARCHAR, status VARCHAR)",
    "CREATE TABLE files(repo_id VARCHAR, rfilename VARCHAR, size_bytes BIGINT, format VARCHAR)",
    "CREATE TABLE drives(drive_label VARCHAR, role VARCHAR, raid_backed BOOLEAN)",
    "CREATE TABLE replicas(repo_id VARCHAR, rfilename VARCHAR, drive_label VARCHAR)",
    "CREATE TABLE verifications(repo_id VARCHAR)",
    "CREATE TABLE selection(repo_id VARCHAR)",
    "CREATE TABLE archived(repo_id VARCHAR, rfilename VARCHAR, stored_name VARCHAR, "
    "drive_label VARCHAR, compressed BOOLEAN)",
    "CREATE TABLE fetch_events(repo_id VARCHAR, outcome VARCHAR)",
)


def _legacy(path: Path):
    con = duckdb.connect(str(path))
    for statement in _TABLES:
        con.execute(statement)
    return con


def test_valid_duckdb_migration_applies_backfills_and_constraints(tmp_path):
    src, dst = tmp_path / "legacy.duckdb", tmp_path / "catalog.sqlite"
    old_data, old_db = db.CATALOG_DIR, db.DB_PATH
    con = _legacy(src)
    con.execute("INSERT INTO models VALUES ('org/model','verified')")
    con.execute("INSERT INTO files VALUES "
                "('org/model','weights/model.safetensors',10,'safetensors')")
    con.execute("INSERT INTO drives VALUES ('drive-01','primary',false)")
    con.execute("INSERT INTO archived VALUES "
                "('org/model','weights/model.safetensors','model.safetensors.znn','drive-01',true)")
    con.close()
    try:
        report = migrate(src, dst)
        out = sqlite3.connect(str(dst))
        assert out.execute("SELECT status,numcopies FROM models").fetchone() == ("inspected", 1)
        assert out.execute("SELECT stored_relpath FROM archived").fetchone()[0] == \
            "weights/model.safetensors.znn"
        assert out.execute("PRAGMA foreign_key_check").fetchall() == []
        assert all(row["src"] == row["dst"] for row in report.values())
        out.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH = old_data, old_db


def test_orphaned_duckdb_row_rolls_back_closes_and_removes_partial_destination(tmp_path):
    src, dst = tmp_path / "orphan.duckdb", tmp_path / "catalog.sqlite"
    old_data, old_db = db.CATALOG_DIR, db.DB_PATH
    con = _legacy(src)
    con.execute("INSERT INTO selection VALUES ('missing/model')")
    con.close()
    try:
        try:
            migrate(src, dst)
            raise AssertionError("an orphaned selection must fail migration")
        except RuntimeError as exc:
            message = str(exc)
            assert "table 'selection'" in message and "source row 1" in message, message
            assert "repo_id='missing/model'" in message and "source is unchanged" in message, message
        assert not dst.exists(), "a partial constrained catalog must not survive a failed import"
        # Both connections are closed: the source can be reopened and the destination recreated.
        reopened = duckdb.connect(str(src), read_only=True)
        assert reopened.execute("SELECT repo_id FROM selection").fetchone()[0] == "missing/model"
        reopened.close()
        probe = sqlite3.connect(str(dst), timeout=0.1)
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("ROLLBACK")
        probe.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH = old_data, old_db


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(Path(tempfile.mkdtemp()))
            print(f"ok  {name}")
    print("all passed")
