"""Pure capacity-evidence primitives for catalog v3 (RFC-002 / DEC-049, issue #35-A).

Functional core only: fingerprint, typed evidence records, and the fail-closed precedence rule are
pure (no SQLite, filesystem, mount, ``df``, fence, clock, or network). The one catalog reader here,
:func:`shadow_by_drive`, is a read-only diagnostic accessor — it derives evidence from persisted
facts alongside the legacy ``drives.free_bytes`` and never becomes admission authority in this phase.

Admission cutover, the live observation/fencing shell, and generation/anchor writes are #35-B/#35-C.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace

# Diagnostic-only floor for the shadow accessor. Migrated drives derive `unknown` (zero executable
# and, with no current-epoch filesystem capacity, no optimistic maximum), so this value never affects
# a decision in this phase; the versioned admission floor arrives with the #35-C authority cutover.
_DIAGNOSTIC_SAFETY_FLOOR = 0


@dataclass(frozen=True)
class Evidence:
    """A typed admission-evidence verdict for one drive. `unknown` contributes zero executable
    capacity; `legacy_free_bytes` is diagnostic only and never executable; `optimistic_usable_max` is
    a non-executable sensitivity bound (post-safety-floor filesystem capacity, or None when the
    current-epoch filesystem capacity is unknown)."""
    kind: str                       # "live" | "anchor" | "unknown"
    executable: bool
    admissible_free: int
    code: str | None = None
    optimistic_usable_max: int | None = None
    legacy_free_bytes: int | None = None


def identity_fingerprint_v1(*, fs_uuid, annex_uuid, serial, filesystem_capacity_bytes) -> str:
    """Version-1 identity fingerprint: lowercase SHA-256 over canonical UTF-8 JSON with sorted keys
    and explicit nulls. At least one of fs_uuid / annex_uuid must be proven."""
    if not fs_uuid and not annex_uuid:
        raise ValueError("identity fingerprint requires at least one of fs_uuid or annex_uuid")
    payload = {
        "annex_uuid": annex_uuid,
        "filesystem_capacity_bytes": filesystem_capacity_bytes,
        "fs_uuid": fs_uuid,
        "serial": serial,
        "v": 1,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _optimistic_usable_max(filesystem_capacity_bytes, safety_floor_bytes):
    if filesystem_capacity_bytes is None:
        return None
    return max(0, filesystem_capacity_bytes - safety_floor_bytes)


def derive(*, mounted, identity_proven, fence_held, write_authority, current_epoch,
           filesystem_capacity_bytes, current_fingerprint, live_free_bytes, dirty,
           anchor_free_bytes, anchor_epoch, anchor_fingerprint, anchor_filesystem_capacity,
           safety_floor_bytes) -> Evidence:
    """Fail-closed evidence precedence over one drive's synthetic observation (contract rule):

    1. mounted + identity-proven + dedicated-local + fenced → live `df`, authoritative even while dirty;
    2. offline + clean + exact identity/capacity epoch under dedicated-local control → latest anchor;
    3. anything else → `unknown` (zero executable), with the binding distinction preserved.

    The safety floor is subtracted exactly once when deriving admissible free.
    """
    optimistic = _optimistic_usable_max(filesystem_capacity_bytes, safety_floor_bytes)

    def unknown(code: str) -> Evidence:
        return Evidence(kind="unknown", executable=False, admissible_free=0, code=code,
                        optimistic_usable_max=optimistic)

    if mounted:
        if not identity_proven:
            return unknown("DRIVE_IDENTITY_UNPROVEN")
        if write_authority != "dedicated_local":
            return unknown("UNSUPPORTED_SHARED_WRITER")
        if not fence_held:
            return unknown("DRIVE_FENCE_UNAVAILABLE")
        if live_free_bytes is None:
            return unknown("CAPACITY_EVIDENCE_UNKNOWN")
        return Evidence(kind="live", executable=True,
                        admissible_free=max(0, live_free_bytes - safety_floor_bytes),
                        code=None, optimistic_usable_max=optimistic)

    # offline
    if dirty:
        return unknown("DRIVE_RECONCILIATION_REQUIRED")
    if anchor_free_bytes is None:
        return unknown("CAPACITY_EVIDENCE_UNKNOWN")
    if write_authority != "dedicated_local":
        return unknown("UNSUPPORTED_SHARED_WRITER")
    if (anchor_epoch != current_epoch or anchor_fingerprint != current_fingerprint
            or anchor_filesystem_capacity != filesystem_capacity_bytes):
        return unknown("DRIVE_RECONCILIATION_REQUIRED")
    if filesystem_capacity_bytes is None or not (0 <= anchor_free_bytes <= filesystem_capacity_bytes):
        return unknown("ANCHOR_OUT_OF_RANGE")
    return Evidence(kind="anchor", executable=True,
                    admissible_free=max(0, anchor_free_bytes - safety_floor_bytes),
                    code=None, optimistic_usable_max=optimistic)


def shadow_by_drive(con) -> dict[str, Evidence]:
    """Internal diagnostic accessor: derive evidence for every drive from persisted catalog facts,
    with the legacy ``free_bytes`` attached as a diagnostic. Read-only — no mounts, ``df``, fencing,
    or writes — and never admission authority. In this phase (no observation shell, migration creates
    no anchors) every drive derives ``unknown`` with zero executable capacity."""
    drives = con.execute(
        "SELECT drive_label, identity_epoch, write_generation, write_authority, "
        "filesystem_capacity_bytes, identity_fingerprint, free_bytes FROM drives ORDER BY drive_label"
    ).fetchall()
    out: dict[str, Evidence] = {}
    for label, epoch, generation, authority, fs_capacity, fingerprint, free in drives:
        anchor = con.execute(
            "SELECT generation, anchor_free_bytes, identity_epoch, identity_fingerprint, "
            "filesystem_capacity_bytes FROM drive_clean_anchors WHERE drive_label=? "
            "ORDER BY generation DESC LIMIT 1", [label]).fetchone()
        anchor_generation = anchor[0] if anchor else None
        # A current generation is clean only when a matching anchor exists; otherwise it is dirty.
        # A never-written drive (generation 0, no anchor) is simply unproven, not dirty.
        dirty = generation > 0 and anchor_generation != generation
        evidence = derive(
            mounted=False, identity_proven=False, fence_held=False, write_authority=authority,
            current_epoch=epoch, filesystem_capacity_bytes=fs_capacity,
            current_fingerprint=fingerprint, live_free_bytes=None, dirty=dirty,
            anchor_free_bytes=(anchor[1] if anchor else None),
            anchor_epoch=(anchor[2] if anchor else None),
            anchor_fingerprint=(anchor[3] if anchor else None),
            anchor_filesystem_capacity=(anchor[4] if anchor else None),
            safety_floor_bytes=_DIAGNOSTIC_SAFETY_FLOOR,
        )
        out[label] = replace(evidence, legacy_free_bytes=free)
    return out
