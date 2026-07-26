"""Pure proposal canonical serializer (RFC-002 / PR-08 A5).

No SQLite, filesystem, clock, network, or config access. Shared by preview, commit
validation, audit, and golden vectors. Mutable lifecycle/audit fields and proposal_id
are deliberately excluded from the hash (RFC-002 / finding 36).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

SERIALIZER_VERSION = "1"

_HEADER_SKIP = frozenset({
    "lifecycle", "created_at", "approved_at", "superseded_at", "proposal_id",
})


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def canonical_bytes(header: Mapping[str, Any], tasks: Sequence[Mapping],
                    files: Sequence[Mapping]) -> bytes:
    """Deterministic UTF-8 JSON bytes for SHA-256 (sort_keys, compact separators)."""
    body = {
        "header": {k: _jsonable(header[k]) for k in sorted(header) if k not in _HEADER_SKIP},
        "tasks": sorted(
            (_jsonable(dict(t)) for t in tasks),
            key=lambda t: (t.get("requirement_id") or "", t.get("order_key") or 0),
        ),
        "files": sorted(
            (_jsonable(dict(f)) for f in files),
            key=lambda f: (f.get("requirement_id") or "", f.get("rfilename") or ""),
        ),
    }
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def serialize_proposal(header: Mapping[str, Any], tasks: Sequence[Mapping],
                       files: Sequence[Mapping]) -> bytes:
    return canonical_bytes(header, tasks, files)


def proposal_hash(header: Mapping[str, Any], tasks: Sequence[Mapping] = (),
                  files: Sequence[Mapping] = ()) -> str:
    return hashlib.sha256(canonical_bytes(header, tasks, files)).hexdigest()


hash_proposal = proposal_hash


def baseline_satisfaction_certificate(
    *,
    requirement_id: str,
    full_manifest_hash: str,
    drive_label: str,
    identity_epoch: int,
    identity_fingerprint: str,
    files: Sequence[Mapping[str, Any]],
) -> str:
    """A10: bind requirement, full-manifest hash, drive identity/epoch, per-file durable evidence."""
    payload = {
        "requirement_id": requirement_id,
        "full_manifest_hash": full_manifest_hash,
        "drive_label": drive_label,
        "identity_epoch": int(identity_epoch),
        "identity_fingerprint": identity_fingerprint,
        "files": sorted(
            (
                {
                    "rfilename": f.get("rfilename"),
                    "orig_sha256": f.get("orig_sha256"),
                    "orig_bytes": f.get("orig_bytes"),
                    "annex_key": f.get("annex_key"),
                    "stored_bytes": f.get("stored_bytes"),
                }
                for f in files
            ),
            key=lambda x: x.get("rfilename") or "",
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


certificate_baseline_satisfied = baseline_satisfaction_certificate
