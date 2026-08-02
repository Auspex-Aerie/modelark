"""Catalog connection, schema bootstrap, and upsert helpers (SQLite, WAL mode).

WAL journaling gives cross-process concurrency: many readers + one writer, no exclusive lock — so a
CLI, a diagnostic, or an audit can read the catalog WHILE the portal is filling (DEC-024). This
replaces DuckDB, whose single-writer lock blocked every concurrent access (the recurring "stop the
portal to inspect" friction). The connect/upsert/replace_files API is unchanged: sqlite3 cursors
support `con.execute(sql, params).fetchone()/.fetchall()`, `?` placeholders, and
`INSERT … ON CONFLICT(pk) DO UPDATE SET col=excluded.col` (SQLite ≥3.24), exactly like before.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable

PKG_ROOT = Path(__file__).resolve().parent           # modelark/core
REPO_ROOT = PKG_ROOT.parent.parent                   # source root for legacy/editable-install detection only


def _xdg_data_home() -> Path:
    """Platform-appropriate writable application-data root, without a third-party dependency."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


def _xdg_state_home() -> Path:
    if sys.platform == "win32":
        return _xdg_data_home()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


CATALOG_DIR = _xdg_data_home() / "modelark"
DB_PATH = CATALOG_DIR / "catalog.sqlite"
STATE_DIR = _xdg_state_home() / "modelark"
SCHEMA_PATH = PKG_ROOT / "schema.sql"


def configure(data_dir: str | Path | None = None, state_dir: str | Path | None = None) -> None:
    """Override writable runtime locations before opening the catalog.

    The CLI exposes this as ``--data-dir``/``--state-dir``; tests use it to guarantee isolation.
    Package resources remain read-only and are resolved separately through importlib.resources.
    """
    global CATALOG_DIR, DB_PATH, STATE_DIR
    if data_dir is not None:
        CATALOG_DIR = Path(data_dir).expanduser().resolve()
        DB_PATH = CATALOG_DIR / "catalog.sqlite"
    if state_dir is not None:
        STATE_DIR = Path(state_dir).expanduser().resolve()
    elif data_dir is not None:
        STATE_DIR = CATALOG_DIR / "state"

# Store Python datetimes as ISO text (Python 3.12 deprecated the implicit datetime adapter).
sqlite3.register_adapter(datetime, lambda d: d.isoformat(sep=" ", timespec="seconds"))


def _statements(sql: str) -> Iterable[str]:
    """Yield executable statements, stripping `--` line comments first so a ';' inside a comment is
    not mistaken for a statement boundary. A `CREATE TRIGGER … BEGIN … END;` body contains its own
    `;` separators, so re-join split fragments while inside a BEGIN…END block (tracked by keyword
    depth) and emit the whole compound statement as one."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    buffer: list[str] = []
    depth = 0
    for chunk in no_comments.split(";"):
        buffer.append(chunk)
        depth += len(re.findall(r"\bBEGIN\b", chunk, re.IGNORECASE))
        depth -= len(re.findall(r"\bEND\b", chunk, re.IGNORECASE))
        if depth <= 0:
            statement = ";".join(buffer)         # restore the internal separators of a trigger body
            if statement.strip():
                yield statement
            buffer = []
            depth = 0
    tail = ";".join(buffer)                       # a final statement with no trailing ';'
    if tail.strip():
        yield tail


# Catalog-v3 (#35-A) append-only evidence tables. The v2->v3 migration creates these transactionally
# after its backup, so the pre-migration tables-only pass must NOT create them first on a v2 catalog.
_V3_EVIDENCE_TABLES = ("drive_dirty_generations", "drive_clean_anchors")
# The v3 drives columns. The v0->v1 integrity rebuild excludes them so a legacy catalog keeps its
# pre-v3 drive shape until the actual v3 transaction takes its own backup.
_V3_DRIVE_COLUMN_NAMES = ("identity_epoch", "write_generation", "filesystem_capacity_bytes",
                          "identity_fingerprint", "write_authority")
# v4 lifecycle/eligibility must not be pulled backward into the v0 integrity rebuild either.
_V4_DRIVE_COLUMN_NAMES = ("lifecycle", "eligibility")


def _apply_schema(con: sqlite3.Connection, tables_only: bool = False) -> None:
    """Apply the packaged schema. The first startup pass creates only tables so legacy data can be
    rebuilt before unique indexes and views are installed; the final pass installs everything."""
    for stmt in _statements(SCHEMA_PATH.read_text()):
        if tables_only and not stmt.lstrip().upper().startswith("CREATE TABLE"):
            continue
        # Never create a v3 evidence table before the v2->v3 migration takes its backup: the migration
        # owns them transactionally, and the final (non-tables-only) pass creates them idempotently.
        if tables_only and any(t in stmt for t in _V3_EVIDENCE_TABLES):
            continue
        # Same rule for v5 proposal-control tables (backup-first v4→v5 migration owns them).
        if tables_only and any(t in stmt for t in _V5_PROPOSAL_TABLES):
            continue
        # v7 repair-state: owned by provenance migration on existing catalogs.
        if tables_only and any(t in stmt for t in _V7_PROVENANCE_TABLES):
            continue
        con.execute(stmt)


def connect(read_only: bool = False, _bootstrapping: bool = False) -> sqlite3.Connection:
    """Open the catalog in WAL mode, applying the schema on first (writable) use. `isolation_level=None`
    → autocommit per statement (matches DuckDB); `check_same_thread=False` because the portal shares
    one connection across its threads under `data._lock`. WAL means readers never block on the writer.
    `_bootstrapping=True` is for the DuckDB→SQLite migrator only — it creates the new catalog.sqlite,
    so it must skip the not-yet-migrated guard below."""
    legacy_sqlite = REPO_ROOT / "catalog" / "catalog.sqlite"
    if not _bootstrapping and DB_PATH != legacy_sqlite and not DB_PATH.exists() and legacy_sqlite.exists():
        raise RuntimeError(
            f"Legacy repo-local catalog found at {legacy_sqlite}. ModelArk will not move or replace it "
            f"automatically. Re-run with --data-dir {legacy_sqlite.parent} (or copy it deliberately "
            f"to {CATALOG_DIR}) after stopping every ModelArk process."
        )
    if read_only:
        # Enforce the diagnostic/portal read contract at SQLite's open boundary.  ``query_only``
        # rejects SQL writes after a normal connection has already opened the file, while URI
        # ``mode=ro`` also prevents bootstrap, journal-mode changes, and accidental file creation.
        # ``as_uri`` percent-encodes spaces and other path characters for SQLite's URI parser.
        uri = f"{DB_PATH.expanduser().resolve().as_uri()}?mode=ro"
        con = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
        con.execute("PRAGMA busy_timeout=15000")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA query_only=ON")
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            _validate_catalog_version(version, read_only=True)
        except Exception:
            con.close()
            raise
        return con

    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    legacy = CATALOG_DIR / "catalog.duckdb"          # guard: never silently start on an EMPTY sqlite when the
    if not _bootstrapping and not DB_PATH.exists() and legacy.exists():   # DuckDB catalog is still the source of truth
        raise RuntimeError(
            f"Catalog not migrated yet: {legacy.name} exists but {DB_PATH.name} does not. Run\n"
            f"  .venv/bin/python -m scripts.migrate_duckdb_to_sqlite {legacy} {DB_PATH}\n"
            f"first (DEC-024), then start the portal.")
    existed = DB_PATH.exists()
    con = sqlite3.connect(str(DB_PATH), isolation_level=None, check_same_thread=False)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        _validate_catalog_version(version)
        # DEC-059: never auto-migrate an existing v6 (or later-pre-target) canonical catalog
        # in place. Fresh catalogs (version 0 / not previously present) receive the packaged
        # target schema. Clone-first rehearsal/publication owns v6→v7 provenance cutover.
        if (
            existed
            and version >= _EXECUTION_CONFIG_HASH_SCHEMA_VERSION
            and version < _SCHEMA_VERSION
        ):
            raise RuntimeError(
                f"Catalog schema v{version} requires clone-first provenance migration to "
                f"v{_SCHEMA_VERSION} (rehearse_provenance_migration / "
                f"publish_provenance_migration); db.connect() will not auto-migrate or mutate it"
            )
        con.execute("PRAGMA journal_mode=WAL")       # persistent once set; concurrent reader/writer
        con.execute("PRAGMA busy_timeout=15000")     # a concurrent WRITER briefly holds the lock → wait, don't error
        con.execute("PRAGMA synchronous=NORMAL")     # WAL-safe durability without an fsync per commit
        # A legacy table rebuild must run with FK enforcement off; validation still happens through
        # PRAGMA foreign_key_check before its transaction commits. Every normal write below runs ON.
        con.execute("PRAGMA foreign_keys=OFF")
        _apply_schema(con, tables_only=True)
        _migrate(con, version, backup_existing=existed)
        _apply_schema(con)
        con.execute("PRAGMA foreign_keys=ON")
    except Exception:
        con.close()
        raise
    return con


# Idempotent column additions for catalogs created before a column existed
# (CREATE TABLE IF NOT EXISTS won't alter an existing table). DEC-014.
_MIGRATIONS = (
    "ALTER TABLE drives ADD COLUMN role VARCHAR DEFAULT 'primary'",
    "ALTER TABLE drives ADD COLUMN raid_backed BOOLEAN DEFAULT false",
    "ALTER TABLE models ADD COLUMN numcopies INTEGER DEFAULT 1",
    "ALTER TABLE archived ADD COLUMN stored_relpath VARCHAR",
)

_INTEGRITY_TABLES = (
    "models", "files", "drives", "replicas", "verifications", "selection",
    "archived", "fetch_events", "plans", "plan_drives",
)
_VIEW_NAMES = ("v_ui", "v_model_summary", "v_storage_by_drive")
_INTEGRITY_SCHEMA_VERSION = 1
_CAPACITY_MODE_SCHEMA_VERSION = 2
_CAPACITY_EVIDENCE_SCHEMA_VERSION = 3
_LIFECYCLE_ELIGIBILITY_SCHEMA_VERSION = 4  # v4: drives.lifecycle + drives.eligibility (#37)
_PROPOSAL_CONTROL_SCHEMA_VERSION = 5  # v5: planner_state + proposals + execution_sessions (#39-A)
_EXECUTION_CONFIG_HASH_SCHEMA_VERSION = 6  # v6: placement_proposals.execution_config_hash (PR-09 / B7)
_PROVENANCE_SCHEMA_VERSION = 7  # v7: DEC-053 provenance + DEF-034 derivation CHECK + DEC-054 repair state
_SCHEMA_VERSION = 7

# v5 proposal-control tables: never created during the pre-migration tables-only pass so
# backup-first v4→v5 owns them transactionally (same class of bug as early v3 evidence tables).
_V5_PROPOSAL_TABLES = (
    "planner_state",
    "placement_proposals",
    "proposal_tasks",
    "proposal_files",
    "execution_sessions",
)
# v7 repair-state table: owned by the provenance migration, not tables-only pre-pass.
_V7_PROVENANCE_TABLES = ("drive_hash_repair_state",)

PROVENANCE_VALUES = frozenset({
    "hub_confirmed", "ingestion_computed", "annex_key",
    "archive-head-blob", "legacy_unknown",
})
DERIVATION_VALUES = frozenset({"optimized", "state_truncated", "canonical_fallback"})
REPAIR_STATUS_VALUES = frozenset({
    "pending", "running", "blocked_absent", "needs_refetch", "halted", "complete",
})


def _validate_catalog_version(version: int, *, read_only: bool = False) -> None:
    if version > _SCHEMA_VERSION:
        raise RuntimeError(
            f"Catalog schema v{version} is newer than this ModelArk build (v{_SCHEMA_VERSION}); "
            "upgrade ModelArk before opening it."
        )
    if read_only and version < _SCHEMA_VERSION:
        raise RuntimeError(
            f"Catalog schema v{version} requires a writable migration to v{_SCHEMA_VERSION}; "
            "open it once with the current ModelArk CLI or service before read-only diagnostics."
        )


def _drop_columns_from_ddl(ddl: str, exclude: tuple[str, ...]) -> str:
    """Remove the named column definitions from a CREATE TABLE statement, splitting the column list on
    depth-0 commas so CHECK(...) parens and quoted commas are respected."""
    open_i = ddl.index("(")
    close_i = ddl.rindex(")")
    body = ddl[open_i + 1:close_i]
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    kept = [p for p in parts if p.strip().split()[0].strip('"') not in exclude]
    return ddl[:open_i + 1] + ",".join(kept) + ddl[close_i:]


def _canonical_table_sql(table: str, replacement: str, exclude: tuple[str, ...] = ()) -> str:
    """Return one canonical CREATE TABLE statement under a temporary table name, optionally excluding
    named columns (used so the integrity rebuild does not pull future-version columns backward)."""
    prefix = re.compile(
        rf"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\b",
        re.IGNORECASE,
    )
    for stmt in _statements(SCHEMA_PATH.read_text()):
        clean = stmt.strip()
        if prefix.match(clean):
            ddl = prefix.sub(f"CREATE TABLE {replacement}", clean, count=1)
            return _drop_columns_from_ddl(ddl, exclude) if exclude else ddl
    raise RuntimeError(f"Packaged schema has no CREATE TABLE statement for {table}")


def _backup_before_migration(con: sqlite3.Connection, label: str) -> Path:
    """Create one consistent, non-overwriting recovery copy before the destructive table swap."""
    backup_path = DB_PATH.with_name(f"{DB_PATH.name}.{label}.bak")
    if backup_path.exists():
        return backup_path
    backup = sqlite3.connect(str(backup_path), isolation_level=None)
    try:
        con.backup(backup)                              # includes committed WAL state consistently
    finally:
        backup.close()
    return backup_path


def _rebuild_integrity_tables(con: sqlite3.Connection) -> None:
    """Upgrade a pre-constraint catalog without dropping or repairing user data silently.

    SQLite cannot add CHECK or FOREIGN KEY clauses with ALTER TABLE. Build every canonical table
    beside the legacy set, copy rows (so CHECK/NOT NULL constraints run), swap them transactionally,
    then run SQLite's cross-table checker before commit. Any invalid legacy row rolls everything back
    and leaves a diagnostic instead of a partly-upgraded catalog.
    """
    if con.execute("PRAGMA foreign_key_list(archived)").fetchall():
        con.execute(f"PRAGMA user_version={_INTEGRITY_SCHEMA_VERSION}")
        return
    if con.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Integrity migration requires foreign_keys=OFF before its transaction")

    current = "catalog"
    con.execute("BEGIN IMMEDIATE")
    try:
        for view in _VIEW_NAMES:
            con.execute(f'DROP VIEW IF EXISTS "{view}"')
        for table in _INTEGRITY_TABLES:
            current = table
            new = f"{table}__integrity_new"
            con.execute(f'DROP TABLE IF EXISTS "{new}"')
            # Preserve the pre-v3 drive shape: a v0/v1 rebuild must not introduce the catalog-v3
            # columns, so the later v2->v3 transaction takes a genuine pre-v3 backup.
            exclude = (
                (_V3_DRIVE_COLUMN_NAMES + _V4_DRIVE_COLUMN_NAMES) if table == "drives" else ()
            )
            con.execute(_canonical_table_sql(table, new, exclude=exclude))
            old_cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
            new_cols = [r[1] for r in con.execute(f'PRAGMA table_info("{new}")').fetchall()]
            cols = [c for c in new_cols if c in old_cols]
            expressions = [f'"{c}"' for c in cols]
            if table == "plans" and "capacity_mode" in new_cols and "provisioning" in old_cols:
                cols.append("capacity_mode")
                expressions.append(
                    "CASE provisioning WHEN 'uncompressed' THEN 'guaranteed' "
                    "WHEN 'compressed' THEN 'compression_aware' ELSE provisioning END"
                )
            quoted = ", ".join(f'"{c}"' for c in cols)
            selected = ", ".join(expressions)
            con.execute(f'INSERT INTO "{new}" ({quoted}) SELECT {selected} FROM "{table}"')

        # Drop children before parents for clarity even though enforcement is deliberately off on
        # this connection. DDL is transactional in SQLite, including the table renames below.
        for table in reversed(_INTEGRITY_TABLES):
            con.execute(f'DROP TABLE "{table}"')
        for table in _INTEGRITY_TABLES:
            con.execute(f'ALTER TABLE "{table}__integrity_new" RENAME TO "{table}"')

        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            detail = "; ".join(
                f"{table} rowid={rowid} references missing {parent} (fk#{fk_id})"
                for table, rowid, parent, fk_id in violations[:12]
            )
            more = f"; plus {len(violations) - 12} more" if len(violations) > 12 else ""
            raise RuntimeError(f"Legacy catalog contains orphaned rows: {detail}{more}")
        # Enforce the cross-row invariant during the same transaction so duplicate active plans also
        # roll the rebuild back instead of leaving a half-upgraded database.
        con.execute(
            "CREATE UNIQUE INDEX idx_plans_one_active ON plans(is_active) WHERE is_active = 1"
        )
        con.execute(f"PRAGMA user_version={_INTEGRITY_SCHEMA_VERSION}")
        con.execute("COMMIT")
    except Exception as exc:
        con.execute("ROLLBACK")
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            f"Cannot add catalog integrity constraints: legacy table {current!r} contains "
            f"invalid data ({exc}). Correct or export that row before retrying."
        ) from exc


def _migrate_capacity_mode_v2(con: sqlite3.Connection, *, backup_existing: bool) -> None:
    """Rename plans.provisioning and map its values without changing admission semantics."""
    if con.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Capacity-mode migration requires foreign_keys=OFF")
    if backup_existing:
        _backup_before_migration(con, "pre-capacity-v2")

    con.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in con.execute('PRAGMA table_info("plans")').fetchall()}
        if "capacity_mode" not in columns and "provisioning" not in columns:
            raise RuntimeError("Cannot migrate plans: neither capacity_mode nor provisioning exists")
        if "capacity_mode" not in columns:
            con.execute('DROP TABLE IF EXISTS "plans__capacity_v2"')
            con.execute(_canonical_table_sql("plans", "plans__capacity_v2"))
            con.execute(
                'INSERT INTO "plans__capacity_v2" '
                '(plan_id,name,annex_root,capacity_mode,status,is_active,created_at,notes) '
                "SELECT plan_id,name,annex_root,CASE provisioning "
                "WHEN 'uncompressed' THEN 'guaranteed' "
                "WHEN 'compressed' THEN 'compression_aware' ELSE provisioning END,"
                "status,is_active,created_at,notes FROM plans"
            )
            con.execute('DROP TABLE "plans"')
            con.execute('ALTER TABLE "plans__capacity_v2" RENAME TO "plans"')

        invalid = con.execute(
            "SELECT plan_id,capacity_mode FROM plans "
            "WHERE capacity_mode NOT IN ('guaranteed','compression_aware')"
        ).fetchall()
        if invalid:
            raise RuntimeError(f"Invalid legacy plan capacity values: {invalid[:12]}")
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Capacity-mode migration produced foreign-key violations: {violations[:12]}")
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_one_active "
            "ON plans(is_active) WHERE is_active = 1"
        )
        con.execute(f"PRAGMA user_version={_CAPACITY_MODE_SCHEMA_VERSION}")
        con.execute("COMMIT")
    except Exception as exc:
        con.execute("ROLLBACK")
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Cannot migrate plans to schema v2 capacity modes ({exc})") from exc


# Catalog-v3 (#35-A) drives-column additions. Introspected ALTER TABLE ADD COLUMN (no drives rebuild);
# each column is defaulted/nullable so existing rows migrate without fabricated evidence.
_V3_DRIVE_COLUMNS = (
    ("identity_epoch",
     "ALTER TABLE drives ADD COLUMN identity_epoch INTEGER NOT NULL DEFAULT 1 "
     "CHECK (identity_epoch >= 1)"),
    ("write_generation",
     "ALTER TABLE drives ADD COLUMN write_generation INTEGER NOT NULL DEFAULT 0 "
     "CHECK (write_generation >= 0)"),
    ("filesystem_capacity_bytes",
     "ALTER TABLE drives ADD COLUMN filesystem_capacity_bytes BIGINT "
     "CHECK (filesystem_capacity_bytes IS NULL OR filesystem_capacity_bytes >= 0)"),
    ("identity_fingerprint",
     "ALTER TABLE drives ADD COLUMN identity_fingerprint VARCHAR "
     "CHECK (identity_fingerprint IS NULL OR length(identity_fingerprint) = 64)"),
    ("write_authority",
     "ALTER TABLE drives ADD COLUMN write_authority VARCHAR NOT NULL DEFAULT 'unknown' "
     "CHECK (write_authority IN ('unknown','dedicated_local'))"),
)


def _v3_object_ddl() -> list[str]:
    """The catalog-v3 evidence tables, indexes, and triggers taken verbatim (single-sourced) from the
    packaged schema, so the migration and a fresh bootstrap create identical objects in FK order."""
    wanted = []
    for stmt in _statements(SCHEMA_PATH.read_text()):
        head = stmt.strip().upper()
        if (head.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE TRIGGER"))
                and any(table in stmt for table in _V3_EVIDENCE_TABLES)):
            wanted.append(stmt)
    return wanted


def _migrate_capacity_evidence_v3(con, *, backup_existing: bool) -> None:
    """Backup-first, transactional, additive v2->v3 migration: add the capacity-evidence columns, the
    two append-only evidence tables, their indexes, and triggers. Create no evidence rows and leave
    every drive unknown (epoch 1 is only a namespace for migrated rows). No v3 object is created
    before the backup, and all v3 DDL plus user_version commit in one transaction, so an injected
    failure leaves a pristine v2 catalog."""
    columns = {row[1] for row in con.execute('PRAGMA table_info("drives")').fetchall()}
    if "identity_epoch" in columns:
        # Already v3-shaped drives; stamp evidence version only (not latest schema).
        con.execute(f"PRAGMA user_version={_CAPACITY_EVIDENCE_SCHEMA_VERSION}")
        return
    if con.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Capacity-evidence migration requires foreign_keys=OFF")
    if backup_existing:
        _backup_before_migration(con, "pre-evidence-v3")        # strictly before any v3 object
    con.execute("BEGIN IMMEDIATE")
    try:
        for name, ddl in _V3_DRIVE_COLUMNS:
            if name not in columns:                             # introspected add
                con.execute(ddl)
        for stmt in _v3_object_ddl():
            con.execute(stmt)
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"Capacity-evidence migration produced foreign-key violations: {violations[:12]}")
        con.execute(f"PRAGMA user_version={_CAPACITY_EVIDENCE_SCHEMA_VERSION}")
        con.execute("COMMIT")
    except Exception as exc:
        con.execute("ROLLBACK")
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Cannot migrate catalog to v3 capacity evidence ({exc})") from exc


# Catalog-v4 (#37) orthogonal lifecycle × eligibility on drives.
_V4_DRIVE_COLUMNS = (
    ("lifecycle",
     "ALTER TABLE drives ADD COLUMN lifecycle VARCHAR NOT NULL DEFAULT 'active' "
     "CHECK (lifecycle IN ('active','lost','retired'))"),
    ("eligibility",
     "ALTER TABLE drives ADD COLUMN eligibility VARCHAR NOT NULL DEFAULT 'enabled' "
     "CHECK (eligibility IN ('enabled','excluded'))"),
)


def _migrate_lifecycle_eligibility_v4(con, *, backup_existing: bool) -> None:
    """Backup-first, transactional, additive v3→v4: add lifecycle + eligibility with safe defaults.

    Existing rows become exactly active+enabled via NOT NULL DEFAULT. No tombstone tables or
    fabricated evidence. All DDL + user_version commit in one transaction.
    """
    columns = {row[1] for row in con.execute('PRAGMA table_info("drives")').fetchall()}
    if "lifecycle" in columns and "eligibility" in columns:
        con.execute(f"PRAGMA user_version={_LIFECYCLE_ELIGIBILITY_SCHEMA_VERSION}")
        return
    if con.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Lifecycle/eligibility migration requires foreign_keys=OFF")
    if backup_existing:
        _backup_before_migration(con, "pre-lifecycle-v4")
    con.execute("BEGIN IMMEDIATE")
    try:
        for name, ddl in _V4_DRIVE_COLUMNS:
            if name not in columns:
                con.execute(ddl)
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"Lifecycle/eligibility migration produced foreign-key violations: {violations[:12]}")
        con.execute(f"PRAGMA user_version={_LIFECYCLE_ELIGIBILITY_SCHEMA_VERSION}")
        con.execute("COMMIT")
    except Exception as exc:
        con.execute("ROLLBACK")
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Cannot migrate catalog to v4 lifecycle/eligibility ({exc})") from exc


def _v5_object_ddl() -> list[str]:
    """v5 proposal-control tables and indexes taken from the packaged schema (single-sourced)."""
    wanted = []
    for stmt in _statements(SCHEMA_PATH.read_text()):
        head = stmt.strip().upper()
        if not head.startswith(("CREATE TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX")):
            continue
        if any(table in stmt for table in _V5_PROPOSAL_TABLES):
            wanted.append(stmt)
    return wanted


def _migrate_proposal_control_v5(con, *, backup_existing: bool) -> None:
    """Backup-first, transactional v4→v5: five planning/control tables + planner_state seed.

    No fabricated proposals or approvals. planner_revision=0, next_fencing_token=0,
    active_approved_proposal_id=NULL. Backup label is pre-proposal-v5 (Gate-1 contract).
    """
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "planner_state" in tables and "placement_proposals" in tables:
        # Already shaped; ensure singleton seed and stamp v5.
        if con.execute("SELECT count(*) FROM planner_state WHERE singleton_id=1").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO planner_state(singleton_id,planner_revision,"
                "active_approved_proposal_id,next_fencing_token) VALUES(1,0,NULL,0)")
        con.execute(f"PRAGMA user_version={_PROPOSAL_CONTROL_SCHEMA_VERSION}")
        return
    if con.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Proposal-control migration requires foreign_keys=OFF")
    if backup_existing:
        _backup_before_migration(con, "pre-proposal-v5")  # strictly before any v5 object
    con.execute("BEGIN IMMEDIATE")
    try:
        for stmt in _v5_object_ddl():
            con.execute(stmt)
        # Seed singleton: revision 0, null active pointer, fencing token 0 (A1 / A2).
        if con.execute("SELECT count(*) FROM planner_state WHERE singleton_id=1").fetchone()[0] == 0:
            con.execute(
                "INSERT INTO planner_state(singleton_id,planner_revision,"
                "active_approved_proposal_id,next_fencing_token) VALUES(1,0,NULL,0)")
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"Proposal-control migration produced foreign-key violations: {violations[:12]}")
        con.execute(f"PRAGMA user_version={_PROPOSAL_CONTROL_SCHEMA_VERSION}")
        con.execute("COMMIT")
    except Exception as exc:
        con.execute("ROLLBACK")
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"Cannot migrate catalog to v5 proposal control ({exc})") from exc


# Public alias for Gate-1 injected-failure contracts.
_migrate_placement_approval_v5 = _migrate_proposal_control_v5


def _placement_proposals_has_execution_config_hash_check(con) -> bool:
    """True when CREATE TABLE SQL enforces null-or-64 execution_config_hash (fresh v6 shape)."""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='placement_proposals'"
    ).fetchone()
    if not row or not row[0]:
        return False
    sql = " ".join(str(row[0]).lower().split())
    return (
        "execution_config_hash" in sql
        and "length(execution_config_hash)" in sql
    )


_PLACEMENT_PROPOSALS_V6_DDL = """
CREATE TABLE placement_proposals (
    proposal_id            VARCHAR PRIMARY KEY NOT NULL CHECK (length(trim(proposal_id)) > 0),
    plan_id                VARCHAR NOT NULL,
    based_on_revision      INTEGER NOT NULL CHECK (based_on_revision >= 0),
    lifecycle              VARCHAR NOT NULL DEFAULT 'draft'
                           CHECK (lifecycle IN ('draft','approved','superseded')),
    canonical_hash         VARCHAR NOT NULL CHECK (length(canonical_hash) = 64),
    mutation_kind          VARCHAR NOT NULL,
    mutation_args_json     TEXT NOT NULL DEFAULT '[]',
    serializer_version     VARCHAR NOT NULL,
    requirement_set_hash   VARCHAR CHECK (requirement_set_hash IS NULL
                                          OR length(requirement_set_hash) = 64),
    semantic_input_hash    VARCHAR CHECK (semantic_input_hash IS NULL
                                          OR length(semantic_input_hash) = 64),
    selection_before_hash  VARCHAR CHECK (selection_before_hash IS NULL
                                          OR length(selection_before_hash) = 64),
    selection_after_hash   VARCHAR CHECK (selection_after_hash IS NULL
                                          OR length(selection_after_hash) = 64),
    capacity_mode          VARCHAR NOT NULL DEFAULT 'guaranteed'
                           CHECK (capacity_mode IN ('guaranteed','compression_aware')),
    policy_version         VARCHAR NOT NULL DEFAULT '1',
    solver_version         VARCHAR NOT NULL DEFAULT '1',
    gate_b_code            VARCHAR NOT NULL DEFAULT 'FEASIBLE',
    derivation_mode        VARCHAR,
    execution_config_hash  VARCHAR CHECK (execution_config_hash IS NULL
                                          OR length(execution_config_hash) = 64),
    created_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at            TIMESTAMP,
    superseded_at          TIMESTAMP,
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
)
"""

# v7 rebuild: same shape as packaged schema (derivation_mode CHECK + execution_config_hash).
_PLACEMENT_PROPOSALS_V7_DDL = """
CREATE TABLE placement_proposals (
    proposal_id            VARCHAR PRIMARY KEY NOT NULL CHECK (length(trim(proposal_id)) > 0),
    plan_id                VARCHAR NOT NULL,
    based_on_revision      INTEGER NOT NULL CHECK (based_on_revision >= 0),
    lifecycle              VARCHAR NOT NULL DEFAULT 'draft'
                           CHECK (lifecycle IN ('draft','approved','superseded')),
    canonical_hash         VARCHAR NOT NULL CHECK (length(canonical_hash) = 64),
    mutation_kind          VARCHAR NOT NULL,
    mutation_args_json     TEXT NOT NULL DEFAULT '[]',
    serializer_version     VARCHAR NOT NULL,
    requirement_set_hash   VARCHAR CHECK (requirement_set_hash IS NULL
                                          OR length(requirement_set_hash) = 64),
    semantic_input_hash    VARCHAR CHECK (semantic_input_hash IS NULL
                                          OR length(semantic_input_hash) = 64),
    selection_before_hash  VARCHAR CHECK (selection_before_hash IS NULL
                                          OR length(selection_before_hash) = 64),
    selection_after_hash   VARCHAR CHECK (selection_after_hash IS NULL
                                          OR length(selection_after_hash) = 64),
    capacity_mode          VARCHAR NOT NULL DEFAULT 'guaranteed'
                           CHECK (capacity_mode IN ('guaranteed','compression_aware')),
    policy_version         VARCHAR NOT NULL DEFAULT '1',
    solver_version         VARCHAR NOT NULL DEFAULT '1',
    gate_b_code            VARCHAR NOT NULL DEFAULT 'FEASIBLE',
    derivation_mode        VARCHAR CHECK (
        derivation_mode IS NULL OR derivation_mode IN (
            'optimized', 'state_truncated', 'canonical_fallback'
        )
    ),
    execution_config_hash  VARCHAR CHECK (execution_config_hash IS NULL
                                          OR length(execution_config_hash) = 64),
    created_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    approved_at            TIMESTAMP,
    superseded_at          TIMESTAMP,
    FOREIGN KEY (plan_id) REFERENCES plans(plan_id)
        ON UPDATE CASCADE ON DELETE RESTRICT
)
"""

_DRIVE_HASH_REPAIR_STATE_DDL = """
CREATE TABLE IF NOT EXISTS drive_hash_repair_state (
    drive_label            VARCHAR NOT NULL,
    identity_epoch         INTEGER NOT NULL CHECK (identity_epoch >= 1),
    identity_fingerprint   VARCHAR CHECK (
        identity_fingerprint IS NULL OR length(identity_fingerprint) = 64
    ),
    status                 VARCHAR NOT NULL CHECK (status IN (
        'pending', 'running', 'blocked_absent', 'needs_refetch', 'halted', 'complete'
    )),
    updated_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    detail                 VARCHAR,
    PRIMARY KEY (drive_label, identity_epoch),
    FOREIGN KEY (drive_label) REFERENCES drives(drive_label)
        ON UPDATE CASCADE ON DELETE RESTRICT
)
"""

_ARCHIVED_PROVENANCE_ADD = (
    "ALTER TABLE archived ADD COLUMN orig_sha256_provenance VARCHAR CHECK ("
    "orig_sha256_provenance IS NULL OR orig_sha256_provenance IN ("
    "'hub_confirmed', 'ingestion_computed', 'annex_key', "
    "'archive-head-blob', 'legacy_unknown'))"
)


def _is_execution_config_hash_check_error(exc: BaseException) -> bool:
    """True when an IntegrityError is the execution_config_hash null-or-64 CHECK.

    PK/FK/unique failures must not be treated as proof the CHECK works (PR-10 hygiene).
    """
    msg = str(exc).lower()
    if "execution_config_hash" in msg:
        return True
    # SQLite often reports only "CHECK constraint failed" for named column checks.
    return "check constraint failed" in msg or (
        "check" in msg and "constraint" in msg
    )


def _migrate_execution_config_hash_v6(con, *, backup_existing: bool) -> None:
    """Backup-first v5→v6: placement_proposals.execution_config_hash with null-or-64 CHECK.

    Rebuilds the table so a migrated catalog matches a fresh v6 schema (finding 35).
    Existing approved/draft rows keep NULL (or valid 64-char) hashes; short values become NULL.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(placement_proposals)").fetchall()}
    if "execution_config_hash" in cols and _placement_proposals_has_execution_config_hash_check(con):
        con.execute(f"PRAGMA user_version={_EXECUTION_CONFIG_HASH_SCHEMA_VERSION}")
        return
    if con.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("execution_config_hash migration requires foreign_keys=OFF")
    if backup_existing:
        _backup_before_migration(con, "pre-execution-config-v6")
    con.execute("BEGIN IMMEDIATE")
    try:
        old_cols = [r[1] for r in con.execute(
            "PRAGMA table_info(placement_proposals)").fetchall()]
        rows = con.execute(
            f"SELECT {', '.join(old_cols)} FROM placement_proposals").fetchall()
        con.execute("ALTER TABLE placement_proposals RENAME TO placement_proposals__pre_v6")
        con.execute(_PLACEMENT_PROPOSALS_V6_DDL)
        new_cols = [r[1] for r in con.execute(
            "PRAGMA table_info(placement_proposals)").fetchall()]
        for row in rows:
            data = dict(zip(old_cols, row))
            h = data.get("execution_config_hash")
            if h is not None and len(str(h)) != 64:
                data["execution_config_hash"] = None
            if "execution_config_hash" not in data:
                data["execution_config_hash"] = None
            cols_ins = [c for c in new_cols if c in data]
            con.execute(
                f"INSERT INTO placement_proposals({','.join(cols_ins)}) "
                f"VALUES({','.join('?' for _ in cols_ins)})",
                [data[c] for c in cols_ins],
            )
        con.execute("DROP TABLE placement_proposals__pre_v6")
        if not _placement_proposals_has_execution_config_hash_check(con):
            raise RuntimeError(
                "v6 migration did not produce execution_config_hash null-or-64 CHECK")
        # Probe: short hash must be rejected by the CHECK (must fail specifically).
        import sqlite3 as _sqlite3
        try:
            con.execute(
                "INSERT INTO placement_proposals("
                "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
                "mutation_kind,mutation_args_json,serializer_version,"
                "execution_config_hash) "
                "VALUES('__v6_probe__','ark',0,'draft',?,?,?,?,?)",
                ["a" * 64, "probe", "[]", "1", "short"])
        except _sqlite3.IntegrityError as ie:
            if not _is_execution_config_hash_check_error(ie):
                raise RuntimeError(
                    "v6 short-hash probe rejected for a non-CHECK reason "
                    f"({ie}); cannot prove execution_config_hash CHECK"
                ) from ie
        else:
            con.execute("DELETE FROM placement_proposals WHERE proposal_id='__v6_probe__'")
            raise RuntimeError("v6 CHECK accepted short execution_config_hash")
        # Positive control: the probe must fail even when plan_id is valid.
        try:
            con.execute("INSERT OR IGNORE INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
        except Exception:
            pass
        try:
            con.execute(
                "INSERT INTO placement_proposals("
                "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
                "mutation_kind,mutation_args_json,serializer_version,"
                "execution_config_hash) "
                "VALUES('__v6_probe2__','ark',0,'draft',?,?,?,?,?)",
                ["b" * 64, "probe", "[]", "1", "short"])
        except _sqlite3.IntegrityError as ie:
            if not _is_execution_config_hash_check_error(ie):
                raise RuntimeError(
                    "v6 short-hash probe2 rejected for a non-CHECK reason "
                    f"({ie}); cannot prove execution_config_hash CHECK"
                ) from ie
        else:
            con.execute("DELETE FROM placement_proposals WHERE proposal_id='__v6_probe2__'")
            raise RuntimeError("v6 CHECK accepted short execution_config_hash (probe2)")
        con.execute(f"PRAGMA user_version={_EXECUTION_CONFIG_HASH_SCHEMA_VERSION}")
        con.execute("COMMIT")
    except Exception as exc:
        con.execute("ROLLBACK")
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            f"Cannot migrate catalog to v6 execution_config_hash ({exc})") from exc


def _migrate(con, version: int, *, backup_existing: bool) -> None:
    if version < _INTEGRITY_SCHEMA_VERSION:
        if backup_existing:
            _backup_before_migration(con, "pre-integrity-v1")
        _migrate_legacy_columns(con)
        _rebuild_integrity_tables(con)
        version = _INTEGRITY_SCHEMA_VERSION
    if version < _CAPACITY_MODE_SCHEMA_VERSION:
        _migrate_capacity_mode_v2(con, backup_existing=backup_existing)
        version = _CAPACITY_MODE_SCHEMA_VERSION
    if version < _CAPACITY_EVIDENCE_SCHEMA_VERSION:
        _migrate_capacity_evidence_v3(con, backup_existing=backup_existing)
        version = _CAPACITY_EVIDENCE_SCHEMA_VERSION
    if version < _LIFECYCLE_ELIGIBILITY_SCHEMA_VERSION:
        _migrate_lifecycle_eligibility_v4(con, backup_existing=backup_existing)
        version = _LIFECYCLE_ELIGIBILITY_SCHEMA_VERSION
    if version < _PROPOSAL_CONTROL_SCHEMA_VERSION:
        _migrate_proposal_control_v5(con, backup_existing=backup_existing)
        version = _PROPOSAL_CONTROL_SCHEMA_VERSION
    if version < _EXECUTION_CONFIG_HASH_SCHEMA_VERSION:
        _migrate_execution_config_hash_v6(con, backup_existing=backup_existing)
        version = _EXECUTION_CONFIG_HASH_SCHEMA_VERSION
    elif version == _EXECUTION_CONFIG_HASH_SCHEMA_VERSION:
        # Repair catalogs stamped v6 by an earlier unconstrained ADD COLUMN.
        if not _placement_proposals_has_execution_config_hash_check(con):
            _migrate_execution_config_hash_v6(con, backup_existing=backup_existing)
            version = _EXECUTION_CONFIG_HASH_SCHEMA_VERSION
    if version < _PROVENANCE_SCHEMA_VERSION:
        # In-place v6→v7 only for bootstrap ladders (version advanced from <6 above)
        # or explicit clone migration helpers. connect() refuses bare existing v6.
        _migrate_provenance_v7(con, backup_existing=backup_existing)
        version = _PROVENANCE_SCHEMA_VERSION
    if version != _SCHEMA_VERSION:
        raise RuntimeError(f"Catalog migration stopped at v{version}, expected v{_SCHEMA_VERSION}")


def _migrate_legacy_columns(con) -> None:
    for stmt in _MIGRATIONS:
        try:
            con.execute(stmt)                        # a duplicate-column ADD raises; ignore (already migrated)
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise
    # Old rows recorded only a basename. Hugging Face preserves rfilename's parent directories on
    # disk, so parent(rfilename)/stored_name recovers the actual relative path without touching bytes.
    for repo_id, rfilename, stored_name, drive_label in con.execute(
            "SELECT repo_id,rfilename,stored_name,drive_label FROM archived "
            "WHERE stored_relpath IS NULL AND stored_name IS NOT NULL").fetchall():
        rel = PurePosixPath(rfilename).parent / stored_name
        if rel.is_absolute() or ".." in rel.parts:
            raise RuntimeError(
                f"Unsafe legacy archive path for {repo_id}/{rfilename} on {drive_label}: {rel}")
        con.execute("UPDATE archived SET stored_relpath=? "
                    "WHERE repo_id=? AND rfilename=? AND drive_label=?",
                    [rel.as_posix(), repo_id, rfilename, drive_label])
    # Before DEC-039, discovery-time Tier A header checks mislabeled models as
    # `verified`. No physical verifier writes this model status, so every such legacy
    # row is safely and idempotently narrowed to the evidence it actually holds.
    con.execute("UPDATE models SET status='inspected' WHERE status='verified'")


# ---------------------------------------------------------------------------
# DEC-053 / DEC-054 / DEF-034 / DEC-059 provenance migration (v7)
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _logical_identity(con: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    tables = sorted(
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )
    for table in tables:
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
        order = ", ".join(f'"{c}"' for c in cols)
        digest.update(table.encode())
        digest.update(b"|")
        digest.update(",".join(cols).encode())
        digest.update(b"\n")
        for row in con.execute(f'SELECT {order} FROM "{table}" ORDER BY {order}'):
            digest.update(repr(tuple(row)).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def _integrity_ok(con: sqlite3.Connection) -> str:
    row = con.execute("PRAGMA integrity_check").fetchone()
    return "ok" if row and row[0] == "ok" else str(row)


def _fk_violations(con: sqlite3.Connection) -> list:
    return list(con.execute("PRAGMA foreign_key_check").fetchall())


def _archived_has_provenance_column(con: sqlite3.Connection) -> bool:
    return "orig_sha256_provenance" in {
        r[1] for r in con.execute("PRAGMA table_info(archived)")
    }


def _placement_has_derivation_check(con: sqlite3.Connection) -> bool:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='placement_proposals'"
    ).fetchone()
    if not row or not row[0]:
        return False
    sql = row[0].lower()
    return (
        "derivation_mode" in sql
        and "optimized" in sql
        and "state_truncated" in sql
        and "canonical_fallback" in sql
    )


def _apply_provenance_schema_objects(con: sqlite3.Connection) -> None:
    """Additive archived provenance + rebuilt placement_proposals + repair state."""
    if not _archived_has_provenance_column(con):
        con.execute(_ARCHIVED_PROVENANCE_ADD)
    if not _placement_has_derivation_check(con):
        # Rebuild so CHECK is part of CREATE TABLE (SQLite cannot ALTER a CHECK onto
        # an existing unconstrained column). Requires foreign_keys=OFF.
        # Create-new → copy → drop-old → rename-new so child FKs that name
        # ``placement_proposals`` keep resolving (unlike RENAME-parent-first,
        # which retargets children at the temporary name under SQLite ≥3.26).
        old_cols = [r[1] for r in con.execute(
            "PRAGMA table_info(placement_proposals)").fetchall()]
        if not old_cols:
            con.execute(_PLACEMENT_PROPOSALS_V7_DDL)
        else:
            rows = con.execute(
                f"SELECT {', '.join(old_cols)} FROM placement_proposals"
            ).fetchall()
            con.execute(
                _PLACEMENT_PROPOSALS_V7_DDL.replace(
                    "CREATE TABLE placement_proposals",
                    "CREATE TABLE placement_proposals__v7_new",
                    1,
                )
            )
            new_cols = [r[1] for r in con.execute(
                "PRAGMA table_info(placement_proposals__v7_new)").fetchall()]
            for row in rows:
                data = dict(zip(old_cols, row))
                cols_ins = [c for c in new_cols if c in data]
                con.execute(
                    f"INSERT INTO placement_proposals__v7_new({','.join(cols_ins)}) "
                    f"VALUES({','.join('?' for _ in cols_ins)})",
                    [data[c] for c in cols_ins],
                )
            con.execute("DROP TABLE placement_proposals")
            con.execute(
                "ALTER TABLE placement_proposals__v7_new RENAME TO placement_proposals"
            )
            # Recreate secondary indexes that lived on the dropped table
            # (CREATE TABLE rebuild does not preserve them).
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_placement_proposals_plan "
                "ON placement_proposals(plan_id, lifecycle)"
            )
    con.execute(_DRIVE_HASH_REPAIR_STATE_DDL)


def _apply_provenance_backfill(con: sqlite3.Connection) -> dict[str, int]:
    """Classify and set orig_sha256_provenance. Refuses digests that disagree with Hub."""
    counts = {
        "hub_confirmed": 0,
        "legacy_unknown": 0,
        "null_digest": 0,
        "disagreement": 0,
    }
    arch_cols = {r[1] for r in con.execute("PRAGMA table_info(archived)")}
    if "orig_sha256" not in arch_cols or "orig_sha256_provenance" not in arch_cols:
        # Stripped intermediate fixtures (pre-integrity) may lack digest columns;
        # nothing to classify until a full integrity rebuild has run.
        return counts
    file_cols = {r[1] for r in con.execute("PRAGMA table_info(files)")}
    hub_expr = "f.sha256" if "sha256" in file_cols else "NULL"
    rows = con.execute(
        f"SELECT a.repo_id, a.rfilename, a.drive_label, a.orig_sha256, {hub_expr} "
        "FROM archived a "
        "LEFT JOIN files f ON f.repo_id=a.repo_id AND f.rfilename=a.rfilename"
    ).fetchall()
    disagreements: list[str] = []
    for repo_id, rfilename, drive_label, orig, hub in rows:
        if orig is None:
            counts["null_digest"] += 1
            continue
        hub_norm = (hub or "").lower() or None
        orig_norm = str(orig).lower()
        if hub_norm and hub_norm == orig_norm:
            prov = "hub_confirmed"
            counts["hub_confirmed"] += 1
        elif hub_norm and hub_norm != orig_norm:
            counts["disagreement"] += 1
            disagreements.append(f"{repo_id}/{rfilename}@{drive_label}")
            continue
        else:
            prov = "legacy_unknown"
            counts["legacy_unknown"] += 1
        con.execute(
            "UPDATE archived SET orig_sha256_provenance=? "
            "WHERE repo_id=? AND rfilename=? AND drive_label=?",
            [prov, repo_id, rfilename, drive_label],
        )
    if disagreements:
        raise RuntimeError(
            "provenance migration refused: digest disagreement with Hub sha256 for "
            + ", ".join(disagreements[:12])
            + (f" (+{len(disagreements) - 12} more)" if len(disagreements) > 12 else "")
        )
    return counts


def _validate_migrated_clone(con: sqlite3.Connection) -> None:
    """Post-migration validation: integrity, FK, CHECK vocabulary, schema objects."""
    if _integrity_ok(con) != "ok":
        raise RuntimeError("migrated clone failed integrity_check")
    viol = _fk_violations(con)
    if viol:
        raise RuntimeError(
            f"migrated clone has foreign-key / orphan integrity failures: {viol[:12]}"
        )
    if not _archived_has_provenance_column(con):
        raise RuntimeError("migrated clone missing orig_sha256_provenance")
    if not _placement_has_derivation_check(con):
        raise RuntimeError("migrated clone missing derivation_mode CHECK")
    tables = {
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "drive_hash_repair_state" not in tables:
        raise RuntimeError("migrated clone missing drive_hash_repair_state")
    version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if version < _PROVENANCE_SCHEMA_VERSION:
        raise RuntimeError(f"migrated clone user_version={version}, expected >=7")
    # Illegal non-null derivation values must already have been rejected by rebuild.
    bad_dm = con.execute(
        "SELECT proposal_id, derivation_mode FROM placement_proposals "
        "WHERE derivation_mode IS NOT NULL AND derivation_mode NOT IN "
        "('optimized','state_truncated','canonical_fallback')"
    ).fetchall()
    if bad_dm:
        raise RuntimeError(
            f"invalid derivation_mode values survived migration: {bad_dm[:8]}"
        )


def _migrate_provenance_v7(con: sqlite3.Connection, *, backup_existing: bool) -> None:
    """Apply v7 provenance schema + backfill on an open connection (clone or ladder)."""
    if (
        _archived_has_provenance_column(con)
        and _placement_has_derivation_check(con)
        and int(con.execute("PRAGMA user_version").fetchone()[0])
        >= _PROVENANCE_SCHEMA_VERSION
    ):
        return
    if backup_existing:
        _backup_before_migration(con, "pre-provenance-v7")
    fk_on = con.execute("PRAGMA foreign_keys").fetchone()[0]
    if fk_on:
        con.execute("PRAGMA foreign_keys=OFF")
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            _apply_provenance_schema_objects(con)
            _apply_provenance_backfill(con)
            con.execute(f"PRAGMA user_version={_PROVENANCE_SCHEMA_VERSION}")
            _validate_migrated_clone(con)
            con.execute("COMMIT")
        except Exception:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        if fk_on:
            con.execute("PRAGMA foreign_keys=ON")


def _manifest_entry(path: Path | None) -> dict:
    if path is not None and path.is_file():
        return {
            "path": str(path.resolve()),
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
            "present": True,
        }
    return {"path": str(path) if path else None, "size": None, "sha256": None, "present": False}


def _source_catalog_path(source_dir: Path) -> Path:
    path = Path(source_dir) / "catalog.sqlite"
    if not path.is_file():
        raise FileNotFoundError(f"source catalog missing: {path}")
    return path


def rehearse_provenance_migration(
    source_dir: str | Path,
    work_dir: str | Path,
    *,
    run_id: str,
) -> dict:
    """DEC-059 clone-first provenance migration rehearsal (never mutates source).

    Snapshot (WAL-consistent) → disposable clone → schema/backfill → validation report.
    """
    source_dir = Path(source_dir)
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    src = _source_catalog_path(source_dir)
    run_root = work_dir / str(run_id)
    if run_root.exists():
        shutil.rmtree(run_root)
    snap_dir = run_root / "snapshot"
    clone_dir = run_root / "clone"
    snap_dir.mkdir(parents=True)
    clone_dir.mkdir(parents=True)
    snapshot_path = snap_dir / "catalog.sqlite"
    clone_path = clone_dir / "catalog.sqlite"

    # Capture source logical identity and integrity BEFORE any work (WAL-visible).
    src_con = sqlite3.connect(f"file:{src.resolve().as_posix()}?mode=ro", uri=True)
    try:
        src_con.execute("PRAGMA query_only=ON")
        source_user_version = int(src_con.execute("PRAGMA user_version").fetchone()[0])
        source_integrity = _integrity_ok(src_con)
        source_fk = _fk_violations(src_con)
        source_identity = _logical_identity(src_con)
    finally:
        src_con.close()

    # WAL-consistent snapshot via backup API (includes committed WAL content).
    src_rw = sqlite3.connect(str(src), isolation_level=None)
    try:
        snap = sqlite3.connect(str(snapshot_path), isolation_level=None)
        try:
            src_rw.backup(snap)
        finally:
            snap.close()
    finally:
        src_rw.close()

    # Manifest of source artifacts at snapshot time (path/size/sha of live files).
    wal = src.parent / f"{src.name}-wal"
    shm = src.parent / f"{src.name}-shm"
    manifest = {
        "source_db": _manifest_entry(src),
        "source_wal": _manifest_entry(wal if wal.is_file() else None),
        "source_shm": _manifest_entry(shm if shm.is_file() else None),
        "run_id": run_id,
        "source_dir": str(source_dir.resolve()),
        "source_catalog": str(src.resolve()),
    }
    manifest_path = run_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    snapshot_sha256 = _sha256_file(snapshot_path)

    # Disposable clone from snapshot bytes.
    shutil.copy2(snapshot_path, clone_path)

    clone_con = sqlite3.connect(str(clone_path), isolation_level=None)
    try:
        clone_con.execute("PRAGMA foreign_keys=OFF")
        # Ensure clone is at least v6 before provenance (frozen fixtures are v6).
        ver = int(clone_con.execute("PRAGMA user_version").fetchone()[0])
        if ver > _SCHEMA_VERSION:
            raise RuntimeError(
                f"clone user_version {ver} newer than build v{_SCHEMA_VERSION}")
        if ver < _EXECUTION_CONFIG_HASH_SCHEMA_VERSION:
            _migrate(clone_con, ver, backup_existing=False)
            ver = int(clone_con.execute("PRAGMA user_version").fetchone()[0])
        if ver < _PROVENANCE_SCHEMA_VERSION:
            _migrate_provenance_v7(clone_con, backup_existing=False)
        clone_con.execute("PRAGMA foreign_keys=ON")
        clone_user_version = int(clone_con.execute("PRAGMA user_version").fetchone()[0])
        clone_integrity = _integrity_ok(clone_con)
        clone_fk = _fk_violations(clone_con)
        # Re-read classification from backfilled rows
        classification = {
            "hub_confirmed": 0,
            "legacy_unknown": 0,
            "null_digest": 0,
            "disagreement": 0,
        }
        for (prov,) in clone_con.execute(
            "SELECT orig_sha256_provenance FROM archived"
        ):
            if prov == "hub_confirmed":
                classification["hub_confirmed"] += 1
            elif prov == "legacy_unknown":
                classification["legacy_unknown"] += 1
            elif prov is None:
                # null provenance with null digest counted as null_digest
                classification["null_digest"] += 1
            # annex_key / archive-head-blob / ingestion_computed are post-repair writers
        # Rows with digest but still null provenance should not remain after backfill;
        # count null digests from orig_sha256 for the report.
        null_digest = clone_con.execute(
            "SELECT count(*) FROM archived WHERE orig_sha256 IS NULL"
        ).fetchone()[0]
        classification["null_digest"] = int(null_digest)
        hub_n = clone_con.execute(
            "SELECT count(*) FROM archived WHERE orig_sha256_provenance='hub_confirmed'"
        ).fetchone()[0]
        leg_n = clone_con.execute(
            "SELECT count(*) FROM archived WHERE orig_sha256_provenance='legacy_unknown'"
        ).fetchone()[0]
        classification["hub_confirmed"] = int(hub_n)
        classification["legacy_unknown"] = int(leg_n)
        clone_identity = _logical_identity(clone_con)
        _validate_migrated_clone(clone_con)
    finally:
        clone_con.close()

    if source_integrity != "ok":
        raise RuntimeError(f"source integrity not ok: {source_integrity}")
    if source_fk:
        raise RuntimeError(f"source foreign-key violations: {source_fk[:12]}")
    if clone_integrity != "ok":
        raise RuntimeError(f"clone integrity not ok: {clone_integrity}")
    if clone_fk:
        raise RuntimeError(f"clone foreign-key violations: {clone_fk[:12]}")

    report = {
        "status": "ok",
        "run_id": run_id,
        "source_user_version": source_user_version,
        "clone_user_version": clone_user_version,
        "source_integrity": source_integrity,
        "clone_integrity": clone_integrity,
        "source_foreign_key_violations": source_fk,
        "clone_foreign_key_violations": clone_fk,
        "source_content_identity": source_identity,
        "clone_content_identity": clone_identity,
        "classification": classification,
        "snapshot_path": str(snapshot_path.resolve()),
        "snapshot_sha256": snapshot_sha256,
        "manifest_path": str(manifest_path.resolve()),
        "manifest_status": "validated",
        "clone_catalog_path": str(clone_path.resolve()),
        "manifest": manifest,
        "work_dir": str(run_root.resolve()),
        "source_catalog": str(src.resolve()),
    }
    (run_root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    return report


def publish_provenance_migration(
    work_dir: str | Path,
    dest_dir: str | Path,
    *,
    confirm_stopped: str,
    writers_stopped: bool = True,
) -> dict:
    """Operator-authorized publication of a rehearsed clone (DEC-059).

    Requires explicit stop confirmation, independent writer-quiescence proof, a
    retained rollback artifact equal to the rehearsal snapshot, and atomic
    same-filesystem replace onto an absent destination catalog.
    """
    work_dir = Path(work_dir)
    dest_dir = Path(dest_dir)
    if not confirm_stopped or str(confirm_stopped).strip() != "MODELARK-STOPPED":
        raise RuntimeError(
            "publication refused: confirm_stopped must be the exact token "
            "'MODELARK-STOPPED' (writers must be stopped and authorized)"
        )
    # Locate rehearsal report (work_dir may be the run root or its parent).
    report_path = work_dir / "report.json"
    if not report_path.is_file():
        candidates = list(work_dir.glob("*/report.json"))
        if len(candidates) == 1:
            report_path = candidates[0]
            work_dir = report_path.parent
        else:
            raise RuntimeError(
                f"publication refused: rehearsal report.json not found under {work_dir}"
            )
    report = json.loads(report_path.read_text())
    if report.get("manifest_status") != "validated" or report.get("status") != "ok":
        raise RuntimeError("publication refused: rehearsal is not validated/ok")
    clone_path = Path(report["clone_catalog_path"])
    snapshot_path = Path(report["snapshot_path"])
    if not clone_path.is_file() or not snapshot_path.is_file():
        raise RuntimeError("publication refused: clone or snapshot missing")
    source_catalog = Path(report.get("source_catalog") or "")
    if not source_catalog.is_file():
        # Fall back to manifest
        man = report.get("manifest") or {}
        source_catalog = Path((man.get("source_db") or {}).get("path") or "")
    if not source_catalog.is_file():
        raise RuntimeError("publication refused: source catalog path unknown")

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_cat = dest_dir / "catalog.sqlite"
    if dest_cat.exists():
        raise RuntimeError(
            f"publication refused: destination already exists ({dest_cat}); "
            "will not overwrite"
        )

    # Independent writer-quiescence proof (even when writers_stopped=True).
    # A held write transaction on the source must refuse publication.
    probe = sqlite3.connect(str(source_catalog), isolation_level=None, timeout=0.05)
    try:
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("ROLLBACK")
        except sqlite3.OperationalError as exc:
            raise RuntimeError(
                "publication refused: source catalog has an active writer / lock "
                f"(quiescence proof failed): {exc}"
            ) from exc
    finally:
        probe.close()
    if not writers_stopped:
        raise RuntimeError(
            "publication refused: writers_stopped must be True after quiescence proof"
        )

    # Rollback artifact: byte-identical copy of the rehearsal snapshot.
    rollback_dir = work_dir / "rollback"
    rollback_dir.mkdir(parents=True, exist_ok=True)
    rollback_artifact = rollback_dir / "catalog.sqlite.pre-publish"
    if rollback_artifact.exists():
        rollback_artifact.unlink()
    shutil.copy2(snapshot_path, rollback_artifact)
    if _sha256_file(rollback_artifact) != report["snapshot_sha256"]:
        raise RuntimeError(
            "publication refused: rollback artifact hash != rehearsal snapshot hash"
        )

    # Stage clone beside destination for same-FS atomic replace.
    staging = dest_dir / ".catalog.sqlite.publish-staging"
    if staging.exists():
        staging.unlink()
    shutil.copy2(clone_path, staging)
    if os.stat(staging).st_dev != os.stat(dest_dir).st_dev:
        staging.unlink(missing_ok=True)
        raise RuntimeError(
            "publication refused: staging and destination are on different filesystems"
        )
    if (
        source_catalog.exists()
        and os.stat(source_catalog).st_dev != os.stat(staging).st_dev
    ):
        staging.unlink(missing_ok=True)
        raise RuntimeError(
            "publication refused: source and destination are on different filesystems"
        )
    # Final atomic replace onto the absent publication target.
    os.replace(str(staging), str(dest_cat))

    return {
        "status": "ok",
        "rollback_artifact": str(rollback_artifact.resolve()),
        "published_catalog": str(dest_cat.resolve()),
        "manifest_status": "validated",
        "manifest_path": report.get("manifest_path"),
        "snapshot_sha256": report["snapshot_sha256"],
    }


def upsert(con, table: str, row: dict, pk: list[str], touch: list[str] | None = None) -> None:
    """Insert or update one row keyed by `pk`. `touch` columns are set to CURRENT_TIMESTAMP on update."""
    cols = list(row)
    placeholders = ", ".join(["?"] * len(cols))
    sets = [f"{c}=excluded.{c}" for c in cols if c not in pk]
    for c in touch or []:
        sets.append(f"{c}=CURRENT_TIMESTAMP")
    update_clause = ", ".join(sets) if sets else f"{pk[0]}=excluded.{pk[0]}"
    sql = (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {update_clause}"
    )
    con.execute(sql, [row[c] for c in cols])


def _replace_files_body(con, repo_id: str, rows: list[dict]) -> None:
    """Unlocked file-row refresh body (caller owns the transaction)."""
    for r in rows:
        row = dict(r)
        row["repo_id"] = repo_id
        upsert(con, "files", row, pk=["repo_id", "rfilename"])
    names = [r["rfilename"] for r in rows]
    keep = ""
    params: list[object] = [repo_id]
    if names:
        keep = f"AND rfilename NOT IN ({', '.join(['?'] * len(names))})"
        params.extend(names)
    con.execute(
        "DELETE FROM files AS f WHERE repo_id=? " + keep + " "
        "AND NOT EXISTS (SELECT 1 FROM archived a "
        "                WHERE a.repo_id=f.repo_id AND a.rfilename=f.rfilename) "
        "AND NOT EXISTS (SELECT 1 FROM replicas r "
        "                WHERE r.repo_id=f.repo_id AND r.rfilename=f.rfilename)",
        params,
    )


def replace_files(con, repo_id: str, rows: list[dict]) -> None:
    """Refresh file rows for a repo in one transaction (bumps planner_revision; PR-08 A3).

    Rediscovery may remove an upstream filename after ModelArk archived it. Such a row is durable
    archive provenance and must survive the refresh; unreferenced stale rows are removed normally.
    """
    from modelark.proposal import GraphResult, graph_write

    def op(c):
        _replace_files_body(c, repo_id, rows)
        return GraphResult(proven_noop=False)

    graph_write(con, op)
