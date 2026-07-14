"""Installed-wheel smoke for the schema-v1 to schema-v2 capacity-mode migration."""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from modelark.core import db


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="modelark-wheel-v2-"))
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
    con.execute("PRAGMA user_version=1")
    con.close()

    migrated = db.connect()
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 2
        assert migrated.execute(
            "SELECT plan_id,capacity_mode FROM plans ORDER BY plan_id"
        ).fetchall() == [("aware", "compression_aware"), ("safe", "guaranteed")]
        assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(plans)").fetchall()}
        assert "capacity_mode" in columns and "provisioning" not in columns
    finally:
        migrated.close()

    backup = db.DB_PATH.with_name(f"{db.DB_PATH.name}.pre-capacity-v2.bak")
    assert backup.is_file()
    legacy = sqlite3.connect(str(backup))
    try:
        assert legacy.execute("PRAGMA user_version").fetchone()[0] == 1
        assert legacy.execute(
            "SELECT provisioning FROM plans ORDER BY plan_id"
        ).fetchall() == [("compressed",), ("uncompressed",)]
    finally:
        legacy.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
