"""DEC-024: catalog on SQLite/WAL — schema bootstraps, upsert/replace work, tags round-trip as JSON,
and (the whole point) a SECOND connection reads while the first is open, no exclusive-lock error."""
from __future__ import annotations

import json
import sqlite3

from modelark.core import db


def _fresh(tmp_path):
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    return db.connect()


def _legacy_without_constraints(tmp_path):
    """Turn a canonical empty catalog into the shape shipped before DEC-041.

    CREATE TABLE AS preserves columns but deliberately strips PK/FK/CHECK/default clauses.
    """
    con = _fresh(tmp_path)
    con.execute("PRAGMA foreign_keys=OFF")
    for view in db._VIEW_NAMES:
        con.execute(f'DROP VIEW IF EXISTS "{view}"')
    for table in db._INTEGRITY_TABLES:
        con.execute(f'CREATE TABLE "{table}__legacy" AS SELECT * FROM "{table}"')
    for table in reversed(db._INTEGRITY_TABLES):
        con.execute(f'DROP TABLE "{table}"')
    for table in db._INTEGRITY_TABLES:
        con.execute(f'ALTER TABLE "{table}__legacy" RENAME TO "{table}"')
    con.execute("PRAGMA user_version=0")
    con.close()
    return sqlite3.connect(str(db.DB_PATH), isolation_level=None)


def test_schema_and_views(tmp_path):
    con = _fresh(tmp_path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"models", "files", "drives", "replicas", "verifications", "selection",
            "archived", "fetch_events"} <= tables, tables
    views = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()}
    assert {"v_ui", "v_model_summary", "v_storage_by_drive"} <= views, views
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("PRAGMA foreign_key_list(archived)").fetchall()
    con.close()


def test_upsert_touch_and_v_ui(tmp_path):
    con = _fresh(tmp_path)
    db.upsert(con, "models", {"repo_id": "org/m", "author": "org", "status": "discovered",
                              "gated": "false", "tags": json.dumps(["a", "b"])}, pk=["repo_id"])
    db.upsert(con, "models", {"repo_id": "org/m", "author": "org2"}, pk=["repo_id"], touch=["updated_at"])
    con.execute("INSERT INTO files (repo_id, rfilename, size_bytes, format) VALUES (?,?,?,?)",
                ["org/m", "model.safetensors", 1000, "safetensors"])
    row = con.execute("SELECT repo_id, author, bytes FROM v_ui").fetchone()
    assert row == ("org/m", "org2", 1000), row                     # update path + FILTER-agg both work
    tags = con.execute("SELECT tags FROM models WHERE repo_id='org/m'").fetchone()[0]
    assert json.loads(tags) == ["a", "b"]                          # tags round-trips as JSON text
    con.close()


def test_concurrent_read_while_writer_open(tmp_path):
    writer = _fresh(tmp_path)
    db.upsert(writer, "models", {"repo_id": "org/x", "status": "discovered"}, pk=["repo_id"])
    reader = db.connect(read_only=True)                            # opens WHILE writer is live — DuckDB refused this
    got = reader.execute("SELECT repo_id FROM models WHERE repo_id='org/x'").fetchone()
    assert got == ("org/x",), got
    db.upsert(writer, "models", {"repo_id": "org/z", "status": "discovered"}, pk=["repo_id"])  # writer keeps writing
    assert reader.execute("SELECT count(*) FROM models").fetchone()[0] >= 2                     # reader sees it live
    try:
        reader.execute("INSERT INTO models (repo_id) VALUES ('nope')")
        raise AssertionError("read_only connection must refuse writes")
    except Exception as e:
        assert "readonly" in str(e).lower() or "read-only" in str(e).lower(), e
    reader.close()
    writer.close()


def test_replace_files_is_atomic_replace(tmp_path):
    con = _fresh(tmp_path)
    con.execute("INSERT INTO models(repo_id) VALUES('org/m')")
    db.replace_files(con, "org/m", [{"repo_id": "org/m", "rfilename": "a", "size_bytes": 1},
                                    {"repo_id": "org/m", "rfilename": "b", "size_bytes": 2}])
    db.replace_files(con, "org/m", [{"repo_id": "org/m", "rfilename": "c", "size_bytes": 3}])
    rows = [r[0] for r in con.execute("SELECT rfilename FROM files WHERE repo_id='org/m'").fetchall()]
    assert rows == ["c"], rows
    con.close()


def test_replace_files_preserves_durable_archive_provenance(tmp_path):
    con = _fresh(tmp_path)
    con.execute("INSERT INTO models(repo_id) VALUES('org/m')")
    con.execute("INSERT INTO drives(drive_label) VALUES('drive-01')")
    db.replace_files(con, "org/m", [
        {"repo_id": "org/m", "rfilename": "keep", "size_bytes": 1},
        {"repo_id": "org/m", "rfilename": "stale", "size_bytes": 2},
    ])
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,compressed) "
                "VALUES('org/m','keep','drive-01',0)")
    db.replace_files(con, "org/m", [
        {"repo_id": "org/m", "rfilename": "new", "size_bytes": 3},
    ])
    got = [r[0] for r in con.execute(
        "SELECT rfilename FROM files WHERE repo_id='org/m' ORDER BY rfilename").fetchall()]
    assert got == ["keep", "new"], got
    con.close()


def test_legacy_catalog_rebuild_preserves_rows_and_applies_migrations(tmp_path):
    con = _legacy_without_constraints(tmp_path)
    con.execute("INSERT INTO models(repo_id,status,numcopies) VALUES('org/m','verified',1)")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                "VALUES('org/m','nested/model.safetensors',10,'safetensors')")
    con.execute("INSERT INTO drives(drive_label,role,raid_backed) "
                "VALUES('drive-01','primary',0)")
    con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,drive_label,compressed) "
                "VALUES('org/m','nested/model.safetensors','model.safetensors.znn','drive-01',1)")
    con.close()

    con = db.connect()
    assert con.execute("SELECT status FROM models WHERE repo_id='org/m'").fetchone()[0] == "inspected"
    got = con.execute("SELECT stored_relpath FROM archived").fetchone()[0]
    assert got == "nested/model.safetensors.znn", got
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert con.execute("PRAGMA foreign_key_list(archived)").fetchall()
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    backup = db.DB_PATH.with_name(f"{db.DB_PATH.name}.pre-integrity-v{db._SCHEMA_VERSION}.bak")
    old = sqlite3.connect(str(backup))
    assert old.execute("SELECT status FROM models").fetchone()[0] == "inspected"
    assert old.execute("PRAGMA foreign_key_list(archived)").fetchall() == []
    old.close()


def test_legacy_orphans_abort_and_roll_back_integrity_rebuild(tmp_path):
    con = _legacy_without_constraints(tmp_path)
    con.execute("INSERT INTO selection(repo_id) VALUES('missing/model')")
    con.close()
    try:
        db.connect()
        raise AssertionError("orphaned legacy rows must abort the migration")
    except RuntimeError as exc:
        assert "orphaned rows" in str(exc) and "selection" in str(exc), exc
    raw = sqlite3.connect(str(db.DB_PATH))
    assert raw.execute("PRAGMA foreign_key_list(archived)").fetchall() == []
    assert raw.execute("SELECT repo_id FROM selection").fetchone()[0] == "missing/model"
    raw.close()
    assert db.DB_PATH.with_name(
        f"{db.DB_PATH.name}.pre-integrity-v{db._SCHEMA_VERSION}.bak").is_file()


def test_foreign_keys_domains_and_single_active_plan_are_enforced(tmp_path):
    con = _fresh(tmp_path)

    def rejected(sql, params=()):
        try:
            con.execute(sql, params)
            raise AssertionError(f"constraint should reject: {sql}")
        except sqlite3.IntegrityError:
            pass

    rejected("INSERT INTO selection(repo_id) VALUES('missing/model')")
    rejected("INSERT INTO models(repo_id,numcopies) VALUES('bad/copies',3)")
    rejected("INSERT INTO drives(drive_label,role) VALUES('bad-drive','cache')")
    # These are intentionally standalone: their CLIs support uncatalogued explicit repo IDs.
    con.execute("INSERT INTO verifications(repo_id) VALUES('uncatalogued/verify')")
    con.execute("INSERT INTO fetch_events(repo_id,outcome) VALUES('uncatalogued/fetch','error')")
    con.execute("INSERT INTO fetch_events(repo_id,outcome) VALUES(NULL,'throttled')")
    con.execute("INSERT INTO plans(plan_id,is_active) VALUES('one',1)")
    rejected("INSERT INTO plans(plan_id,is_active) VALUES('two',1)")
    con.close()


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(Path(tempfile.mkdtemp()))
            print(f"ok  {name}")
    print("all passed")
