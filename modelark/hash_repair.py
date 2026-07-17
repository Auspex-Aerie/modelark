"""Repair missing original-file hashes in legacy archive records.

Older ModelArk builds recorded ``archived.orig_sha256`` only when Hugging Face supplied a
canonical digest. Ordinary Git-tracked files often have no Hub sha256, so otherwise complete
archives can fail closed during restore. This module repairs only evidence that can be recovered
without trusting the mutable work tree: a raw regular file must be byte-identical to the blob at
the same path in the archive's ``HEAD`` commit before its sha256 is eligible for backfill.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Callable, Iterable

from modelark import archive_hash, register


class HashRepairError(RuntimeError):
    """Legacy hash evidence could not be repaired safely."""


def _safe_relative(value: str, description: str) -> PurePosixPath:
    rel = PurePosixPath(value)
    if (not value or "\\" in value or rel.is_absolute() or PureWindowsPath(value).drive
            or ".." in rel.parts or "." in rel.parts or value != rel.as_posix()):
        raise HashRepairError(f"unsafe {description}: {value!r}")
    return rel


def _stored_relative(row: dict) -> PurePosixPath:
    value = row["stored_relpath"] or str(
        PurePosixPath(row["rfilename"]).parent / (row["stored_name"] or "")
    )
    return _safe_relative(value, "stored path")


def _expected_hash(row: dict) -> str | None:
    return archive_hash.expected_sha256(
        catalog_sha=row["catalog_sha"],
        orig_sha256=row["orig_sha256"],
        compressed=bool(row["compressed"]),
        annex_key=row["annex_key"],
    )


def _git_head_blob(archive: Path, relative: PurePosixPath) -> tuple[str, str]:
    """Return ``(mode, oid)`` for one exact regular path in the archive's HEAD tree."""
    try:
        result = subprocess.run(
            ["git", "-C", str(archive), "ls-tree", "-z", "HEAD", "--", relative.as_posix()],
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise HashRepairError("git is not installed") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip().replace("\n", " ")
        raise HashRepairError(f"cannot read archive HEAD: {detail[:180] or 'git ls-tree failed'}")
    entries = [entry for entry in result.stdout.split(b"\0") if entry]
    if len(entries) != 1 or b"\t" not in entries[0]:
        raise HashRepairError("path is not tracked by the archive HEAD commit")
    metadata, raw_path = entries[0].split(b"\t", 1)
    try:
        mode, object_type, oid = metadata.decode("ascii").split()
        tracked_path = raw_path.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise HashRepairError("archive HEAD returned malformed path metadata") from exc
    if tracked_path != relative.as_posix():
        raise HashRepairError("archive HEAD did not resolve the exact stored path")
    if object_type != "blob" or mode not in ("100644", "100755"):
        raise HashRepairError(f"archive HEAD path is not a regular file (mode={mode}, type={object_type})")
    if len(oid) not in (40, 64) or not all(c in "0123456789abcdef" for c in oid):
        raise HashRepairError(f"archive HEAD returned an unsupported object id: {oid!r}")
    return mode, oid


def _object_hasher(oid: str):
    if len(oid) == 64:
        return hashlib.sha256()
    try:
        return hashlib.sha1(usedforsecurity=False)
    except TypeError:  # pragma: no cover - compatibility with older OpenSSL/Python builds
        return hashlib.sha1()


def _hash_regular_file(path: Path, head_oid: str) -> tuple[str, str, int]:
    """Hash one stable regular-file descriptor as both a Git blob and sha256."""
    if path.is_symlink():
        raise HashRepairError("stored path is a symlink, not a Git-tracked regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise HashRepairError(f"cannot open stored file: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise HashRepairError("stored path is not a regular file")
        git_digest = _object_hasher(head_oid)
        git_digest.update(f"blob {before.st_size}\0".encode("ascii"))
        sha256 = hashlib.sha256()
        read_bytes = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            read_bytes += len(chunk)
            git_digest.update(chunk)
            sha256.update(chunk)
        after = os.fstat(fd)
        if (read_bytes != before.st_size or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns):
            raise HashRepairError("stored file changed while it was being validated")
        return git_digest.hexdigest(), sha256.hexdigest(), read_bytes
    finally:
        os.close(fd)


def _rows(con, repo_ids: Iterable[str] | None) -> list[dict]:
    params = list(dict.fromkeys(repo_ids or ()))
    where = ""
    if params:
        where = f"WHERE a.repo_id IN ({', '.join('?' for _ in params)})"
    columns = (
        "repo_id", "rfilename", "stored_name", "stored_relpath", "drive_label",
        "orig_sha256", "orig_bytes", "stored_bytes", "compressed", "annex_key",
        "catalog_sha", "catalog_bytes",
    )
    values = con.execute(
        "SELECT a.repo_id,a.rfilename,a.stored_name,a.stored_relpath,a.drive_label,"
        "a.orig_sha256,a.orig_bytes,a.stored_bytes,a.compressed,a.annex_key,"
        f"f.sha256,f.size_bytes FROM archived a JOIN files f USING(repo_id,rfilename) {where} "
        "ORDER BY a.repo_id,a.rfilename,a.drive_label",
        params,
    ).fetchall()
    return [dict(zip(columns, row)) for row in values]


def _diagnostic(row: dict, code: str, detail: str) -> dict:
    return {
        "repo_id": row["repo_id"],
        "rfilename": row["rfilename"],
        "drive_label": row["drive_label"],
        "code": code,
        "detail": detail,
    }


def _validate_candidate(row: dict, archive: Path) -> dict:
    if row["compressed"]:
        raise HashRepairError(
            "stored copy is compressed; its original digest cannot be proven from the committed blob"
        )
    repo_rel = _safe_relative(row["repo_id"], "repository id")
    original_rel = _safe_relative(row["rfilename"], "original file path")
    stored_rel = _stored_relative(row)
    if stored_rel != original_rel:
        raise HashRepairError(
            f"raw stored path {stored_rel.as_posix()!r} differs from original path "
            f"{original_rel.as_posix()!r}"
        )
    archive_rel = repo_rel / stored_rel
    stored = archive / Path(*archive_rel.parts)
    try:
        archive_root = archive.resolve(strict=True)
        stored.resolve(strict=True).relative_to(archive_root)
    except (OSError, ValueError) as exc:
        raise HashRepairError("stored path is missing or escapes the mounted archive") from exc
    _, head_oid = _git_head_blob(archive, archive_rel)
    git_oid, digest, size = _hash_regular_file(stored, head_oid)
    if git_oid != head_oid:
        raise HashRepairError(
            f"stored bytes do not match archive HEAD (expected Git object {head_oid}, got {git_oid})"
        )
    for label in ("catalog_bytes", "orig_bytes", "stored_bytes"):
        expected_size = row[label]
        if expected_size is not None and int(expected_size) != size:
            raise HashRepairError(
                f"stored size {size} disagrees with {label}={int(expected_size)}"
            )
    return {
        "repo_id": row["repo_id"],
        "rfilename": row["rfilename"],
        "drive_label": row["drive_label"],
        "sha256": digest,
        "bytes": size,
        "evidence": "archive-head-blob",
    }


def audit_hashes(
    con,
    repo_ids: Iterable[str] | None = None,
    *,
    archive_resolver: Callable[[object, str], Path | None] | None = None,
) -> dict:
    """Return a read-only legacy-hash repair plan and its fail-closed diagnostics."""
    scope = list(dict.fromkeys(repo_ids or ()))
    resolver = archive_resolver or register.archive_path
    rows = _rows(con, scope)
    diagnostics: list[dict] = []
    repairs: list[dict] = []
    if scope:
        found = {row["repo_id"] for row in rows}
        for missing_repo in sorted(set(scope) - found):
            diagnostics.append({
                "repo_id": missing_repo, "rfilename": None, "drive_label": None,
                "code": "NO_ARCHIVED_ROWS", "detail": "repository has no archived file records",
            })

    known: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        digest = _expected_hash(row)
        if digest:
            known.setdefault((row["repo_id"], row["rfilename"]), set()).add(digest)

    candidates = [row for row in rows if _expected_hash(row) is None]
    archives: dict[str, Path | None] = {}
    for row in candidates:
        drive_label = row["drive_label"]
        if drive_label not in archives:
            resolved = resolver(con, drive_label)
            archives[drive_label] = Path(resolved) if resolved is not None else None
        archive = archives[drive_label]
        if archive is None:
            diagnostics.append(_diagnostic(
                row, "DRIVE_UNAVAILABLE", "recorded drive is offline or not mounted"
            ))
            continue
        try:
            repair = _validate_candidate(row, Path(archive))
        except HashRepairError as exc:
            diagnostics.append(_diagnostic(row, "UNPROVEN_BYTES", str(exc)))
            continue
        existing = known.get((row["repo_id"], row["rfilename"]), set())
        if existing and existing != {repair["sha256"]}:
            diagnostics.append(_diagnostic(
                row, "HASH_CONFLICT",
                f"Git-proven sha256 {repair['sha256']} conflicts with existing evidence "
                f"{', '.join(sorted(existing))}",
            ))
            continue
        repairs.append(repair)

    # Two independently committed copies of one logical original must agree before either is
    # trusted. Remove the whole conflicting group so an apply can never partially bless it.
    by_file: dict[tuple[str, str], list[dict]] = {}
    for repair in repairs:
        by_file.setdefault((repair["repo_id"], repair["rfilename"]), []).append(repair)
    conflicts = {key for key, items in by_file.items() if len({i["sha256"] for i in items}) > 1}
    if conflicts:
        kept = []
        for repair in repairs:
            key = (repair["repo_id"], repair["rfilename"])
            if key in conflicts:
                diagnostics.append(_diagnostic(
                    repair, "HASH_CONFLICT", "Git-proven archive copies disagree on sha256"
                ))
            else:
                kept.append(repair)
        repairs = kept

    return {
        "mode": "dry-run",
        "scope": scope,
        "archived_rows": len(rows),
        "already_verifiable": len(rows) - len(candidates),
        "missing_evidence": len(candidates),
        "repairs": repairs,
        "errors": diagnostics,
        "applied": 0,
        "backup": None,
    }


def _consistent_backup(con) -> Path:
    database_rows = con.execute("PRAGMA database_list").fetchall()
    main = next((row[2] for row in database_rows if row[1] == "main"), "")
    if not main:
        raise HashRepairError("cannot back up a transient or in-memory catalog")
    source = Path(main).expanduser().resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = source.with_name(f"{source.name}.pre-hash-repair-{stamp}.bak")
    temporary = source.with_name(f".{source.name}.hash-repair-{stamp}.tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except OSError as exc:
        raise HashRepairError(f"cannot reserve catalog-backup staging file: {exc}") from exc
    backup = None
    try:
        backup = sqlite3.connect(str(temporary), isolation_level=None)
        con.backup(backup)
        result = backup.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise HashRepairError(f"catalog backup failed integrity_check: {result}")
        violations = backup.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise HashRepairError(
                f"catalog backup has foreign-key violations: {violations[:12]}"
            )
        backup.close()
        backup = None
        # A hard-link publish is atomic and refuses an existing destination. A crash can leave only
        # the dot-prefixed staging file, never a plausible-looking partial ``.bak``.
        os.link(temporary, destination)
        temporary.unlink()
        directory_fd = os.open(source.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if backup is not None:
            backup.close()
            backup = None
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if backup is not None:
            backup.close()
    return destination


def repair_hashes(
    con,
    repo_ids: Iterable[str] | None = None,
    *,
    apply: bool = False,
    archive_resolver: Callable[[object, str], Path | None] | None = None,
) -> dict:
    """Audit or atomically apply a legacy original-hash repair.

    Dry-run is the default. Applying is all-or-nothing, refuses every unresolved candidate, creates
    a consistent SQLite backup before the first update, and never overwrites existing evidence.
    """
    scope = list(dict.fromkeys(repo_ids or ()))
    report = audit_hashes(con, scope, archive_resolver=archive_resolver)
    if not apply:
        return report
    if report["errors"]:
        first = report["errors"][0]
        raise HashRepairError(
            f"refusing hash repair: {len(report['errors'])} candidate(s) lack provable evidence; "
            f"first is {first['repo_id']}/{first.get('rfilename') or '?'} "
            f"[{first['code']}]: {first['detail']} (run without --apply for the full audit)"
        )
    if not report["repairs"]:
        return {**report, "mode": "apply"}
    if getattr(con, "in_transaction", False):
        raise HashRepairError("hash repair requires a connection with no active transaction")

    backup = _consistent_backup(con)
    con.execute("BEGIN IMMEDIATE")
    applied = 0
    try:
        # Re-read both durable metadata and Git/work-tree evidence after the backup while holding
        # the catalog write lock. A stale audit can therefore never become write authority.
        rechecked = audit_hashes(con, scope, archive_resolver=archive_resolver)
        expected = [
            (r["repo_id"], r["rfilename"], r["drive_label"], r["sha256"])
            for r in report["repairs"]
        ]
        observed = [
            (r["repo_id"], r["rfilename"], r["drive_label"], r["sha256"])
            for r in rechecked["repairs"]
        ]
        if rechecked["errors"] or observed != expected:
            raise HashRepairError(
                f"repair evidence changed after backup; no catalog rows updated (backup: {backup})"
            )
        for repair in report["repairs"]:
            cursor = con.execute(
                "UPDATE archived SET orig_sha256=? "
                "WHERE repo_id=? AND rfilename=? AND drive_label=? AND orig_sha256 IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM files f WHERE f.repo_id=archived.repo_id "
                "AND f.rfilename=archived.rfilename AND f.sha256 IS NOT NULL)",
                [repair["sha256"], repair["repo_id"], repair["rfilename"], repair["drive_label"]],
            )
            if cursor.rowcount != 1:
                raise HashRepairError(
                    f"catalog evidence changed for {repair['repo_id']}/{repair['rfilename']} "
                    f"on {repair['drive_label']}"
                )
            applied += 1
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return {**report, "mode": "apply", "applied": applied, "backup": str(backup)}
