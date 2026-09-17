"""Read-only annex-payload conversion inspect. Disposable apply freezes a plan.

Live catalog cutover remains forbidden. Physical Git/annex conversion is later.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
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


def _git_env():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
    return env


def _git(archive, *args, check=True):
    result = subprocess.run(
        ["git", "-C", str(archive), "-c", "user.name=ModelArk",
         "-c", "user.email=publication@modelark.invalid", "-c", "commit.gpgsign=false",
         "-c", "core.hooksPath=/dev/null", "-c", "core.pager=", *args],
        env=_git_env(), capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise PublicationRefused("PUBLICATION_MIGRATE_GIT_FAILED", stderr=(result.stderr or "")[-500:])
    return (result.stdout or "").strip()


def _source_path(archive, candidate):
    stored = candidate.get("stored_relpath") or candidate.get("rfilename")
    if not stored:
        raise PublicationRefused("PUBLICATION_MIGRATE_SOURCE_UNPROVEN")
    path = archive / stored
    if not path.is_file() and not path.is_symlink():
        alt = archive / candidate["rfilename"]
        if alt.is_file() or alt.is_symlink():
            return alt
        raise PublicationRefused("PUBLICATION_MIGRATE_SOURCE_UNPROVEN", path=str(path))
    return path


def _convert_file(archive, candidate):
    archive = Path(archive)
    src = _source_path(archive, candidate)
    data = src.read_bytes()
    expected = candidate.get("orig_sha256")
    digest = hashlib.sha256(data).hexdigest()
    if expected and digest != expected:
        raise PublicationRefused("PUBLICATION_MIGRATE_HASH_MISMATCH", expected=expected, observed=digest)
    dest_rel = candidate["proposed_stored_relpath"]
    dest = archive / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.resolve() != src.resolve():
        dest.write_bytes(data)
    _git(archive, "-c", "annex.largefiles=anything", "annex", "add", "--", dest_rel)
    key = _git(archive, "annex", "lookupkey", "--", dest_rel)
    if not key:
        raise PublicationRefused("PUBLICATION_MIGRATE_KEY_UNPROVEN", path=dest_rel)
    src_rel = str(src.relative_to(archive))
    if dest.resolve() != src.resolve():
        _git(archive, "rm", "-q", "--", src_rel, check=False)
    _git(archive, "commit", "-qm", f"annex-migrate {candidate['rfilename']}")
    return key, dest_rel


def _current_annex_key(con, candidate):
    row = con.execute(
        "SELECT annex_key FROM archived WHERE drive_label=? AND repo_id=? AND rfilename=?",
        [candidate["drive_label"], candidate["repo_id"], candidate["rfilename"]]).fetchone()
    return None if row is None else row[0]


def _publish_key(con, candidate, key, stored_relpath):
    from modelark.proposal import GraphResult, graph_write

    def write(c):
        changed = c.execute(
            "UPDATE archived SET annex_key=?, stored_relpath=? "
            "WHERE drive_label=? AND repo_id=? AND rfilename=? "
            "AND (annex_key IS NULL OR annex_key='')",
            [key, stored_relpath, candidate["drive_label"], candidate["repo_id"],
             candidate["rfilename"]])
        if changed.rowcount != 1:
            raise PublicationRefused("PUBLICATION_MIGRATE_CATALOG_UNPROVEN",
                                     drive_label=candidate["drive_label"],
                                     rfilename=candidate["rfilename"])
        return GraphResult(proven_noop=False)

    graph_write(con, write)


def _convert_archives(con, frozen, archives):
    converted = []
    for candidate in frozen.get("candidates") or []:
        if candidate.get("state") != "convertible":
            continue
        if _current_annex_key(con, candidate):
            continue
        label = candidate["drive_label"]
        archive = archives.get(label)
        if archive is None:
            raise PublicationRefused("PUBLICATION_MIGRATE_ARCHIVE_REQUIRED", drive_label=label)
        key, stored = _convert_file(archive, candidate)
        _publish_key(con, candidate, key, stored)
        converted.append({"drive_label": label, "rfilename": candidate["rfilename"],
                          "annex_key": key, "stored_relpath": stored})
    frozen = dict(frozen)
    frozen["apply"] = "physical-disposable"
    frozen["converted"] = converted
    return frozen


def apply_conversion(con, plan=None, *, writers_stopped=False, dest_dir=None, archives=None):
    """Freeze an inspect plan; optionally convert on disposable archives."""
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
    if path.exists():
        if archives is None:
            raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_EXISTS", path=str(path))
        try:
            frozen = json.loads(_read_private(path))
        except OSError as exc:
            raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_UNPROVEN", path=str(path)) from exc
        if frozen.get("seal") != seal or frozen.get("seal") != _seal(frozen):
            raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_UNPROVEN", path=str(path))
    else:
        try:
            _write_private(path, plan_json(frozen))
        except FileExistsError as exc:
            raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_EXISTS", path=str(path)) from exc
        except OSError as exc:
            raise PublicationRefused("PUBLICATION_MIGRATE_PLAN_UNPROVEN", path=str(path)) from exc
    frozen["plan_path"] = str(path)
    if archives:
        frozen = _convert_archives(con, frozen, {str(label): Path(root) for label, root in archives.items()})
    return frozen


def resume_conversion(con, plan_id, *, writers_stopped=False, dest_dir=None, archives=None):
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
    if archives:
        frozen = _convert_archives(con, frozen, {str(label): Path(root) for label, root in archives.items()})
    return frozen


def plan_json(plan):
    return json.dumps(plan, indent=2, sort_keys=True) + "\n"
