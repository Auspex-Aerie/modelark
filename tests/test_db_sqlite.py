"""DEC-024: catalog on SQLite/WAL — schema bootstraps, upsert/replace work, tags round-trip as JSON,
and (the whole point) a SECOND connection reads while the first is open, no exclusive-lock error."""
from __future__ import annotations

import json

from modelark.core import db


def _fresh(tmp_path):
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    return db.connect()


def test_schema_and_views(tmp_path):
    con = _fresh(tmp_path)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"models", "files", "drives", "replicas", "verifications", "selection",
            "archived", "fetch_events"} <= tables, tables
    views = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='view'").fetchall()}
    assert {"v_ui", "v_model_summary", "v_storage_by_drive"} <= views, views
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
    db.replace_files(con, "org/m", [{"repo_id": "org/m", "rfilename": "a", "size_bytes": 1},
                                    {"repo_id": "org/m", "rfilename": "b", "size_bytes": 2}])
    db.replace_files(con, "org/m", [{"repo_id": "org/m", "rfilename": "c", "size_bytes": 3}])
    rows = [r[0] for r in con.execute("SELECT rfilename FROM files WHERE repo_id='org/m'").fetchall()]
    assert rows == ["c"], rows
    con.close()


def test_legacy_archive_basename_migrates_to_nested_relpath(tmp_path):
    con = _fresh(tmp_path)
    con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,drive_label) "
                "VALUES('org/m','nested/model.safetensors','model.safetensors.znn','drive-01')")
    db._migrate(con)
    got = con.execute("SELECT stored_relpath FROM archived").fetchone()[0]
    assert got == "nested/model.safetensors.znn", got
    con.close()


def test_legacy_tier_a_verified_status_is_narrowed_to_inspected(tmp_path):
    con = _fresh(tmp_path)
    con.execute("INSERT INTO models(repo_id,status) VALUES('org/m','verified')")
    db._migrate(con)
    assert con.execute("SELECT status FROM models WHERE repo_id='org/m'").fetchone()[0] == "inspected"
    con.close()


if __name__ == "__main__":
    import tempfile
    from pathlib import Path
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(Path(tempfile.mkdtemp()))
            print(f"ok  {name}")
    print("all passed")
