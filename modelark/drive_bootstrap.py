"""Drive identity bootstrap + first clean anchor + sessionless dirty recovery (RFC-002 / DEC-049 #35-B,
PR-03c1) — the smallest usable predecessor to the #35-C admission-authority cutover.

A neutral module: it depends only on the low-level catalog-v3 primitives (``drive_fence``,
``drive_mutation``, ``register``, ``capacity_evidence``) and NEVER on the transport module ``fetch`` —
so there is no ``register -> fetch`` / ``register -> drive_bootstrap`` ownership cycle, and no broad
transport refactor. The physical-mutation envelope PR-03a/03b built is inert until a drive carries a
proven identity + ``dedicated_local`` authority; ``reconcile_drive`` is what establishes them.

One operator operation, ``drive reconcile <label>`` (``reconcile_drive``), covers, under the controller +
drive fences and committed in ONE short transaction (identity evidence + dirty generation + clean anchor
+ authority, atomically — a crash before that commit leaves the drive unknown and anchorless):

  * bootstrap        — a drive with no persisted identity: prove it, full-inventory reconcile, open
                       generation 1, publish the first anchor storing the RAW observed free.
  * refresh          — a currently-clean anchored drive at the same identity/capacity whose free has
                       drifted only WITHIN the versioned diagnostic tolerance: advance a generation and
                       re-anchor the fresh raw free.  Above tolerance -> DRIVE_FREE_DRIFT unless
                       ``accept_drift`` re-anchors after a full reconciliation under a distinct
                       ``accept-drift`` operation code.
  * epoch transition — same identity, CHANGED filesystem capacity: reset the namespace to
                       (new epoch, generation 1) and re-anchor.
  * recovery         — a dirty generation with no anchor and NO owner session (sessionless): reconcile
                       and republish THAT generation's anchor via the (epoch, generation) CAS.

``dedicated_local`` is an explicit operator/policy assertion of exclusivity (``dedicated=True``), never
derivable from identity probes; without it the drive stays a valid ``unknown`` identity, and a
``dedicated=False`` reconcile never downgrades an already-authoritative drive (a typed refusal —
revocation is a separate lifecycle).  A different identity under an existing label refuses before any
mutation.  Session-attributed recovery and label reuse/retirement are out of scope (#39, DEF-029).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from modelark import capacity_evidence, drive_fence, register
from modelark import drive_mutation as dm
from modelark.core import db

# The diagnostic free-drift tolerance is one filesystem allocation unit plus this bounded metadata
# allowance. It is DIAGNOSTIC ONLY — it gates refresh-vs-refuse; the anchor always stores the raw
# observed free, so the tolerance is never extra capacity headroom.
_DRIFT_METADATA_ALLOWANCE_BYTES = 1 << 20               # v1: 1 MiB


@dataclass(frozen=True)
class Inventory:
    """A bounded, report-only full reconciliation of a drive against its catalogued claims."""
    present: list                                        # [(repo_id, rfilename)] proven present
    missing: list                                        # [(repo_id, rfilename)] absent/unprovable claims
    debris: list = field(default_factory=list)           # recognized staging/.incomplete/tmp relpaths
    extra: list = field(default_factory=list)            # unexplained extra relpaths (reported, not deleted)

    @property
    def complete(self) -> bool:
        return not self.missing


@dataclass(frozen=True)
class Reconciliation:
    """The operator-visible outcome of ``reconcile_drive``."""
    outcome: str                                         # bootstrapped|refreshed|drift_accepted|
    identity_epoch: int                                  #   epoch_advanced|recovered|unknown_no_authority
    generation: int
    anchor_free_bytes: int | None
    inventory: Inventory | None = None


def free_drift_tolerance_v1(alloc_unit_bytes: int) -> int:
    """Versioned diagnostic free-drift tolerance: one filesystem allocation unit + a bounded metadata
    allowance. NEVER capacity headroom — it only gates whether a refresh re-anchors or refuses."""
    return alloc_unit_bytes + _DRIFT_METADATA_ALLOWANCE_BYTES


# ---- physical-evidence seams (isolated so the fenced/atomic logic above stays testable) -------------

def _alloc_unit(dest) -> int:
    """The filesystem allocation unit (statvfs f_frsize) backing the drift tolerance."""
    return os.statvfs(dest).f_frsize


def _live_observation(con, label: str) -> dm.Observation:
    """Fenced live observation: prove the drive's identity from the CURRENT volume (fs/annex/serial +
    filesystem capacity), never the persisted row, and read its live free/capacity. Identity is unproven
    when the drive is absent or exposes neither an fs nor an annex UUID. (Mirrors fetch._observe_drive
    without importing the transport module.)"""
    path = register.archive_path(con, label)
    if path is None:
        return dm.Observation(False, None, None, None, "", "")
    try:
        st = os.statvfs(path)
    except OSError:                                       # unmounted/vanished mid-probe -> unknown, not error
        return dm.Observation(False, None, None, None, "", "")
    fs_uuid = register.probe_fs_uuid(path)
    annex_uuid = register.probe_annex_uuid(path)
    if not (fs_uuid or annex_uuid):
        return dm.Observation(False, None, None, None, "", "")
    capacity = st.f_blocks * st.f_frsize
    fingerprint = capacity_evidence.identity_fingerprint_v1(
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=register.probe_serial(path),
        filesystem_capacity_bytes=capacity)
    proof = json.dumps({"v": 1, "fs_uuid": fs_uuid, "annex_uuid": annex_uuid,
                        "serial": register.probe_serial(path)}, sort_keys=True, separators=(",", ":"))
    return dm.Observation(True, st.f_bavail * st.f_frsize, capacity, fingerprint, proof, proof)


def _annex_key_present(dest, key: str, *, target_uuid) -> bool:
    """Prove an annex key is physically present on the drive (its uuid appears in ``whereis``). The
    worktree symlink is NOT the proof — ``git annex copy --to`` deposits the object without one."""
    if not (key and target_uuid):
        return False
    try:
        out = subprocess.run(["git", "-C", str(dest), "annex", "whereis", "--key", key],
                             capture_output=True, text=True, check=False)
    except OSError:
        return False
    return out.returncode == 0 and target_uuid in out.stdout


_DEBRIS_SUFFIXES = (".incomplete", ".tmp")


def _is_debris(relpath: str) -> bool:
    """Recognize known staging/partial/temporary debris (never counted as a catalogued copy)."""
    name = relpath.rsplit("/", 1)[-1]
    return name.endswith(_DEBRIS_SUFFIXES) or name.startswith(".modelark-probe")


def _walk_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            yield p


def _inventory(con, label: str, dest) -> Inventory:
    """Full, bounded, report-only reconciliation of a drive against every catalogued claim: prove each
    raw/annex copy present, recognize debris, and report unexplained extra content WITHOUT deleting it
    (the final free observation accounts for its bytes). An unprovable/absent claim is never counted
    present — it lands in ``missing`` and blocks a clean anchor."""
    dest = Path(dest)
    target_uuid = (con.execute("SELECT annex_uuid FROM drives WHERE drive_label=?",
                               [label]).fetchone() or [None])[0]
    present, missing = [], []
    claimed = set()
    for repo_id, rfilename, annex_key, relpath in con.execute(
            "SELECT repo_id, rfilename, annex_key, repo_id || '/' || stored_relpath "
            "FROM archived WHERE drive_label=?", [label]).fetchall():
        claimed.add(relpath)
        proven = (_annex_key_present(dest, annex_key, target_uuid=target_uuid) if annex_key
                  else (dest / relpath).exists())
        (present if proven else missing).append((repo_id, rfilename))
    debris, extra = [], []
    for path in _walk_files(dest):
        rel = path.relative_to(dest).as_posix()
        if _is_debris(rel):
            debris.append(rel)
        elif rel not in claimed:
            extra.append(rel)
    return Inventory(present, missing, debris, extra)


def _require_complete_inventory(con, label: str, dest) -> Inventory:
    inv = _inventory(con, label, dest)
    if inv.missing:                                      # an unproved catalogued copy leaves it dirty
        raise dm.DriveMutationRefused("DRIVE_RECONCILIATION_REQUIRED", drive=label, missing=len(inv.missing))
    return inv


# ---- the single operator operation ------------------------------------------------------------------

def reconcile_drive(con, label: str, *, now, dedicated: bool = False, accept_drift: bool = False,
                    blocking: bool = True) -> Reconciliation:
    """Bootstrap / refresh / epoch-transition / recover one drive under the controller + drive fences,
    committing identity evidence + generation + anchor + authority atomically. See the module docstring
    for the full contract; raises a typed ``dm.DriveMutationRefused`` for every fail-closed path."""
    p_epoch, p_gen, p_fp, p_cap, p_auth = dm._drive_facts(con, label)
    try:
        with drive_fence.hold_controller(db.DB_PATH, blocking=blocking):
            obs = _live_observation(con, label)          # derive the live identity under the controller
            if not obs.identity_proven:
                raise dm.DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
            live_fp = obs.fingerprint
            if p_fp is not None and live_fp != p_fp:     # a different identity under an existing label
                raise dm.DriveMutationRefused("DRIVE_IDENTITY_MISMATCH", drive=label,
                                              persisted=p_fp, live=live_fp)
            if not dedicated:                            # exclusivity is an explicit assertion, not a probe
                if p_auth == "dedicated_local":
                    raise dm.DriveMutationRefused("DRIVE_AUTHORITY_DOWNGRADE_REFUSED", drive=label)
                return Reconciliation("unknown_no_authority", p_epoch, p_gen, None)
            with drive_fence.hold_drives_sorted([(live_fp, p_epoch)], blocking=blocking):
                obs = _live_observation(con, label)      # re-prove identity under the drive fence
                if not obs.identity_proven or obs.fingerprint != live_fp:
                    raise dm.DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
                dest = register.archive_path(con, label)
                return _decide_and_commit(con, label, dest, p_epoch, p_gen, p_fp, p_cap, obs, now,
                                          accept_drift)
    except drive_fence.FenceUnavailable as exc:
        raise dm.DriveMutationRefused("DRIVE_FENCE_UNAVAILABLE", **exc.evidence) from exc


def _decide_and_commit(con, label, dest, p_epoch, p_gen, p_fp, p_cap, obs, now, accept_drift):
    live_cap, live_free = obs.filesystem_capacity, obs.free_bytes

    if p_fp is None:                                     # (A) bootstrap: establish the identity + first anchor
        _require_complete_inventory(con, label, dest)

        def _body():
            con.execute("UPDATE drives SET identity_epoch=?, filesystem_capacity_bytes=?, "
                        "identity_fingerprint=?, write_authority='dedicated_local' WHERE drive_label=?",
                        [p_epoch, live_cap, obs.fingerprint, label])
            gen = dm._advance_one(con, label, "bootstrap")           # generation 0 -> 1
            dm._publish_anchor_locked(con, label, p_epoch, gen, obs, now)
            return gen
        return Reconciliation("bootstrapped", p_epoch, dm._immediate(con, _body), live_free)

    if live_cap != p_cap:                                # (D) capacity-epoch transition: reset the namespace
        _require_complete_inventory(con, label, dest)
        new_epoch = p_epoch + 1

        def _body():
            con.execute("UPDATE drives SET identity_epoch=?, filesystem_capacity_bytes=?, "
                        "write_generation=0 WHERE drive_label=?", [new_epoch, live_cap, label])
            gen = dm._advance_one(con, label, "epoch")               # 0 -> 1 under the new epoch
            dm._publish_anchor_locked(con, label, new_epoch, gen, obs, now)
            return gen
        return Reconciliation("epoch_advanced", new_epoch, dm._immediate(con, _body), live_free)

    if not dm._generation_is_clean(con, label, p_epoch, p_gen, p_fp, live_cap, "dedicated_local"):
        # (B) sessionless dirty recovery — republish THIS generation's anchor (session-attributed = #39)
        owner = con.execute("SELECT owner_session_id FROM drive_dirty_generations "
                            "WHERE drive_label=? AND identity_epoch=? AND generation=?",
                            [label, p_epoch, p_gen]).fetchone()
        if owner is not None and owner[0] is not None:
            raise dm.DriveMutationRefused("DRIVE_RECOVERY_SESSION_ACTIVE", drive=label)
        _require_complete_inventory(con, label, dest)
        dm.publish_clean_anchor(con, label, p_epoch, p_gen, obs, now)
        return Reconciliation("recovered", p_epoch, p_gen, live_free)

    # (C) refresh of a currently-clean anchored drive — drift-gated
    last_free = con.execute("SELECT anchor_free_bytes FROM drive_clean_anchors WHERE drive_label=? "
                           "AND identity_epoch=? AND generation=?", [label, p_epoch, p_gen]).fetchone()[0]
    drifted = abs(live_free - last_free) > free_drift_tolerance_v1(_alloc_unit(dest))
    if drifted and not accept_drift:
        raise dm.DriveMutationRefused("DRIVE_FREE_DRIFT", drive=label, anchored=last_free, observed=live_free)
    _require_complete_inventory(con, label, dest)
    op = "accept-drift" if drifted else "reconcile"

    def _body():
        gen = dm._advance_one(con, label, op)                        # clean -> generation + 1
        dm._publish_anchor_locked(con, label, p_epoch, gen, obs, now)
        return gen
    return Reconciliation("drift_accepted" if drifted else "refreshed", p_epoch,
                          dm._immediate(con, _body), live_free)
