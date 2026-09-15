"""Pure archive-publication representation rules (DEC-157).

No command execution, catalog authority, filesystem access or implicit migration.
These rules are shared by the future publisher's proof factories; importing this
module does not enable neutral mappings or change the catalog reader floor.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
import posixpath
import re
from typing import Mapping


NAMESPACE = "__modelark_payload_v1__"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"SHA256-s(0|[1-9][0-9]*)--([0-9a-f]{64})\Z")
_EXTENDED_KEY = re.compile(r"SHA256E-s(0|[1-9][0-9]*)--([0-9a-f]{64})(.*)\Z")
# Exact observed annex-8 suffixes, not a general extension algorithm. Legacy
# SHA256E identity includes this suffix even when two keys hash identical bytes.
# Unknown/Unicode suffixes fail closed; new publication still forces SHA256.
# Evidence: docs/acceptance/annex-sha256e-stage1-2026-09-14.{md,json}.
SHA256E_SUFFIXES = frozenset({
    "", ".json", ".znn", ".zstd", ".gguf", ".bin", ".pt", ".pth", ".ckpt",
    ".pkl", ".onnx", ".npz", ".npy", ".md", ".txt", ".py", ".png", ".jpg",
    ".yaml", ".yml", ".blob", ".JSON", ".q4.gguf", ".json.znn", ".txt.znn",
    ".gguf.znn", ".tar.gz", ".json.gz", ".zip", ".bz2", ".zst",
})
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_LOCATION = re.compile(rf"([0-9]+(?:\.[0-9]+)?)s ([01]) ({_UUID})\Z")


class PublicationRefused(ValueError):
    """A publication cannot supply the required evidence; not a retry hint."""

    def __init__(self, code: str, **evidence):
        self.code, self.evidence = code, evidence
        super().__init__(code)


def _require(condition: bool, code: str, **evidence) -> None:
    if not condition:
        raise PublicationRefused(code, **evidence)


def relative_path(value: str) -> PurePosixPath:
    _require(isinstance(value, str) and bool(value) and "\0" not in value
             and "\\" not in value, "PUBLICATION_PATH_INVALID")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PublicationRefused("PUBLICATION_PATH_INVALID") from exc
    path = PurePosixPath(value)
    _require(not path.is_absolute() and not PureWindowsPath(value).drive
             and all(part not in {"", ".", "..", ".git"} for part in value.split("/"))
             and path.as_posix() == value, "PUBLICATION_PATH_INVALID", path=value)
    return path


def payload_relative_path(logical_path: str) -> str:
    """Map hidden upstream components only; names and original bytes stay distinct.

    A reserved namespace in an upstream path is a collision, not an already-owned
    object. Existing stored paths need an ownership proof, not this pure mapper.
    """
    path = relative_path(logical_path)
    _require(NAMESPACE not in path.parts, "PUBLICATION_NAMESPACE_COLLISION", path=logical_path)
    if not any(part.startswith(".") for part in path.parts):
        return logical_path
    name_hash = hashlib.sha256(logical_path.encode("utf-8")).hexdigest()
    return f"{NAMESPACE}/p-{name_hash}.blob"


def sha256_key(size: int, digest: str) -> str:
    _require(type(size) is int and size >= 0 and isinstance(digest, str)
             and _SHA256.fullmatch(digest) is not None, "PUBLICATION_CONTENT_ID_INVALID")
    return f"SHA256-s{size}--{digest}"


def parse_sha256_key(key: str) -> tuple[int, str]:
    """Read qualified SHA256-family content facts, preserving the caller's key.

    Legacy API name remains compatible. SHA256E's exact suffix is part of its
    annex identity, not its content digest: never rebuild an existing key from
    this result or substitute a SHA256 key when checking pointers/locations.
    """
    match = _KEY.fullmatch(key) if isinstance(key, str) else None
    if match is None and isinstance(key, str):
        extended = _EXTENDED_KEY.fullmatch(key)
        if extended is not None and extended[3] in SHA256E_SUFFIXES:
            match = extended
    _require(match is not None, "PUBLICATION_KEY_UNQUALIFIED")
    return int(match[1]), match[2]


def check_committed_pointer(*, mode: str, blob: bytes, stored_path: str,
                            key: str, qualified_object_path: str) -> None:
    """Compare a complete committed pointer with the qualified client's object path.

    Caller must independently obtain/seal the native object path using its admitted
    profile. This pure check proves neither object presence nor physical confinement.
    """
    relative_path(stored_path)
    parse_sha256_key(key)
    _require(isinstance(qualified_object_path, str), "PUBLICATION_OBJECT_PATH_INVALID")
    expected_object = re.fullmatch(
        r"\.git/annex/objects/[A-Za-z0-9]{2}/[A-Za-z0-9]{2}/" + re.escape(key) + "/" + re.escape(key),
        qualified_object_path)
    _require(expected_object is not None, "PUBLICATION_OBJECT_PATH_INVALID")
    if mode == "120000":
        expected = posixpath.relpath(qualified_object_path, str(PurePosixPath(stored_path).parent)).encode()
    else:
        _require(mode == "100644", "PUBLICATION_POINTER_MODE_INVALID", mode=mode)
        expected = f"/annex/objects/{key}\n".encode()
    _require(blob == expected, "PUBLICATION_POINTER_MISMATCH", path=stored_path, key=key)


@dataclass(frozen=True, order=True)
class LocationRecord:
    identity: str
    clock: Decimal
    present: bool

    def __post_init__(self):
        _require(isinstance(self.identity, str) and re.fullmatch(_UUID, self.identity) is not None
                 and isinstance(self.clock, Decimal) and self.clock.is_finite()
                 and self.clock >= 0 and type(self.present) is bool,
                 "PUBLICATION_LOCATION_LOG_INVALID")


def _location_records(records):
    _require(isinstance(records, frozenset) and all(isinstance(r, LocationRecord) for r in records),
             "PUBLICATION_LOCATION_LOG_INVALID")
    clocks = {}
    for record in records:
        identity = record.identity, record.clock
        _require(identity not in clocks or clocks[identity] == record.present,
                 "PUBLICATION_LOCATION_CLOCK_CONFLICT")
        clocks[identity] = record.present


def parse_location_log(data: bytes) -> frozenset[LocationRecord]:
    """Pinned annex-8 timestamped location grammar; retain history, reject ties.

    Empty logs and unqualified grammars refuse. Duplicate identical records are
    harmless; equal-clock contradictory records are not an arbitrary tie-break.
    """
    _require(isinstance(data, bytes) and bool(data) and data.endswith(b"\n"),
             "PUBLICATION_LOCATION_LOG_INVALID")
    records: dict[tuple[str, Decimal], LocationRecord] = {}
    try:
        lines = data.decode("ascii").split("\n")[:-1]
        for line in lines:
            match = _LOCATION.fullmatch(line)
            _require(match is not None, "PUBLICATION_LOCATION_LOG_INVALID")
            record = LocationRecord(match[3], Decimal(match[1]), match[2] == "1")
            previous = records.get((record.identity, record.clock))
            _require(previous is None or previous == record, "PUBLICATION_LOCATION_CLOCK_CONFLICT")
            records[(record.identity, record.clock)] = record
    except (UnicodeDecodeError, InvalidOperation) as exc:
        raise PublicationRefused("PUBLICATION_LOCATION_LOG_INVALID") from exc
    return frozenset(records.values())


def effective_locations(records: frozenset[LocationRecord]) -> frozenset[str]:
    _location_records(records)
    latest = {}
    for record in sorted(records):
        latest[record.identity] = record
    return frozenset(identity for identity, record in latest.items() if record.present)


def check_location_delta(*, before: frozenset[LocationRecord], after: frozenset[LocationRecord],
                         allowed_uuids: frozenset[str], map_uuid: str,
                         observed_clock_ceiling: Decimal, preserve_history: bool = True
                         ) -> frozenset[LocationRecord]:
    """Admit only selected positive additions; physical proof comes from the caller.

    Source quarantine may omit map history; final merged output must preserve it.
    This validates native records, never constructs a merged log or a timestamp.
    """
    _location_records(before)
    _location_records(after)
    _require(isinstance(observed_clock_ceiling, Decimal) and observed_clock_ceiling.is_finite()
             and observed_clock_ceiling >= 0, "PUBLICATION_CLOCK_BOUND_INVALID")
    _require(type(preserve_history) is bool, "PUBLICATION_HISTORY_POLICY_INVALID")
    if preserve_history:
        _require(before <= after, "PUBLICATION_LOCATION_HISTORY_CHANGED")
    additions = after - before
    for record in additions:
        _require(record.identity != map_uuid, "PUBLICATION_MAP_CONTENT_CLAIM")
        _require(record.identity in allowed_uuids, "PUBLICATION_UUID_UNPROVEN", uuid=record.identity)
        _require(record.present, "PUBLICATION_DROP_CLAIM")
        _require(record.clock <= observed_clock_ceiling, "PUBLICATION_FUTURE_CLOCK")
        old_clocks = [r.clock for r in before if r.identity == record.identity]
        _require(not old_clocks or record.clock > max(old_clocks), "PUBLICATION_LOCATION_CLOCK_CONFLICT")
    return additions


def check_replay_state(*, old: Mapping[str, tuple[str, str]], new: Mapping[str, tuple[str, str]],
                       index: Mapping[str, tuple[str, str]], worktree: Mapping[str, tuple[str, str]],
                       directories: frozenset[str]) -> None:
    """Recognize read-tree's old-index/partial-worktree or entirely-new state.

    Tuple values are (mode, raw blob SHA256). The caller must capture them under
    fences with no-follow traversal; this function does not access the filesystem.
    """
    permitted_dirs = {str(parent) for path in old.keys() | new.keys()
                      for parent in relative_path(path).parents if str(parent) != "."}
    _require(directories <= permitted_dirs, "PUBLICATION_REPLAY_UNRELATED_DIRECTORY")
    _require(index == old or index == new, "PUBLICATION_REPLAY_MIXED_INDEX")
    if index == new:
        _require(worktree == new, "PUBLICATION_REPLAY_INCOMPLETE_WORKTREE")
    else:
        for path in old.keys() | new.keys() | worktree.keys():
            _require(path in old or path in new, "PUBLICATION_REPLAY_UNRELATED_PATH")
            _require(worktree.get(path) in (old.get(path), new.get(path)), "PUBLICATION_REPLAY_PATH_CHANGED")
