"""Installed-wheel smoke for DEC-059 clone-first schema migration.

Builds a genuine pre-v2 catalog (user_version=1, ``provisioning`` plans, v2-shaped
drives without capacity-evidence columns), then exercises the installed package
through rehearse → publish rather than obsolete in-place auto-migration.

Assertions:
  • source remains v1 and byte-immutable through rehearsal and publication
  • rehearsed clone reaches current schema with capacity/evidence defaults
  • published destination opens cleanly and preserves the same migrated facts
"""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path

from modelark.core import db

# Frozen v2 ``drives`` shape (no catalog-v3 columns), so the chained migration
# performs real ADD COLUMN + evidence-table creation rather than short-circuiting.
_V2_DRIVE_COLS = (
    "drive_label,fs_uuid,annex_uuid,capacity_bytes,free_bytes,hw_model,serial,"
    "physical_location,role,raid_backed,health,last_seen,notes"
)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_genuine_v1_source(root: Path) -> Path:
    """Bootstrap current schema, then downgrade to a real v1 catalog on disk."""
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
    # A drive carrying a legacy free_bytes scalar (preserved; no fabricated evidence).
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes) "
        "VALUES('drive-00',1000,500)"
    )
    con.execute("PRAGMA legacy_alter_table=ON")
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
    con.execute(
        f"INSERT INTO drives__v2({_V2_DRIVE_COLS}) SELECT {_V2_DRIVE_COLS} FROM drives"
    )
    con.execute("DROP TABLE drives")
    con.execute("ALTER TABLE drives__v2 RENAME TO drives")
    con.execute("PRAGMA user_version=1")
    con.close()
    assert db.DB_PATH.is_file()
    return db.DB_PATH


def _assert_migrated_capacity_evidence(con: sqlite3.Connection, *, label: str) -> None:
    """Shared capacity-mode + evidence assertions (clone and published dest)."""
    ver = con.execute("PRAGMA user_version").fetchone()[0]
    assert ver == db._SCHEMA_VERSION, f"{label}: user_version={ver}"
    assert con.execute(
        "SELECT plan_id,capacity_mode FROM plans ORDER BY plan_id"
    ).fetchall() == [("aware", "compression_aware"), ("safe", "guaranteed")], label
    assert con.execute("PRAGMA foreign_key_check").fetchall() == [], label
    plan_cols = {row[1] for row in con.execute("PRAGMA table_info(plans)").fetchall()}
    assert "capacity_mode" in plan_cols and "provisioning" not in plan_cols, label
    row = con.execute(
        "SELECT identity_epoch,write_generation,write_authority,filesystem_capacity_bytes,"
        "identity_fingerprint,free_bytes FROM drives WHERE drive_label='drive-00'"
    ).fetchone()
    assert row == (1, 0, "unknown", None, None, 500), f"{label}: drive row {row}"
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"drive_dirty_generations", "drive_clean_anchors"} <= tables, label
    assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
    # Provenance column present at current schema (DEC-053).
    arch_cols = {r[1] for r in con.execute("PRAGMA table_info(archived)").fetchall()}
    assert "orig_sha256_provenance" in arch_cols, label


def _assert_source_still_v1(path: Path, *, before_sha: str, before_identity: str) -> None:
    assert path.is_file()
    assert _sha256_file(path) == before_sha, "source catalog bytes must not change"
    src = sqlite3.connect(str(path))
    try:
        assert src.execute("PRAGMA user_version").fetchone()[0] == 1
        assert src.execute(
            "SELECT provisioning FROM plans ORDER BY plan_id"
        ).fetchall() == [("compressed",), ("uncompressed",)]
        plan_cols = {r[1] for r in src.execute("PRAGMA table_info(plans)").fetchall()}
        assert "provisioning" in plan_cols and "capacity_mode" not in plan_cols
        drive_cols = {r[1] for r in src.execute("PRAGMA table_info(drives)").fetchall()}
        assert "identity_epoch" not in drive_cols
        # Logical identity (when available) must match the pre-rehearsal snapshot.
        metrics = db._catalog_snapshot_metrics(path)
        assert metrics["user_version"] == 1
        assert metrics["content_identity"] == before_identity
    finally:
        src.close()


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="modelark-wheel-dec059-"))
    source_path = _build_genuine_v1_source(root)
    source_sha = _sha256_file(source_path)
    source_metrics = db._catalog_snapshot_metrics(source_path)
    assert source_metrics["user_version"] == 1
    assert source_metrics["integrity"] == "ok"
    source_identity = source_metrics["content_identity"]

    work = root / "work"
    work.mkdir()
    report = db.rehearse_provenance_migration(root, work, run_id="wheel-smoke")
    assert report.get("status") == "ok", report
    assert report.get("source_user_version") == 1, report
    assert report.get("clone_user_version") == db._SCHEMA_VERSION, report
    assert report.get("source_content_identity") == source_identity, report
    assert report.get("source_integrity") == "ok"
    assert report.get("clone_integrity") == "ok"
    assert report.get("snapshot_path") and Path(report["snapshot_path"]).is_file()
    assert report.get("snapshot_sha256")
    assert report.get("clone_catalog_path") and Path(report["clone_catalog_path"]).is_file()
    assert report.get("manifest_path") and Path(report["manifest_path"]).is_file()

    # Source immutability after rehearsal.
    _assert_source_still_v1(source_path, before_sha=source_sha, before_identity=source_identity)

    # Rehearsed clone carries capacity/evidence migration outcomes.
    clone_path = Path(report["clone_catalog_path"])
    clone = sqlite3.connect(str(clone_path))
    try:
        _assert_migrated_capacity_evidence(clone, label="clone")
    finally:
        clone.close()

    # Publish to a separate empty destination (DEC-059 cutover seam).
    dest = root / "dest"
    pub = db.publish_provenance_migration(
        work,
        dest,
        confirm_stopped="MODELARK-STOPPED",
        writers_stopped=True,
    )
    if isinstance(pub, dict):
        assert pub.get("status") in ("ok", "published", None) or "dest" in str(pub).lower()
    dest_cat = dest / "catalog.sqlite"
    assert dest_cat.is_file(), f"published catalog missing: {dest_cat} (report={pub!r})"

    # Source still v1 and unchanged after publication.
    _assert_source_still_v1(source_path, before_sha=source_sha, before_identity=source_identity)

    # Open the published destination via normal configure/connect (current schema).
    db.configure(dest, dest / "state")
    published = db.connect()
    try:
        _assert_migrated_capacity_evidence(published, label="published")
        assert published.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
        arch_cols = {
            r[1] for r in published.execute("PRAGMA table_info(archived)").fetchall()
        }
        assert "orig_sha256_provenance" in arch_cols
        tables = {
            r[0] for r in published.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "drive_hash_repair_state" in tables
    finally:
        published.close()

    # DEC-059 deliberately does not require in-place .pre-capacity-v2.bak /
    # .pre-evidence-v3.bak sidecars; source immutability + clone/publication above
    # replace those obsolete connect()-auto-migration expectations.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
