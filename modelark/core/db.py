"""Catalog connection, schema bootstrap, and upsert helpers (SQLite, WAL mode).

WAL journaling gives cross-process concurrency: many readers + one writer, no exclusive lock — so a
CLI, a diagnostic, or an audit can read the catalog WHILE the portal is filling (DEC-024). This
replaces DuckDB, whose single-writer lock blocked every concurrent access (the recurring "stop the
portal to inspect" friction). The connect/upsert/replace_files API is unchanged: sqlite3 cursors
support `con.execute(sql, params).fetchone()/.fetchall()`, `?` placeholders, and
`INSERT … ON CONFLICT(pk) DO UPDATE SET col=excluded.col` (SQLite ≥3.24), exactly like before.
"""
from __future__ import annotations

import os
import re
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
    """Yield executable statements, stripping `--` line comments first so a ';'
    inside a comment is not mistaken for a statement boundary."""
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    for chunk in no_comments.split(";"):
        if chunk.strip():
            yield chunk


def _apply_schema(con: sqlite3.Connection, tables_only: bool = False) -> None:
    """Apply the packaged schema. The first startup pass creates only tables so legacy data can be
    rebuilt before unique indexes and views are installed; the final pass installs everything."""
    for stmt in _statements(SCHEMA_PATH.read_text()):
        if tables_only and not stmt.lstrip().upper().startswith("CREATE TABLE"):
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
    CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    legacy = CATALOG_DIR / "catalog.duckdb"          # guard: never silently start on an EMPTY sqlite when the
    if not _bootstrapping and not DB_PATH.exists() and legacy.exists():   # DuckDB catalog is still the source of truth
        raise RuntimeError(
            f"Catalog not migrated yet: {legacy.name} exists but {DB_PATH.name} does not. Run\n"
            f"  .venv/bin/python -m scripts.migrate_duckdb_to_sqlite {legacy} {DB_PATH}\n"
            f"first (DEC-024), then start the portal.")
    con = sqlite3.connect(str(DB_PATH), isolation_level=None, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")           # persistent once set; concurrent reader/writer
    con.execute("PRAGMA busy_timeout=15000")         # a concurrent WRITER briefly holds the lock → wait, don't error
    con.execute("PRAGMA synchronous=NORMAL")         # WAL-safe durability without an fsync per commit
    if read_only:
        con.execute("PRAGMA foreign_keys=ON")         # every connection must opt in; SQLite defaults this OFF
        con.execute("PRAGMA query_only=ON")          # this connection reads live data but cannot write
        return con
    try:
        # A legacy table rebuild must run with FK enforcement off; validation still happens through
        # PRAGMA foreign_key_check before its transaction commits. Every normal write below runs ON.
        con.execute("PRAGMA foreign_keys=OFF")
        _apply_schema(con, tables_only=True)
        _migrate(con)
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
_SCHEMA_VERSION = 1


def _canonical_table_sql(table: str, replacement: str) -> str:
    """Return one canonical CREATE TABLE statement under a temporary table name."""
    prefix = re.compile(
        rf"^CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\b",
        re.IGNORECASE,
    )
    for stmt in _statements(SCHEMA_PATH.read_text()):
        clean = stmt.strip()
        if prefix.match(clean):
            return prefix.sub(f"CREATE TABLE {replacement}", clean, count=1)
    raise RuntimeError(f"Packaged schema has no CREATE TABLE statement for {table}")


def _backup_before_integrity_migration(con: sqlite3.Connection) -> Path:
    """Create one consistent, non-overwriting recovery copy before the destructive table swap."""
    backup_path = DB_PATH.with_name(f"{DB_PATH.name}.pre-integrity-v{_SCHEMA_VERSION}.bak")
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
        con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        return
    if con.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise RuntimeError("Integrity migration requires foreign_keys=OFF before its transaction")

    _backup_before_integrity_migration(con)
    current = "catalog"
    con.execute("BEGIN IMMEDIATE")
    try:
        for view in _VIEW_NAMES:
            con.execute(f'DROP VIEW IF EXISTS "{view}"')
        for table in _INTEGRITY_TABLES:
            current = table
            new = f"{table}__integrity_new"
            con.execute(f'DROP TABLE IF EXISTS "{new}"')
            con.execute(_canonical_table_sql(table, new))
            old_cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
            new_cols = [r[1] for r in con.execute(f'PRAGMA table_info("{new}")').fetchall()]
            cols = [c for c in new_cols if c in old_cols]
            quoted = ", ".join(f'"{c}"' for c in cols)
            con.execute(f'INSERT INTO "{new}" ({quoted}) SELECT {quoted} FROM "{table}"')

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
        con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        con.execute("COMMIT")
    except Exception as exc:
        con.execute("ROLLBACK")
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            f"Cannot add catalog integrity constraints: legacy table {current!r} contains "
            f"invalid data ({exc}). Correct or export that row before retrying."
        ) from exc


def _migrate(con) -> None:
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
    _rebuild_integrity_tables(con)


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


def replace_files(con, repo_id: str, rows: list[dict]) -> None:
    """Refresh file rows for a repo in one transaction.

    Rediscovery may remove an upstream filename after ModelArk archived it. Such a row is durable
    archive provenance and must survive the refresh; unreferenced stale rows are removed normally.
    """
    con.execute("BEGIN")
    try:
        for r in rows:
            upsert(con, "files", r, pk=["repo_id", "rfilename"])
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
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
