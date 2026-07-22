"""PR-02 / #35-A catalog-v3 migration contract (tests-first, RFC-002 / DEC-049).

Gate 1: expresses the v2->v3 migration + schema contract BEFORE production. Change-contract tests are
RED until the v3 migration and DDL exist, and fail for the reviewed reason (missing v3 behavior), not
a broken fixture — each builds a genuine v2 catalog with the current build, then asserts the v3 result.

Scope (per Gate 0): schema additions + backup-first, transactional, additive migration only. No
fencing, mounts, df, drive writes, recovery, or admission cutover (those are #35-B/#35-C).

The pre-existing generic "a newer schema is rejected" test in test_db_sqlite.py is retained and will be
re-pointed to v4 alongside the implementation; it is not duplicated here.
"""
from __future__ import annotations

import sqlite3

from modelark.core import db


class _FailOn:
    """A connection proxy that raises when a chosen marker appears in a statement, to inject a
    mid-migration failure while delegating everything else (including con.backup) to the real con."""

    def __init__(self, con, marker):
        self._con = con
        self._marker = marker

    def execute(self, sql, *args):
        if self._marker in sql:
            raise sqlite3.OperationalError(f"injected failure at: {self._marker}")
        return self._con.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._con, name)


def _seed_v2(tmp_path):
    """Build a real v2 catalog (current build) with a drive carrying identity + legacy scalars."""
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 2, "fixture must start at schema v2"
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
                "VALUES('org/m','model.safetensors',100,'safetensors','bf16')")
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,fs_uuid,annex_uuid,serial) "
                "VALUES('drive-00',1000,500,'fs-uuid-00','annex-uuid-00','serial-00')")
    con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,compressed) "
                "VALUES('org/m','model.safetensors','drive-00',0)")
    con.close()


def _reopen_raw():
    return sqlite3.connect(str(db.DB_PATH), isolation_level=None)


def test_migrate_v2_to_v3_adds_schema_and_preserves_rows(tmp_path):
    _seed_v2(tmp_path)
    con = db.connect()                                  # triggers the v2->v3 migration
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3, \
        "v2->v3 migration not implemented (expected Gate-1 red)"

    dcols = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    assert {"identity_epoch", "write_generation", "filesystem_capacity_bytes",
            "identity_fingerprint", "write_authority"} <= dcols
    row = con.execute(
        "SELECT identity_epoch, write_generation, write_authority, filesystem_capacity_bytes, "
        "identity_fingerprint, free_bytes, capacity_bytes, fs_uuid, annex_uuid, serial "
        "FROM drives WHERE drive_label='drive-00'").fetchone()
    # defaults on the migrated row + no fabricated evidence; legacy scalars/identity preserved exactly
    assert row == (1, 0, "unknown", None, None, 500, 1000, "fs-uuid-00", "annex-uuid-00", "serial-00")

    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"drive_dirty_generations", "drive_clean_anchors"} <= tables
    assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM models").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 1

    bak = db.DB_PATH.with_name(db.DB_PATH.name + ".pre-evidence-v3.bak")
    assert bak.is_file(), "non-overwriting v2 backup must exist"
    b = sqlite3.connect(str(bak))
    assert b.execute("PRAGMA user_version").fetchone()[0] == 2, "backup must remain a v2 catalog"
    btables = {r[0] for r in b.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "drive_clean_anchors" not in btables
    b.close()
    con.close()


def test_migration_is_idempotent(tmp_path):
    _seed_v2(tmp_path)
    db.connect().close()
    con = db.connect()                                  # second open must be a stable no-op
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3, \
        "v2->v3 migration not implemented (expected Gate-1 red)"
    assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
    dcols = [r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()]
    assert len(dcols) == len(set(dcols)), "no duplicate columns from a second migration"
    con.close()


def test_injected_failure_rolls_back_to_v2(tmp_path):
    assert hasattr(db, "_migrate_capacity_evidence_v3"), \
        "v3 migration not implemented yet (expected Gate-1 red)"
    _seed_v2(tmp_path)
    con = _reopen_raw()
    con.execute("PRAGMA foreign_keys=OFF")
    proxy = _FailOn(con, "drive_clean_anchors")         # fail mid-DDL, after the backup
    try:
        db._migrate_capacity_evidence_v3(proxy, backup_existing=True)
        raise AssertionError("migration should have raised")
    except sqlite3.OperationalError:
        pass
    con.close()

    raw = _reopen_raw()
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2, "failed migration must leave v2"
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "drive_dirty_generations" not in tables and "drive_clean_anchors" not in tables
    dcols = {r[1] for r in raw.execute("PRAGMA table_info(drives)").fetchall()}
    assert "identity_epoch" not in dcols, "no v3 column may survive a rolled-back migration"
    assert raw.execute("SELECT count(*) FROM drives").fetchone()[0] == 1, "data preserved"
    raw.close()
    assert db.DB_PATH.with_name(db.DB_PATH.name + ".pre-evidence-v3.bak").is_file()


def test_v3_schema_constraint_matrix(tmp_path):
    _seed_v2(tmp_path)
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3, \
        "v3 schema not created (expected Gate-1 red)"

    def rejected(sql, exc_types, contains=None):
        """A negative case: the statement must raise the SPECIFIC constraint error, so an unrelated
        OperationalError (missing/misspelled column in a malformed schema) is NOT mistaken for a
        working constraint (Greptile #1)."""
        try:
            con.execute(sql)
        except exc_types as exc:
            return contains is None or contains.lower() in str(exc).lower()
        return False

    fp = "a" * 64
    anchor = ("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
              "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
              "observed_at) VALUES('drive-00',1,{gen},{free},1000,'{fp}','{auth}','p','p','now')")

    # Positive controls: valid rows insert cleanly. If the schema is malformed these raise and the
    # test fails here rather than letting negative cases pass by accident.
    con.execute("INSERT INTO drive_dirty_generations"
                "(drive_label,identity_epoch,generation,operation_code) VALUES('drive-00',1,1,'test')")
    con.execute("INSERT INTO drive_dirty_generations"
                "(drive_label,identity_epoch,generation,operation_code,owner_session_id,owner_fencing_token)"
                " VALUES('drive-00',1,2,'test','sess',7)")
    con.execute(anchor.format(gen=1, free=500, fp=fp, auth="dedicated_local"))   # valid clean anchor

    # Paired dirty-owner CHECK: exactly one of the pair is refused.
    assert rejected(
        "INSERT INTO drive_dirty_generations"
        "(drive_label,identity_epoch,generation,operation_code,owner_session_id) "
        "VALUES('drive-00',1,3,'test','sess')", sqlite3.IntegrityError)

    # Append-only triggers reject UPDATE and DELETE on BOTH evidence tables (Greptile #2). Triggers
    # RAISE(ABORT, '... append-only'), so assert the trigger error specifically, not any error.
    for table in ("drive_dirty_generations", "drive_clean_anchors"):
        assert rejected(f"UPDATE {table} SET identity_epoch=9 WHERE generation=1",
                        sqlite3.OperationalError, "append-only"), table
        assert rejected(f"DELETE FROM {table} WHERE generation=1",
                        sqlite3.OperationalError, "append-only"), table

    # Anchor free bytes must not exceed filesystem capacity; fingerprint must be 64 chars;
    # write_authority is constrained — each on the existing, still-unanchored generation 2.
    assert rejected(anchor.format(gen=2, free=2000, fp=fp, auth="dedicated_local"), sqlite3.IntegrityError)
    assert rejected(anchor.format(gen=2, free=500, fp="short", auth="dedicated_local"), sqlite3.IntegrityError)
    assert rejected(anchor.format(gen=2, free=500, fp=fp, auth="shared"), sqlite3.IntegrityError)

    # Anchor -> dirty-generation FK is exercised independently (Greptile #3): a structurally valid
    # anchor referencing a nonexistent generation must be rejected by the foreign key, not a CHECK.
    assert rejected(anchor.format(gen=999, free=500, fp=fp, auth="dedicated_local"), sqlite3.IntegrityError)

    # drives.write_authority is constrained to the two accepted values.
    assert rejected("UPDATE drives SET write_authority='bogus' WHERE drive_label='drive-00'",
                    sqlite3.IntegrityError)
    con.close()


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
                fn(Path(tempfile.mkdtemp(prefix="mark-v3-")))
            else:
                fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001 — Gate-1 wants the full red/green map
            failed.append(name)
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
