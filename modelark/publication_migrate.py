"""Read-only annex-payload conversion inspect. Disposable apply freezes a plan.

Live catalog cutover remains forbidden. Physical Git/annex conversion is later.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from modelark.core.db import _xdg_data_home
from modelark.publication_policy import PublicationRefused
from modelark.publication_store import library


def live_catalog_path():
    return (_xdg_data_home() / "modelark" / "catalog.sqlite").expanduser().resolve()


def _catalog_file(con):
    paths = [row[2] for row in con.execute("PRAGMA database_list") if row[1] == "main"]
    if not paths or not paths[0]:
        return None
    return Path(paths[0]).expanduser().resolve()


_PAYLOAD_PREFIX = "__modelark_payload_v1__"


def _state(row):
    annex, digest, size = row["annex_key"], row["orig_sha256"], row["orig_bytes"]
    if annex:
        return "already-converted-with-proof"
    if not digest or size is None:
        return "needs-evidence"
    return "convertible"


def _mapping(rfilename):
    digest = hashlib.sha256(str(rfilename).encode("utf-8")).hexdigest()
    return f"{_PAYLOAD_PREFIX}/p-{digest}.blob"


def inspect_conversion(con, *, drive=None, repos=None):
    """Read-only census of archived copies without annex keys.

    Opens no Git, writes no SQLite, starts no generation. Live apply/cutover
    remains a separate authorized step.
    """
    identity = library(con)
    clauses = ["(annex_key IS NULL OR annex_key='')"]
    params = []
    if drive is not None:
        clauses.append("drive_label=?")
        params.append(drive)
    if repos:
        clauses.append(f"repo_id IN ({','.join('?' * len(repos))})")
        params.extend(repos)
    columns = ("drive_label", "repo_id", "rfilename", "orig_sha256", "orig_bytes",
               "stored_relpath", "stored_name", "annex_key")
    rows = con.execute(
        f"SELECT {','.join(columns)} FROM archived WHERE {' AND '.join(clauses)} "
        "ORDER BY drive_label, repo_id, rfilename",
        params,
    ).fetchall()
    candidates = []
    counts = {}
    for raw in rows:
        item = dict(zip(columns, raw))
        item["state"] = _state(item)
        item["proposed_stored_relpath"] = _mapping(item["rfilename"])
        candidates.append(item)
        counts[item["state"]] = counts.get(item["state"], 0) + 1
    return {
        "kind": "annex-migrate-inspect",
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "library_id": None if identity is None else identity[0],
        "map_uuid": None if identity is None else identity[1],
        "drive": drive,
        "repos": None if repos is None else list(repos),
        "counts": counts,
        "candidates": candidates,
        "apply": "disabled-until-explicit-cutover",
    }


_ENVELOPE = frozenset({"frozen", "seal", "apply", "plan_path"})


def _census(plan):
    return {key: value for key, value in plan.items() if key not in _ENVELOPE}


def _seal(plan):
    body = json.dumps(_census(plan), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _write_private(path, text):
    path = Path(path)
    os.makedirs(path.parent, mode=0o700, exist_ok=True)
    os.chmod(path.parent, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def _read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        return os.read(fd, 1 << 22).decode()
    finally:
        os.close(fd)


def apply_conversion(con, plan=None, *, writers_stopped=False, dest_dir=None):
    """Freeze an inspect plan on a disposable catalog. Never converts live bytes."""
    if not writers_stopped:
        raise PublicationRefused("PUBLICATION_WRITERS_STILL_RUNNING")
    catalog = _catalog_file(con)
    if catalog is not None and catalog == live_catalog_path():
        raise PublicationRefused("PUBLICATION_LIVE_CUTOVER_FORBIDDEN")
    if dest_dir is None:
        raise PublicationRefused("PUBLICATION_MIGRATE_DEST_REQUIRED")
    dest_dir = Path(dest_dir)
    plan = dict(plan or inspect_conversion(con))
    seal = _seal(plan)
    frozen = {**_census(plan), "frozen": True, "seal": seal, "apply": "frozen-inspect-only"}
    path = dest_dir / f"annex-migrate-{seal[:12]}.json"
    try:
        _write_private(path, plan_json(frozen))
    except FileExistsError as exc:
        raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_EXISTS", path=str(path)) from exc
    except OSError as exc:
        raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_UNPROVEN", path=str(path)) from exc
    frozen["plan_path"] = str(path)
    return frozen


def resume_conversion(con, plan_id, *, writers_stopped=False, dest_dir=None):
    """Reload a frozen inspect plan. Does not convert live bytes."""
    if not writers_stopped:
        raise PublicationRefused("PUBLICATION_WRITERS_STILL_RUNNING")
    catalog = _catalog_file(con)
    if catalog is not None and catalog == live_catalog_path():
        raise PublicationRefused("PUBLICATION_LIVE_CUTOVER_FORBIDDEN")
    if dest_dir is None:
        raise PublicationRefused("PUBLICATION_MIGRATE_DEST_REQUIRED")
    dest_dir = Path(dest_dir)
    prefix = str(plan_id)[:12]
    matches = sorted(dest_dir.glob(f"annex-migrate-{prefix}*.json"))
    if len(matches) != 1:
        raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_UNPROVEN", plan_id=plan_id)
    try:
        frozen = json.loads(_read_private(matches[0]))
    except OSError as exc:
        raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_UNPROVEN", plan_id=plan_id) from exc
    if frozen.get("seal") != _seal(frozen):
        raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_UNPROVEN", plan_id=plan_id)
    if frozen.get("seal", "")[:12] != prefix:
        raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_UNPROVEN", plan_id=plan_id)
    return frozen


def plan_json(plan):
    return json.dumps(plan, indent=2, sort_keys=True) + "\n"
