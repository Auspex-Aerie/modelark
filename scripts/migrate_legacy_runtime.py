"""Rehearse or execute a non-overwriting ModelArk catalog migration.

The default is inspection only. Execution requires an explicit stopped-writer assertion and always
works from a backup: it snapshots a legacy SQLite catalog (or copies a stopped DuckDB catalog plus
WAL), migrates that copy through the current schema, validates it, and atomically publishes a NEW
destination data directory. The source is never replaced.

Examples:

    python -m scripts.migrate_legacy_runtime \
      --source-data-dir /path/to/legacy/catalog \
      --destination-data-dir /path/to/modelark-next \
      --backup-root /path/to/backups

    python -m scripts.migrate_legacy_runtime ... \
      --execute --confirm-stopped MODELARK-STOPPED

The actual operator checkout cutover is deliberately outside this tool; follow
``docs/legacy-cutover.md`` with the operator present.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from modelark import plan
from modelark.core import db

_CONFIRMATION = "MODELARK-STOPPED"
_RUNTIME_CONFIGS = ("library.json",)
_RUN_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_SOURCE_KINDS = ("sqlite", "duckdb")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _paths(source: Path, destination: Path, backup_root: Path) -> tuple[Path, Path, Path]:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    backup_root = backup_root.expanduser().resolve()
    if source == destination or _inside(destination, source) or _inside(source, destination):
        raise RuntimeError("source and destination data directories must be separate, non-nested paths")
    if _inside(backup_root, source):
        raise RuntimeError("backup root must be outside the source data directory")
    if _inside(backup_root, destination):
        raise RuntimeError("backup root must be outside the destination data directory")
    return source, destination, backup_root


def _source_kind(source_dir: Path, requested_kind: str | None = None) -> tuple[str, Path]:
    sqlite_path = source_dir / "catalog.sqlite"
    duckdb_path = source_dir / "catalog.duckdb"
    found = [("sqlite", sqlite_path)] if sqlite_path.is_file() else []
    if duckdb_path.is_file():
        found.append(("duckdb", duckdb_path))
    if requested_kind is not None:
        if requested_kind not in _SOURCE_KINDS:
            raise RuntimeError(
                f"unknown source kind {requested_kind!r}; must be 'sqlite' or 'duckdb'")
        requested = dict(found).get(requested_kind)
        if requested is None:
            raise RuntimeError(
                f"explicit {requested_kind} source requested, but its catalog does not exist in "
                f"{source_dir}")
        return requested_kind, requested
    if not found:
        raise RuntimeError(
            f"no catalog.sqlite or catalog.duckdb found in source data directory {source_dir}")
    if len(found) > 1:
        raise RuntimeError(
            f"both catalog.sqlite and catalog.duckdb exist in {source_dir}; identify the active "
            "catalog before migration")
    return found[0]


def _sqlite_inventory(path: Path) -> dict:
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=1)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name").fetchall()]
        counts = {name: con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                  for name in tables}
        return {"integrity_check": integrity, "user_version": con.execute(
            "PRAGMA user_version").fetchone()[0], "tables": counts}
    finally:
        con.close()


def _duckdb_inventory(path: Path) -> dict:
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError(
            "DuckDB source detected; install the migration extra with "
            "`pip install 'modelark[migration]'`") from exc
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception as exc:
        raise RuntimeError(
            f"cannot open DuckDB source read-only; it may still have an active writer: {exc}") from exc
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' "
            "ORDER BY table_name").fetchall()]
        return {"integrity_check": "DuckDB read-only open succeeded", "user_version": None,
                "tables": {name: con.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                           for name in tables}}
    finally:
        con.close()


def inspect(source_data_dir: Path, destination_data_dir: Path, backup_root: Path,
            source_kind: str | None = None) -> dict:
    source, destination, backups = _paths(source_data_dir, destination_data_dir, backup_root)
    kind, catalog = _source_kind(source, source_kind)
    inventory = _sqlite_inventory(catalog) if kind == "sqlite" else _duckdb_inventory(catalog)
    return {
        "mode": "inspection-only",
        "source_kind": kind,
        "source_selection": "explicit" if source_kind else "automatic",
        "source_catalog": str(catalog),
        "destination_data_dir": str(destination),
        "backup_root": str(backups),
        "destination_exists": destination.exists(),
        "runtime_configs": [name for name in _RUNTIME_CONFIGS if (source / name).is_file()],
        "source": inventory,
        "next": f"stop every ModelArk writer, then rerun with --execute --confirm-stopped {_CONFIRMATION}",
    }


def _copy_runtime_configs(source: Path, target: Path) -> list[str]:
    copied = []
    for name in _RUNTIME_CONFIGS:
        src = source / name
        if src.is_file():
            shutil.copy2(src, target / name)
            copied.append(name)
    return copied


def _backup_sqlite(source: Path, run_backup: Path) -> tuple[Path, list[Path], sqlite3.Connection]:
    """Acquire a no-writer guard and return it open until source capture is complete."""
    guard = sqlite3.connect(str(source), isolation_level=None, timeout=0)
    try:
        guard.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        guard.close()
        raise RuntimeError(
            "source SQLite catalog is busy; a writer may still be running. Stop it and retry") from exc

    snapshot = run_backup / "catalog.sqlite.snapshot"
    raw_dir = run_backup / "raw-source"
    raw_dir.mkdir()
    try:
        reader = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        out = sqlite3.connect(str(snapshot))
        try:
            reader.backup(out)
        finally:
            out.close()
            reader.close()
        raw = []
        for path in (source, Path(str(source) + "-wal"), Path(str(source) + "-shm")):
            if path.is_file():
                dst = raw_dir / path.name
                shutil.copy2(path, dst)
                raw.append(dst)
        return snapshot, raw, guard
    except Exception:
        guard.execute("ROLLBACK")
        guard.close()
        raise


def _backup_duckdb(source: Path, run_backup: Path) -> tuple[Path, list[Path]]:
    # A read-only open is both a format check and DuckDB's own active-writer refusal.
    _duckdb_inventory(source)
    raw_dir = run_backup / "raw-source"
    raw_dir.mkdir()
    raw = []
    for path in (source, Path(str(source) + ".wal")):
        if path.is_file():
            dst = raw_dir / path.name
            shutil.copy2(path, dst)
            raw.append(dst)
    return raw_dir / source.name, raw


def _migrate_duckdb_copy(source: Path, target: Path) -> dict:
    module_path = Path(__file__).with_name("migrate_duckdb_to_sqlite.py")
    spec = importlib.util.spec_from_file_location("modelark_duckdb_migrator", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load DuckDB migrator from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.migrate(source, target)


def _publish_current_catalog(source_kind: str, backup_catalog: Path, stage: Path) -> dict:
    stage.mkdir(exist_ok=True)
    target = stage / "catalog.sqlite"
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        if source_kind == "sqlite":
            src = sqlite3.connect(f"file:{backup_catalog.as_posix()}?mode=ro", uri=True)
            dst = sqlite3.connect(str(target))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            import_report = None
        else:
            import_report = _migrate_duckdb_copy(backup_catalog, target)

        db.configure(stage, stage / "state")
        con = db.connect()
        try:
            migrated_plan = plan.bootstrap(con)["plan_id"]
            integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = con.execute("PRAGMA foreign_key_check").fetchall()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            inventory = {name: count for name, count in _sqlite_inventory(target)["tables"].items()}
            user_version = con.execute("PRAGMA user_version").fetchone()[0]
        finally:
            con.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old
    if integrity != "ok" or foreign_keys:
        raise RuntimeError(
            f"migrated catalog validation failed: integrity={integrity!r}, "
            f"foreign_key_violations={foreign_keys[:12]!r}")
    return {"integrity_check": integrity, "foreign_key_check": foreign_keys,
            "user_version": user_version, "tables": inventory,
            "active_plan": migrated_plan, "duckdb_import": import_report}


def execute(source_data_dir: Path, destination_data_dir: Path, backup_root: Path,
            confirmation: str, run_id: str | None = None,
            source_kind: str | None = None) -> dict:
    if confirmation != _CONFIRMATION:
        raise RuntimeError(
            f"execution requires --confirm-stopped {_CONFIRMATION} after every ModelArk writer is stopped")
    source, destination, backups = _paths(source_data_dir, destination_data_dir, backup_root)
    kind, catalog = _source_kind(source, source_kind)
    if destination.exists():
        raise RuntimeError(f"destination already exists; refusing to overwrite {destination}")
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not _RUN_ID.fullmatch(run_id):
        raise RuntimeError("run id may contain only letters, digits, dot, underscore, and hyphen")
    run_backup = backups / f"modelark-migration-{run_id}"
    stage = destination.parent / f".{destination.name}.migrating-{run_id}"
    if run_backup.exists() or stage.exists():
        raise RuntimeError("migration run id already exists; choose a new --run-id")

    destination.parent.mkdir(parents=True, exist_ok=True)
    backups.mkdir(parents=True, exist_ok=True)
    run_backup.mkdir()
    manifest = {
        "status": "started", "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_kind": kind, "source_catalog": str(catalog),
        "source_selection": "explicit" if source_kind else "automatic",
        "destination_data_dir": str(destination), "backup_dir": str(run_backup),
    }
    guard = None
    try:
        if kind == "sqlite":
            backup_catalog, raw, guard = _backup_sqlite(catalog, run_backup)
        else:
            backup_catalog, raw = _backup_duckdb(catalog, run_backup)
        backup_configs = _copy_runtime_configs(source, run_backup)
        if guard is not None:
            guard.execute("ROLLBACK")
            guard.close()
            guard = None

        source_inventory = (_sqlite_inventory(backup_catalog) if kind == "sqlite"
                            else _duckdb_inventory(backup_catalog))
        stage.mkdir()
        _copy_runtime_configs(run_backup, stage)
        destination_inventory = _publish_current_catalog(kind, backup_catalog, stage)

        common = set(source_inventory["tables"]) & set(destination_inventory["tables"])
        changes = {name: [source_inventory["tables"][name], destination_inventory["tables"][name]]
                   for name in sorted(common)
                   if source_inventory["tables"][name] != destination_inventory["tables"][name]}
        # Current startup idempotently creates the default `ark` plan and assigns every registered
        # drive. Those are the only intentional row-count additions; every other common table must
        # match exactly, and bootstrap may never reduce plan rows.
        mismatches = {name: values for name, values in changes.items()
                      if name not in {"plans", "plan_drives"} or values[1] < values[0]}
        if mismatches:
            raise RuntimeError(f"row-count mismatch after migration: {mismatches}")
        manifest.update({
            "status": "validated", "source": source_inventory,
            "destination": destination_inventory, "row_count_changes": changes,
            "row_count_mismatches": mismatches,
            "backup_configs": backup_configs,
            "files": {str(path.relative_to(run_backup)): _sha256(path)
                      for path in [backup_catalog, *raw, *[run_backup / n for n in backup_configs]]},
        })
        (run_backup / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        manifest["destination_sha256"] = _sha256(stage / "catalog.sqlite")
        destination_manifest = dict(manifest, status="published")
        (stage / "migration-manifest.json").write_text(
            json.dumps(destination_manifest, indent=2, sort_keys=True) + "\n")
        os.replace(stage, destination)
        manifest["status"] = "published"
        (run_backup / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return manifest
    except Exception as exc:
        if guard is not None:
            try:
                guard.execute("ROLLBACK")
            finally:
                guard.close()
        if stage.exists():
            shutil.rmtree(stage)
        manifest.update({"status": "failed", "error": str(exc)})
        (run_backup / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data-dir", type=Path, required=True)
    parser.add_argument("--destination-data-dir", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument(
        "--source-kind", choices=_SOURCE_KINDS,
        help="explicit active catalog engine; required to disambiguate when both catalogs exist",
    )
    parser.add_argument("--execute", action="store_true",
                        help="create backup and publish a new migrated destination (default: inspect only)")
    parser.add_argument("--confirm-stopped", metavar="TEXT",
                        help=f"with --execute, must be exactly {_CONFIRMATION}")
    parser.add_argument("--run-id", help="optional unique manifest suffix")
    args = parser.parse_args(argv)
    try:
        if args.execute:
            result = execute(args.source_data_dir, args.destination_data_dir, args.backup_root,
                             args.confirm_stopped or "", args.run_id, args.source_kind)
        else:
            if args.confirm_stopped or args.run_id:
                parser.error("--confirm-stopped/--run-id are valid only with --execute")
            result = inspect(args.source_data_dir, args.destination_data_dir, args.backup_root,
                             args.source_kind)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except RuntimeError as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
