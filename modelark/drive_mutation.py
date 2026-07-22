"""Catalog-v3 physical-mutation envelope (RFC-002 / DEC-049 #35-B slice 1).

The complete internal facility that fences a filesystem mutation over one or more drives:

    controller flock -> sorted per-drive flocks -> short SQLite transaction

Under the held fences it proves each drive's identity BEFORE dirtying, advances the dirty generation
atomically (all drives in one transaction, both owner fields null) before any allocation, runs the
caller's body, reconciles only the generation's touched paths/keys, and publishes a FRESH clean anchor
under a captured ``(identity_epoch, generation)`` CAS. Any failure after dirtying leaves the generation
durably dirty (no clean anchor); an initial identity failure refuses without dirtying.

PR-03a is dormant: no production call site invokes this yet (child-FD inheritance + transport
integration = PR-03b; registration/recovery/full inventory = PR-03c; admission cutover = #35-C;
durable sessions/fencing tokens = #39).
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from modelark import drive_fence
from modelark.core import db


@dataclass(frozen=True)
class Observation:
    """A fenced observation of a drive: whether its identity is proven, the raw free bytes, and the
    attested current filesystem capacity + fingerprint (plus opaque audit proofs)."""
    identity_proven: bool
    free_bytes: int | None
    filesystem_capacity: int | None
    fingerprint: str | None
    identity_proof: str
    fence_proof: str


class DriveMutationRefused(Exception):
    """A typed, expected refusal (not an integrity defect). ``code`` is the contract outcome."""

    def __init__(self, code, **evidence):
        super().__init__(code)
        self.code = code
        self.evidence = evidence


class _Writer:
    """Records the exact paths and annex keys a mutation touched per drive, for generation-scoped
    reconciliation (no full-drive inventory)."""

    def __init__(self):
        self._touched = {}

    def record_touched(self, drive_label, *, paths=(), keys=()):
        entry = self._touched.setdefault(drive_label, ([], []))
        entry[0].extend(paths)
        entry[1].extend(keys)

    def touched_for(self, drive_label):
        paths, keys = self._touched.get(drive_label, ([], []))
        return tuple(paths), tuple(keys)


def _immediate(con, body):
    """Run ``body`` inside one short BEGIN IMMEDIATE transaction; rollback and re-raise on any error
    (the original exception type is preserved)."""
    con.execute("BEGIN IMMEDIATE")
    try:
        result = body()
        con.execute("COMMIT")
        return result
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _drive_facts(con, label):
    row = con.execute(
        "SELECT identity_epoch, write_generation, identity_fingerprint, filesystem_capacity_bytes, "
        "write_authority FROM drives WHERE drive_label=?", [label]).fetchone()
    if row is None:
        raise DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
    return row  # (identity_epoch, write_generation, identity_fingerprint, filesystem_capacity, authority)


def _generation_is_clean(con, label, epoch, generation, fingerprint, capacity, authority):
    """A current generation is clean only if a matching anchor exists whose identity/capacity/authority
    still equal the drive's current values — anchor existence alone is not enough."""
    anchor = con.execute(
        "SELECT identity_epoch, identity_fingerprint, filesystem_capacity_bytes, write_authority "
        "FROM drive_clean_anchors WHERE drive_label=? AND identity_epoch=? AND generation=?",
        [label, epoch, generation]).fetchone()
    return anchor is not None and anchor == (epoch, fingerprint, capacity, authority)


def _advance_one(con, label, operation_code, captured=None):
    """Guarded dirty-generation advance for one drive, within the caller's transaction. When
    ``captured`` (the epoch/fingerprint the drive locks were derived from) is given, revalidate the
    current identity right before dirtying: a lifecycle change since capture is refused."""
    epoch, generation, fingerprint, capacity, authority = _drive_facts(con, label)
    if captured is not None and (epoch, fingerprint) != (captured[0], captured[2]):
        raise DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
    if generation == 0:
        new_generation = 1
    elif _generation_is_clean(con, label, epoch, generation, fingerprint, capacity, authority):
        new_generation = generation + 1
    else:
        raise DriveMutationRefused("DIRTY_GENERATION_CONFLICT", drive=label)
    con.execute("INSERT INTO drive_dirty_generations"
                "(drive_label,identity_epoch,generation,operation_code) VALUES(?,?,?,?)",
                [label, epoch, new_generation, operation_code])
    con.execute("UPDATE drives SET write_generation=? WHERE drive_label=?", [new_generation, label])
    return new_generation


def begin_generation(con, label, operation_code):
    """Advance one drive's dirty generation in a single short transaction (both owner fields null)."""
    return _immediate(con, lambda: _advance_one(con, label, operation_code))


def _require_identity(observation, fingerprint, capacity, label):
    if (not observation.identity_proven or observation.fingerprint != fingerprint
            or observation.filesystem_capacity != capacity):
        raise DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)


def _publish_anchor_locked(con, label, identity_epoch, generation, observation, now):
    """Publish one clean anchor under a captured (identity_epoch, generation) CAS, WITHOUT its own
    transaction (so multiple drives publish atomically in one caller transaction)."""
    epoch, current_generation, fingerprint, capacity, authority = _drive_facts(con, label)
    if (epoch, current_generation) != (identity_epoch, generation):
        raise DriveMutationRefused("CLEAN_ANCHOR_CAS_FAILED", drive=label,
                                   captured=(identity_epoch, generation),
                                   current=(epoch, current_generation))
    _require_identity(observation, fingerprint, capacity, label)
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
        "observed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [label, identity_epoch, generation, observation.free_bytes, observation.filesystem_capacity,
         observation.fingerprint, authority, observation.identity_proof, observation.fence_proof, now])


def publish_clean_anchor(con, label, identity_epoch, generation, observation, now):
    """Publish one clean anchor in its own short transaction (single-drive/direct use)."""
    return _immediate(
        con, lambda: _publish_anchor_locked(con, label, identity_epoch, generation, observation, now))


@contextmanager
def drive_mutation(con, drive_labels, operation_code, *, observe, reconcile, now, blocking=True):
    """Fence, dirty, run ``body``, reconcile the touched set, and publish a fresh clean anchor for
    every drive. ``observe(label) -> Observation`` is the fenced identity/free reader; ``reconcile(
    label, paths, keys)`` reconciles the generation's touched set. Yields a writer with
    ``record_touched(label, paths=…, keys=…)``."""
    try:
        # Controller fence FIRST: no lifecycle change can race between reading facts and dirtying,
        # so identity facts + drive-lock keys are captured under it (not before it).
        with drive_fence.hold_controller(db.DB_PATH, blocking=blocking):
            facts = {label: _drive_facts(con, label) for label in drive_labels}
            for label, (_epoch, _gen, fingerprint, _cap, _auth) in facts.items():
                if not fingerprint:                  # only a stable proven identity may be locked here
                    raise DriveMutationRefused("DRIVE_IDENTITY_UNPROVEN", drive=label)
            keyed = sorted((facts[label][2], facts[label][0]) for label in drive_labels)  # (fp, epoch)
            with drive_fence.hold_drives_sorted(keyed, blocking=blocking):
                # identity proven under BOTH fences before any dirtying
                for label, (_epoch, _gen, fingerprint, capacity, _auth) in facts.items():
                    _require_identity(observe(label), fingerprint, capacity, label)
                # atomic dirty-generation advance across all drives, revalidating captured identity
                captured = _immediate(con, lambda: {
                    label: _advance_one(con, label, operation_code, facts[label])
                    for label in drive_labels})
                writer = _Writer()
                yield writer
                # collect ALL candidate anchors (reconcile + fresh observation per drive), then publish
                # them in ONE transaction so a later drive's failure leaves no drive marked clean
                candidates = {}
                for label, (_epoch, _gen, fingerprint, capacity, _auth) in facts.items():
                    paths, keys = writer.touched_for(label)
                    reconcile(label, paths, keys)
                    observation = observe(label)
                    _require_identity(observation, fingerprint, capacity, label)
                    candidates[label] = observation
                _immediate(con, lambda: [
                    _publish_anchor_locked(con, label, facts[label][0], captured[label],
                                           candidates[label], now)
                    for label in drive_labels])
    except drive_fence.FenceUnavailable as exc:
        raise DriveMutationRefused("DRIVE_FENCE_UNAVAILABLE", **exc.evidence) from exc
