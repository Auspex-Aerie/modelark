"""Shared pure budget seam for capacity planning and #36a candidate construction (RFC-002 / DEC-049).

One budget truth. Both the legacy ``capacity`` tiered_v1 path and the canonical ``candidates`` core
compute per-file durable/workspace budgets here, so they cannot drift. Pure: no SQLite, filesystem,
configuration, clock, or network access — graph-affecting compression config and the observed float
ratio are passed in as data by the impure shell (they are never read from ``wishlist`` here).
"""
from __future__ import annotations

from dataclasses import dataclass

from modelark import archive_manifest, compress, streamznn

# The estimate margin lives here (the single owner) and is re-exported by ``capacity`` for compatibility.
EXPECTED_MARGIN = 1.08


@dataclass(frozen=True)
class FileBudget:
    rfilename: str
    guaranteed_durable: int
    expected_durable: int
    workspace_peak_guaranteed: int
    workspace_peak_expected: int
    evidence: str

    def durable_for(self, guaranteed: bool) -> int:
        return self.guaranteed_durable if guaranteed else self.expected_durable

    def workspace_for(self, guaranteed: bool) -> int:
        return self.workspace_peak_guaranteed if guaranteed else self.workspace_peak_expected


@dataclass(frozen=True)
class CandidateBudget:
    guaranteed_durable: int
    expected_durable: int
    workspace_peak_guaranteed: int
    workspace_peak_expected: int
    file_budgets: tuple[FileBudget, ...]


def expected_durable_bytes(item: archive_manifest.ManifestFile, ratio: float) -> int:
    """Estimate-mode durable bytes: compressible float shards shrink by ``ratio``; all inflate by the
    versioned safety margin. Identical to the legacy ``capacity._expected_file_bytes``."""
    basis = item.size_bytes * ratio if item.storage_action == "compress" else item.size_bytes
    return int(basis * EXPECTED_MARGIN)


def file_budget(
    item: archive_manifest.ManifestFile,
    ratio: float,
    compression_cfg,
    *,
    exact_source_bytes: int | None = None,
    replica: bool = False,
) -> FileBudget:
    """One per-file budget for every mode:

    * download (default): guaranteed = raw size, expected = ratio/margin estimate, workspace = the
      codec's peak output cap chosen from the injected ``compression_cfg`` (direct ``cfg[...]`` access —
      no invented defaults);
    * replica with a proven exact source (``exact_source_bytes``): a copy transfers exactly the stored
      bytes, so every field is that size and evidence is ``exact``;
    * replica estimate (``replica=True``, source pending): every field is the ratio/margin estimate.
    """
    if exact_source_bytes is not None:
        value = int(exact_source_bytes)
        return FileBudget(item.rfilename, value, value, value, value, "exact")
    if replica:
        value = expected_durable_bytes(item, ratio)
        return FileBudget(item.rfilename, value, value, value, value, "estimate")

    expected = expected_durable_bytes(item, ratio)
    workspace_g = workspace_e = 0
    if item.storage_action == "compress":
        codec = compress.plan_codec(item.size_bytes, dict(compression_cfg))
        if codec != compress.CODEC_RAW:
            output_cap = compress.codec_output_cap(
                item.size_bytes, codec, stream_chunk_bytes=streamznn.DEFAULT_CHUNK
            )
            workspace_g = output_cap
            workspace_e = max(0, item.size_bytes + output_cap - expected)
    return FileBudget(item.rfilename, item.size_bytes, expected, workspace_g, workspace_e, "estimate")


def aggregate(file_budgets) -> CandidateBudget:
    """Roll per-file budgets into a task/candidate budget: durable sums, workspace peaks max."""
    budgets = tuple(file_budgets)
    return CandidateBudget(
        guaranteed_durable=sum(item.guaranteed_durable for item in budgets),
        expected_durable=sum(item.expected_durable for item in budgets),
        workspace_peak_guaranteed=max((item.workspace_peak_guaranteed for item in budgets), default=0),
        workspace_peak_expected=max((item.workspace_peak_expected for item in budgets), default=0),
        file_budgets=budgets,
    )
