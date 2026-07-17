"""Restore archived models to their original Hugging Face directory layout.

Restore is deliberately catalog-driven: ``archived`` identifies the stored blob, its copies,
and the ingested original-byte hash, while ``files`` supplies any Hugging Face hash and dtype. A model is
materialized in a hidden sibling directory and published only after every planned file has
been retrieved, decompressed when necessary, and hash-verified.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from modelark import archive_hash, archive_manifest, compress, register


class RestoreError(RuntimeError):
    """The archive could not produce a complete, verified model tree."""


def _safe_relative(value: str, *, description: str) -> PurePosixPath:
    rel = PurePosixPath(value)
    if (not value or "\\" in value or rel.is_absolute()
            or PureWindowsPath(value).drive or ".." in rel.parts or "." in rel.parts
            or value != rel.as_posix()):
        raise RestoreError(f"unsafe {description}: {value!r}")
    return rel


def _stored_relative(row: dict) -> PurePosixPath:
    value = row["stored_relpath"] or str(
        PurePosixPath(row["rfilename"]).parent / (row["stored_name"] or "")
    )
    return _safe_relative(value, description="stored path")


def _run_annex(archive: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(archive), "annex", *args],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def _annex_content(archive: Path, row: dict, stored: Path) -> tuple[Path | None, bool, str]:
    """Return readable content, asking git-annex to retrieve a dropped blob when possible."""
    if stored.exists():
        return stored, False, "available"
    if not (archive / ".git").exists():
        return None, False, "recorded blob is missing (archive is not a git-annex checkout)"

    rel = stored.relative_to(archive).as_posix()
    result = _run_annex(archive, "get", "--", rel)
    retrieved = result.returncode == 0
    if stored.exists():
        return stored, retrieved, "retrieved with git-annex" if retrieved else "available"

    # A stale checkout can lack the work-tree link while its key is still recoverable.  Fetch
    # by key, then read git-annex's internal content path without fabricating its layout.
    key = row["annex_key"]
    if key:
        by_key = _run_annex(archive, "get", f"--key={key}")
        retrieved = retrieved or by_key.returncode == 0
        location = _run_annex(archive, "contentlocation", key)
        if location.returncode == 0 and location.stdout.strip():
            try:
                content_rel = _safe_relative(
                    location.stdout.strip(), description="git-annex content path"
                )
            except RestoreError as exc:
                return None, retrieved, str(exc)
            content = archive / Path(*content_rel.parts)
            if content.exists():
                return content, retrieved, "retrieved by git-annex key"
        result = by_key

    detail = (result.stderr or result.stdout).strip().replace("\n", " ")
    return None, retrieved, f"git-annex could not retrieve blob: {detail[:180] or 'content unavailable'}"


def _expected_hash(row: dict) -> str | None:
    return archive_hash.expected_sha256(
        catalog_sha=row["catalog_sha"],
        orig_sha256=row["orig_sha256"],
        compressed=bool(row["compressed"]),
        annex_key=row["annex_key"],
    )


def _materialize(source: Path, destination: Path, row: dict, expected: str) -> None:
    """Create and verify one file through a same-directory temporary, then atomically replace it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=str(destination.parent), prefix=destination.name + ".", suffix=".restore-tmp"
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        if row["compressed"]:
            try:
                compress.decompress_file(
                    source, temporary, dtype=compress.zipnn_dtype(row["quant"])
                )
            except Exception as exc:
                raise RestoreError(f"decompression failed: {exc}") from exc
        else:
            shutil.copyfile(source, temporary)
        with temporary.open("rb") as restored:
            os.fsync(restored.fileno())
        actual = compress.sha256_file(temporary)
        if actual.lower() != expected:
            raise RestoreError(
                f"sha256 mismatch after restore (expected {expected}, got {actual})"
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _rows(con, repo_id: str) -> dict[str, list[dict]]:
    columns = [
        "rfilename", "stored_name", "stored_relpath", "drive_label", "orig_sha256",
        "compressed", "annex_key", "catalog_sha", "quant",
    ]
    grouped: dict[str, list[dict]] = {}
    for values in con.execute(
        "SELECT a.rfilename,a.stored_name,a.stored_relpath,a.drive_label,a.orig_sha256,"
        "a.compressed,a.annex_key,f.sha256,f.quant FROM archived a LEFT JOIN files f "
        "ON f.repo_id=a.repo_id AND f.rfilename=a.rfilename "
        "WHERE a.repo_id=? ORDER BY a.rfilename,a.drive_label",
        [repo_id],
    ).fetchall():
        row = dict(zip(columns, values))
        grouped.setdefault(row["rfilename"], []).append(row)
    return grouped


def restore_repo(con, repo_id: str, output_root: str | Path) -> dict:
    """Restore one repo below ``output_root`` and return an operator-facing summary.

    The final path is ``<output_root>/<org>/<model>``. Existing destinations are never
    overwritten. On failure no final model directory is published.
    """
    repo_rel = _safe_relative(repo_id, description="repository id")
    copies = _rows(con, repo_id)
    if not copies:
        raise RestoreError(f"{repo_id}: no archived files recorded")
    # Acquisition policy cannot strand bytes already accepted into the archive. Pickle
    # remains inert here: restore copies/decompresses and hashes it, but never imports it.
    # A legacy/foreign archive may contain formats the current acquisition planner does
    # not support; in that case its durable archive records are the recovery manifest.
    try:
        planned = [
            item.rfilename
            for item in archive_manifest.manifest_for_repo(
                con, repo_id, archive_manifest.recovery_policy()
            )
        ]
    except archive_manifest.ArchivePolicyError:
        planned = sorted(copies)
    if not planned:
        raise RestoreError(f"{repo_id}: catalog has no restorable planned files")
    missing = sorted(set(planned) - set(copies))
    if missing:
        raise RestoreError(
            f"{repo_id}: archive is incomplete; no recorded copy for {', '.join(missing)}"
        )

    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / Path(*repo_rel.parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = destination.parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise RestoreError(
            f"{repo_id}: destination parent escapes output root: {resolved_parent}"
        ) from exc
    destination = resolved_parent / destination.name
    if destination.exists() or destination.is_symlink():
        raise RestoreError(f"{repo_id}: destination already exists: {destination}")

    stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.restore-", dir=destination.parent))
    restored: list[dict] = []
    warnings: list[dict] = []
    retrievals = 0
    try:
        for rfilename in planned:
            output_rel = _safe_relative(rfilename, description="Hugging Face file path")
            attempts: list[str] = []
            completed = False
            for row in copies[rfilename]:
                drive = row["drive_label"]
                archive = register.archive_path(con, drive)
                if archive is None:
                    attempts.append(f"{drive}: offline/not mounted")
                    continue
                try:
                    stored_rel = _stored_relative(row)
                except RestoreError as exc:
                    attempts.append(f"{drive}: {exc}")
                    continue
                stored = Path(archive) / Path(*repo_rel.parts) / Path(*stored_rel.parts)
                source, retrieved, source_detail = _annex_content(Path(archive), row, stored)
                if source is None:
                    attempts.append(f"{drive}: {source_detail}")
                    continue
                retrievals += int(retrieved)
                expected = _expected_hash(row)
                if expected is None:
                    attempts.append(f"{drive}: no original-byte sha256 available")
                    continue
                try:
                    _materialize(source, stage / Path(*output_rel.parts), row, expected)
                except (OSError, RuntimeError) as exc:
                    attempts.append(f"{drive}: {exc}")
                    continue
                restored.append(
                    {"file": rfilename, "drive": drive, "retrieved": retrieved,
                     "compressed": bool(row["compressed"]), "sha256": expected}
                )
                warnings.extend({"file": rfilename, "detail": detail} for detail in attempts)
                completed = True
                break
            if not completed:
                raise RestoreError(
                    f"{repo_id}/{rfilename}: no readable, verified copy; " + "; ".join(attempts)
                )
        os.replace(stage, destination)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return {
        "repo": repo_id,
        "path": str(destination),
        "files": restored,
        "n_files": len(restored),
        "annex_retrievals": retrievals,
        "warnings": warnings,
    }
