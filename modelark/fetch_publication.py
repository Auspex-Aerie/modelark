"""Acquisition/replica entry adapters for the owning archive publisher.

Catalog routing is read-only and fail-closed. This module never upgrades a
catalog, invokes legacy annex probes, repairs rows, or cleans pending staging.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
from urllib.parse import quote

from modelark import archive_manifest, publication_store as store, register
from modelark.archive_publisher import ArchivePublisher, FileRequest
from modelark.publication_policy import PublicationRefused


def enabled(con):
    return store.library(con) is not None


def _pending(con, labels, *, kind, session_id, fencing_token):
    """Read-only retry routing; the reopened coordinator re-proves all authority."""
    labels = frozenset(labels)
    try:
        store.require_clear(con, labels, tree_change=True)
        return None
    except PublicationRefused as exc:
        if exc.code != "MAINTENANCE_REQUIRED":
            raise
        ids = exc.evidence.get("operation_ids", [])
        if len(ids) != 1:
            raise
    operation_id = ids[0]
    row = con.execute("SELECT binding_json,binding_digest,state FROM publication_operations WHERE operation_id=?",
                      [operation_id]).fetchone()
    if row is None or row[2] != "PREPARED":
        raise PublicationRefused("PUBLICATION_RESUME_OPERATION_REQUIRED")
    binding = store._unseal(row[0], row[1])
    participants = binding.get("participants", [])
    if (binding.get("kind") != kind or {item[0] for item in participants} != labels
            or any(item[5:] != [session_id, fencing_token] for item in participants)):
        raise PublicationRefused("PUBLICATION_RESUME_OWNER_OR_WORKSET_CHANGED", operation_id=operation_id)
    # A pending clone is a separate obligation, never waived by selecting its operation.
    clones = con.execute("SELECT d.drive_label FROM publication_clone_obligations c "
                         "LEFT JOIN drives d ON d.annex_uuid=c.annex_uuid WHERE c.state!='CLOSED'").fetchall()
    if any(label in labels for (label,) in clones):
        raise PublicationRefused("PUBLICATION_RESUME_CLONE_PENDING")
    try:
        requests = tuple(sorted(FileRequest(**{name: item[name] for name in
                                 ("repo_id", "rfilename", "drive_label", "source_drive")})
                                for item in binding["before_state"]["requests"].values()))
    except (KeyError, TypeError, ValueError) as exc:
        raise PublicationRefused("PUBLICATION_RESUME_WORKSET_CHANGED") from exc
    return operation_id, requests


def legacy_map_targets(map_root, targets):
    """Read-only exact routing check inside the already-held map/drive scope."""
    for label, destination in targets.items():
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", label):
            raise PublicationRefused("ARCHIVE_MAP_REMOTE_MISMATCH")
        try:
            configured = register._git(map_root, "config", "--local", "--get-all", f"remote.{label}.url")
            if (not configured or "\n" in configured or "\r" in configured
                    or not Path(configured).is_absolute()
                    or Path(configured).resolve(strict=True) != Path(destination).resolve(strict=True)):
                raise PublicationRefused("ARCHIVE_MAP_REMOTE_MISMATCH", drive_label=label)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            if isinstance(exc, PublicationRefused):
                raise
            raise PublicationRefused("ARCHIVE_MAP_REMOTE_MISMATCH", drive_label=label) from exc


def _catalog_path(con):
    paths = [row[2] for row in con.execute("PRAGMA database_list") if row[1] == "main"]
    if len(paths) != 1 or not paths[0] or not Path(paths[0]).is_absolute():
        raise PublicationRefused("PUBLICATION_CATALOG_IDENTITY_UNPROVEN")
    try:
        return Path(paths[0]).resolve(strict=True)
    except OSError as exc:
        raise PublicationRefused("PUBLICATION_CATALOG_IDENTITY_UNPROVEN") from exc


def _same_file(path, descriptor):
    try:
        retained, named = os.fstat(descriptor), path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(retained.st_mode) or not stat.S_ISREG(named.st_mode)
                or (retained.st_dev, retained.st_ino) != (named.st_dev, named.st_ino)):
            raise PublicationRefused("PUBLICATION_CATALOG_IDENTITY_CHANGED")
    except OSError as exc:
        raise PublicationRefused("PUBLICATION_CATALOG_IDENTITY_CHANGED") from exc


@contextmanager
def _connection(ctx):
    """Dedicated writer, same existing file; never take the UI lock across IO."""
    with ctx.lock:
        if ctx.con.in_transaction:
            raise PublicationRefused("PUBLICATION_CATALOG_TRANSACTION_ACTIVE")
        path = _catalog_path(ctx.con)
        identity = store.library(ctx.con)
        if identity is None:
            raise PublicationRefused("PUBLICATION_MIGRATION_REQUIRED")
        busy_ms = int(ctx.con.execute("PRAGMA busy_timeout").fetchone()[0])
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise PublicationRefused("PUBLICATION_CATALOG_IDENTITY_UNPROVEN") from exc
    con = None
    try:
        _same_file(path, descriptor)
        con = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=rw", uri=True, isolation_level=None)
        con.execute("PRAGMA foreign_keys=ON")
        con.execute(f"PRAGMA busy_timeout={busy_ms}")
        if _catalog_path(con) != path or store.library(con) != identity:
            raise PublicationRefused("PUBLICATION_CATALOG_IDENTITY_CHANGED")
        _same_file(path, descriptor)
        yield con
        _same_file(path, descriptor)
    except sqlite3.Error as exc:
        raise PublicationRefused("PUBLICATION_CATALOG_CONNECTION_FAILED") from exc
    finally:
        if con is not None:
            con.close()
        os.close(descriptor)


def fill_requests(ctx, repo_ids, drive_label, task_manifests):
    """Freeze only absent exact task entries before entering any physical scope."""
    with ctx.lock:
        pending = _pending(ctx.con, [drive_label], kind="fill", session_id=ctx.session_id,
                           fencing_token=ctx.fencing_token)
        requests, manifests = [], {}
        selected = set()
        for repo_id in repo_ids:
            manifest = (archive_manifest.manifest_for_repo(ctx.con, repo_id) if task_manifests is None
                        else task_manifests.get(repo_id))
            if manifest is None:
                raise PublicationRefused("PUBLICATION_TASK_MANIFEST_MISSING", repo_id=repo_id)
            manifests[repo_id] = tuple(manifest)
            selected.update(FileRequest(repo_id, item.rfilename, drive_label) for item in manifests[repo_id])
            present = {row[0] for row in ctx.con.execute(
                "SELECT rfilename FROM archived WHERE repo_id=? AND drive_label=?", [repo_id, drive_label])}
            requests.extend(FileRequest(repo_id, item.rfilename, drive_label)
                            for item in manifests[repo_id] if item.rfilename not in present)
        if pending is not None:
            saved = set(pending[1])
            if not saved <= selected or not set(requests) <= saved:
                raise PublicationRefused("PUBLICATION_RESUME_WORKSET_CHANGED", operation_id=pending[0])
            requests = list(pending[1])  # Includes already-published files still awaiting map/closure.
    return tuple(requests), manifests


@contextmanager
def scope(ctx, requests, *, kind="fill", destination=None, drive_label=None):
    if ctx._publication is not None:
        raise PublicationRefused("PUBLICATION_FETCH_SCOPE_ALREADY_ACTIVE")
    with _connection(ctx) as con:
        requests = tuple(sorted(requests))
        labels = {item.drive_label for item in requests} | {item.source_drive for item in requests
                                                          if item.source_drive is not None}
        pending = _pending(con, labels, kind=kind, session_id=ctx.session_id, fencing_token=ctx.fencing_token)
        if pending is not None and requests != pending[1]:
            raise PublicationRefused("PUBLICATION_RESUME_WORKSET_CHANGED", operation_id=pending[0])
        coordinator = (ArchivePublisher.resume(con, pending[0], session_id=ctx.session_id,
                                               fencing_token=ctx.fencing_token) if pending is not None else
                       ArchivePublisher(con, requests, kind=kind, session_id=ctx.session_id,
                                        fencing_token=ctx.fencing_token))
        with coordinator as owner:
            if destination is not None and owner._repositories[drive_label].tree.path != Path(destination).resolve():
                raise PublicationRefused("PUBLICATION_FETCH_DESTINATION_MISMATCH")
            ctx._publication = owner
            try:
                yield None
            finally:
                ctx._publication = None


def require_owner(ctx, destination, drive_label):
    owner = ctx._publication
    if type(owner) is not ArchivePublisher:
        raise PublicationRefused("PUBLICATION_FETCH_SCOPE_REQUIRED")
    with ctx.lock:
        if _catalog_path(owner._connection) != _catalog_path(ctx.con):
            raise PublicationRefused("PUBLICATION_CATALOG_IDENTITY_CHANGED")
    owner._require()
    if drive_label not in owner._repositories or owner._repositories[drive_label].tree.path != Path(destination).resolve():
        raise PublicationRefused("PUBLICATION_FETCH_DESTINATION_MISMATCH")
    return owner


def replica_tasks(ctx, tasks, result):
    """Exact absent-file replica groups; completion requires enclosing closure."""
    from modelark.publication_replica_pipeline import publish

    grouped = {}
    for task in tasks:
        grouped.setdefault((task.source_drive, task.target_drive), []).append(task)
    for (source, target), group in sorted(grouped.items(), key=lambda item: (item[0][1], item[0][0] or "")):
        try:
            if source is None:
                raise PublicationRefused("SOURCE_INCOMPLETE")
            with ctx.lock:
                pending = _pending(ctx.con, [source, target], kind="replica", session_id=ctx.session_id,
                                   fencing_token=ctx.fencing_token)
                requests = set()
                for task in group:
                    for name in task.budget.missing_files:
                        request = FileRequest(task.repo_id, name, target, source)
                        if (ctx.con.execute("SELECT 1 FROM archived WHERE repo_id=? AND rfilename=? AND drive_label=?",
                                            [task.repo_id, name, target]).fetchone()
                                and (pending is None or request not in pending[1])):
                            raise PublicationRefused("PUBLICATION_REPLICA_TARGET_REQUIRES_RECONCILIATION",
                                                     repo_id=task.repo_id, rfilename=name, drive_label=target)
                        requests.add(request)
            if not requests:
                continue
            with scope(ctx, tuple(sorted(requests)), kind="replica"):
                owner = ctx._publication
                progressed = set()
                for request in sorted(requests):
                    if ctx.should_stop():
                        raise PublicationRefused("PUBLICATION_ACQUISITION_STOPPED", operation_id=owner.operation_id)
                    publish(owner, request)
                    result["copied_files"] += 1
                    for task in group:
                        if task.repo_id == request.repo_id and request.rfilename in task.budget.missing_files:
                            progressed.add(task.requirement_id)
                    ctx.on_progress({"phase": "replica", "drive": target, "archive_changed": True,
                                     "say": f"    ✓ replica {target} file published"})
                result["progressed_requirements"].extend(sorted(progressed))
                owner.finish()
                result["completed_requirements"].extend(sorted(task.requirement_id for task in group))
                result["copied_targets"].append(target)
        except PublicationRefused as exc:
            result["failed"].append({"code": exc.code, "target": target, "evidence": exc.evidence,
                                     "requirements": [task.requirement_id for task in group]})
            ctx.on_progress({"phase": "fetch-blocked", "drive": target, "code": exc.code,
                             "say": f"🔴 {exc.code} — publication recovery required."})
            break  # Never start a second operation after leaving pending obligations.
    return result
