"""Canonical archive manifest selection.

This module is the single definition of which catalog files form one restorable archive
copy.  It is deliberately independent of fetch/execution so planning, reconciliation,
verification, and restore cannot drift into different definitions of completeness.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from modelark import formats, wishlist


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
    # Never reinterpret stored classifications on read: doing so could silently
    # broaden a frozen Fill. Explicit metadata reclassification below owns that
    # graph mutation. Refuse administrative names even in stale/misclassified rows.
    for item in files:
        if item["format"] in {"safetensors", "gguf", "pytorch", "aux"} and not (
            formats.is_upstream_payload_path(item["rfilename"])
        ):
            raise ArchivePolicyError(f"{repo_id}: not an upstream payload path: {item['rfilename']!r}")
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
        found_formats = sorted({str(item["format"] or "unknown") for item in files if item["format"] != "aux"})
        detail = f" (found: {', '.join(found_formats)})" if found_formats else ""
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


@dataclass(frozen=True)
class MetadataClassificationPreview:
    """Exact dormant catalog-only correction; never acquisition or conversion.

    ``files_before`` includes all manifest source fields, including unselected
    files, so a correction cannot silently adopt a changed upstream snapshot.
    ``manifest_changes`` exposes exact logical before/after file sets for review.
    """

    planner_revision: int
    repo_ids: tuple[str, ...]
    files_before: tuple[tuple, ...]
    changed_files: tuple[tuple[str, str], ...]
    manifest_changes: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]


def preview_metadata_classification(con, repo_ids: Sequence[str]) -> MetadataClassificationPreview:
    """Preview only exact upstream .gitignore/.gitattributes rows stored as other.

    No discovery, metadata refresh, archive row creation or implicit invocation.
    Supported hidden YAML/JSON/etc. already follow their existing classification.
    """
    unique = tuple(sorted(set(repo_ids)))
    revision = int(con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0])
    if not unique:
        return MetadataClassificationPreview(revision, (), (), (), ())
    placeholders = ",".join("?" for _ in unique)
    rows = tuple(tuple(row) for row in con.execute(
        "SELECT repo_id,rfilename,size_bytes,is_lfs,sha256,format,quant,quant_bits,safety "
        f"FROM files WHERE repo_id IN ({placeholders}) ORDER BY repo_id,rfilename", unique
    ).fetchall())
    changed = tuple((row[0], row[1]) for row in rows if row[5] == "other"
                    and formats.is_upstream_payload_path(row[1])
                    and row[1].rsplit("/", 1)[-1] in formats.UPSTREAM_CONTROL_BASENAMES)
    changed_set = set(changed)
    manifests = []
    for repo_id in sorted({repo for repo, _ in changed}):
        before = [(r[1], r[2], r[4], r[5], r[6]) for r in rows if r[0] == repo_id]
        after = [(name, size, sha, "aux" if (repo_id, name) in changed_set else fmt,
                  None if (repo_id, name) in changed_set else quant)
                 for name, size, sha, fmt, quant in before]
        # Recovery policy keeps inert pickle bytes eligible independently of the
        # current acquisition setting; no bytes are downloaded by this correction.
        manifests.append((repo_id,
                          tuple(f.rfilename for f in _select(repo_id, before, recovery_policy())),
                          tuple(f.rfilename for f in _select(repo_id, after, recovery_policy()))))
    return MetadataClassificationPreview(revision, unique, rows, changed, tuple(manifests))


def apply_metadata_classification(con, preview: MetadataClassificationPreview) -> tuple[tuple[str, str], ...]:
    """Apply a reviewed exact preview through the existing graph authority.

    Live Fill is refused by graph_write. Affected approvals are superseded, but
    their immutable files/tasks and paused session history are never rewritten.
    This helper stays dormant until explicitly invoked by a maintenance caller.
    """
    from modelark.proposal import GraphResult, Refusal, graph_write, require_publication_clear

    if not isinstance(preview, MetadataClassificationPreview):
        raise TypeError("expected MetadataClassificationPreview")

    def op(c):
        require_publication_clear(c)
        if preview_metadata_classification(c, preview.repo_ids) != preview:
            raise Refusal("METADATA_CLASSIFICATION_PREVIEW_STALE", None, ("preview_again",))
        if not preview.changed_files:
            return GraphResult(proven_noop=True, value=())
        for repo_id, name in preview.changed_files:
            c.execute("UPDATE files SET format='aux',quant=NULL,quant_bits=NULL,safety='safe' "
                      "WHERE repo_id=? AND rfilename=? AND format='other'", (repo_id, name))
        affected_repos = tuple(sorted({repo for repo, _ in preview.changed_files}))
        placeholders = ",".join("?" for _ in affected_repos)
        approvals = c.execute(
            "SELECT DISTINCT p.proposal_id FROM placement_proposals p "
            "JOIN proposal_tasks t USING(proposal_id) "
            f"WHERE p.lifecycle='approved' AND t.repo_id IN ({placeholders})", affected_repos
        ).fetchall()
        for (proposal_id,) in approvals:
            c.execute("UPDATE placement_proposals SET lifecycle='superseded', "
                      "superseded_at=CURRENT_TIMESTAMP WHERE proposal_id=?", (proposal_id,))
            c.execute("UPDATE planner_state SET active_approved_proposal_id=NULL "
                      "WHERE singleton_id=1 AND active_approved_proposal_id=?", (proposal_id,))
        return GraphResult(value=preview.changed_files)

    return graph_write(con, op).value
