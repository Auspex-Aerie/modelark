"""Read-only catalog-to-physical-archive attachment proof for publication.

Uses the existing attachment-bound volume and parent-disk observers, not a new
partition-serial policy or caller-provided observer callback. An observation is
not a mount lease: the coordinator must recheck it at its IO boundaries and bind
its QualifiedRepository to the same root/inode/mount and annex UUID. No catalog
update, repair, mount, initialization, content transfer or write probe occurs.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import subprocess

from modelark import publication_locks, publication_store as store, register
from modelark.block_identity import BlockObservationError
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.drive_mutation import DriveMutationRefused
from modelark.publication_policy import PublicationRefused, _require
from modelark.serial_evidence import bridge_serial_for_observation
from modelark.serial_identity import SerialIdentityUnproven, serial_for_identity
from modelark.slice.linux import BoundTree
from modelark.slice.transaction import TransferRefusal


def _scope(scope, label):
    _require(type(scope) is publication_locks._FenceScope,
             "PUBLICATION_FENCE_AUTHORITY_MISSING")
    try:
        scope.require_io()
    except DriveMutationRefused as exc:
        raise PublicationRefused(exc.code, **exc.evidence) from exc
    _require(isinstance(label, str) and label in scope.identities,
             "PUBLICATION_PARTICIPANT_UNSELECTED")


def _path(value):
    _require(value is not None, "PUBLICATION_ATTACHMENT_UNAVAILABLE")
    try:
        raw = os.fspath(value)
        _require(isinstance(raw, str) and bool(raw) and "\0" not in raw
                 and Path(raw).is_absolute() and ".." not in Path(raw).parts,
                 "PUBLICATION_ATTACHMENT_PATH_INVALID")
        canonical = os.path.abspath(raw)
        canonical.encode("utf-8")
        return canonical
    except (TypeError, UnicodeError, ValueError) as exc:
        if isinstance(exc, PublicationRefused):
            raise
        raise PublicationRefused("PUBLICATION_ATTACHMENT_PATH_INVALID") from exc


def _facts(scope, label):
    row = scope.connection.execute(
        "SELECT write_generation,write_authority,lifecycle FROM drives WHERE drive_label=?", [label]).fetchone()
    _require(row is not None and type(row[0]) is int and row[0] >= 0
             and row[1] == "dedicated_local" and row[2] == "active",
             "PUBLICATION_ATTACHMENT_AUTHORITY_UNPROVEN")
    return row


def _live(scope, label, expected, volume):
    _require(type(volume) is register.ArchiveVolumeObservation,
             "PUBLICATION_ATTACHMENT_OBSERVATION_INVALID")
    _require(type(volume.capacity_bytes) is int and volume.capacity_bytes > 0
             and type(volume.free_bytes) is int and 0 <= volume.free_bytes <= volume.capacity_bytes
             and type(volume.alloc_unit_bytes) is int and volume.alloc_unit_bytes > 0,
             "PUBLICATION_ATTACHMENT_CAPACITY_UNPROVEN")
    _require(volume.fs_uuid == expected.fs_uuid and bool(volume.fs_uuid)
             and volume.annex_uuid == expected.annex_uuid and bool(volume.annex_uuid)
             and volume.capacity_bytes == expected.filesystem_capacity_bytes,
             "PUBLICATION_ATTACHMENT_IDENTITY_MISMATCH")
    try:
        identity_serial = serial_for_identity(expected.serial, volume.serial)
        if expected.serial and identity_serial != expected.serial:
            # Exact historical bridge evidence is required; no generic hex
            # decoding, partition fallback, or invented catalog serial occurs.
            identity_serial = bridge_serial_for_observation(
                scope.connection, label, volume.fs_uuid, volume.annex_uuid,
                volume.serial, volume.capacity_bytes)
    except SerialIdentityUnproven as exc:
        raise PublicationRefused("PUBLICATION_ATTACHMENT_SERIAL_UNPROVEN") from exc
    _require(not expected.serial or identity_serial == expected.serial,
             "PUBLICATION_ATTACHMENT_IDENTITY_MISMATCH")
    fingerprint = identity_fingerprint_v1(
        fs_uuid=volume.fs_uuid, annex_uuid=volume.annex_uuid, serial=identity_serial,
        filesystem_capacity_bytes=volume.capacity_bytes)
    _require(fingerprint == expected.identity_fingerprint,
             "PUBLICATION_ATTACHMENT_IDENTITY_MISMATCH")
    return identity_serial, fingerprint


@dataclass(frozen=True)
class AttachmentProof:
    library_id: str
    map_uuid: str
    drive_label: str
    root: str
    root_identity: tuple[int, int, int]
    mount_id: int
    fs_uuid: str
    annex_uuid: str
    serial: str | None
    observed_serial: str | None
    filesystem_capacity_bytes: int
    free_bytes: int
    alloc_unit_bytes: int
    identity_epoch: int
    generation: int
    identity_fingerprint: str
    write_authority: str
    lifecycle: str

    def __post_init__(self):
        store.canonical_uuid(self.library_id)
        store.canonical_uuid(self.map_uuid)
        store.canonical_uuid(self.annex_uuid)
        store._require_digest(self.identity_fingerprint)
        _require(isinstance(self.drive_label, str) and bool(self.drive_label)
                 and isinstance(self.fs_uuid, str) and bool(self.fs_uuid)
                 and self.fs_uuid == self.fs_uuid.strip()
                 and self.root == _path(self.root), "PUBLICATION_ATTACHMENT_PROOF_INVALID")
        _require(type(self.root_identity) is tuple and len(self.root_identity) == 3
                 and all(type(value) is int and value >= 0 for value in self.root_identity)
                 and all(type(value) is int and value > 0 for value in (
                     self.mount_id, self.filesystem_capacity_bytes, self.alloc_unit_bytes,
                     self.identity_epoch))
                 and type(self.generation) is int and self.generation >= 0
                 and type(self.free_bytes) is int and 0 <= self.free_bytes <= self.filesystem_capacity_bytes
                 and self.write_authority == "dedicated_local" and self.lifecycle == "active",
                 "PUBLICATION_ATTACHMENT_PROOF_INVALID")
        try:
            serial_for_identity(self.serial, self.observed_serial)
        except SerialIdentityUnproven as exc:
            raise PublicationRefused("PUBLICATION_ATTACHMENT_PROOF_INVALID") from exc

    def record(self):
        return json.loads(store.canonical({"version": 1, "kind": "archive-attachment", **asdict(self)}))

    @property
    def digest(self):
        return store.digest(self.record())


def capture(scope: publication_locks._FenceScope, label: str) -> AttachmentProof:
    """Resolve the current catalog filesystem UUID and freshly prove its archive.

    The shared volume observer checks retained-directory attachment around every
    component, including live UUID/config reads, statvfs and whole-parent serial.
    The outer BoundTree also ties those observations to the root the publisher
    will use. Catalog/lifecycle/operation ownership is rechecked after physical IO.
    A proven newly registered drive may still have generation zero. This observes
    that initial attachment; only the existing owning lifecycle may dirty it, and
    the coordinator must capture again after its atomic generation advance.
    """
    _scope(scope, label)
    expected = scope.identities[label]
    _require(isinstance(expected.fs_uuid, str) and bool(expected.fs_uuid)
             and "\0" not in expected.fs_uuid and "/" not in expected.fs_uuid
             and expected.fs_uuid not in {".", ".."}, "PUBLICATION_ATTACHMENT_IDENTITY_MISMATCH")
    store.canonical_uuid(expected.annex_uuid)
    before = _facts(scope, label)
    try:
        root = _path(register.archive_path(scope.connection, label))
        with BoundTree(root) as tree:
            root_identity, mount_id = tuple(tree.identity(tree.fd)), tree.mount_id
            tree.check()
            volume = register.observe_archive_volume(root)
            tree.check()
            serial, fingerprint = _live(scope, label, expected, volume)
            _require(_path(register.archive_path(scope.connection, label)) == root,
                     "PUBLICATION_ATTACHMENT_PATH_CHANGED")
            tree.check()
            _scope(scope, label)
            _require(_facts(scope, label) == before, "PUBLICATION_ATTACHMENT_CATALOG_CHANGED")
            _require(tuple(tree.identity(tree.fd)) == root_identity and tree.mount_id == mount_id,
                     "PUBLICATION_ATTACHMENT_CHANGED")
            tree.check()
        return AttachmentProof(
            scope.library[0], scope.library[1], label, root, root_identity, mount_id,
            volume.fs_uuid, volume.annex_uuid, serial, volume.serial, volume.capacity_bytes,
            volume.free_bytes, volume.alloc_unit_bytes, expected.identity_epoch,
            before[0], fingerprint, before[1], before[2])
    except (BlockObservationError, OSError, subprocess.SubprocessError, TransferRefusal) as exc:
        raise PublicationRefused("PUBLICATION_ATTACHMENT_UNPROVEN", drive=label,
                                 reason=getattr(exc, "code", str(exc))) from exc


def recheck(scope: publication_locks._FenceScope, proof: AttachmentProof) -> AttachmentProof:
    """Fresh physical evidence must match the captured attachment; only free drifts.

    A new root spelling, inode/mount, raw serial observation, capacity, epoch or
    generation is a conflict, not permission to redirect an in-progress publisher.
    Returns the fresh free-space observation without mutating the old proof.
    """
    _require(type(proof) is AttachmentProof, "PUBLICATION_ATTACHMENT_PROOF_INVALID")
    current = capture(scope, proof.drive_label)
    _require(replace(current, free_bytes=proof.free_bytes) == proof,
             "PUBLICATION_ATTACHMENT_CHANGED")
    return current
