"""Versioned native-folder plans; observations and seals are not writer authority.

Capacity is immutable review evidence, never attachment identity or a reservation.
The admitted adapter must authenticate the parent/output objects and recheck shared
space at runtime. This envelope cannot be decoded as a legacy USB transaction.
"""
from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import PurePosixPath
import re

from . import domain as d
from .folder_contract import CapacityObservation, FolderProfile, FolderTarget, _decode, _fields
from .transaction import TransferRefusal


VERSION = "modelark.slice.native-transaction.v1"
_ADMISSION_PREFIX = "native-folder-v1:"
_SHA = re.compile(r"[0-9a-f]{64}\Z")


def _absolute(value):
    if (not isinstance(value, str) or not value or "\0" in value
            or not value.startswith("/") or value.startswith("//")
            or str(PurePosixPath(value)) != value or ".." in PurePosixPath(value).parts):
        raise TransferRefusal("FOLDER_PLAN_INVALID", "canonical absolute path required")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise TransferRefusal("FOLDER_PLAN_INVALID", "strict UTF-8 path required") from exc
    return value


def _backing(values):
    if (not isinstance(values, (tuple, list)) or not values
            or any(not isinstance(value, str) or not value or value != value.strip()
                   or "\0" in value for value in values)):
        raise TransferRefusal("FOLDER_PLAN_INVALID", "backing identities required")
    if tuple(values) != tuple(sorted(set(values))):
        raise TransferRefusal("FOLDER_PLAN_INVALID", "backing identities must be sorted and unique")
    return tuple(values)


def _proposal_record(record):
    """Reject unknown fields even where the shared legacy decoder selects keys."""
    _fields(record, {field.name for field in fields(d.SlicePreview)})
    _fields(record["spec"], {field.name for field in fields(d.SliceSpec)})
    for artifact in record["closure"]:
        _fields(artifact, {field.name for field in fields(d.Artifact)})
        for source in artifact["sources"]:
            _fields(source, {field.name for field in fields(d.SourceEvidence)})
            for key, record_type in (("copy", d.CopyFact), ("drive", d.DriveFact), ("anchor", d.AnchorFact)):
                _fields(source[key], {field.name for field in fields(record_type)})
    for gap in record["gaps"]:
        _fields(gap, {field.name for field in fields(d.Gap)})
    return record


@dataclass(frozen=True)
class NativeBinding:
    target: FolderTarget
    admission_id: str

    def __post_init__(self):
        if (not isinstance(self.target, FolderTarget)
                or self.target.profile is not FolderProfile.NATIVE_EXT4
                or not isinstance(self.admission_id, str)
                or not self.admission_id.startswith(_ADMISSION_PREFIX)
                or not _SHA.fullmatch(self.admission_id[len(_ADMISSION_PREFIX):])):
            raise TransferRefusal("FOLDER_PLAN_INVALID", "native binding required")

    @property
    def device_id(self):
        return self.target.target_id

    @property
    def filesystem_id(self):
        return self.target.filesystem_scope

    @property
    def mount_id(self):
        return self.admission_id


def binding_for(target, catalog, parent_path, backing_ids):
    """Bind stable admission policy, excluding mutable free-space observations."""
    if not isinstance(target, FolderTarget) or target.profile is not FolderProfile.NATIVE_EXT4:
        raise TransferRefusal("FOLDER_PLAN_INVALID", "native target required")
    payload = {"version": VERSION, "target": json.loads(target.to_json()),
               "catalog": _absolute(catalog), "parent_path": _absolute(parent_path),
               "backing_ids": _backing(backing_ids)}
    return NativeBinding(target, _ADMISSION_PREFIX + hashlib.sha256(d._json(payload)).hexdigest())


@dataclass(frozen=True)
class NativePlan:
    proposal: d.SlicePreview
    destination: NativeBinding
    catalog: str
    parent_path: str
    backing_ids: tuple[str, ...]
    capacity: CapacityObservation
    metadata_reserve_bytes: int
    version: str = VERSION

    def __post_init__(self):
        if (self.version != VERSION or not isinstance(self.proposal, d.SlicePreview)
                or not isinstance(self.destination, NativeBinding)
                or not isinstance(self.capacity, CapacityObservation)
                or not d._integer(self.metadata_reserve_bytes)):
            raise TransferRefusal("FOLDER_PLAN_INVALID", "typed native plan required")
        d._verify(self.proposal)
        if not self.proposal.source_ready:
            raise TransferRefusal("PREVIEW_BLOCKED")
        object.__setattr__(self, "backing_ids", _backing(self.backing_ids))
        expected = binding_for(self.destination.target, self.catalog, self.parent_path, self.backing_ids)
        if self.destination != expected:
            raise TransferRefusal("ADMISSION_CORRUPT", "native admission differs from binding")
        if (self.proposal.spec.destination_id != expected.device_id
                or self.proposal.spec.destination_root != expected.target.child_name):
            raise TransferRefusal("DESTINATION_CHANGED", "closure differs from reviewed folder intent")
        self.required_bytes("")

    @property
    def is_folder(self):
        return True

    @property
    def control_path(self):
        return self.destination.target.child_name + "/.modelark-slice-owner"

    @property
    def required_inodes(self):
        root = PurePosixPath(self.proposal.spec.destination_root)
        directories = {parent for artifact in self.proposal.closure
                       for parent in (root / artifact.repo_id / artifact.rfilename).parents
                       if str(parent) != "."}
        # Staging and final names share one inode, including during publication.
        return len(directories) + len(self.proposal.closure) + 2  # control + receipt

    def to_json(self):
        return d._json(asdict(self)).decode()

    @property
    def seal(self):
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def receipt(self, tx, files):
        return {"version": self.version, "transaction": tx, "seal": self.seal,
                "destination": asdict(self.destination), "files": files,
                "plan": json.loads(self.to_json()), "topology": "folder", "status": "complete",
                "profile": self.destination.target.profile.value, "resume_policy": "authenticated",
                "capacity_evidence": "advisory-shared-space-not-reserved",
                "verification": {"content": "sha256-original-bytes", "layout": "authenticated",
                                 "result": "verified", "file_count": len(files)}}

    def _metadata_minimum(self, store_root):
        tx = "0" * 32
        root = PurePosixPath(self.proposal.spec.destination_root)
        files = [{"path": str(root / artifact.repo_id / artifact.rfilename),
                  "size": artifact.size_bytes, "sha256": artifact.sha256,
                  "source": asdict(max(artifact.sources, key=lambda source: len(d._json(asdict(source)))))}
                 for artifact in self.proposal.closure]
        control = {"transaction": tx, "seal": self.seal, "store": str(store_root),
                   "destination": asdict(self.destination)}
        return len(d._json(control)) + len(d._json(self.receipt(tx, files)))

    def required_bytes(self, store_root):
        if self.metadata_reserve_bytes < self._metadata_minimum(store_root):
            raise TransferRefusal("DESTINATION_CAPACITY_UNPROVEN", "native metadata reserve is too small")
        # No comparison with reviewed available_bytes: shared free space can change.
        return self.proposal.total_bytes + self.metadata_reserve_bytes + self.capacity.headroom_bytes

    @classmethod
    def from_json(cls, payload):
        from .transaction import decode_proposal
        try:
            obj = _decode(payload)
            _fields(obj, {"proposal", "destination", "catalog", "parent_path", "backing_ids",
                          "capacity", "metadata_reserve_bytes", "version"})
            if obj["version"] != VERSION:
                raise ValueError("unsupported native transaction version")
            _fields(obj["destination"], {"target", "admission_id"})
            target = obj["destination"]["target"]
            _fields(target, {"profile", "filesystem_scope", "parent_parts", "parent_identity", "child_name"})
            _fields(obj["capacity"], {"available_bytes", "free_inodes", "headroom_bytes"})
            return cls(decode_proposal(_proposal_record(obj["proposal"])),
                       NativeBinding(FolderTarget(**target), obj["destination"]["admission_id"]),
                       obj["catalog"], obj["parent_path"], obj["backing_ids"],
                       CapacityObservation(**obj["capacity"]), obj["metadata_reserve_bytes"], obj["version"])
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise TransferRefusal("STATE_CORRUPT", "invalid native plan: " + str(exc)) from exc


def estimate_metadata(proposal, binding, catalog, parent_path, backing_ids, capacity, store_root):
    """Conservative advisory metadata budget, not proof of filesystem allocation.

    Include serialized evidence and per-inode/directory overhead. Runtime space and
    inode checks remain mandatory; external writers and quota limits can still win.
    """
    root = PurePosixPath(proposal.spec.destination_root)
    directories = {str(parent) for artifact in proposal.closure
                   for parent in (root / artifact.repo_id / artifact.rfilename).parents
                   if str(parent) != "."}
    objects = len(proposal.closure) + len(directories) + 2
    record = {"proposal": asdict(proposal), "destination": asdict(binding), "catalog": catalog,
              "parent_path": parent_path, "backing_ids": backing_ids, "capacity": asdict(capacity),
              "store": str(store_root)}
    allowance = 256 * 1024 + objects * 16 * 1024
    reserve = allowance + 3 * len(d._json(record))
    plan = NativePlan(proposal, binding, catalog, parent_path, backing_ids, capacity, reserve)
    reserve = max(reserve, plan._metadata_minimum(store_root) + allowance)
    return reserve
