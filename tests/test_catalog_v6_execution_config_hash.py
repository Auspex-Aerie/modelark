"""PR-09 / B7 catalog-v6 execution_config_hash migration (null-or-64 CHECK)."""
from __future__ import annotations

import sqlite3

from modelark.core import db


def _seed_v5(tmp_path):
    """Minimal v5 catalog without execution_config_hash column."""
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    db.STATE_DIR = tmp_path / "state"
    # Fresh bootstrap (absent file), then downgrade shape for the test.
    # Do not pre-create an empty catalog.sqlite — ordinary connect refuses
    # existing user_version=0 files under DEC-059 clone-first.
    assert not db.DB_PATH.exists()
    con = db.connect()
    con.close()
    raw = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    raw.execute("PRAGMA foreign_keys=OFF")
    # Force weak unconstrained column if present, or drop to pre-v6.
    sql = raw.execute(
        "SELECT sql FROM sqlite_master WHERE name='placement_proposals'").fetchone()[0]
    if "execution_config_hash" in sql and "length(execution_config_hash)" in sql.lower():
        # Rebuild without CHECK to simulate weak prior migration, then stamp v5.
        cols = [r[1] for r in raw.execute("PRAGMA table_info(placement_proposals)")]
        raw.execute("ALTER TABLE placement_proposals RENAME TO pp_tmp")
        # Create unconstrained
        raw.execute(
            """
            CREATE TABLE placement_proposals (
                proposal_id VARCHAR PRIMARY KEY NOT NULL,
                plan_id VARCHAR NOT NULL,
                based_on_revision INTEGER NOT NULL,
                lifecycle VARCHAR NOT NULL DEFAULT 'draft',
                canonical_hash VARCHAR NOT NULL,
                mutation_kind VARCHAR NOT NULL,
                mutation_args_json TEXT NOT NULL DEFAULT '[]',
                serializer_version VARCHAR NOT NULL,
                requirement_set_hash VARCHAR,
                semantic_input_hash VARCHAR,
                selection_before_hash VARCHAR,
                selection_after_hash VARCHAR,
                capacity_mode VARCHAR NOT NULL DEFAULT 'guaranteed',
                policy_version VARCHAR NOT NULL DEFAULT '1',
                solver_version VARCHAR NOT NULL DEFAULT '1',
                gate_b_code VARCHAR NOT NULL DEFAULT 'FEASIBLE',
                derivation_mode VARCHAR,
                execution_config_hash VARCHAR,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                approved_at TIMESTAMP,
                superseded_at TIMESTAMP
            )
            """
        )
        common = [c for c in cols if c != "execution_config_hash"]
        raw.execute(
            f"INSERT INTO placement_proposals({','.join(common)},execution_config_hash) "
            f"SELECT {','.join(common)}, NULL FROM pp_tmp")
        raw.execute("DROP TABLE pp_tmp")
    raw.execute("PRAGMA user_version=5")
    raw.close()


def test_v6_migration_rejects_short_hash_like_fresh_schema(tmp_path):
    _seed_v5(tmp_path)
    con = db.migrate_existing_catalog()
    assert con.execute("PRAGMA user_version").fetchone()[0] == db._SCHEMA_VERSION
    assert db._placement_proposals_has_execution_config_hash_check(con)
    con.execute("INSERT OR IGNORE INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    # Short hash must fail CHECK
    try:
        con.execute(
            "INSERT INTO placement_proposals("
            "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
            "mutation_kind,mutation_args_json,serializer_version,execution_config_hash) "
            "VALUES('p-short','ark',0,'draft',?,?,?,?,?)",
            ["a" * 64, "adopt_current", "[]", "1", "short"])
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "migrated v6 must reject short execution_config_hash"
    # NULL is allowed
    con.execute(
        "INSERT INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
        "mutation_kind,mutation_args_json,serializer_version,execution_config_hash) "
        "VALUES('p-null','ark',0,'draft',?,?,?,?,NULL)",
        ["b" * 64, "adopt_current", "[]", "1"])
    # Valid 64-char allowed
    con.execute(
        "INSERT INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
        "mutation_kind,mutation_args_json,serializer_version,execution_config_hash) "
        "VALUES('p-ok','ark',0,'draft',?,?,?,?,?)",
        ["c" * 64, "adopt_current", "[]", "1", "d" * 64])
    con.close()


def test_v6_backup_created_on_upgrade(tmp_path):
    _seed_v5(tmp_path)
    # Ensure we are at v5 weak shape
    raw = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    raw.execute("PRAGMA user_version=5")
    raw.close()
    db.migrate_existing_catalog().close()
    bak = db.DB_PATH.with_name(db.DB_PATH.name + ".pre-execution-config-v6.bak")
    assert bak.is_file(), "backup-first v6 migration must create pre-execution-config-v6.bak"
