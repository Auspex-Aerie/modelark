"""Non-executable folder contracts (DEC-130, Gate A).

These are trusted-process observations, NOT ownership capabilities. A later qualified
adapter must produce the unambiguous filesystem scope, filesystem-relative parent path,
and parent identity from retained descriptors. Constructing/deserializing these values
does not prove those facts and never authorizes a write or resume.

Native execution consumes these contracts through its separately versioned plan
and durable claim gate. These values alone remain non-executable; the FAT32 live
binding and alias rules are still unqualified. Legacy seals retain their policy.
"""
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import re

from .transaction import TransferRefusal


_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_TARGET_VERSION = "modelark.slice.folder-target.v1"
_REVIEW_VERSION = "modelark.slice.folder-review.v1"


def _refuse(detail):
    raise TransferRefusal("FOLDER_CONTRACT_INVALID", detail)


def _integer(value):
    return type(value) is int and value >= 0


def _text(value):
    return isinstance(value, str) and bool(value) and value == value.strip() and "\0" not in value


def _component(value):
    if not _text(value) or value in {".", ".."} or any(c in value for c in "/\\:"):
        _refuse("canonical single path component required")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        _refuse("output path component must be strict UTF-8")


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _fields(value, names):
    if type(value) is not dict or set(value) != set(names):
        _refuse("missing, extra or mixed-version fields")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _refuse("duplicate JSON key")
        result[key] = value
    return result


def _decode(payload):
    if not isinstance(payload, str):
        _refuse("JSON text required")
    try:
        return json.loads(payload, object_pairs_hook=_pairs,
                          parse_constant=lambda _: _refuse("non-finite JSON value"))
    except (ValueError, RecursionError) as exc:
        _refuse("invalid JSON: " + str(exc))


class FolderProfile(str, Enum):
    """Planned policies, not filesystem qualification.

    Native v1 requires case-sensitive directories (no ext4 casefold). FAT32 name
    equivalence/short-name aliases must be qualified before proving disjointness.
    """

    NATIVE_EXT4 = "native-ext4-folder.v1"
    FAT32_SESSION = "fat32-folder-session.v1"

    @property
    def resume_policy(self):
        return "authenticated" if self is FolderProfile.NATIVE_EXT4 else "new-root"


@dataclass(frozen=True)
class FolderTarget:
    """Pre-creation target; no created root or free-space value is identity.

    Native identity is the observed parent (inode, birth_ns). FAT32 identity is a
    host-generated token naming a *retained live parent binding*, never a token read
    from media. It cannot be recreated from serialized state to authorize resume.
    filesystem_scope/path must already disambiguate aliases/backing identity; a UUID
    alone is not enough. Unknown topology must be refused by the future observer.
    """

    profile: FolderProfile
    filesystem_scope: str
    parent_parts: tuple[str, ...]
    parent_identity: tuple[int, int] | str
    child_name: str

    def __post_init__(self):
        try:
            profile = FolderProfile(self.profile)
        except (ValueError, TypeError):
            _refuse("unsupported folder profile")
        object.__setattr__(self, "profile", profile)
        if not _text(self.filesystem_scope):
            _refuse("unambiguous filesystem scope required")
        if not isinstance(self.parent_parts, (tuple, list)):
            _refuse("filesystem-relative parent components required")
        for part in self.parent_parts:
            _component(part)
        object.__setattr__(self, "parent_parts", tuple(self.parent_parts))
        _component(self.child_name)
        if profile is FolderProfile.NATIVE_EXT4:
            identity = self.parent_identity
            if (not isinstance(identity, (tuple, list)) or len(identity) != 2
                    or any(not _integer(n) or n == 0 for n in identity)):
                _refuse("native parent inode and birth identity required")
            object.__setattr__(self, "parent_identity", tuple(identity))
        elif not isinstance(self.parent_identity, str) or not _HEX32.fullmatch(self.parent_identity):
            _refuse("retained-session parent token required; persistent FAT32 identity is unsupported")

    @property
    def parts(self):
        return (*self.parent_parts, self.child_name)

    def _record(self):
        return {"version": _TARGET_VERSION, **asdict(self)}

    def to_json(self):
        return _encode(self._record())

    @property
    def target_id(self):
        return "folder-target-v1:" + hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def _from_record(cls, value):
        _fields(value, {"version", "profile", "filesystem_scope", "parent_parts", "parent_identity", "child_name"})
        if value["version"] != _TARGET_VERSION:
            _refuse("unsupported folder target version")
        return cls(**{key: item for key, item in value.items() if key != "version"})

    @classmethod
    def from_json(cls, payload):
        return cls._from_record(_decode(payload))


def overlaps(first: FolderTarget, second: FolderTarget):
    """Namespace conflict, not ownership proof or a cross-version reservation gate.

    Compare components rather than textual prefixes. Even replaced parents or
    distinct FAT32 session tokens do not erase a claim on the same target path.
    Inputs must use the same canonical scope for recognized filesystem aliases.
    """
    if not isinstance(first, FolderTarget) or not isinstance(second, FolderTarget):
        _refuse("folder targets required for overlap comparison")
    if first.filesystem_scope != second.filesystem_scope:
        return False
    if first.profile is not second.profile:
        _refuse("inconsistent profiles for one filesystem scope")
    common = min(len(first.parts), len(second.parts))
    if first.parts[:common] == second.parts[:common]:
        return True
    if first.profile is FolderProfile.FAT32_SESSION:
        _refuse("FAT32 disjointness requires qualified driver name/alias semantics")
    return False


@dataclass(frozen=True)
class CapacityObservation:
    """Shared-space snapshot, not a reservation, guarantee or destination identity."""

    available_bytes: int
    free_inodes: int | None
    headroom_bytes: int = 0

    def __post_init__(self):
        if (not _integer(self.available_bytes) or not _integer(self.headroom_bytes)
                or (self.free_inodes is not None and not _integer(self.free_inodes))):
            _refuse("nonnegative integer capacity observations required")


@dataclass(frozen=True)
class FolderReview:
    """Review record only: cannot be approved/stored/started by the legacy engine.

    The full record seal includes advisory observations so the reviewed text is
    immutable. Its target ID does not. closure_seal references the unchanged domain
    closure; this record is not a TransferPlan and provides no execution authority.
    """

    target: FolderTarget
    closure_seal: str
    capacity: CapacityObservation

    def __post_init__(self):
        if (not isinstance(self.target, FolderTarget) or not isinstance(self.capacity, CapacityObservation)
                or not isinstance(self.closure_seal, str) or not _HEX64.fullmatch(self.closure_seal)):
            _refuse("folder target, domain closure seal and capacity observation required")

    @property
    def version(self):
        return _REVIEW_VERSION

    @property
    def execution_ready(self):
        return False

    def to_json(self):
        return _encode({"version": self.version, "target": self.target._record(),
                        "closure_seal": self.closure_seal, "capacity": asdict(self.capacity)})

    @property
    def seal(self):
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_json(cls, payload):
        value = _decode(payload)
        _fields(value, {"version", "target", "closure_seal", "capacity"})
        if value["version"] != _REVIEW_VERSION:
            _refuse("unsupported folder review version")
        _fields(value["capacity"], {"available_bytes", "free_inodes", "headroom_bytes"})
        return cls(FolderTarget._from_record(value["target"]), value["closure_seal"],
                   CapacityObservation(**value["capacity"]))
