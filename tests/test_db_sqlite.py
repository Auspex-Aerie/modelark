"""DEC-024: catalog on SQLite/WAL — schema bootstraps, upsert/replace work, tags round-trip as JSON,
and (the whole point) a SECOND connection reads while the first is open, no exclusive-lock error."""
from __future__ import annotations

import json
import sqlite3
from unittest import mock

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
    # Represent a genuine pre-v3 catalog: strip the catalog-v3 evidence objects/columns a real legacy
    # database never had, so the rebuild exercises the v0->v3 path from scratch (not a hybrid shape).
    con.execute("DROP TABLE IF EXISTS drive_clean_anchors")
    con.execute("DROP TABLE IF EXISTS drive_dirty_generations")
    pre_v3 = ("drive_label,fs_uuid,annex_uuid,capacity_bytes,free_bytes,hw_model,serial,"
              "physical_location,role,raid_backed,health,last_seen,notes")
    con.execute(f"CREATE TABLE drives__pre3 AS SELECT {pre_v3} FROM drives")
    con.execute("DROP TABLE drives")
    con.execute("ALTER TABLE drives__pre3 RENAME TO drives")
    con.execute("PRAGMA user_version=0")
    con.close()
    return sqlite3.connect(str(db.DB_PATH), isolation_level=None)


def _legacy_v1_plans(tmp_path, *, constrained=True):
    """Create the prior plans.provisioning schema at user_version=1."""
    con = _fresh(tmp_path)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("DROP INDEX IF EXISTS idx_plans_one_active")
    check = "CHECK (provisioning IN ('uncompressed','compressed'))" if constrained else ""
    con.execute(f"""
        CREATE TABLE plans__v1 (
            plan_id VARCHAR PRIMARY KEY NOT NULL,
            name VARCHAR,
            annex_root VARCHAR,
            provisioning VARCHAR NOT NULL DEFAULT 'uncompressed' {check},
            status VARCHAR NOT NULL DEFAULT 'active',
            is_active BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes VARCHAR
        )
    """)
    con.execute("DROP TABLE plans")
    con.execute("ALTER TABLE plans__v1 RENAME TO plans")
    con.execute("CREATE UNIQUE INDEX idx_plans_one_active ON plans(is_active) WHERE is_active=1")
    con.execute("PRAGMA user_version=1")
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
    plan_columns = {row[1] for row in con.execute("PRAGMA table_info(plans)").fetchall()}
    assert "capacity_mode" in plan_columns and "provisioning" not in plan_columns
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    # v3 (#35-A) evidence objects are bootstrapped for a fresh catalog too
    drive_columns = {row[1] for row in con.execute("PRAGMA table_info(drives)").fetchall()}
    assert {"identity_epoch", "write_generation", "write_authority"} <= drive_columns
    assert {"drive_dirty_generations", "drive_clean_anchors"} <= tables
    con.close()


def test_normal_connect_does_not_scan_every_foreign_key_row(tmp_path):
    statements = []
    real_connect = sqlite3.connect

    class TracedConnection:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *args):
            statements.append(sql.strip().lower())
            return self._con.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self._con, name)

    initial = _fresh(tmp_path)
    initial.close()
    with mock.patch.object(db.sqlite3, "connect", side_effect=lambda *a, **kw:
                           TracedConnection(real_connect(*a, **kw))):
        con = db.connect()
        con.close()
    assert "pragma foreign_key_check" not in statements, statements


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


def test_read_only_connect_is_enforced_at_open_and_never_bootstraps(tmp_path):
    missing = tmp_path / "missing" / "catalog.sqlite"
    db.CATALOG_DIR = missing.parent
    db.DB_PATH = missing
    try:
        db.connect(read_only=True)
        raise AssertionError("a read-only connection must not create a missing catalog")
    except sqlite3.OperationalError as exc:
        assert "unable to open" in str(exc).lower(), exc
    assert not missing.parent.exists()

    writer = _fresh(tmp_path / "existing")
    writer.close()
    mode = db.DB_PATH.stat().st_mode
    db.DB_PATH.chmod(0o444)
    try:
        reader = db.connect(read_only=True)
        assert reader.execute("SELECT count(*) FROM models").fetchone() == (0,)
        reader.close()
    finally:
        db.DB_PATH.chmod(mode)


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
    backup = db.DB_PATH.with_name(f"{db.DB_PATH.name}.pre-integrity-v1.bak")
    old = sqlite3.connect(str(backup))
    assert old.execute("SELECT status FROM models").fetchone()[0] == "verified"
    assert old.execute("PRAGMA foreign_key_list(archived)").fetchall() == []
    old.close()
    v2_backup = sqlite3.connect(str(db.DB_PATH.with_name(
        f"{db.DB_PATH.name}.pre-capacity-v2.bak"
    )))
    assert v2_backup.execute("SELECT status FROM models").fetchone()[0] == "inspected"
    v2_backup.close()


def test_v0_to_v3_migration_backs_up_a_genuine_v2_before_evidence(tmp_path):
    """A v0 catalog migrating all the way to v3 must still take the evidence backup, and that backup
    must be a genuine v2 (no v3 columns/tables): the integrity rebuild must not pull the v3 drive
    columns backward and let the v3 step short-circuit past its own backup."""
    con = _legacy_without_constraints(tmp_path)
    con.execute("INSERT INTO drives(drive_label,free_bytes,role,raid_backed) "
                "VALUES('drive-01',500,'primary',0)")
    con.close()

    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    drive_columns = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    assert {"identity_epoch", "write_authority"} <= drive_columns
    assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0   # no fabricated evidence
    con.close()

    v3_backup = db.DB_PATH.with_name(f"{db.DB_PATH.name}.pre-evidence-v3.bak")
    assert v3_backup.is_file(), "v0->v3 must still take the evidence backup"
    backup = sqlite3.connect(str(v3_backup))
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
    assert "identity_epoch" not in {r[1] for r in backup.execute("PRAGMA table_info(drives)").fetchall()}
    tables = {r[0] for r in backup.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "drive_clean_anchors" not in tables and "drive_dirty_generations" not in tables
    backup.close()


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
    assert db.DB_PATH.with_name(f"{db.DB_PATH.name}.pre-integrity-v1.bak").is_file()


def test_v1_capacity_modes_map_transactionally_and_idempotently(tmp_path):
    legacy = _legacy_v1_plans(tmp_path)
    legacy.execute(
        "INSERT INTO plans(plan_id,name,provisioning,is_active) VALUES('safe','Safe','uncompressed',1)"
    )
    legacy.execute(
        "INSERT INTO plans(plan_id,name,provisioning,is_active) VALUES('aware','Aware','compressed',0)"
    )
    legacy.execute("INSERT INTO drives(drive_label) VALUES('drive-01')")
    legacy.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('safe','drive-01')")
    legacy.close()

    con = db.connect()
    assert con.execute(
        "SELECT plan_id,capacity_mode FROM plans ORDER BY plan_id"
    ).fetchall() == [("aware", "compression_aware"), ("safe", "guaranteed")]
    columns = {row[1] for row in con.execute("PRAGMA table_info(plans)").fetchall()}
    assert "capacity_mode" in columns and "provisioning" not in columns
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("SELECT plan_id,drive_label FROM plan_drives").fetchone() == (
        "safe", "drive-01"
    )
    con.close()

    backup = db.DB_PATH.with_name(f"{db.DB_PATH.name}.pre-capacity-v2.bak")
    assert backup.is_file()
    before = backup.stat().st_mtime_ns
    con = db.connect()
    assert con.execute("SELECT count(*) FROM plans").fetchone()[0] == 2
    con.close()
    assert backup.stat().st_mtime_ns == before, "the recovery backup must never be overwritten"


def test_capacity_mode_schema_inspection_runs_under_immediate_lock(tmp_path):
    con = _fresh(tmp_path)
    con.execute("PRAGMA foreign_keys=OFF")
    statements = []
    con.set_trace_callback(statements.append)
    db._migrate_capacity_mode_v2(con, backup_existing=False)
    con.set_trace_callback(None)
    normalized = [statement.strip().lower() for statement in statements]
    begin = normalized.index("begin immediate")
    inspect = next(
        index for index, statement in enumerate(normalized)
        if statement.startswith('pragma table_info("plans")')
    )
    assert begin < inspect
    con.close()


def test_v2_capacity_mode_migration_rolls_back_invalid_legacy_value(tmp_path):
    legacy = _legacy_v1_plans(tmp_path, constrained=False)
    legacy.execute(
        "INSERT INTO plans(plan_id,name,provisioning,is_active) VALUES('bad','Bad','lz4',1)"
    )
    legacy.close()
    try:
        db.connect()
        raise AssertionError("invalid legacy capacity values must abort schema v2")
    except RuntimeError as exc:
        assert "schema v2 capacity modes" in str(exc) or "capacity" in str(exc), exc
    raw = sqlite3.connect(str(db.DB_PATH))
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    columns = {row[1] for row in raw.execute("PRAGMA table_info(plans)").fetchall()}
    assert "provisioning" in columns and "capacity_mode" not in columns
    assert raw.execute("SELECT provisioning FROM plans").fetchone()[0] == "lz4"
    raw.close()
    assert db.DB_PATH.with_name(f"{db.DB_PATH.name}.pre-capacity-v2.bak").is_file()


def test_newer_catalog_is_rejected_without_stamping_down(tmp_path):
    con = _fresh(tmp_path)
    con.execute("PRAGMA user_version=4")             # a schema newer than this build (_SCHEMA_VERSION=3)
    con.close()
    before = db.DB_PATH.read_bytes()
    sidecars_before = {
        path.name for path in db.DB_PATH.parent.iterdir()
        if path.name.startswith(db.DB_PATH.name)
    }
    try:
        db.connect()
        raise AssertionError("an older program must reject a newer catalog")
    except RuntimeError as exc:
        assert "newer than this ModelArk build" in str(exc), exc
    raw = sqlite3.connect(str(db.DB_PATH))
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 4
    raw.close()
    assert db.DB_PATH.read_bytes() == before
    assert {
        path.name for path in db.DB_PATH.parent.iterdir()
        if path.name.startswith(db.DB_PATH.name)
    } == sidecars_before


def test_read_only_open_refuses_an_unmigrated_v1_catalog(tmp_path):
    legacy = _legacy_v1_plans(tmp_path)
    legacy.close()
    try:
        db.connect(read_only=True)
        raise AssertionError("read-only diagnostics must not attempt a schema migration")
    except RuntimeError as exc:
        assert "requires a writable migration" in str(exc), exc
    raw = sqlite3.connect(str(db.DB_PATH))
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "provisioning" in {
        row[1] for row in raw.execute("PRAGMA table_info(plans)").fetchall()
    }
    raw.close()


def test_read_only_version_validation_failure_closes_connection(tmp_path):
    writer = _fresh(tmp_path)
    writer.close()
    raw = sqlite3.connect(str(db.DB_PATH), isolation_level=None)

    class TracedConnection:
        closed = False

        def execute(self, sql, *args):
            return raw.execute(sql, *args)

        def close(self):
            self.closed = True
            raw.close()

    traced = TracedConnection()
    with mock.patch.object(db.sqlite3, "connect", return_value=traced), \
         mock.patch.object(db, "_validate_catalog_version", side_effect=RuntimeError("old")):
        try:
            db.connect(read_only=True)
            raise AssertionError("schema validation failure must propagate")
        except RuntimeError as exc:
            assert str(exc) == "old"
    assert traced.closed


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
