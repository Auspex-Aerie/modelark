"""FAT32 durable path/backing intent, distinct from live Start authority.

No saved parent token, inode, marker, plan or receipt authorizes FAT32 resume.
The execution layer must consume a single attempt before creating the absent
output root and must retain its fresh parent/object capabilities for that attempt.
"""
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath
import re

from . import domain as d
from . import fat32_layout as layout
from .folder_contract import CapacityObservation, FolderProfile, _decode, _fields, _text
from .folder_plan import _absolute, _backing, _proposal_record
from .transaction import TransferRefusal
from modelark.artifact_policy import DecodePolicy


VERSION = "modelark.slice.fat32-transaction.v1"
POLICY_VERSION = "modelark.slice.fat32-transaction.v2"
_INTENT_VERSION = "modelark.slice.fat32-intent.v1"
_ADMISSION_PREFIX = "fat32-folder-v1:"
_SHA = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class Fat32Intent:
    """Reviewed namespace intent, not proof of parent continuity or ownership."""

    profile: FolderProfile
    filesystem_scope: str
    parent_parts: tuple[str, ...]
    child_name: str

    def __post_init__(self):
        try:
            profile = FolderProfile(self.profile)
        except (TypeError, ValueError) as exc:
            raise TransferRefusal("FAT32_PLAN_INVALID", "FAT32 session profile required") from exc
        if (profile is not FolderProfile.FAT32_SESSION or not _text(self.filesystem_scope)
                or not isinstance(self.parent_parts, (tuple, list))):
            raise TransferRefusal("FAT32_PLAN_INVALID", "FAT32 path/backing intent required")
        for part in (*self.parent_parts, self.child_name):
            layout._name(part)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "parent_parts", tuple(self.parent_parts))

    @property
    def parts(self):
        return (*self.parent_parts, self.child_name)

    def to_json(self):
        return d._json({"version": _INTENT_VERSION, **asdict(self)}).decode()

    @property
    def target_id(self):
        return "fat32-intent-v1:" + hashlib.sha256(self.to_json().encode()).hexdigest()

    @classmethod
    def from_json(cls, payload):
        try:
            record = _decode(payload)
            _fields(record, {"version", "profile", "filesystem_scope", "parent_parts", "child_name"})
            if record["version"] != _INTENT_VERSION:
                raise ValueError("unknown FAT32 intent version")
            return cls(**{key: value for key, value in record.items() if key != "version"})
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise TransferRefusal("STATE_CORRUPT", "invalid FAT32 intent: " + str(exc)) from exc


@dataclass(frozen=True)
class Fat32Binding:
    target: Fat32Intent
    admission_id: str

    def __post_init__(self):
        if (not isinstance(self.target, Fat32Intent) or not isinstance(self.admission_id, str)
                or self.admission_id.split(":", 1)[0] not in
                {"fat32-folder-v1", "fat32-folder-v2"}
                or not _SHA.fullmatch(self.admission_id.split(":", 1)[-1])):
            raise TransferRefusal("FAT32_PLAN_INVALID", "FAT32 intent binding required")

    @property
    def device_id(self):
        return self.target.target_id

    @property
    def filesystem_id(self):
        return self.target.filesystem_scope

    @property
    def mount_id(self):
        return self.admission_id


def binding_for(target, catalog, parent_path, backing_ids, decode_policy=None):
    """Seal stable path/backing policy, never a live descriptor or capacity claim."""
    if not isinstance(target, Fat32Intent):
        raise TransferRefusal("FAT32_PLAN_INVALID", "FAT32 intent required")
    payload = {"version": POLICY_VERSION if decode_policy is not None else VERSION,
               "target": json.loads(target.to_json()),
               "catalog": _absolute(catalog), "parent_path": _absolute(parent_path),
               "backing_ids": _backing(backing_ids)}
    if decode_policy is not None:
        if not isinstance(decode_policy, DecodePolicy):
            raise TransferRefusal("ADMISSION_CORRUPT", "typed decode policy required")
        payload["decode_policy"] = decode_policy.to_record()
    prefix = "fat32-folder-v2:" if decode_policy is not None else _ADMISSION_PREFIX
    return Fat32Binding(target, prefix + hashlib.sha256(d._json(payload)).hexdigest())


@dataclass(frozen=True)
class Fat32Plan:
    proposal: d.SlicePreview
    destination: Fat32Binding
    catalog: str
    parent_path: str
    backing_ids: tuple[str, ...]
    capacity: CapacityObservation
    metadata_reserve_bytes: int
    version: str = VERSION
    decode_policy: DecodePolicy | None = None

    def __post_init__(self):
        if (self.version != (POLICY_VERSION if self.decode_policy is not None else VERSION)
                or self.decode_policy is not None and not isinstance(self.decode_policy, DecodePolicy)
                or not isinstance(self.proposal, d.SlicePreview)
                or not isinstance(self.destination, Fat32Binding)
                or not isinstance(self.capacity, CapacityObservation)
                or not d._integer(self.metadata_reserve_bytes)):
            raise TransferRefusal("FAT32_PLAN_INVALID", "typed FAT32 plan required")
        d._verify(self.proposal)
        if not self.proposal.source_ready:
            raise TransferRefusal("PREVIEW_BLOCKED")
        object.__setattr__(self, "backing_ids", _backing(self.backing_ids))
        expected = binding_for(self.destination.target, self.catalog, self.parent_path,
                               self.backing_ids, self.decode_policy)
        if self.destination != expected:
            raise TransferRefusal("ADMISSION_CORRUPT", "FAT32 admission differs from binding")
        if (self.proposal.spec.destination_id != expected.device_id
                or self.proposal.spec.destination_root != expected.target.child_name):
            raise TransferRefusal("DESTINATION_CHANGED", "closure differs from reviewed FAT32 intent")
        self.required_bytes("")

    @property
    def is_folder(self):
        return True

    @property
    def session_only(self):
        return True

    @property
    def control_path(self):
        return self.destination.target.child_name + "/.modelark-slice-owner"

    def to_json(self):
        record = asdict(self)
        if self.decode_policy is None:
            record.pop("decode_policy")  # preserve every legacy canonical byte/seal
        return d._json(record).decode()

    @property
    def seal(self):
        return hashlib.sha256(self.to_json().encode()).hexdigest()

    def receipt(self, tx, files):
        # Published before the host's final commit. This report must not assert
        # that a later host commit or its own subsequent flush has succeeded.
        return {"version": self.version, "transaction": tx, "seal": self.seal,
                "destination": asdict(self.destination), "files": files,
                "plan": json.loads(self.to_json()), "topology": "folder", "status": "export-verified",
                "profile": self.destination.target.profile.value, "resume_policy": "new-root",
                "host_transaction_completion": "not-claimed",
                "receipt_persistence": "not-self-certified",
                "capacity_evidence": "advisory-shared-space-not-reserved",
                "verification": {"content": "sha256-original-bytes", "layout": "live-session-authenticated",
                                 "result": "verified", "file_count": len(files)}}

    def _metadata_sizes(self, store_root):
        tx = "0" * 32
        root = PurePosixPath(self.proposal.spec.destination_root)
        files = [{"path": str(root / artifact.repo_id / artifact.rfilename),
                  "size": artifact.size_bytes, "sha256": artifact.sha256,
                  "source": asdict(max(artifact.sources, key=lambda source: len(d._json(asdict(source)))))}
                 for artifact in self.proposal.closure]
        control = {"transaction": tx, "seal": self.seal, "store": str(store_root),
                   "destination": asdict(self.destination)}
        return len(d._json(control)), len(d._json(self.receipt(tx, files)))

    def required_bytes(self, store_root):
        control_size, receipt_size = self._metadata_sizes(store_root)
        layout.admit_layout(((artifact.repo_id + "/" + artifact.rfilename, artifact.size_bytes)
                             for artifact in self.proposal.closure),
                            output_root=self.destination.target.child_name, parent_path=self.parent_path,
                            control_size=control_size, receipt_size=receipt_size)
        if self.metadata_reserve_bytes < control_size + receipt_size:
            raise TransferRefusal("DESTINATION_CAPACITY_UNPROVEN", "FAT32 metadata reserve is too small")
        return self.proposal.total_bytes + self.metadata_reserve_bytes + self.capacity.headroom_bytes

    @classmethod
    def from_json(cls, payload):
        from .transaction import decode_proposal
        try:
            obj = _decode(payload)
            if type(obj) is not dict:
                raise ValueError("plan envelope required")
            keys = {"proposal", "destination", "catalog", "parent_path", "backing_ids",
                    "capacity", "metadata_reserve_bytes", "version"}
            if obj.get("version") == POLICY_VERSION:
                keys.add("decode_policy")
            _fields(obj, keys)
            if obj["version"] not in {VERSION, POLICY_VERSION}:
                raise ValueError("unsupported FAT32 transaction version")
            _fields(obj["destination"], {"target", "admission_id"})
            _fields(obj["destination"]["target"], {"profile", "filesystem_scope", "parent_parts", "child_name"})
            _fields(obj["capacity"], {"available_bytes", "free_inodes", "headroom_bytes"})
            return cls(decode_proposal(_proposal_record(obj["proposal"])),
                       Fat32Binding(Fat32Intent(**obj["destination"]["target"]), obj["destination"]["admission_id"]),
                       obj["catalog"], obj["parent_path"], obj["backing_ids"],
                       CapacityObservation(**obj["capacity"]), obj["metadata_reserve_bytes"], obj["version"],
                       DecodePolicy.from_record(obj["decode_policy"]) if obj["version"] == POLICY_VERSION else None)
        except (KeyError, TypeError, ValueError, RecursionError) as exc:
            raise TransferRefusal("STATE_CORRUPT", "invalid FAT32 plan: " + str(exc)) from exc


def estimate_metadata(proposal, binding, catalog, parent_path, backing_ids, capacity, store_root,
                      decode_policy=None):
    """Conservative shared-space estimate; the live adapter still checks capacity."""
    root = PurePosixPath(proposal.spec.destination_root)
    directories = {str(parent) for artifact in proposal.closure
                   for parent in (root / artifact.repo_id / artifact.rfilename).parents
                   if str(parent) != "."}
    objects = len(proposal.closure) + len(directories) + 2
    record = {"proposal": asdict(proposal), "destination": asdict(binding), "catalog": catalog,
              "parent_path": parent_path, "backing_ids": backing_ids, "capacity": asdict(capacity),
              "store": str(store_root)}
    allowance = 256 * 1024 + objects * 128 * 1024
    reserve = allowance + 3 * len(d._json(record))
    plan = Fat32Plan(proposal, binding, catalog, parent_path, backing_ids, capacity, reserve,
                     POLICY_VERSION if decode_policy is not None else VERSION, decode_policy)
    reserve = max(reserve, sum(plan._metadata_sizes(store_root)) + allowance)
    plan.required_bytes(store_root)
    return reserve
