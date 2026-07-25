"""PR-08 / #39-A catalog-v5 proposal/control migration (tests-first, DEC-049 / RFC-002).

Gate 1: v4→v5 migration BEFORE production. RED until five planning/control tables, singleton
planner_state (revision=0, next_fencing_token=0, null active pointer), backup-first transactional
migration, full execution_sessions/proposal domain constraints, and no early v5 table creation.
"""
from __future__ import annotations

import sqlite3

from modelark.core import db


class _FailOn:
    def __init__(self, con, marker):
        self._con = con
        self._marker = marker

    def execute(self, sql, *args):
        if self._marker in sql:
            raise sqlite3.OperationalError(f"injected failure at: {self._marker}")
        return self._con.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._con, name)


_V4_DRIVES_DDL = """
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
                              CHECK (write_authority IN ('unknown','dedicated_local')),
    lifecycle                 VARCHAR NOT NULL DEFAULT 'active'
                              CHECK (lifecycle IN ('active','lost','retired')),
    eligibility               VARCHAR NOT NULL DEFAULT 'enabled'
                              CHECK (eligibility IN ('enabled','excluded'))
)
"""

_V5_TABLES = (
    "planner_state",
    "placement_proposals",
    "proposal_tasks",
    "proposal_files",
    "execution_sessions",
)

# RFC-002 execution_sessions column surface (schema-tested now; no runtime writers).
_SESSION_REQUIRED_COLS = {
    "session_id", "plan_id", "approved_proposal_id", "resumed_from_session_id",
    "controller_identity", "worker_identity", "state",
    "bound_planner_revision", "fencing_token",
    "acquired_at", "heartbeat_at", "expires_at", "terminal_at",
    "terminal_code", "terminal_evidence",
}

_LIVE_STATES = ("starting", "running", "stopping")
_TERMINAL_STATES = ("paused", "blocked", "stopped", "done", "failed")
_ALL_STATES = _LIVE_STATES + _TERMINAL_STATES


def _reopen_raw():
    return sqlite3.connect(str(db.DB_PATH), isolation_level=None)


def _seed_v4(tmp_path):
    """Genuine frozen v4 catalog at user_version=4 — never depends on packaged schema version."""
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    con = sqlite3.connect(str(db.DB_PATH), isolation_level=None)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(_V4_DRIVES_DDL)
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
    con.execute("""
        CREATE TABLE drive_dirty_generations (
            drive_label         VARCHAR NOT NULL,
            identity_epoch      INTEGER NOT NULL CHECK (identity_epoch >= 1),
            generation          INTEGER NOT NULL CHECK (generation >= 1),
            operation_code      VARCHAR NOT NULL,
            owner_session_id    VARCHAR,
            owner_fencing_token INTEGER,
            started_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (drive_label, identity_epoch, generation),
            FOREIGN KEY (drive_label) REFERENCES drives(drive_label),
            CHECK ((owner_session_id IS NULL) = (owner_fencing_token IS NULL))
        )
    """)
    con.execute("""
        CREATE TABLE drive_clean_anchors (
            anchor_id                  INTEGER PRIMARY KEY,
            drive_label               VARCHAR NOT NULL,
            identity_epoch            INTEGER NOT NULL,
            generation                INTEGER NOT NULL,
            anchor_free_bytes         BIGINT NOT NULL,
            filesystem_capacity_bytes BIGINT NOT NULL,
            identity_fingerprint      VARCHAR NOT NULL,
            write_authority           VARCHAR NOT NULL,
            identity_proof            TEXT NOT NULL,
            fence_proof               TEXT NOT NULL,
            observed_at               TIMESTAMP NOT NULL,
            created_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (drive_label, identity_epoch, generation)
        )
    """)
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16')")
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,fs_uuid,annex_uuid,serial,"
        "role,raid_backed,identity_epoch,write_generation,write_authority,lifecycle,eligibility) "
        "VALUES('drive-00',1000,500,'fs-00','annex-00','serial-00',"
        "'primary',0,1,0,'unknown','active','enabled')")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,stored_bytes) "
        "VALUES('org/m','model.safetensors','drive-00',0,100,100)")
    con.execute(
        "INSERT INTO plans(plan_id,name,is_active,capacity_mode) "
        "VALUES('ark','Ark',1,'guaranteed')")
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','drive-00')")
    con.execute("PRAGMA user_version=4")
    con.close()

    check = _reopen_raw()
    assert check.execute("PRAGMA user_version").fetchone()[0] == 4
    tables = {r[0] for r in check.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert not any(t in tables for t in _V5_TABLES)
    check.close()


def _hash64(ch="a"):
    """64-char hex string bound as a parameter (never bare '0'*64 in SQL — SQLite yields int 0)."""
    return ch * 64


def test_migrate_v4_to_v5_adds_five_tables_and_seeds_planner_state(tmp_path):
    _seed_v4(tmp_path)
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 5, (
        "v4→v5 proposal/control migration not implemented (expected Gate-1 red)")

    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for name in _V5_TABLES:
        assert name in tables, f"missing v5 table {name}"

    row = con.execute(
        "SELECT singleton_id, planner_revision, active_approved_proposal_id, next_fencing_token "
        "FROM planner_state WHERE singleton_id=1"
    ).fetchone()
    assert row is not None, "planner_state singleton must be seeded"
    assert row[0] == 1
    assert row[1] == 0, f"planner_revision must be 0 after migration; got {row[1]}"
    assert row[2] is None, "active_approved_proposal_id must be NULL (no fabricated approval)"
    assert row[3] == 0, f"next_fencing_token must initialize at 0; got {row[3]}"

    assert con.execute("SELECT count(*) FROM placement_proposals").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM proposal_tasks").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM proposal_files").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM execution_sessions").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM models").fetchone()[0] == 1
    assert con.execute(
        "SELECT lifecycle, eligibility FROM drives WHERE drive_label='drive-00'"
    ).fetchone() == ("active", "enabled")
    con.close()


def test_v5_backup_precedes_every_v5_object(tmp_path):
    _seed_v4(tmp_path)
    db.connect().close()
    bak = db.DB_PATH.with_name(db.DB_PATH.name + ".pre-proposal-v5.bak")
    assert bak.is_file(), (
        "non-overwriting v4 backup must exist before any v5 object is created "
        "(expected Gate-1 red until backup-first migration)")
    b = sqlite3.connect(str(bak))
    assert b.execute("PRAGMA user_version").fetchone()[0] == 4
    btables = {r[0] for r in b.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for name in _V5_TABLES:
        assert name not in btables, f"backup must not contain v5 table {name}"
    b.close()


def test_v5_migration_is_idempotent(tmp_path):
    _seed_v4(tmp_path)
    db.connect().close()
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 5, (
        "v5 migration not implemented (expected Gate-1 red)")
    row = con.execute(
        "SELECT planner_revision, active_approved_proposal_id, next_fencing_token "
        "FROM planner_state WHERE singleton_id=1"
    ).fetchone()
    assert row == (0, None, 0), row
    assert con.execute("SELECT count(*) FROM planner_state").fetchone()[0] == 1
    con.close()


def test_v5_injected_failure_rolls_back_tables_and_user_version(tmp_path):
    migrate = getattr(db, "_migrate_proposal_control_v5", None) or getattr(
        db, "_migrate_placement_approval_v5", None)
    assert migrate is not None, (
        "v5 migration helper not implemented yet (expected Gate-1 red)")
    _seed_v4(tmp_path)
    con = _reopen_raw()
    con.execute("PRAGMA foreign_keys=OFF")
    proxy = _FailOn(con, "proposal_tasks")
    try:
        migrate(proxy, backup_existing=True)
        raise AssertionError("migration should have raised")
    except RuntimeError:
        pass
    con.close()
    raw = _reopen_raw()
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 4
    tables = {r[0] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for name in _V5_TABLES:
        assert name not in tables, f"rolled-back migration must not leave {name}"
    raw.close()
    assert db.DB_PATH.with_name(db.DB_PATH.name + ".pre-proposal-v5.bak").is_file()


def test_v5_tables_not_introduced_during_integrity_rebuild(tmp_path):
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
    for name in _V5_TABLES:
        con.execute(f'DROP TABLE IF EXISTS "{name}"')
    con.execute("DROP TABLE IF EXISTS drive_clean_anchors")
    con.execute("DROP TABLE IF EXISTS drive_dirty_generations")
    present = {r[1] for r in con.execute("PRAGMA table_info(drives)").fetchall()}
    pre_v3 = ("drive_label,fs_uuid,annex_uuid,capacity_bytes,free_bytes,hw_model,serial,"
              "physical_location,role,raid_backed,health,last_seen,notes")
    pre_cols = ",".join(c for c in pre_v3.split(",") if c in present)
    con.execute(f"CREATE TABLE drives__pre AS SELECT {pre_cols} FROM drives")
    con.execute("DROP TABLE drives")
    con.execute("ALTER TABLE drives__pre RENAME TO drives")
    con.execute("INSERT INTO drives(drive_label,free_bytes,role,raid_backed) "
                "VALUES('drive-00',500,'primary',0)")
    con.execute("PRAGMA user_version=0")
    con.close()

    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 5, (
        "full migration must land at v5 (expected Gate-1 red)")
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for name in _V5_TABLES:
        assert name in tables
    v3_bak = db.DB_PATH.with_name(db.DB_PATH.name + ".pre-evidence-v3.bak")
    assert v3_bak.is_file()
    b = sqlite3.connect(str(v3_bak))
    btables = {r[0] for r in b.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for name in _V5_TABLES:
        assert name not in btables, f"integrity/evidence must not introduce {name} early"
    b.close()
    con.close()


def test_newer_catalog_rejection_uses_schema_version_plus_one(tmp_path):
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


def _seed_minimal_approved_proposal(con, proposal_id="prop-1"):
    """Insert one approved proposal for session FK tests. Uses bound parameters for hash."""
    h = _hash64("c")
    con.execute(
        "INSERT INTO placement_proposals("
        "proposal_id, plan_id, based_on_revision, lifecycle, canonical_hash, "
        "mutation_kind, serializer_version) "
        "VALUES(?,?,0,'approved',?,'adopt_current','1')",
        [proposal_id, "ark", h],
    )


def test_proposal_lifecycle_and_task_row_kind_constraints(tmp_path):
    _seed_v4(tmp_path)
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 5, (
        "v5 required (expected Gate-1 red)")

    def rejected(sql, params=()):
        try:
            con.execute(sql, params)
            return False
        except sqlite3.IntegrityError:
            return True

    h = _hash64("d")
    assert rejected(
        "INSERT INTO placement_proposals(proposal_id,plan_id,based_on_revision,lifecycle,"
        "canonical_hash,mutation_kind,serializer_version) "
        "VALUES('p-bad','ark',0,'ghost',?,'adopt_current','1')", [h]), (
        "proposal lifecycle domain must reject invalid lifecycle")
    con.execute(
        "INSERT INTO placement_proposals(proposal_id,plan_id,based_on_revision,lifecycle,"
        "canonical_hash,mutation_kind,serializer_version) "
        "VALUES('p-ok','ark',0,'draft',?,'adopt_current','1')", [h])
    assert rejected(
        "INSERT INTO proposal_tasks(proposal_id,requirement_id,row_kind,repo_id,"
        "full_manifest_hash) VALUES('p-ok','primary:org/m','ghost','org/m',?)",
        [_hash64("m")]), (
        "proposal_tasks.row_kind must be executable|baseline_satisfied only")
    con.execute(
        "INSERT INTO proposal_tasks(proposal_id,requirement_id,row_kind,repo_id,"
        "full_manifest_hash) VALUES('p-ok','primary:org/m','executable','org/m',?)",
        [_hash64("m")])
    con.execute(
        "INSERT INTO proposal_tasks(proposal_id,requirement_id,row_kind,repo_id,"
        "full_manifest_hash) VALUES('p-ok','primary:org/n','baseline_satisfied','org/n',?)",
        [_hash64("n")])
    con.close()


def test_execution_sessions_schema_constraints_with_synthetic_rows(tmp_path):
    """Complete schema pin: columns, FKs, state domain, worker-claim, live uniqueness."""
    _seed_v4(tmp_path)
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 5, (
        "v5 schema required for execution_sessions (expected Gate-1 red)")
    con.execute("PRAGMA foreign_keys=ON")

    cols = {r[1] for r in con.execute("PRAGMA table_info(execution_sessions)").fetchall()}
    assert _SESSION_REQUIRED_COLS <= cols, (
        f"execution_sessions missing columns; have={sorted(cols)} "
        f"need={sorted(_SESSION_REQUIRED_COLS)}")

    _seed_minimal_approved_proposal(con, "prop-1")

    def rejected(sql, params=()):
        try:
            con.execute(sql, params)
            return False
        except sqlite3.IntegrityError:
            return True

    # Invalid state domain.
    assert rejected(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('s-bad','ark','prop-1','ctl',NULL,'ghost',0,1)"), (
        "invalid session state must be rejected by CHECK")

    # plan_id FK
    assert rejected(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('s-pf','no-such-plan','prop-1','ctl',NULL,'starting',0,1)"), (
        "plan_id must FK to plans")

    # approved_proposal_id FK
    assert rejected(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('s-af','ark','no-prop','ctl',NULL,'starting',0,1)"), (
        "approved_proposal_id must FK to placement_proposals")

    # Worker claim: running/stopping require non-null worker_identity.
    assert rejected(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('s-rw','ark','prop-1','ctl',NULL,'running',0,1)"), (
        "running must require worker_identity NOT NULL")
    assert rejected(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('s-sw','ark','prop-1','ctl',NULL,'stopping',0,1)"), (
        "stopping must require worker_identity NOT NULL")

    # starting may have NULL worker (unclaimed).
    con.execute(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('s1','ark','prop-1','ctl',NULL,'starting',0,1)")

    # Global live uniqueness: second live state fails.
    assert rejected(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('s2','ark','prop-1','ctl','worker-1','running',0,2)"), (
        "global partial unique on live states must reject a second live session")

    # Terminal allowed while a live session exists? RFC: at most one live; terminals OK.
    # After s1 is still starting (live), terminal s3 may be allowed (historical).
    con.execute(
        "UPDATE execution_sessions SET state='stopped', worker_identity=NULL "
        "WHERE session_id='s1'")
    con.execute(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('s3','ark','prop-1','ctl','worker-1','running',0,3)")

    # resumed_from_session_id self-FK
    assert rejected(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "resumed_from_session_id,controller_identity,worker_identity,state,"
        "bound_planner_revision,fencing_token) "
        "VALUES('s4','ark','prop-1','no-such','ctl',NULL,'starting',0,4)"), (
        "resumed_from_session_id must self-FK execution_sessions")
    con.execute(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "resumed_from_session_id,controller_identity,worker_identity,state,"
        "bound_planner_revision,fencing_token) "
        "VALUES('s5','ark','prop-1','s3','ctl',NULL,'starting',0,5)")

    # fencing_token / revision domain
    assert rejected(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('s6','ark','prop-1','ctl',NULL,'starting',0,0)"), (
        "fencing_token must be >= 1 when present as allocated token")
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
                fn(Path(tempfile.mkdtemp(prefix="mark-v5-")))
            else:
                fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, type(exc).__name__, str(exc)[:220]))
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    print("Gate-1 tests-only: v5 migration contracts EXPECTED RED until PR-08 production.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
