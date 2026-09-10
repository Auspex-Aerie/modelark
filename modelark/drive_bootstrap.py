"""Drive identity bootstrap + first clean anchor + fenced dirty recovery (RFC-002 / DEC-049 #35-B,
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
drive. Ended-session recovery preserves its owner/history and republishes only the existing
generation after fenced inventory and transactional owner validation. Label reuse/retirement
remains out of scope (DEF-029).
"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from modelark import capacity_evidence, drive_fence, register
from modelark.block_identity import BlockObservationError
from modelark import drive_mutation as dm
from modelark import execution_authority as authority
from modelark.drive_identity import FenceIdentity, UnprovenFenceIdentity, compatible_keys
from modelark.core import db
from modelark.catalog_versions import SUPPORTED_CATALOG_VERSIONS, SERIAL_REPAIR_CATALOG_VERSION
from modelark.serial_identity import recognize_legacy_anchor, SerialIdentityUnproven, is_legacy_serial_mismatch

# Re-export the typed refusal so operator entry points (the `drive reconcile` CLI) can translate it to a
# clean message via THIS module, without importing the fenced envelope directly — keeping the envelope
# importer set limited to the reviewed owners (see test_envelope_wired_only_into_reviewed_transport).
DriveMutationRefused = dm.DriveMutationRefused

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
    composite identity fingerprint. ``serial`` remains the actual disk readout;
    ``serial_in_identity`` records whether the catalog binds serial as identity.
    Successful discovery never silently enriches a serial-less registration."""
    path: str | None
    fs_uuid: str | None
    annex_uuid: str | None
    serial: str | None
    capacity: int | None
    free: int | None
    alloc_unit: int | None
    fingerprint: str | None
    proven: bool
    serial_in_identity: bool = True

    def observation(self) -> dm.Observation:
        proof = json.dumps({"v": 1, "fs_uuid": self.fs_uuid, "annex_uuid": self.annex_uuid,
                            "serial": self.serial if self.serial_in_identity else None},
                           sort_keys=True, separators=(",", ":"))
        observed = json.dumps({"v": 1, "fs_uuid": self.fs_uuid, "annex_uuid": self.annex_uuid,
                               "serial": self.serial}, sort_keys=True, separators=(",", ":"))
        return dm.Observation(self.proven, self.free, self.capacity, self.fingerprint, proof, observed)


@dataclass(frozen=True)
class Reconciliation:
    """The operator-visible outcome of ``reconcile_drive``."""
    outcome: str                                         # bootstrapped|refreshed|drift_accepted|
    identity_epoch: int                                  #   epoch_advanced|recovered|unknown_no_authority
    generation: int
    anchor_free_bytes: int | None
    inventory: Inventory | None = None


@dataclass(frozen=True)
class ReconciliationProgress:
    """A bounded operator-facing milestone; never identity or admission evidence."""
    phase: str
    completed: int = 0
    total: int | None = None


ProgressCallback = Callable[[ReconciliationProgress], None]
_FILE_PROGRESS_INTERVAL = 1000


def _emit_progress(
    callback: ProgressCallback | None,
    phase: str,
    *,
    completed: int = 0,
    total: int | None = None,
) -> None:
    if callback is not None:
        callback(ReconciliationProgress(phase, completed, total))


def free_drift_tolerance_v1(alloc_unit_bytes: int) -> int:
    """Versioned diagnostic free-drift tolerance: one filesystem allocation unit + a bounded metadata
    allowance. NEVER capacity headroom — it only gates whether a refresh re-anchors or refuses."""
    return alloc_unit_bytes + _DRIFT_METADATA_ALLOWANCE_BYTES


# ---- physical-evidence seams (isolated so the fenced/atomic logic below stays testable) -------------

def _live_evidence(con, label: str) -> _LiveEvidence:
    """Observe the current disk between matching volume identity/geometry reads.

    Identity is unproven for an absent, changing or unobservable volume, or one
    exposing neither an fs nor an annex UUID. The observation is not a lease.
    """
    path = register.archive_path(con, label)
    if path is None:
        return _LiveEvidence(None, None, None, None, None, None, None, None, False)
    try:
        volume = register.observe_archive_volume(path)
    except BlockObservationError:
        return _LiveEvidence(str(path), None, None, None, None, None, None, None, False)
    fs_uuid, annex_uuid, serial = volume.fs_uuid, volume.annex_uuid, volume.serial
    if not (fs_uuid or annex_uuid):
        return _LiveEvidence(str(path), fs_uuid, annex_uuid, serial, None, None, None, None, False)
    capacity = volume.capacity_bytes
    saved = con.execute('SELECT serial FROM drives WHERE drive_label=?', [label]).fetchone()
    from modelark.serial_identity import serial_for_identity
    try:
        identity_serial = serial_for_identity(saved[0] if saved else None, serial)
    except SerialIdentityUnproven:
        return _LiveEvidence(str(path), fs_uuid, annex_uuid, serial, capacity,
                             volume.free_bytes, volume.alloc_unit_bytes, None, False)
    fingerprint = capacity_evidence.identity_fingerprint_v1(
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=identity_serial, filesystem_capacity_bytes=capacity)
    known_serial_matches = saved is not None and (not saved[0] or saved[0] == serial)
    return _LiveEvidence(str(path), fs_uuid, annex_uuid, serial, capacity,
                         volume.free_bytes, volume.alloc_unit_bytes, fingerprint, known_serial_matches,
                         serial_in_identity=bool(saved and saved[0]))


def _annex_keys_present(dest, keys, *, target_uuid) -> set[str]:
    """Return catalogued keys recorded present on exactly ``target_uuid`` using one annex query.

    ``whereis --all --in <uuid>`` reads the same annex location log as the former per-key calls, but
    emits the keys for the requested target UUID in one process.  Any command failure returns no
    proof, causing every annex claim to fail closed in the inventory.
    """
    wanted = {str(key) for key in keys if key}
    if not wanted or not target_uuid:
        return set()
    try:
        out = subprocess.run(
            [
                "git", "-C", str(dest), "annex", "whereis", "--all",
                "--in", str(target_uuid), "--format=${key}\\n",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    if out.returncode != 0:
        return set()
    return wanted.intersection(line for line in out.stdout.splitlines() if line)


_DEBRIS_SUFFIXES = (".incomplete", ".tmp")


def _is_debris(relpath: str) -> bool:
    """Recognize known staging/partial/temporary debris (never counted as a catalogued copy)."""
    name = relpath.rsplit("/", 1)[-1]
    return name.endswith(_DEBRIS_SUFFIXES) or name.startswith(".modelark-probe")


def _walk_files(root: Path):
    """Walk worktree files while pruning every Git metadata tree before descent."""
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        current_path = Path(current)
        for filename in filenames:
            yield current_path / filename


def _inventory(
    con,
    label: str,
    dest,
    *,
    progress: ProgressCallback | None = None,
) -> Inventory:
    """Full, bounded, report-only reconciliation against every catalogued claim: prove each raw/annex
    copy present, recognize debris, and report unexplained extra content WITHOUT deleting it (the final
    free observation accounts for its bytes). An unprovable/absent claim is never counted present — it
    lands in ``missing`` and blocks a clean anchor."""
    dest = Path(dest)
    target_uuid = (con.execute("SELECT annex_uuid FROM drives WHERE drive_label=?",
                               [label]).fetchone() or [None])[0]
    rows = con.execute(
        "SELECT repo_id, rfilename, annex_key, repo_id || '/' || stored_relpath "
        "FROM archived WHERE drive_label=? ORDER BY repo_id,rfilename", [label]
    ).fetchall()
    annex_keys = {str(row[2]) for row in rows if row[2]}
    _emit_progress(progress, "inventory_started", total=len(rows))
    _emit_progress(progress, "annex_membership_started", total=len(annex_keys))
    proven_annex_keys = _annex_keys_present(dest, annex_keys, target_uuid=target_uuid)
    _emit_progress(
        progress,
        "annex_membership_completed",
        completed=len(proven_annex_keys),
        total=len(annex_keys),
    )
    present, missing = [], []
    claimed = set()
    for repo_id, rfilename, annex_key, relpath in rows:
        claimed.add(relpath)
        proven = (annex_key in proven_annex_keys if annex_key
                  else (dest / relpath).exists())
        (present if proven else missing).append((repo_id, rfilename))
    debris, extra = [], []
    scanned = 0
    _emit_progress(progress, "filesystem_scan_started")
    for path in _walk_files(dest):
        scanned += 1
        rel = path.relative_to(dest).as_posix()
        if _is_debris(rel):
            debris.append(rel)
        elif rel not in claimed:
            extra.append(rel)
        if scanned % _FILE_PROGRESS_INTERVAL == 0:
            _emit_progress(progress, "filesystem_scan_progress", completed=scanned)
    _emit_progress(progress, "filesystem_scan_completed", completed=scanned)
    return Inventory(present, missing, debris, extra)


def _require_complete_inventory(
    con,
    label: str,
    dest,
    *,
    progress: ProgressCallback | None = None,
) -> Inventory:
    inv = _inventory(con, label, dest, progress=progress)
    if inv.missing:                                      # an unproved catalogued copy leaves it dirty
        raise dm.DriveMutationRefused("DRIVE_RECONCILIATION_REQUIRED", drive=label, missing=len(inv.missing))
    return inv


# ---- the single operator operation ------------------------------------------------------------------

def _persisted(con, label: str):
    row = con.execute(
        "SELECT identity_epoch, write_generation, identity_fingerprint, filesystem_capacity_bytes, "
        "write_authority, fs_uuid, annex_uuid, serial FROM drives WHERE drive_label=?", [label]).fetchone()
    if row is None:
        raise dm.DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
    return row


def _serial_repair_state(con, label):
    """Capture one coherent binding, reusing a caller-owned transaction when present."""
    own = not con.in_transaction
    if own:
        con.execute("BEGIN")
    try:
        return _serial_repair_state_snapshot(con, label)
    finally:
        if own:
            con.execute("ROLLBACK")


def _serial_repair_state_snapshot(con, label):
    from modelark.proposal import approved_proposals_bound_to_drive

    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version not in SUPPORTED_CATALOG_VERSIONS:
        raise dm.DriveMutationRefused("CATALOG_VERSION_UNSUPPORTED", drive=label)
    facts = _persisted(con, label)
    epoch, generation, fingerprint, capacity, write_authority, fs_uuid, annex_uuid, serial = facts
    try:
        identity = FenceIdentity(fs_uuid, annex_uuid, serial, capacity, epoch, fingerprint)
        identity.lock_keys()
    except UnprovenFenceIdentity as exc:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label, reason=str(exc)) from exc
    if write_authority != "dedicated_local" or type(generation) is not int or generation < 1:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label)
    metadata = con.execute("SELECT lifecycle,eligibility FROM drives WHERE drive_label=?", [label]).fetchone()
    if metadata[0] != "active":
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label, lifecycle=metadata[0])
    dirty = con.execute(
        "SELECT * FROM drive_dirty_generations WHERE drive_label=? AND identity_epoch=? AND generation=?",
        [label, epoch, generation]).fetchone()
    if dirty is None:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label, reason="generation missing")
    owner_id = con.execute(
        "SELECT owner_session_id FROM drive_dirty_generations "
        "WHERE drive_label=? AND identity_epoch=? AND generation=?", [label, epoch, generation]).fetchone()[0]
    owner = (con.execute("SELECT * FROM execution_sessions WHERE session_id=?", [owner_id]).fetchone()
             if owner_id is not None else None)
    clean = dm._generation_is_clean(con, label, epoch, generation, fingerprint, capacity, write_authority)
    current_anchor = con.execute(
        "SELECT * FROM drive_clean_anchors WHERE drive_label=? AND identity_epoch=? AND generation=?",
        [label, epoch, generation]).fetchone()
    if current_anchor is not None and not clean:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label, reason="contradictory anchor")
    anchor = current_anchor or con.execute(
        "SELECT * FROM drive_clean_anchors WHERE drive_label=? AND identity_epoch=? AND generation<? "
        "AND identity_fingerprint=? AND filesystem_capacity_bytes=? AND write_authority=? "
        "ORDER BY generation DESC LIMIT 1", [label, epoch, generation, fingerprint, capacity, write_authority]
    ).fetchone()
    canonical = capacity_evidence.identity_fingerprint_v1(
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial, filesystem_capacity_bytes=capacity)
    if not isinstance(serial, str) or not serial or serial != serial.strip() or anchor is None:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label, reason="serial or anchor missing")
    if fingerprint == canonical:
        status = "already_correct" if clean else "correct_identity_dirty"
    else:
        columns = [r[1] for r in con.execute("PRAGMA table_info(drive_clean_anchors)")]
        proof = dict(zip(columns, anchor))
        try:
            recognize_legacy_anchor(
                fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial, fingerprint=fingerprint,
                filesystem_capacity_bytes=capacity, anchor_identity_proof=proof["identity_proof"],
                anchor_fingerprint=proof["identity_fingerprint"],
                anchor_capacity_bytes=proof["filesystem_capacity_bytes"],
                anchor_authority=proof["write_authority"])
        except SerialIdentityUnproven as exc:
            raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label, reason=str(exc)) from exc
        status = "legacy_clean" if clean else "legacy_dirty"
    return {"catalog_version": version, "drive_label": label, "facts": facts, "metadata": metadata,
            "dirty_generation": dirty, "owner_session": owner, "anchor": anchor, "status": status,
            "canonical_fingerprint": canonical,
            "affected_approvals": approved_proposals_bound_to_drive(con, label)}


def _serial_binding(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def inspect_serial_identity(con, label: str) -> dict:
    """Report saved repair evidence without changing it or authorizing archive IO.

    The binding is operator intent for one exact captured state. Repair must still
    prove live media, ownership, inventory and all fences; inspection is not admission.
    """
    own = not con.in_transaction
    if own:
        con.execute("BEGIN")
    try:
        state = _serial_repair_state(con, label)
        epoch, generation, fingerprint = state["facts"][:3]
        return {"drive_label": label, "status": state["status"], "binding": _serial_binding(state),
                "identity_epoch": epoch, "generation": generation, "old_fingerprint": fingerprint,
                "canonical_fingerprint": state["canonical_fingerprint"],
                "affected_approvals": state["affected_approvals"], "requires_live_verification": True}
    finally:
        if own:
            con.execute("ROLLBACK")


def _serial_live(con, label, state):
    ev = _live_evidence(con, label)
    facts = state["facts"]
    if (not ev.proven or not ev.path or ev.fs_uuid != facts[5] or ev.annex_uuid != facts[6]
            or ev.serial != facts[7] or ev.capacity != facts[3]
            or ev.fingerprint != state["canonical_fingerprint"]
            or type(ev.free) is not int or not 0 <= ev.free <= facts[3]):
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_LIVE_MISMATCH", drive=label)
    return ev


def _serial_guard(con, label, expected):
    from modelark.execution_session import require_no_live_session
    require_no_live_session(con)
    if _serial_repair_state(con, label) != expected:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_STALE", drive=label)


def _serial_recover_locked(con, label, state, owner, final, now):
    """Bridge only the existing dirty generation to its OLD identity, never enrich it."""
    from modelark.proposal import bump_revision
    _serial_guard(con, label, state)
    if state["status"] != "legacy_dirty":
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label)
    observed_owner = _capture_recovery_owner(con, label, state["facts"], expected=owner)
    if observed_owner != owner:
        raise dm.DriveMutationRefused("DRIVE_RECOVERY_OWNER_CHANGED", drive=label)
    facts = state["facts"]
    legacy_proof = json.dumps({"v": 1, "fs_uuid": facts[5], "annex_uuid": facts[6], "serial": None},
                              sort_keys=True, separators=(",", ":"))
    # Identity proof deliberately encodes the legacy identity; fence proof retains
    # the newly observed serial. Neither claims that today's serial was absent.
    observation = dm.Observation(True, final.free, final.capacity, facts[2],
                                 legacy_proof, final.observation().identity_proof)
    dm._publish_anchor_locked(con, label, facts[0], facts[1], observation, now)
    bump_revision(con)
    clean = _serial_repair_state(con, label)
    if clean["status"] != "legacy_clean" or any(
            clean[key] != state[key] for key in state if key not in {"anchor", "status"}):
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_STALE", drive=label)
    return clean


def _serial_enrich_locked(con, label, state, final, now):
    """One composite transaction: old clean -> new generation -> new identity -> anchor."""
    from modelark.proposal import bump_revision, supersede_serial_repair_approvals
    _serial_guard(con, label, state)
    if state["status"] != "legacy_clean":
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label)
    facts = state["facts"]
    generation = dm._advance_one(con, label, "serial_identity_repair", captured=facts)
    con.execute("UPDATE drives SET identity_fingerprint=? WHERE drive_label=?",
                [state["canonical_fingerprint"], label])
    dm._publish_anchor_locked(con, label, facts[0], generation, final.observation(), now)
    affected = supersede_serial_repair_approvals(con, label)
    con.execute(f"PRAGMA user_version={SERIAL_REPAIR_CATALOG_VERSION}")
    bump_revision(con)
    return generation, affected


def _serial_backup_rehearsal(con, label, state, owner, final, now, *, artifacts):
    """Retain a consistent pre-repair backup and rehearse only catalog transactions.

    Archive inspection has already completed under the real fences. Replay uses
    those captured observations on a disposable clone, never changes archive bytes,
    and is not a general catalog replacement/publication mechanism.
    """
    source = next((r[2] for r in con.execute("PRAGMA database_list") if r[1] == "main"), "")
    if not source:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_BACKUP_REQUIRED", drive=label)
    directory = Path(tempfile.mkdtemp(prefix=".serial-repair-", dir=Path(source).resolve().parent))
    backup_path, rehearsal_path = directory / "before.sqlite", directory / "rehearsal.sqlite"
    # Preserve attempted artifact locations even if creation/rehearsal/fsync fails.
    # A failure report is not evidence that either file is a validated backup.
    artifacts.update(backup=str(backup_path), rehearsal=str(rehearsal_path))
    backup = sqlite3.connect(backup_path, isolation_level=None)
    try:
        con.backup(backup)
        if (backup.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                or backup.execute("PRAGMA foreign_key_check").fetchall()):
            raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_BACKUP_INVALID", drive=label)
        if _serial_repair_state(backup, label) != state:
            raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_STALE", drive=label)
        rehearsal = sqlite3.connect(rehearsal_path, isolation_level=None)
        try:
            backup.backup(rehearsal)
            rehearsal.execute("PRAGMA foreign_keys=ON")
            if state["status"] == "legacy_dirty":
                dm._immediate(rehearsal, lambda: _serial_recover_locked(
                    rehearsal, label, state, owner, final, now))
            clean = _serial_repair_state(rehearsal, label)
            dm._immediate(rehearsal, lambda: _serial_enrich_locked(rehearsal, label, clean, final, now))
            if (rehearsal.execute("PRAGMA integrity_check").fetchone() != ("ok",)
                    or rehearsal.execute("PRAGMA foreign_key_check").fetchall()):
                raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_REHEARSAL_FAILED", drive=label)
        finally:
            rehearsal.close()
    finally:
        backup.close()
    for path in (backup_path, rehearsal_path):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    for parent in (directory, directory.parent):
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def repair_serial_identity(con, label: str, *, expected_binding: str, now,
                           writers_stopped: bool = False, blocking: bool = False,
                           progress: ProgressCallback | None = None) -> dict:
    """Explicit bound repair; never called by normal reconciliation or catalog open."""
    from modelark.execution_session import require_no_live_session
    if writers_stopped is not True:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_QUIESCENCE_REQUIRED", drive=label)
    if con.in_transaction:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_TRANSACTION_ACTIVE", drive=label)
    if not isinstance(expected_binding, str) or len(expected_binding) != 64:
        raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_BINDING_REQUIRED", drive=label)
    require_no_live_session(con)
    recovered = False
    artifacts = {}
    try:
        with drive_fence.hold_controller(db.DB_PATH, blocking=blocking):
            require_no_live_session(con)
            state = _serial_repair_state(con, label)
            if _serial_binding(state) != expected_binding:
                raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_STALE", drive=label)
            facts = state["facts"]
            keys = FenceIdentity(facts[5], facts[6], facts[7], facts[3], facts[0], facts[2]).lock_keys()
            with drive_fence.hold_drives_sorted(keys, blocking=blocking):
                _serial_guard(con, label, state)
                first = _serial_live(con, label, state)
                if state["status"] == "already_correct":
                    return {**inspect_serial_identity(con, label), "observed_serial": first.serial}
                if state["status"] not in {"legacy_clean", "legacy_dirty"}:
                    raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_UNPROVEN", drive=label)
                owner = _capture_recovery_owner(con, label, facts)
                inventory = _require_complete_inventory(con, label, first.path, progress=progress)
                final = _serial_live(con, label, state)
                _serial_guard(con, label, state)
                _serial_backup_rehearsal(con, label, state, owner, final, now, artifacts=artifacts)
                _serial_guard(con, label, state)
                if state["status"] == "legacy_dirty":
                    def recover():
                        # BEGIN may wait for an unrelated catalog writer. Refresh
                        # hardware only after acquisition, not before that wait.
                        observed = _serial_live(con, label, state)
                        return _serial_recover_locked(con, label, state, owner, observed, now)

                    state = dm._immediate(con, recover)
                    recovered = True
                    _emit_progress(progress, "serial_legacy_recovered")
                    _serial_guard(con, label, state)

                def enrich():
                    # The actual catalog commit uses fresh identity/free-space
                    # evidence; clone rehearsal deliberately uses captured data.
                    observed = _serial_live(con, label, state)
                    return _serial_enrich_locked(con, label, state, observed, now), observed

                (generation, affected), final = dm._immediate(con, enrich)
                return {"drive_label": label, "status": "repaired", "identity_epoch": facts[0],
                        "generation": generation, "old_fingerprint": facts[2],
                        "canonical_fingerprint": final.fingerprint, "observed_serial": final.serial,
                        "legacy_recovered": recovered, "superseded_approvals": affected,
                        "inventory_present": len(inventory.present), "inventory_extra": len(inventory.extra),
                        "inventory_debris": len(inventory.debris), **artifacts}
    except drive_fence.FenceUnavailable as exc:
        raise dm.DriveMutationRefused("DRIVE_FENCE_UNAVAILABLE", **exc.evidence,
                                     legacy_recovered=recovered, **artifacts) from exc
    except dm.DriveMutationRefused as exc:
        exc.evidence.update(legacy_recovered=recovered, **artifacts)
        raise
    except Exception as exc:
        raise dm.DriveMutationRefused(getattr(exc, "code", "DRIVE_SERIAL_REPAIR_FAILED"),
                                     drive=label, reason=str(exc), legacy_recovered=recovered,
                                     **artifacts) from exc


def _stable_identity_matches(ev: _LiveEvidence, persisted_fs, persisted_annex) -> bool:
    """Compare stable UUIDs; the observation separately verifies a known saved serial."""
    if persisted_fs is not None and ev.fs_uuid != persisted_fs:
        return False
    if persisted_annex is not None and ev.annex_uuid != persisted_annex:
        return False
    return True


_ENDED_STATES = frozenset({"paused", "blocked", "stopped", "failed", "done"})


@dataclass(frozen=True)
class _RecoveryOwner:
    attempt: authority.Attempt
    state: str


def _dirty_owner(con, label, facts):
    epoch, gen, fp, cap, auth = facts[:5]
    if dm._generation_is_clean(con, label, epoch, gen, fp, cap, auth):
        return None
    row = con.execute(
        "SELECT owner_session_id,owner_fencing_token FROM drive_dirty_generations "
        "WHERE drive_label=? AND identity_epoch=? AND generation=?", [label, epoch, gen]
    ).fetchone()
    return None if row is None or row == (None, None) else row


def _capture_recovery_owner(con, label, facts, *, expected=None):
    """Validate durable identity; callers separately retain the physical exclusion fences."""
    code = "DRIVE_RECOVERY_OWNER_CHANGED" if expected is not None else "DRIVE_RECOVERY_OWNER_UNPROVEN"
    row = _dirty_owner(con, label, facts)
    if row is None and expected is None:
        return None
    try:
        if (row is None or not isinstance(row[0], str) or not row[0].strip()
                or type(row[1]) is not int or row[1] < 1):
            raise authority.AuthorityLost("incomplete dirty owner")
        attempt = authority.Attempt(row[0], int(row[1]))
        session = con.execute(
            "SELECT fencing_token,state FROM execution_sessions WHERE session_id=?", [attempt.owner]
        ).fetchone()
        if session is None or type(session[0]) is not int:
            raise authority.AuthorityLost("missing owner session/token")
        captured = _RecoveryOwner(attempt, session[1])
        if expected is not None and captured != expected:
            raise authority.AuthorityLost("dirty owner or state changed")
        authority.require_current(
            attempt, authority.Attempt(attempt.owner, int(session[0])), session[1],
            {expected.state} if expected is not None else _ENDED_STATES,
        )
    except authority.AuthorityLost as exc:
        raise dm.DriveMutationRefused(code, drive=label) from exc
    from modelark.execution_recovery import child_fence_still_held
    try:
        held = child_fence_still_held(session_id=attempt.owner)
    except OSError as exc:
        raise dm.DriveMutationRefused("DRIVE_RECOVERY_CHILD_UNPROVEN", drive=label) from exc
    if held:
        raise dm.DriveMutationRefused("DRIVE_RECOVERY_CHILD_UNPROVEN", drive=label)
    return captured


def _recover_owned_generation(con, label, dest, facts, owner, ev, now, progress):
    """Inventory outside SQLite's write transaction, then CAS + anchor in one short commit."""
    from modelark.execution_session import require_no_live_session
    from modelark.proposal import bump_revision

    inventory = _require_complete_inventory(con, label, dest, progress=progress)
    final = _final_observation(con, label, ev)
    epoch, gen = facts[:2]

    def publish():
        require_no_live_session(con)
        if _persisted(con, label) != facts:
            raise dm.DriveMutationRefused("DRIVE_RECOVERY_OWNER_CHANGED", drive=label)
        _capture_recovery_owner(con, label, facts, expected=owner)
        dm._publish_anchor_locked(con, label, epoch, gen, final.observation(), now)
        bump_revision(con)

    dm._immediate(con, publish)
    return Reconciliation("recovered", epoch, gen, final.free, inventory=inventory)


def reconcile_drive(con, label: str, *, now, dedicated: bool = False, accept_drift: bool = False,
                    blocking: bool = True,
                    progress: ProgressCallback | None = None) -> Reconciliation:
    """Bootstrap / refresh / epoch-transition / recover one drive under the controller + drive fences,
    committing identity evidence + generation + anchor + authority atomically. See the module docstring
    for the full contract; raises a typed ``dm.DriveMutationRefused`` for every fail-closed path."""
    from modelark.execution_session import require_no_live_session
    require_no_live_session(con)
    # Early diagnostic only: capture again after both fences, before the inventory.
    _capture_recovery_owner(con, label, _persisted(con, label))
    try:
        with drive_fence.hold_controller(db.DB_PATH, blocking=blocking):
            require_no_live_session(con)
            facts = _persisted(con, label)
            p_epoch, p_gen, p_fp, p_cap, p_auth, p_fs, p_annex, p_serial = facts
            owner = _capture_recovery_owner(con, label, facts)
            ev = _live_evidence(con, label)
            if not ev.proven:
                raise dm.DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
            if not _stable_identity_matches(ev, p_fs, p_annex):      # different media under an existing label
                raise dm.DriveMutationRefused("DRIVE_IDENTITY_MISMATCH", drive=label,
                                              persisted=(p_fs, p_annex), live=(ev.fs_uuid, ev.annex_uuid))
            if is_legacy_serial_mismatch(
                    fs_uuid=p_fs, annex_uuid=p_annex, serial=p_serial, fingerprint=p_fp,
                    filesystem_capacity_bytes=p_cap, live_fingerprint=ev.fingerprint):
                raise dm.DriveMutationRefused("DRIVE_SERIAL_REPAIR_REQUIRED", drive=label)
            if not dedicated:                            # exclusivity is an explicit assertion, not a probe
                if p_auth == "dedicated_local":
                    raise dm.DriveMutationRefused("DRIVE_AUTHORITY_DOWNGRADE_REFUSED", drive=label)
                return Reconciliation("unknown_no_authority", p_epoch, p_gen, None)
            # A capacity change on the same stable identity transitions the epoch and changes the
            # fingerprint, so hold BOTH the old and the prospective new (fingerprint, epoch) drive fences.
            transition = p_fp is not None and ev.capacity != p_cap
            if owner is not None and (p_gen <= 0 or p_fp != ev.fingerprint or p_cap != ev.capacity
                                      or p_auth != "dedicated_local"):
                raise dm.DriveMutationRefused("DRIVE_RECOVERY_IDENTITY_CHANGED", drive=label)
            identities = [FenceIdentity(
                ev.fs_uuid, ev.annex_uuid, p_serial, ev.capacity,
                p_epoch + 1 if transition else p_epoch, ev.fingerprint)]
            if p_fp is not None:
                identities.append(FenceIdentity(p_fs, p_annex, p_serial, p_cap, p_epoch, p_fp))
            try:
                keyed = compatible_keys(identities)
            except UnprovenFenceIdentity as exc:
                raise dm.DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label, reason=str(exc)) from exc
            with drive_fence.hold_drives_sorted(keyed, blocking=blocking):
                require_no_live_session(con)
                if _persisted(con, label) != facts:
                    raise dm.DriveMutationRefused("DRIVE_RECOVERY_OWNER_CHANGED", drive=label)
                fenced_owner = _capture_recovery_owner(con, label, facts, expected=owner)
                if fenced_owner != owner:
                    raise dm.DriveMutationRefused("DRIVE_RECOVERY_OWNER_CHANGED", drive=label)
                owner = fenced_owner
                dest = register.archive_path(con, label)
                if owner is not None:
                    return _recover_owned_generation(con, label, dest, facts, owner, ev, now, progress)
                return _decide_and_commit(con, label, dest, p_epoch, p_gen, p_fp, p_cap, ev, now,
                                          accept_drift, transition, progress, facts)
    except drive_fence.FenceUnavailable as exc:
        raise dm.DriveMutationRefused("DRIVE_FENCE_UNAVAILABLE", **exc.evidence) from exc


def _final_observation(con, label: str, ev: _LiveEvidence) -> _LiveEvidence:
    """A FRESH observation under the held drive fences after inventory. Refuse (no anchor) if the drive
    vanished or its stable identity/capacity changed during inventory — the anchor must reflect the
    final observed state, never the pre-inventory one."""
    final = _live_evidence(con, label)
    if (not final.proven or final.fingerprint != ev.fingerprint or final.capacity != ev.capacity
            or final.serial != ev.serial):
        raise dm.DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
    return final


def _decide_and_commit(
    con,
    label,
    dest,
    p_epoch,
    p_gen,
    p_fp,
    p_cap,
    ev,
    now,
    accept_drift,
    transition,
    progress,
    captured_facts,
):
    def commit(body):
        def checked():
            from modelark.execution_session import require_no_live_session
            require_no_live_session(con)
            if (_persisted(con, label) != captured_facts
                    or _dirty_owner(con, label, captured_facts) is not None):
                raise dm.DriveMutationRefused("DRIVE_RECOVERY_OWNER_CHANGED", drive=label)
            return body()
        return dm._immediate(con, checked)

    if p_fp is None:                                     # (A) bootstrap: establish identity + first anchor
        inventory = _require_complete_inventory(con, label, dest, progress=progress)
        final = _final_observation(con, label, ev)
        obs = final.observation()

        def _body():
            con.execute("UPDATE drives SET identity_epoch=?, filesystem_capacity_bytes=?, "
                        "identity_fingerprint=?, write_authority='dedicated_local' WHERE drive_label=?",
                        [p_epoch, final.capacity, final.fingerprint, label])
            gen = dm._advance_one(con, label, "bootstrap")           # generation 0 -> 1
            dm._publish_anchor_locked(con, label, p_epoch, gen, obs, now)
            from modelark.proposal import bump_revision
            bump_revision(con)
            return gen
        return Reconciliation(
            "bootstrapped",
            p_epoch,
            commit(_body),
            final.free,
            inventory=inventory,
        )

    if transition:                                       # (D) capacity-epoch transition: reset the namespace
        inventory = _require_complete_inventory(con, label, dest, progress=progress)
        final = _final_observation(con, label, ev)
        obs = final.observation()
        new_epoch = p_epoch + 1

        def _body():
            con.execute("UPDATE drives SET identity_epoch=?, filesystem_capacity_bytes=?, "
                        "identity_fingerprint=?, write_generation=0 WHERE drive_label=?",
                        [new_epoch, final.capacity, final.fingerprint, label])   # persist the NEW fingerprint
            gen = dm._advance_one(con, label, "epoch")               # 0 -> 1 under the new epoch
            dm._publish_anchor_locked(con, label, new_epoch, gen, obs, now)
            from modelark.proposal import bump_revision
            bump_revision(con)
            return gen
        return Reconciliation(
            "epoch_advanced",
            new_epoch,
            commit(_body),
            final.free,
            inventory=inventory,
        )

    if p_gen == 0:
        # Identity known but never dirty-advanced (generation 0 has no anchor namespace): open gen 1.
        inventory = _require_complete_inventory(con, label, dest, progress=progress)
        final = _final_observation(con, label, ev)
        obs = final.observation()

        def _body0():
            gen = dm._advance_one(con, label, "reconcile")
            dm._publish_anchor_locked(con, label, p_epoch, gen, obs, now)
            from modelark.proposal import bump_revision
            bump_revision(con)
            return gen
        return Reconciliation(
            "refreshed",
            p_epoch,
            commit(_body0),
            final.free,
            inventory=inventory,
        )

    if not dm._generation_is_clean(con, label, p_epoch, p_gen, p_fp, p_cap, "dedicated_local"):
        # (B) sessionless recovery; validated ended owners use the guarded path above.
        owner = con.execute("SELECT owner_session_id FROM drive_dirty_generations "
                            "WHERE drive_label=? AND identity_epoch=? AND generation=?",
                            [label, p_epoch, p_gen]).fetchone()
        if owner is not None and owner[0] is not None:
            raise dm.DriveMutationRefused("DRIVE_RECOVERY_SESSION_ACTIVE", drive=label)
        inventory = _require_complete_inventory(con, label, dest, progress=progress)
        final = _final_observation(con, label, ev)
        def publish():
            dm._publish_anchor_locked(con, label, p_epoch, p_gen, final.observation(), now)
            from modelark.proposal import bump_revision
            bump_revision(con)
        commit(publish)
        return Reconciliation(
            "recovered",
            p_epoch,
            p_gen,
            final.free,
            inventory=inventory,
        )

    # (C) refresh of a currently-clean anchored drive — drift-gated on the first observation (refuse
    # before the full inventory), then anchored from the fresh final observation
    last_free = con.execute("SELECT anchor_free_bytes FROM drive_clean_anchors WHERE drive_label=? "
                           "AND identity_epoch=? AND generation=?", [label, p_epoch, p_gen]).fetchone()[0]
    drifted = abs(ev.free - last_free) > free_drift_tolerance_v1(ev.alloc_unit)
    if drifted and not accept_drift:
        raise dm.DriveMutationRefused("DRIVE_FREE_DRIFT", drive=label, anchored=last_free, observed=ev.free)
    inventory = _require_complete_inventory(con, label, dest, progress=progress)
    final = _final_observation(con, label, ev)
    obs = final.observation()
    op = "accept-drift" if drifted else "reconcile"

    def _body():
        gen = dm._advance_one(con, label, op)                        # clean -> generation + 1
        dm._publish_anchor_locked(con, label, p_epoch, gen, obs, now)
        from modelark.proposal import bump_revision
        bump_revision(con)
        return gen
    return Reconciliation(
        "drift_accepted" if drifted else "refreshed",
        p_epoch,
        commit(_body),
        final.free,
        inventory=inventory,
    )
