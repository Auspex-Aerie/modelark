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
        con.execute("PRAGMA query_only=ON")          # this connection reads live data but cannot write
        return con
    for stmt in _statements(SCHEMA_PATH.read_text()):
        con.execute(stmt)
    _migrate(con)
    return con


# Idempotent column additions for catalogs created before a column existed
# (CREATE TABLE IF NOT EXISTS won't alter an existing table). DEC-014.
_MIGRATIONS = (
    "ALTER TABLE drives ADD COLUMN role VARCHAR DEFAULT 'primary'",
    "ALTER TABLE drives ADD COLUMN raid_backed BOOLEAN DEFAULT false",
    "ALTER TABLE models ADD COLUMN numcopies INTEGER DEFAULT 1",
    "ALTER TABLE archived ADD COLUMN stored_relpath VARCHAR",
)


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
    """Replace the file rows for a repo in a single transaction."""
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM files WHERE repo_id = ?", [repo_id])
        for r in rows:
            cols = list(r)
            con.execute(
                f"INSERT INTO files ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))})",
                [r[c] for c in cols],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
