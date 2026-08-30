"""Admission-evidence shell (RFC-002 / DEC-049, issue #35-C).

Derives one typed :class:`capacity_evidence.Evidence` per drive by combining persisted catalog facts
with a fenced live/anchor observation, through the pure ``capacity_evidence.derive`` precedence rule.
Two paths share the RULE but not one volatile snapshot:

* :func:`preview_by_drive` — preview/API/CLI. Per drive: capture the persisted identity, try the
  identity-derived drive fence NON-BLOCKING, observe + revalidate UNDER the held fence, derive, then
  RELEASE. A snapshot, never a reservation. A migrated/unproven drive has no identity to fence and is
  ``unknown``; a contended fence is fail-closed ``DRIVE_FENCE_UNAVAILABLE``.
* :func:`execution_evidence` — per-file. Consumes a FRESH observation taken while ``drive_mutation``
  ALREADY holds the fence. It never reacquires a fence and never falls back to an unfenced/legacy read.

Neutral imperative shell: it reuses ``capacity_evidence`` (pure derivation), ``capacity`` (the versioned
safety-floor policy), ``drive_fence`` (the lock primitive), and ``core.db`` — never the protected
transport module ``fetch``. The live observation is supplied by an INJECTED callback, so the evidence
layer does not import transport internals.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from modelark import capacity, capacity_evidence, drive_fence


@dataclass(frozen=True)
class _Facts:
    epoch: int | None
    generation: int
    fingerprint: str | None
    filesystem_capacity: int | None
    authority: str
    raid: bool
    anchor: tuple | None            # (anchor_free_bytes, identity_epoch, identity_fingerprint, fs_capacity)


def _facts(con, label: str) -> _Facts:
    row = con.execute(
        "SELECT identity_epoch, write_generation, identity_fingerprint, filesystem_capacity_bytes, "
        "coalesce(write_authority,'unknown'), coalesce(raid_backed,0) FROM drives WHERE drive_label=?",
        [label]).fetchone()
    if row is None:
        return _Facts(None, 0, None, None, "unknown", False, None)
    epoch, generation, fingerprint, fs_capacity, authority, raid = row
    # The anchor for the drive's EXACT current (identity_epoch, write_generation): a stale old-epoch
    # anchor at a higher generation must not shadow the current one.
    anchor = con.execute(
        "SELECT anchor_free_bytes, identity_epoch, identity_fingerprint, filesystem_capacity_bytes "
        "FROM drive_clean_anchors WHERE drive_label=? AND identity_epoch=? AND generation=?",
        [label, epoch, generation]).fetchone()
    return _Facts(epoch, generation, fingerprint, fs_capacity, authority, bool(raid), anchor)


def _derive(con, label: str, *, observation, fence_held: bool, now: str) -> capacity_evidence.Evidence:
    """Re-read the current facts and derive evidence through the shared rule. Reading facts HERE (not a
    pre-captured snapshot) is the revalidation: a lifecycle change since capture is caught because the
    observation must agree with the CURRENT fingerprint/capacity."""
    f = _facts(con, label)
    floor = capacity.safety_floor(f.filesystem_capacity, f.raid) if f.filesystem_capacity is not None else 0
    dirty = f.generation > 0 and f.anchor is None
    if observation is not None:
        mounted = True
        identity_proven = bool(
            f.fingerprint
            and observation.identity_proven
            and observation.fingerprint == f.fingerprint
            and observation.filesystem_capacity == f.filesystem_capacity)
        live_free = observation.free_bytes
    else:
        mounted = False
        identity_proven = False
        live_free = None
    evidence = capacity_evidence.derive(
        mounted=mounted, identity_proven=identity_proven, fence_held=fence_held,
        write_authority=f.authority, current_epoch=f.epoch, filesystem_capacity_bytes=f.filesystem_capacity,
        current_fingerprint=f.fingerprint, live_free_bytes=live_free, dirty=dirty,
        anchor_free_bytes=(f.anchor[0] if f.anchor else None),
        anchor_epoch=(f.anchor[1] if f.anchor else None),
        anchor_fingerprint=(f.anchor[2] if f.anchor else None),
        anchor_filesystem_capacity=(f.anchor[3] if f.anchor else None),
        safety_floor_bytes=floor)
    return replace(evidence, observed_at=now, identity_epoch=f.epoch)


def execution_evidence(con, label: str, observation, *, now: str) -> capacity_evidence.Evidence:
    """Per-file EXECUTION admission. ``observation`` was taken while ``drive_mutation`` already holds the
    drive fence, so ``fence_held`` is implied True; this never acquires a fence and never falls back to an
    unfenced/legacy read (an unproven observation is typed ``unknown``, not admitted on legacy free)."""
    return _derive(con, label, observation=observation, fence_held=True, now=now)


def preview_by_drive(con, labels, *, observe, now: str,
                     fence=drive_fence.hold_drives_sorted) -> dict[str, capacity_evidence.Evidence]:
    """Preview/API/CLI SNAPSHOT admission for each drive. ``observe(label) -> Observation | None`` is the
    injected live reader (``None`` when the drive is not mounted). For a proven drive the identity-derived
    drive fence is tried NON-BLOCKING; the observation + derivation happen UNDER the held fence and the
    fence is released immediately after (a snapshot, not a reservation). A contended fence is
    fail-closed."""
    out: dict[str, capacity_evidence.Evidence] = {}
    for label in labels:
        captured = _facts(con, label)
        if not captured.fingerprint:
            # A migrated/unproven drive has no identity to fence or attest -> unknown (offline derivation).
            out[label] = _derive(con, label, observation=None, fence_held=False, now=now)
            continue
        try:
            with fence([(captured.fingerprint, captured.epoch)], blocking=False):
                observation = observe(label)                 # observe + revalidate UNDER the held fence
                out[label] = _derive(con, label, observation=observation, fence_held=True, now=now)
        except drive_fence.FenceUnavailable:
            # Fail-closed: a mounted-but-unfenceable drive is DRIVE_FENCE_UNAVAILABLE (diagnostic observe
            # only, still zero executable); an offline one derives from the anchor/unknown path.
            observation = observe(label)
            out[label] = _derive(con, label, observation=observation, fence_held=False, now=now)
    return out
