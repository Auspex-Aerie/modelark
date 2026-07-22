"""Installed-wheel smoke for the chained schema v1 -> v2 (capacity mode) -> v3 (#35-A capacity
evidence) migration. Downgrades a freshly bootstrapped catalog to a genuine pre-v2/pre-v3 shape so the
capacity-mode rebuild and the additive, backup-first evidence migration both actually run."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from modelark.core import db

# Frozen v2 `drives` shape (no catalog-v3 columns), so the v2->v3 migration performs a real
# ADD COLUMN + evidence-table creation rather than short-circuiting on an already-v3 table.
_V2_DRIVE_COLS = ("drive_label,fs_uuid,annex_uuid,capacity_bytes,free_bytes,hw_model,serial,"
                  "physical_location,role,raid_backed,health,last_seen,notes")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="modelark-wheel-v3-"))
    db.configure(root, root / "state")
    con = db.connect(_bootstrapping=True)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute("DROP INDEX IF EXISTS idx_plans_one_active")
    con.execute(
        "CREATE TABLE plans__v1 ("
        "plan_id VARCHAR PRIMARY KEY NOT NULL,name VARCHAR,annex_root VARCHAR,"
        "provisioning VARCHAR NOT NULL DEFAULT 'uncompressed' "
        "CHECK (provisioning IN ('uncompressed','compressed')),"
        "status VARCHAR NOT NULL DEFAULT 'active',is_active BOOLEAN NOT NULL DEFAULT false,"
        "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,notes VARCHAR)"
    )
    con.execute(
        "INSERT INTO plans__v1(plan_id,name,provisioning,is_active) "
        "VALUES('safe','Safe','uncompressed',1),('aware','Aware','compressed',0)"
    )
    con.execute("DROP TABLE plans")
    con.execute("ALTER TABLE plans__v1 RENAME TO plans")
    con.execute("CREATE UNIQUE INDEX idx_plans_one_active ON plans(is_active) WHERE is_active=1")
    # A drive carrying a legacy free_bytes scalar, to prove preservation + no fabricated evidence.
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes) VALUES('drive-00',1000,500)")
    # Downgrade drives to a genuine v2 shape (frozen DDL keeps the PRIMARY KEY so the replicas/archived
    # FKs still resolve) and drop the v3 evidence objects, so the v2->v3 migration genuinely runs.
    con.execute("PRAGMA legacy_alter_table=ON")      # rename must not rewrite FK targets in other tables
    con.execute("DROP TABLE IF EXISTS drive_clean_anchors")
    con.execute("DROP TABLE IF EXISTS drive_dirty_generations")
    con.execute(
        "CREATE TABLE drives__v2 ("
        "drive_label VARCHAR PRIMARY KEY NOT NULL CHECK (length(trim(drive_label)) > 0),"
        "fs_uuid VARCHAR,annex_uuid VARCHAR,"
        "capacity_bytes BIGINT CHECK (capacity_bytes IS NULL OR capacity_bytes >= 0),"
        "free_bytes BIGINT CHECK (free_bytes IS NULL OR free_bytes >= 0),"
        "hw_model VARCHAR,serial VARCHAR,physical_location VARCHAR,"
        "role VARCHAR NOT NULL DEFAULT 'primary' CHECK (role IN ('primary','replica')),"
        "raid_backed BOOLEAN NOT NULL DEFAULT false CHECK (raid_backed IN (0,1)),"
        "health VARCHAR,last_seen TIMESTAMP,notes VARCHAR)"
    )
    con.execute(f"INSERT INTO drives__v2({_V2_DRIVE_COLS}) SELECT {_V2_DRIVE_COLS} FROM drives")
    con.execute("DROP TABLE drives")
    con.execute("ALTER TABLE drives__v2 RENAME TO drives")
    con.execute("PRAGMA user_version=1")
    con.close()

    migrated = db.connect()
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
        assert migrated.execute(
            "SELECT plan_id,capacity_mode FROM plans ORDER BY plan_id"
        ).fetchall() == [("aware", "compression_aware"), ("safe", "guaranteed")]
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        plan_cols = {row[1] for row in migrated.execute("PRAGMA table_info(plans)").fetchall()}
        assert "capacity_mode" in plan_cols and "provisioning" not in plan_cols
        # v3 columns present with migration defaults; legacy scalar preserved; no fabricated evidence.
        row = migrated.execute(
            "SELECT identity_epoch,write_generation,write_authority,filesystem_capacity_bytes,"
            "identity_fingerprint,free_bytes FROM drives WHERE drive_label='drive-00'").fetchone()
        assert row == (1, 0, "unknown", None, None, 500), row
        tables = {r[0] for r in migrated.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"drive_dirty_generations", "drive_clean_anchors"} <= tables
        assert migrated.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
        assert migrated.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
    finally:
        migrated.close()

    # Both migration backups exist and each retains its prior schema version.
    v2_backup = db.DB_PATH.with_name(f"{db.DB_PATH.name}.pre-capacity-v2.bak")
    assert v2_backup.is_file()
    v3_backup = db.DB_PATH.with_name(f"{db.DB_PATH.name}.pre-evidence-v3.bak")
    assert v3_backup.is_file()
    legacy = sqlite3.connect(str(v2_backup))
    try:
        assert legacy.execute("PRAGMA user_version").fetchone()[0] == 1
        assert legacy.execute(
            "SELECT provisioning FROM plans ORDER BY plan_id"
        ).fetchall() == [("compressed",), ("uncompressed",)]
    finally:
        legacy.close()
    pre_v3 = sqlite3.connect(str(v3_backup))
    try:
        assert pre_v3.execute("PRAGMA user_version").fetchone()[0] == 2
        drive_cols = {r[1] for r in pre_v3.execute("PRAGMA table_info(drives)").fetchall()}
        assert "identity_epoch" not in drive_cols, "the pre-v3 backup must remain a v2 catalog"
    finally:
        pre_v3.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
