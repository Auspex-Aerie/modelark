"""Frozen ExecutionConfig + graph-affecting config hash (PR-09 / B7). Schema v6 binds hash."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from modelark.proposal import Refusal

# Keys that affect execution semantics (included in hash).
_GRAPH_AFFECTING_KEYS = frozenset({
    "capacity_mode", "policy_version", "solver_version",
    "compression", "numcopies_default",
})

# Literal operational defaults for transport keys that may be omitted from the
# graph-affecting freeze payload. Never sourced from a live wishlist reread.
_COMPRESSION_TRANSPORT_DEFAULTS = {
    "max_compress_ram_gb": 4.0,
    "stream_compress": True,
    "threads": 1,
}


def _canonical_graph_affecting(values: Mapping[str, Any]) -> dict:
    out = {}
    for k in sorted(_GRAPH_AFFECTING_KEYS):
        if k in values:
            out[k] = values[k]
    return out


def hash_config(values: Mapping[str, Any] | None) -> str:
    """Deterministic SHA-256 of graph-affecting configuration only."""
    payload = _canonical_graph_affecting(values or {})
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


execution_config_hash = hash_config
canonical_execution_config_hash = hash_config


@dataclass(frozen=True)
class ExecutionConfig:
    values: Mapping[str, Any]
    canonical_hash: str

    @classmethod
    def from_values(cls, values: Mapping[str, Any]) -> "ExecutionConfig":
        v = _canonical_graph_affecting(values)
        return cls(values=v, canonical_hash=hash_config(v))


def freeze_execution_config(values: Mapping[str, Any]) -> ExecutionConfig:
    return ExecutionConfig.from_values(values)


def mark_proposal_pre_pr09_unbound(con, proposal_id: str) -> None:
    """Tests/production helper: strip config binding so start must refuse.

    semantic_input_hash is CHECK-constrained to NULL or 64 hex — clear to NULL.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(placement_proposals)").fetchall()}
    if "execution_config_hash" in cols:
        con.execute(
            "UPDATE placement_proposals SET execution_config_hash=NULL WHERE proposal_id=?",
            [proposal_id])
    con.execute(
        "UPDATE placement_proposals SET semantic_input_hash=NULL WHERE proposal_id=?",
        [proposal_id])


strip_execution_config_binding_for_test = mark_proposal_pre_pr09_unbound


def require_bound_execution_config(con, proposal_id: str) -> str:
    """Return stored config hash or raise APPROVED_INPUT_CHANGED if unbound."""
    row = con.execute(
        "SELECT semantic_input_hash FROM placement_proposals WHERE proposal_id=?",
        [proposal_id]).fetchone()
    if not row or not row[0] or row[0] == "UNBOUND_PRE_PR09" or len(str(row[0])) != 64:
        raise Refusal(
            "APPROVED_INPUT_CHANGED",
            {"reason": "execution_config_unbound", "proposal_id": proposal_id},
            ("preview_again",))
    # semantic_input_hash may be full semantic; config hash is last 64 if composite,
    # or equal to hash when we store pure config digests at approve time.
    return str(row[0])


def assert_frozen_unchanged(frozen: Any, reader) -> None:
    """Refuse hostile global reread that would replace freeze."""
    current = reader.read_graph_affecting_config()
    cur_hash = hash_config(current)
    frozen_hash = getattr(frozen, "canonical_hash", None)
    if frozen_hash is None and isinstance(frozen, Mapping):
        frozen_hash = frozen.get("canonical_hash")
    if cur_hash != frozen_hash:
        raise Refusal(
            "APPROVED_INPUT_CHANGED",
            {"reason": "global_config_reread", "frozen": frozen_hash, "current": cur_hash},
            ("preview_again",))


def refresh_against_global(session_start_or_frozen, services) -> None:
    frozen = getattr(session_start_or_frozen, "execution_config", None) or session_start_or_frozen
    assert_frozen_unchanged(frozen, services.config)


def get_frozen_execution_config(session_start) -> ExecutionConfig | Any:
    return getattr(session_start, "execution_config", None) or session_start
