"""Drive identity bootstrap + first clean anchor + sessionless dirty recovery (RFC-002 / DEC-049 #35-B,
PR-03c1) — the smallest usable predecessor to the #35-C admission-authority cutover.

A neutral module: it depends only on the low-level catalog-v3 primitives (``drive_fence``,
``drive_mutation``, ``register``, ``capacity_evidence``) and NEVER on the transport module ``fetch`` —
so there is no ``register -> fetch`` / ``register -> drive_bootstrap`` ownership cycle and no transport
refactor. The physical-mutation envelope PR-03a/03b built is inert until a drive carries a proven
identity + ``dedicated_local`` authority; ``reconcile_drive`` is what establishes them.

Real-evidence contract (the identity fingerprint deliberately folds ``filesystem_capacity_bytes`` in, so
a resize changes the fingerprint):

  * STABLE identity is the filesystem/annex UUID pair (serial is supporting evidence). Every decision —
    mismatch refusal, adopt, epoch transition — compares the stable identity, NOT the capacity-bearing
    fingerprint. A migrated row (fingerprint NULL) is adopted only when the live stable identity equals
    every persisted non-null UUID; different media under an existing label refuses (DEF-029 reuse stays
    deferred).
  * A capacity change on the SAME stable identity is a capacity-epoch transition: it holds BOTH the old
    ``(fingerprint, epoch)`` and the prospective new ``(fingerprint, epoch+1)`` drive fences, and persists
    the NEW fingerprint + capacity + epoch + generation 1 + anchor atomically.
  * Every anchor is published from a FRESH post-inventory observation taken under the held drive fences;
    a drive that vanishes or changes identity/capacity during inventory leaves no anchor.

``reconcile_drive`` runs under the controller + drive fences and commits identity evidence + dirty
generation + clean anchor + authority in ONE short transaction (a crash before it leaves the drive
unknown and anchorless). ``dedicated_local`` is an explicit operator assertion (``dedicated=True``),
never a probe result; ``dedicated=False`` persists nothing and refuses to downgrade an authoritative
drive. Session-attributed recovery and label reuse/retirement are out of scope (#39, DEF-029).
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
# allowance. DIAGNOSTIC ONLY — it gates refresh-vs-refuse; the anchor always stores the raw observed
# free, so the tolerance is never extra capacity headroom.
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
class _LiveEvidence:
    """One consistent snapshot of a drive's live volume (probed once, so identity/capacity/free never
    disagree within a decision): the stable identifiers, capacity/free, allocation unit, and the
    composite identity fingerprint."""
    path: str | None
    fs_uuid: str | None
    annex_uuid: str | None
    serial: str | None
    capacity: int | None
    free: int | None
    alloc_unit: int | None
    fingerprint: str | None
    proven: bool

    def observation(self) -> dm.Observation:
        proof = json.dumps({"v": 1, "fs_uuid": self.fs_uuid, "annex_uuid": self.annex_uuid,
                            "serial": self.serial}, sort_keys=True, separators=(",", ":"))
        return dm.Observation(self.proven, self.free, self.capacity, self.fingerprint, proof, proof)


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


# ---- physical-evidence seams (isolated so the fenced/atomic logic below stays testable) -------------

def _live_evidence(con, label: str) -> _LiveEvidence:
    """Probe the CURRENT volume once and return a consistent evidence snapshot. Identity is unproven when
    the drive is absent/unmounted or exposes neither an fs nor an annex UUID."""
    path = register.archive_path(con, label)
    if path is None:
        return _LiveEvidence(None, None, None, None, None, None, None, None, False)
    try:
        st = os.statvfs(path)
    except OSError:                                      # unmounted/vanished mid-probe -> unknown, not error
        return _LiveEvidence(str(path), None, None, None, None, None, None, None, False)
    fs_uuid = register.probe_fs_uuid(path)
    annex_uuid = register.probe_annex_uuid(path)
    serial = register.probe_serial(path)                 # probed ONCE, reused for fingerprint + proof
    if not (fs_uuid or annex_uuid):
        return _LiveEvidence(str(path), fs_uuid, annex_uuid, serial, None, None, None, None, False)
    capacity = st.f_blocks * st.f_frsize
    fingerprint = capacity_evidence.identity_fingerprint_v1(
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial, filesystem_capacity_bytes=capacity)
    return _LiveEvidence(str(path), fs_uuid, annex_uuid, serial, capacity,
                         st.f_bavail * st.f_frsize, st.f_frsize, fingerprint, True)


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
    """Full, bounded, report-only reconciliation against every catalogued claim: prove each raw/annex
    copy present, recognize debris, and report unexplained extra content WITHOUT deleting it (the final
    free observation accounts for its bytes). An unprovable/absent claim is never counted present — it
    lands in ``missing`` and blocks a clean anchor."""
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

def _persisted(con, label: str):
    row = con.execute(
        "SELECT identity_epoch, write_generation, identity_fingerprint, filesystem_capacity_bytes, "
        "write_authority, fs_uuid, annex_uuid FROM drives WHERE drive_label=?", [label]).fetchone()
    if row is None:
        raise dm.DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
    return row


def _stable_identity_matches(ev: _LiveEvidence, persisted_fs, persisted_annex) -> bool:
    """The live stable identity must equal every persisted non-null filesystem/annex UUID. Serial is
    only supporting evidence and never on its own admits or rejects."""
    if persisted_fs is not None and ev.fs_uuid != persisted_fs:
        return False
    if persisted_annex is not None and ev.annex_uuid != persisted_annex:
        return False
    return True


def reconcile_drive(con, label: str, *, now, dedicated: bool = False, accept_drift: bool = False,
                    blocking: bool = True) -> Reconciliation:
    """Bootstrap / refresh / epoch-transition / recover one drive under the controller + drive fences,
    committing identity evidence + generation + anchor + authority atomically. See the module docstring
    for the full contract; raises a typed ``dm.DriveMutationRefused`` for every fail-closed path."""
    try:
        with drive_fence.hold_controller(db.DB_PATH, blocking=blocking):
            p_epoch, p_gen, p_fp, p_cap, p_auth, p_fs, p_annex = _persisted(con, label)   # facts UNDER controller
            ev = _live_evidence(con, label)
            if not ev.proven:
                raise dm.DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
            if not _stable_identity_matches(ev, p_fs, p_annex):      # different media under an existing label
                raise dm.DriveMutationRefused("DRIVE_IDENTITY_MISMATCH", drive=label,
                                              persisted=(p_fs, p_annex), live=(ev.fs_uuid, ev.annex_uuid))
            if not dedicated:                            # exclusivity is an explicit assertion, not a probe
                if p_auth == "dedicated_local":
                    raise dm.DriveMutationRefused("DRIVE_AUTHORITY_DOWNGRADE_REFUSED", drive=label)
                return Reconciliation("unknown_no_authority", p_epoch, p_gen, None)
            # A capacity change on the same stable identity transitions the epoch and changes the
            # fingerprint, so hold BOTH the old and the prospective new (fingerprint, epoch) drive fences.
            transition = p_fp is not None and ev.capacity != p_cap
            keyed = ([(p_fp, p_epoch), (ev.fingerprint, p_epoch + 1)] if transition
                     else [(ev.fingerprint, p_epoch)])
            with drive_fence.hold_drives_sorted(keyed, blocking=blocking):
                dest = register.archive_path(con, label)
                return _decide_and_commit(con, label, dest, p_epoch, p_gen, p_fp, p_cap, ev, now,
                                          accept_drift, transition)
    except drive_fence.FenceUnavailable as exc:
        raise dm.DriveMutationRefused("DRIVE_FENCE_UNAVAILABLE", **exc.evidence) from exc


def _final_observation(con, label: str, ev: _LiveEvidence) -> _LiveEvidence:
    """A FRESH observation under the held drive fences after inventory. Refuse (no anchor) if the drive
    vanished or its stable identity/capacity changed during inventory — the anchor must reflect the
    final observed state, never the pre-inventory one."""
    final = _live_evidence(con, label)
    if not final.proven or final.fingerprint != ev.fingerprint or final.capacity != ev.capacity:
        raise dm.DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
    return final


def _decide_and_commit(con, label, dest, p_epoch, p_gen, p_fp, p_cap, ev, now, accept_drift, transition):
    if p_fp is None:                                     # (A) bootstrap: establish identity + first anchor
        _require_complete_inventory(con, label, dest)
        final = _final_observation(con, label, ev)
        obs = final.observation()

        def _body():
            con.execute("UPDATE drives SET identity_epoch=?, filesystem_capacity_bytes=?, "
                        "identity_fingerprint=?, write_authority='dedicated_local' WHERE drive_label=?",
                        [p_epoch, final.capacity, final.fingerprint, label])
            gen = dm._advance_one(con, label, "bootstrap")           # generation 0 -> 1
            dm._publish_anchor_locked(con, label, p_epoch, gen, obs, now)
            return gen
        return Reconciliation("bootstrapped", p_epoch, dm._immediate(con, _body), final.free)

    if transition:                                       # (D) capacity-epoch transition: reset the namespace
        _require_complete_inventory(con, label, dest)
        final = _final_observation(con, label, ev)
        obs = final.observation()
        new_epoch = p_epoch + 1

        def _body():
            con.execute("UPDATE drives SET identity_epoch=?, filesystem_capacity_bytes=?, "
                        "identity_fingerprint=?, write_generation=0 WHERE drive_label=?",
                        [new_epoch, final.capacity, final.fingerprint, label])   # persist the NEW fingerprint
            gen = dm._advance_one(con, label, "epoch")               # 0 -> 1 under the new epoch
            dm._publish_anchor_locked(con, label, new_epoch, gen, obs, now)
            return gen
        return Reconciliation("epoch_advanced", new_epoch, dm._immediate(con, _body), final.free)

    if not dm._generation_is_clean(con, label, p_epoch, p_gen, p_fp, p_cap, "dedicated_local"):
        # (B) sessionless dirty recovery — republish THIS generation's anchor (session-attributed = #39)
        owner = con.execute("SELECT owner_session_id FROM drive_dirty_generations "
                            "WHERE drive_label=? AND identity_epoch=? AND generation=?",
                            [label, p_epoch, p_gen]).fetchone()
        if owner is not None and owner[0] is not None:
            raise dm.DriveMutationRefused("DRIVE_RECOVERY_SESSION_ACTIVE", drive=label)
        _require_complete_inventory(con, label, dest)
        final = _final_observation(con, label, ev)
        dm.publish_clean_anchor(con, label, p_epoch, p_gen, final.observation(), now)
        return Reconciliation("recovered", p_epoch, p_gen, final.free)

    # (C) refresh of a currently-clean anchored drive — drift-gated on the first observation (refuse
    # before the full inventory), then anchored from the fresh final observation
    last_free = con.execute("SELECT anchor_free_bytes FROM drive_clean_anchors WHERE drive_label=? "
                           "AND identity_epoch=? AND generation=?", [label, p_epoch, p_gen]).fetchone()[0]
    drifted = abs(ev.free - last_free) > free_drift_tolerance_v1(ev.alloc_unit)
    if drifted and not accept_drift:
        raise dm.DriveMutationRefused("DRIVE_FREE_DRIFT", drive=label, anchored=last_free, observed=ev.free)
    _require_complete_inventory(con, label, dest)
    final = _final_observation(con, label, ev)
    obs = final.observation()
    op = "accept-drift" if drifted else "reconcile"

    def _body():
        gen = dm._advance_one(con, label, op)                        # clean -> generation + 1
        dm._publish_anchor_locked(con, label, p_epoch, gen, obs, now)
        return gen
    return Reconciliation("drift_accepted" if drifted else "refreshed", p_epoch,
                          dm._immediate(con, _body), final.free)
