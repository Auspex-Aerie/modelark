"""PR-07 / #37 catalog-v4 lifecycle + eligibility migration (tests-first, DEC-049 / RFC-002).

Gate 1: v3→v4 migration contract BEFORE production. Change-contract tests are RED until
``lifecycle`` / ``eligibility`` columns, domain CHECKs, and a backup-first transactional
migration exist. Fail for the reviewed missing v4 behavior — not a broken fixture.

The v3 fixture is FROZEN and production-independent: it never calls ``db.connect()`` to build the
pre-migration catalog (that would bake in whatever the packaged schema is after Gate 2).
"""
from __future__ import annotations

import sqlite3

from modelark.core import db


class _FailOn:
    """Connection proxy that raises when a marker appears in a statement (mid-migration inject)."""

    def __init__(self, con, marker):
        self._con = con
        self._marker = marker

    def execute(self, sql, *args):
        if self._marker in sql:
            raise sqlite3.OperationalError(f"injected failure at: {self._marker}")
        return self._con.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._con, name)


# Frozen catalog-v3 drives shape — independent of packaged schema after Gate 2 adds lifecycle cols.
_V3_DRIVES_DDL = """
CREATE TABLE drives (
    drive_label        VARCHAR PRIMARY KEY NOT NULL CHECK (length(trim(drive_label)) > 0),
    fs_uuid            VARCHAR,
    annex_uuid         VARCHAR,
    capacity_bytes     BIGINT CHECK (capacity_bytes IS NULL OR capacity_bytes >= 0),
    free_bytes         BIGINT CHECK (free_bytes IS NULL OR free_bytes >= 0),
    hw_model           VARCHAR,
    serial             VARCHAR,
    physical_location  VARCHAR,
    role               VARCHAR NOT NULL DEFAULT 'primary' CHECK (role IN ('primary','replica')),
    raid_backed        BOOLEAN NOT NULL DEFAULT false CHECK (raid_backed IN (0, 1)),
    health             VARCHAR,
    last_seen          TIMESTAMP,
    notes              VARCHAR,
    identity_epoch            INTEGER NOT NULL DEFAULT 1 CHECK (identity_epoch >= 1),
    write_generation          INTEGER NOT NULL DEFAULT 0 CHECK (write_generation >= 0),
    filesystem_capacity_bytes BIGINT
                              CHECK (filesystem_capacity_bytes IS NULL
                                     OR filesystem_capacity_bytes >= 0),
    identity_fingerprint      VARCHAR
                              CHECK (identity_fingerprint IS NULL
                                     OR length(identity_fingerprint) = 64),
    write_authority           VARCHAR NOT NULL DEFAULT 'unknown'
                              CHECK (write_authority IN ('unknown','dedicated_local'))
)
"""


def _reopen_raw():
    return sqlite3.connect(str(db.DB_PATH), isolation_level=None)


def _seed_v3(tmp_path):
    """Genuine frozen v3 catalog at user_version=3 — never depends on packaged schema version."""
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    con = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(_V3_DRIVES_DDL)
    con.execute("""
        CREATE TABLE models (
            repo_id VARCHAR PRIMARY KEY NOT NULL,
            numcopies INTEGER NOT NULL DEFAULT 1
        )
    """)
    con.execute("""
        CREATE TABLE files (
            repo_id VARCHAR NOT NULL,
            rfilename VARCHAR NOT NULL,
            size_bytes BIGINT,
            format VARCHAR,
            quant VARCHAR,
            PRIMARY KEY (repo_id, rfilename),
            FOREIGN KEY (repo_id) REFERENCES models(repo_id)
        )
    """)
    con.execute("""
        CREATE TABLE archived (
            repo_id VARCHAR NOT NULL,
            rfilename VARCHAR NOT NULL,
            drive_label VARCHAR NOT NULL,
            compressed INTEGER NOT NULL DEFAULT 0,
            orig_bytes BIGINT,
            stored_bytes BIGINT,
            PRIMARY KEY (repo_id, rfilename, drive_label),
            FOREIGN KEY (drive_label) REFERENCES drives(drive_label)
        )
    """)
    con.execute("""
        CREATE TABLE plans (
            plan_id VARCHAR PRIMARY KEY NOT NULL,
            name VARCHAR,
            is_active INTEGER NOT NULL DEFAULT 0,
            capacity_mode VARCHAR NOT NULL DEFAULT 'guaranteed'
        )
    """)
    con.execute("""
        CREATE TABLE plan_drives (
            plan_id VARCHAR NOT NULL,
            drive_label VARCHAR NOT NULL,
            PRIMARY KEY (plan_id, drive_label),
            FOREIGN KEY (plan_id) REFERENCES plans(plan_id),
            FOREIGN KEY (drive_label) REFERENCES drives(drive_label)
        )
    """)
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16')")
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,fs_uuid,annex_uuid,serial,"
        "role,raid_backed,identity_epoch,write_generation,write_authority) "
        "VALUES('drive-00',1000,500,'fs-00','annex-00','serial-00',"
        "'primary',0,1,0,'unknown')")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,stored_bytes) "
        "VALUES('org/m','model.safetensors','drive-00',0,100,100)")
    con.execute(
        "INSERT INTO plans(plan_id,name,is_active,capacity_mode) "
        "VALUES('ark','Ark',1,'guaranteed')")
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','drive-00')")
    con.execute("PRAGMA user_version=3")
    con.close()

    check = _reopen_raw()
    assert check.execute("PRAGMA user_version").fetchone()[0] == 3
    dcols = {r[1] for r in check.execute("PRAGMA table_info(drives)").fetchall()}
    assert "lifecycle" not in dcols and "eligibility" not in dcols
    assert {"identity_epoch", "write_authority"} <= dcols
    check.close()


def test_migrate_v3_to_v4_adds_lifecycle_eligibility_and_preserves_rows(tmp_path):
    _seed_v3(tmp_path)
    con = db.connect()  # must run v3→v4 migration once implemented
    assert con.execute("PRAGMA user_version").fetchone()[0] == 4, (
        "v3→v4 lifecycle/eligibility migration not implemented (expected Gate-1 red)")

    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    assert {"lifecycle", "eligibility"} <= dcols

    row = con.execute(
        "SELECT lifecycle, eligibility, capacity_bytes, free_bytes, fs_uuid, annex_uuid, serial, "
        "identity_epoch, write_generation, write_authority "
        "FROM drives WHERE drive_label='drive-00'").fetchone()
    assert row[0] == "active" and row[1] == "enabled", row
    assert row[2:] == (1000, 500, "fs-00", "annex-00", "serial-00", 1, 0, "unknown"), row

    assert con.execute("SELECT count(*) FROM models").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM plan_drives").fetchone()[0] == 1
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "drive_tombstones" not in tables

    bak = db.DB_PATH.with_name(db.DB_PATH.name + ".pre-lifecycle-v4.bak")
    assert bak.is_file(), "non-overwriting v3 backup must exist before column migration"
    b = sqlite3.connect(str(bak))
    assert b.execute("PRAGMA user_version").fetchone()[0] == 3, "backup must remain a v3 catalog"
    assert "lifecycle" not in {r[1] for r in b.execute("PRAGMA table_info(drives)").fetchall()}
    b.close()
    con.close()


def test_v4_migration_is_idempotent(tmp_path):
    _seed_v3(tmp_path)
    db.connect().close()
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 4, (
        "v3→v4 migration not implemented (expected Gate-1 red)")
    dcols = [r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()]
    assert len(dcols) == len(set(dcols)), "no duplicate columns from a second migration"
    row = con.execute(
        "SELECT lifecycle, eligibility FROM drives WHERE drive_label='drive-00'").fetchone()
    assert row == ("active", "enabled")
    con.close()


def test_v4_injected_failure_rolls_back_columns_and_user_version(tmp_path):
    assert hasattr(db, "_migrate_lifecycle_eligibility_v4"), (
        "v4 migration helper not implemented yet (expected Gate-1 red)")
    _seed_v3(tmp_path)
    con = _reopen_raw()
    con.execute("PRAGMA foreign_keys=OFF")
    proxy = _FailOn(con, "eligibility")
    try:
        db._migrate_lifecycle_eligibility_v4(proxy, backup_existing=True)
        raise AssertionError("migration should have raised")
    except RuntimeError:
        pass
    con.close()

    raw = _reopen_raw()
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 3, "failed migration must leave v3"
    dcols = {r[1] for r in raw.execute("PRAGMA table_info(drives)").fetchall()}
    assert "lifecycle" not in dcols and "eligibility" not in dcols
    assert raw.execute("SELECT count(*) FROM drives").fetchone()[0] == 1
    raw.close()
    assert db.DB_PATH.with_name(db.DB_PATH.name + ".pre-lifecycle-v4.bak").is_file()


def test_v4_domain_and_not_null_constraints(tmp_path):
    _seed_v3(tmp_path)
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 4, (
        "v4 schema not created (expected Gate-1 red)")

    def rejected(sql):
        try:
            con.execute(sql)
            return False
        except sqlite3.IntegrityError:
            return True

    assert rejected("UPDATE drives SET lifecycle='ghost' WHERE drive_label='drive-00'")
    assert rejected("UPDATE drives SET eligibility='maybe' WHERE drive_label='drive-00'")
    assert rejected("UPDATE drives SET lifecycle=NULL WHERE drive_label='drive-00'")
    assert rejected("UPDATE drives SET eligibility=NULL WHERE drive_label='drive-00'")
    con.execute("UPDATE drives SET lifecycle='retired', eligibility='excluded' "
                "WHERE drive_label='drive-00'")
    assert con.execute(
        "SELECT lifecycle, eligibility FROM drives WHERE drive_label='drive-00'"
    ).fetchone() == ("retired", "excluded")
    con.close()


def test_v0_to_v4_does_not_introduce_lifecycle_columns_during_integrity_rebuild(tmp_path):
    """Integrity rebuild must not invent v4 columns early; evidence backup remains pre-lifecycle."""
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    con = db.connect()
    con.execute("PRAGMA foreign_keys=OFF")
    for view in db._VIEW_NAMES:
        con.execute(f'DROP VIEW IF EXISTS "{view}"')
    for table in db._INTEGRITY_TABLES:
        con.execute(f'CREATE TABLE "{table}__legacy" AS SELECT * FROM "{table}"')
    for table in reversed(db._INTEGRITY_TABLES):
        con.execute(f'DROP TABLE "{table}"')
    for table in db._INTEGRITY_TABLES:
        con.execute(f'ALTER TABLE "{table}__legacy" RENAME TO "{table}"')
    con.execute("DROP TABLE IF EXISTS drive_clean_anchors")
    con.execute("DROP TABLE IF EXISTS drive_dirty_generations")
    pre_v3 = ("drive_label,fs_uuid,annex_uuid,capacity_bytes,free_bytes,hw_model,serial,"
              "physical_location,role,raid_backed,health,last_seen,notes")
    # Drop lifecycle cols if a future packaged schema already has them before strip.
    present = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    pre_cols = ",".join(c for c in pre_v3.split(",") if c in present)
    con.execute(f"CREATE TABLE drives__pre3 AS SELECT {pre_cols} FROM drives")
    con.execute("DROP TABLE drives")
    con.execute("ALTER TABLE drives__pre3 RENAME TO drives")
    con.execute("INSERT INTO drives(drive_label,free_bytes,role,raid_backed) "
                "VALUES('drive-00',500,'primary',0)")
    con.execute("PRAGMA user_version=0")
    con.close()

    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 4, (
        "full migration must land at v4 (expected Gate-1 red until v4 exists)")
    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    assert {"lifecycle", "eligibility"} <= dcols
    row = con.execute(
        "SELECT lifecycle, eligibility FROM drives WHERE drive_label='drive-00'").fetchone()
    assert row == ("active", "enabled"), row
    v3_bak = db.DB_PATH.with_name(db.DB_PATH.name + ".pre-evidence-v3.bak")
    assert v3_bak.is_file(), "v0 path must still take the evidence v3 backup"
    b = sqlite3.connect(str(v3_bak))
    bcols = {r[1] for r in b.execute("PRAGMA table_info(drives)").fetchall()}
    assert "lifecycle" not in bcols and "eligibility" not in bcols, (
        "integrity/evidence steps must not introduce v4 columns early")
    b.close()
    con.close()


def test_newer_catalog_rejection_uses_schema_version_plus_one(tmp_path):
    """Hard-coded future version must track the build, not a frozen literal forever."""
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    con = db.connect()
    future = db._SCHEMA_VERSION + 1
    con.execute(f"PRAGMA user_version={future}")
    con.close()
    try:
        db.connect()
        raise AssertionError("an older program must reject a newer catalog")
    except RuntimeError as exc:
        assert "newer than this ModelArk build" in str(exc), exc
    raw = sqlite3.connect(str(db.DB_PATH))
    assert raw.execute("PRAGMA user_version").fetchone()[0] == future
    raw.close()


def main():
    import inspect
    import tempfile
    from pathlib import Path

    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                fn(Path(tempfile.mkdtemp(prefix="mark-v4-")))
            else:
                fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, type(exc).__name__, str(exc)[:200]))
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    print("Gate-1 tests-only: migration contracts EXPECTED RED until v4 production lands.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
