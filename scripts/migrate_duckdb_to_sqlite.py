"""One-time catalog migration: DuckDB → SQLite (DEC-024).

Reads every table from a source .duckdb and writes it into a FRESH SQLite catalog (schema from
modelark/core/db.py + schema.sql). Conversions: `models.tags` DuckDB list → JSON text; datetimes →
'YYYY-MM-DD HH:MM:SS' text (matching CURRENT_TIMESTAMP); booleans → 0/1. Row counts are verified per
table. The source is opened but never modified. Usage:

    python -m scripts.migrate_duckdb_to_sqlite  <src.duckdb>  <dst.sqlite>

Test it safely on a COPY of the live catalog (no portal stop):
    cp catalog/catalog.duckdb  /tmp/x.duckdb  &&  cp catalog/catalog.duckdb.wal /tmp/x.duckdb.wal 2>/dev/null
    python -m scripts.migrate_duckdb_to_sqlite  /tmp/x.duckdb  /tmp/x.sqlite
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

from modelark.core import db

TABLES = ["models", "files", "drives", "replicas", "verifications",
          "selection", "archived", "fetch_events"]


def _cell(col: str, val: object) -> object:
    if val is None:
        return None
    if col == "tags" or isinstance(val, (list, tuple)):     # DuckDB list → JSON text
        return json.dumps(list(val))
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")            # match CURRENT_TIMESTAMP format
    if isinstance(val, date):
        return val.isoformat()
    if isinstance(val, bool):
        return 1 if val else 0
    return val


def migrate(src_path: Path, dst_path: Path) -> dict:
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError(
            "DuckDB migration support is optional; install it with "
            "`pip install 'modelark[migration]'`."
        ) from exc
    src = dst = None
    destination_started = completed = False
    sidecars = (dst_path, Path(str(dst_path) + "-wal"), Path(str(dst_path) + "-shm"))
    try:
        src = duckdb.connect(str(src_path))                 # read-write on the COPY → replays any .wal
        db.CATALOG_DIR = dst_path.parent
        db.DB_PATH = dst_path
        for path in sidecars:
            path.unlink(missing_ok=True)
        destination_started = True
        dst = db.connect(_bootstrapping=True)               # creates the constrained SQLite schema
        sqlite_cols = {
            t: {r[1] for r in dst.execute(f"PRAGMA table_info({t})").fetchall()}
            for t in TABLES
        }

        report = {}
        for t in TABLES:
            src_cols = [c[0] for c in src.execute(f"SELECT * FROM {t} LIMIT 0").description]
            use = [c for c in src_cols if c in sqlite_cols[t]]  # columns present in BOTH schemas
            dropped = [c for c in src_cols if c not in sqlite_cols[t]]
            rows = src.execute(f"SELECT {', '.join(use)} FROM {t}").fetchall()
            ph = ", ".join(["?"] * len(use))
            row_no, row_key = 0, "before first row"
            try:
                dst.execute("BEGIN")
                for row_no, row in enumerate(rows, 1):
                    values = [_cell(use[i], row[i]) for i in range(len(use))]
                    row_key = f"{use[0]}={values[0]!r}" if use else "no shared columns"
                    # Pre-DEC-039 Tier A mislabeled remote-header evidence as `verified`.
                    if t == "models" and "status" in use:
                        status = use.index("status")
                        if values[status] == "verified":
                            values[status] = "inspected"
                    dst.execute(f"INSERT INTO {t} ({', '.join(use)}) VALUES ({ph})", values)
                dst.execute("COMMIT")
            except Exception as exc:
                if dst.in_transaction:
                    dst.execute("ROLLBACK")
                raise RuntimeError(
                    f"DuckDB migration failed in table {t!r} at source row {row_no} "
                    f"({row_key[:160]}): {exc}. The partial SQLite destination was removed; "
                    "the DuckDB source is unchanged."
                ) from exc
            got = dst.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            report[t] = {"src": len(rows), "dst": got, "dropped_cols": dropped}
        # The destination schema is bootstrapped before source rows exist. Run row-level backfills
        # once more after import (notably nested archived stored_relpath recovery).
        db._migrate_legacy_columns(dst)
        completed = True
        return report
    finally:
        if dst is not None:
            try:
                if dst.in_transaction:
                    dst.execute("ROLLBACK")
            finally:
                dst.close()
        if src is not None:
            src.close()
        if destination_started and not completed:
            for path in sidecars:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # preserve the original migration diagnostic


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    report = migrate(Path(argv[1]), Path(argv[2]))
    print(f"{'table':16}{'src':>8}{'dst':>8}   status")
    ok = True
    for t, r in report.items():
        match = r["src"] == r["dst"]
        ok &= match
        note = "✓" if match else "✗ ROW MISMATCH"
        if r["dropped_cols"]:
            note += f"  (dropped cols not in sqlite schema: {r['dropped_cols']})"
        print(f"{t:16}{r['src']:>8}{r['dst']:>8}   {note}")
    print("\nALL ROW COUNTS MATCH ✓" if ok else "\nROW COUNT MISMATCH ✗")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
