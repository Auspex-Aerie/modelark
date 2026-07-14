"""Canonical archive manifest selection.

This module is the single definition of which catalog files form one restorable archive
copy.  It is deliberately independent of fetch/execution so planning, reconciliation,
verification, and restore cannot drift into different definitions of completeness.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from modelark import wishlist


FLOAT_QUANTS = frozenset(
    {None, "bf16", "bfloat16", "fp16", "f16", "float16", "fp32", "f32", "float32"}
)


class ArchivePolicyError(RuntimeError):
    """A repository cannot produce an archive manifest under the selected policy."""


@dataclass(frozen=True)
class ArchivePolicy:
    """Policy choices that affect file acquisition, separate from storage execution."""

    allow_pickle: bool


@dataclass(frozen=True)
class ManifestFile:
    rfilename: str
    size_bytes: int
    sha256: str | None
    format: str
    quant: str | None
    storage_action: str  # "compress" or "raw"; kept string-compatible with fetch records

    def as_fetch_record(self) -> dict:
        """Compatibility shape consumed by the existing fetch pipeline."""
        return {
            "rfilename": self.rfilename,
            "size": self.size_bytes,
            "sha256": self.sha256,
            "fmt": self.format,
            "quant": self.quant,
            "mode": self.storage_action,
        }


@dataclass(frozen=True)
class ManifestBatch:
    manifests: Mapping[str, tuple[ManifestFile, ...]]
    errors: Mapping[str, ArchivePolicyError]


def acquisition_policy(*, allow_pickle: bool | None = None) -> ArchivePolicy:
    """Current acquisition policy, with an explicit override for recovery/tests."""
    if allow_pickle is None:
        allow_pickle = not wishlist.exclude_pickle_only()
    return ArchivePolicy(allow_pickle=bool(allow_pickle))


def recovery_policy() -> ArchivePolicy:
    """Recovery may copy inert pickle bytes already accepted into the archive."""
    return ArchivePolicy(allow_pickle=True)


def _select(repo_id: str, rows: Iterable[tuple], policy: ArchivePolicy) -> tuple[ManifestFile, ...]:
    files = [
        {
            "rfilename": row[0],
            "size": int(row[1] or 0),
            "sha256": row[2],
            "format": row[3],
            "quant": row[4],
        }
        for row in rows
    ]
    safetensors = [item for item in files if item["format"] == "safetensors"]
    gguf = [item for item in files if item["format"] == "gguf"]
    pickle = [item for item in files if item["format"] == "pytorch"]

    if safetensors:
        selected_weights = safetensors
    elif gguf:
        selected_weights = gguf
    elif pickle:
        if not policy.allow_pickle:
            raise ArchivePolicyError(
                f"{repo_id}: pickle-only weights are blocked by exclude.pickle_only=true; "
                "select a safetensors/GGUF repository or explicitly opt in to inert pickle storage"
            )
        selected_weights = pickle
    else:
        formats = sorted({str(item["format"] or "unknown") for item in files if item["format"] != "aux"})
        detail = f" (found: {', '.join(formats)})" if formats else ""
        raise ArchivePolicyError(
            f"{repo_id}: no supported archive weights; expected safetensors, GGUF, or opted-in pickle"
            + detail
        )

    selected_names = {item["rfilename"] for item in selected_weights}
    manifest = []
    for item in files:
        fmt = item["format"]
        if item["rfilename"] in selected_names and fmt == "safetensors":
            action = "compress" if item["quant"] in FLOAT_QUANTS else "raw"
        elif item["rfilename"] in selected_names:
            action = "raw"
        elif fmt == "aux":
            action = "raw"
        else:
            continue
        manifest.append(
            ManifestFile(
                rfilename=item["rfilename"],
                size_bytes=item["size"],
                sha256=item["sha256"],
                format=fmt,
                quant=item["quant"],
                storage_action=action,
            )
        )
    return tuple(sorted(manifest, key=lambda item: item.rfilename))


def inspect_manifests_for_repos(
    con,
    repo_ids: Sequence[str],
    policy: ArchivePolicy | None = None,
) -> ManifestBatch:
    """Bulk-load and classify manifests, retaining per-repository policy errors."""
    unique = tuple(sorted(set(repo_ids)))
    if not unique:
        return ManifestBatch(manifests={}, errors={})
    policy = policy or acquisition_policy()
    placeholders = ",".join("?" for _ in unique)
    rows = con.execute(
        "SELECT repo_id,rfilename,size_bytes,sha256,format,quant FROM files "
        f"WHERE repo_id IN ({placeholders}) ORDER BY repo_id,rfilename",
        list(unique),
    ).fetchall()
    grouped: dict[str, list[tuple]] = {repo_id: [] for repo_id in unique}
    for row in rows:
        grouped[row[0]].append(tuple(row[1:]))

    manifests: dict[str, tuple[ManifestFile, ...]] = {}
    errors: dict[str, ArchivePolicyError] = {}
    for repo_id in unique:
        try:
            manifests[repo_id] = _select(repo_id, grouped[repo_id], policy)
        except ArchivePolicyError as exc:
            errors[repo_id] = exc
    return ManifestBatch(manifests=manifests, errors=errors)


def manifests_for_repos(
    con,
    repo_ids: Sequence[str],
    policy: ArchivePolicy | None = None,
) -> dict[str, tuple[ManifestFile, ...]]:
    """Return canonical manifests, failing closed on the first ineligible repository."""
    batch = inspect_manifests_for_repos(con, repo_ids, policy)
    if batch.errors:
        first = sorted(batch.errors)[0]
        raise batch.errors[first]
    return dict(batch.manifests)


def manifest_for_repo(
    con,
    repo_id: str,
    policy: ArchivePolicy | None = None,
) -> tuple[ManifestFile, ...]:
    return manifests_for_repos(con, [repo_id], policy)[repo_id]
