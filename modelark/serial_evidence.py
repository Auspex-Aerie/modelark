"""Read-only resolution of the one explicit bridge repair in a drive epoch.

The catalog is trusted append-only evidence, not live hardware proof. A binding
is ephemeral: consumers must still prove the current attachment and retain their
normal physical fences. Slice additionally supplies its exact sealed source.
"""
from dataclasses import dataclass

from modelark.serial_identity import (
    SerialIdentityUnproven, _identity_proof, recognize_bridge_anchor,
)


@dataclass(frozen=True)
class BridgeBinding:
    fs_uuid: str
    annex_uuid: str
    serial: str
    observed_serial: str
    capacity: int

    def match(self, fs_uuid, annex_uuid, serial, capacity):
        if ((fs_uuid, annex_uuid, capacity) != (self.fs_uuid, self.annex_uuid, self.capacity)
                or type(capacity) is not int or serial not in (self.serial, self.observed_serial)):
            raise SerialIdentityUnproven('live attachment does not match proven bridge repair')
        return self.serial


def _anchor(con, label, epoch, generation):
    row = con.execute(
        'SELECT anchor_id,identity_fingerprint,filesystem_capacity_bytes,write_authority,'
        'identity_proof,fence_proof FROM drive_clean_anchors '
        'WHERE drive_label=? AND identity_epoch=? AND generation=?',
        [label, epoch, generation]).fetchone()
    if row is None:
        raise SerialIdentityUnproven('repair or sealed clean anchor missing')
    return row


def load_bridge_binding(con, label, *, sealed_source=None):
    """Return a proven transition or None; never select an arbitrary old serial.

    Call within a consistent catalog transaction and the consumer's physical
    fence. Literal matching does not require this optional compatibility proof.
    A missing/invalid/ambiguous bridge transition must never produce a binding.
    Such absence is not a refusal of the ordinary literal/serial-less path.
    """
    try:
        return _load_bridge_binding(con, label, sealed_source=sealed_source)
    except SerialIdentityUnproven:
        return None


def _load_bridge_binding(con, label, *, sealed_source):
    row = con.execute(
        'SELECT fs_uuid,annex_uuid,serial,filesystem_capacity_bytes,identity_epoch,'
        'write_generation,identity_fingerprint,write_authority,lifecycle '
        'FROM drives WHERE drive_label=?', [label]).fetchone()
    if row is None:
        return None
    fs, annex, serial, capacity, epoch, generation, fingerprint, authority, lifecycle = row
    if (type(epoch) is not int or epoch < 1 or type(generation) is not int or generation < 1
            or type(capacity) is not int or capacity <= 0
            or authority != 'dedicated_local' or lifecycle != 'active'):
        return None
    transitions = con.execute(
        "SELECT generation FROM drive_dirty_generations WHERE drive_label=? "
        "AND identity_epoch=? AND operation_code='serial_identity_repair' ORDER BY generation",
        [label, epoch]).fetchall()
    if len(transitions) != 1:
        return None
    repaired = transitions[0][0]
    if type(repaired) is not int or not 1 < repaired <= generation:
        return None
    old = _anchor(con, label, epoch, repaired - 1)
    new = _anchor(con, label, epoch, repaired)
    correction = recognize_bridge_anchor(
        fs_uuid=fs, annex_uuid=annex, serial=serial, fingerprint=old[1],
        filesystem_capacity_bytes=capacity, anchor_identity_proof=old[4],
        anchor_fence_proof=old[5], anchor_fingerprint=old[1], anchor_capacity_bytes=old[2],
        anchor_authority=old[3])
    canonical = dict(v=1, fs_uuid=fs, annex_uuid=annex, serial=serial)
    raw = dict(canonical, serial=correction.observed_serial)
    if (new[1:4] != (correction.new_fingerprint, capacity, authority)
            or fingerprint != correction.new_fingerprint
            or _identity_proof(new[4]) != canonical or _identity_proof(new[5]) != raw):
        raise SerialIdentityUnproven('repair transition does not bind canonical identity and raw observation')
    # This is an additional condition for GRANTING encoded-serial matching,
    # not an additional gate on ordinary reads. No binding means literal only.
    if sealed_source is not None:
        d, a = sealed_source.drive, sealed_source.anchor
        if ((label, fs, annex, serial, capacity, epoch, generation, fingerprint, authority, lifecycle)
                != (d.drive_label, d.fs_uuid, d.annex_uuid, d.serial, d.filesystem_capacity_bytes,
                    d.identity_epoch, d.write_generation, d.identity_fingerprint,
                    d.write_authority, d.lifecycle)):
            raise SerialIdentityUnproven('current drive differs from sealed source')
        current = _anchor(con, label, epoch, generation)
        if ((label, epoch, generation, fingerprint, capacity, authority, str(current[0]))
                != (a.drive_label, a.identity_epoch, a.generation, a.identity_fingerprint,
                    a.filesystem_capacity_bytes, a.write_authority, a.anchor_id)
                or current[1:4] != (fingerprint, capacity, authority)):
            raise SerialIdentityUnproven('exact sealed clean anchor changed')
    return BridgeBinding(fs, annex, serial, correction.observed_serial, capacity)


def bridge_serial_for_observation(con, label, fs_uuid, annex_uuid, serial, capacity):
    """Resolve an exact registered/observed mismatch without implicit enrichment."""
    own = not con.in_transaction
    if own:
        con.execute('BEGIN')
    try:
        binding = load_bridge_binding(con, label)
        if binding is None:
            raise SerialIdentityUnproven('no unique proven bridge repair')
        return binding.match(fs_uuid, annex_uuid, serial, capacity)
    finally:
        if own:
            con.execute('ROLLBACK')
