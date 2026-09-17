"""Pure admission/validation for the qualified native annex metadata merge.

These functions inspect already captured regular-blob trees. The caller must
capture mode 100644 blobs using Git-only quarantine inspection, prove/seal the
selected physical UUIDs and annotation source writes, and run the qualified
native merge only *after* admission. A returned summary is not write authority,
a physical-presence proof, or a publication/replay receipt. No log is constructed
here: the native merger owns its serialization and timestamp semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import re
from typing import Mapping

from modelark.publication_annotations import (
    MODEL_FIELDS, check_annotation_merge, check_annotation_write, parse_annotations,
)
from modelark.publication_policy import (
    _require, check_location_delta, effective_locations, parse_location_log,
    parse_sha256_key, relative_path,
)

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_LOG = re.compile(r"[0-9a-f]{3}/[0-9a-f]{3}/(SHA256[^/\0]*)\.log(\.met)?\Z")


def _seal(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def _clock(value: Decimal) -> str:
    # Decimal.normalize() uses the active precision and may round large native
    # timestamps. Strip insignificant fractional zeroes without arithmetic.
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _tree(value: Mapping[str, bytes]) -> dict[str, bytes]:
    _require(isinstance(value, Mapping), "PUBLICATION_METADATA_TREE_INVALID")
    result = dict(value)
    for path, blob in result.items():
        relative_path(path)
        _require(isinstance(blob, bytes), "PUBLICATION_METADATA_TREE_INVALID")
    return result


def _tree_seal(value: Mapping[str, bytes]) -> str:
    return _seal({path: hashlib.sha256(blob).hexdigest() for path, blob in value.items()})


def _recognized(path: str) -> tuple[str, bool] | None:
    match = _LOG.fullmatch(path)
    if match is None:
        return None
    # One shared key grammar for payloads, pointers and native metadata. Do not
    # duplicate the conservative SHA256E suffix profile in the path regex.
    parse_sha256_key(match[1])
    return match[1], bool(match[2])


def _paths(*trees) -> None:
    # An arbitrary alternate bucket must not hide a second record for one key.
    # The native runner additionally binds paths to its qualified key-path query.
    locations = {}
    for tree in trees:
        for path in tree:
            info = _recognized(path)
            if info:
                key, _ = info
                base = path.removesuffix(".met")
                _require(key not in locations or locations[key] == base,
                         "PUBLICATION_METADATA_KEY_PATH_CONFLICT", key=key)
                locations[key] = base


@dataclass(frozen=True)
class AnnotationWrite:
    """Sealed source-write facts, not a capability or self-authenticating proof.

    ``binding_digest`` binds the caller's source operation/profile/authority.
    ``seal()`` must match a digest independently frozen by that coordinator, not
    an expected digest calculated from untrusted quarantine input. Assignments
    are sorted tuples so a frozen value cannot hide a mutable caller dictionary.
    """

    path: str
    before: bytes
    after: bytes
    assignments: tuple[tuple[str, str], ...]
    binding_digest: str

    def seal(self) -> str:
        info = _recognized(self.path) if isinstance(self.path, str) else None
        _require(info is not None and info[1], "PUBLICATION_ANNOTATION_PROOF_INVALID")
        _require(isinstance(self.before, bytes) and isinstance(self.after, bytes)
                 and isinstance(self.binding_digest, str)
                 and _DIGEST.fullmatch(self.binding_digest) is not None,
                 "PUBLICATION_ANNOTATION_PROOF_INVALID")
        _require(type(self.assignments) is tuple and bool(self.assignments)
                 and all(type(pair) is tuple and len(pair) == 2
                         and all(isinstance(value, str) for value in pair)
                         for pair in self.assignments), "PUBLICATION_ANNOTATION_PROOF_INVALID")
        _require(self.assignments == tuple(sorted(self.assignments))
                 and len(dict(self.assignments)) == len(self.assignments),
                 "PUBLICATION_ANNOTATION_PROOF_INVALID")
        return _seal({"version": 1, "path": self.path, "before": self.before.hex(),
                      "after": self.after.hex(), "assignments": self.assignments,
                      "binding_digest": self.binding_digest})


def _contract(allowed_key_uuids, map_uuid, clock_ceiling):
    _require(isinstance(map_uuid, str) and _UUID.fullmatch(map_uuid) is not None,
             "PUBLICATION_MAP_UUID_INVALID")
    _require(isinstance(clock_ceiling, Decimal) and clock_ceiling.is_finite()
             and clock_ceiling >= 0, "PUBLICATION_CLOCK_BOUND_INVALID")
    _require(isinstance(allowed_key_uuids, Mapping), "PUBLICATION_METADATA_ALLOWLIST_INVALID")
    allowed = {}
    for key, identities in allowed_key_uuids.items():
        parse_sha256_key(key)
        _require(isinstance(identities, frozenset) and bool(identities)
                 and all(isinstance(identity, str) and _UUID.fullmatch(identity) is not None
                         and identity != map_uuid for identity in identities),
                 "PUBLICATION_METADATA_ALLOWLIST_INVALID")
        allowed[key] = identities
    return allowed


def _annotations(baseline, incoming, allowed, writes, expected_seals, clock_ceiling):
    _require(isinstance(writes, Mapping) and isinstance(expected_seals, Mapping)
             and writes.keys() == expected_seals.keys(), "PUBLICATION_ANNOTATION_PROOF_SET_MISMATCH")
    seals = {}
    for path, proof in writes.items():
        _require(isinstance(proof, AnnotationWrite) and proof.path == path,
                 "PUBLICATION_ANNOTATION_PROOF_INVALID")
        seal = proof.seal()
        _require(seal == expected_seals[path], "PUBLICATION_ANNOTATION_PROOF_CHANGED")
        _require(_recognized(path)[0] in allowed, "PUBLICATION_METADATA_KEY_UNSELECTED")
        _require(incoming.get(path) == proof.after, "PUBLICATION_ANNOTATION_SOURCE_CHANGED")
        check_annotation_write(before=proof.before, after=proof.after,
                               assignments=dict(proof.assignments), clock_ceiling=clock_ceiling)
        previous = parse_annotations(baseline.get(path, b""))
        for pair, cell in parse_annotations(proof.after).items():
            prior = previous.get(pair)
            if prior == cell:
                continue
            _require(pair[0] in MODEL_FIELDS, "PUBLICATION_ANNOTATION_UNRELATED_CHANGE")
            _require(prior is None or prior.clock != cell.clock,
                     "PUBLICATION_ANNOTATION_CLOCK_CONFLICT")
            _require(cell.clock <= clock_ceiling, "PUBLICATION_FUTURE_CLOCK")
        seals[path] = seal
    return seals


def _records(blob):
    return parse_location_log(blob) if blob is not None else frozenset()


def _addition_summary(records):
    return [{"uuid": record.identity, "clock": _clock(record.clock), "present": record.present}
            for record in sorted(records)]


def _description_subset(before: bytes, source: bytes) -> bool:
    # No descriptions are created or rewritten. Only already accepted full lines
    # can enter native staging; the final tree must retain the baseline verbatim.
    return (bool(before) and before.endswith(b"\n") and source.endswith(b"\n")
            and b"\0" not in before and b"\0" not in source
            and b"" not in source.split(b"\n")[:-1]
            and set(source.split(b"\n")[:-1]) <= set(before.split(b"\n")[:-1]))


def admit_input(*, baseline: Mapping[str, bytes], incoming: Mapping[str, bytes],
                allowed_key_uuids: Mapping[str, frozenset[str]], map_uuid: str,
                clock_ceiling: Decimal, annotation_writes: Mapping[str, AnnotationWrite],
                expected_annotation_seals: Mapping[str, str]) -> dict:
    """Git-only quarantine admission, before any native annex-recognized ref.

    A source may omit baseline entries/history, but cannot add unrelated records.
    Such omissions do not authorize their removal from the final merged tree.
    """
    old, source = _tree(baseline), _tree(incoming)
    _paths(old, source)
    allowed = _contract(allowed_key_uuids, map_uuid, clock_ceiling)
    seals = _annotations(old, source, allowed, annotation_writes,
                         expected_annotation_seals, clock_ceiling)
    additions = {}
    for path, blob in sorted(source.items()):
        if path in seals or old.get(path) == blob:
            continue
        info = _recognized(path)
        if info and not info[1]:
            key = info[0]
            delta = check_location_delta(before=_records(old.get(path)), after=_records(blob),
                                         allowed_uuids=allowed.get(key, frozenset()), map_uuid=map_uuid,
                                         observed_clock_ceiling=clock_ceiling, preserve_history=False)
            if delta:
                additions[key] = _addition_summary(delta)
        else:
            _require(path == "uuid.log" and path in old and _description_subset(old[path], blob),
                     "PUBLICATION_METADATA_INPUT_UNADMITTED", path=path)
    return {"version": 1, "phase": "ADMITTED_INPUT", "baseline_digest": _tree_seal(old),
            "source_digest": _tree_seal(source), "map_uuid": map_uuid,
            "allowlist": {key: sorted(value) for key, value in sorted(allowed.items())},
            "clock_ceiling": _clock(clock_ceiling),
            "location_additions": additions, "annotation_seals": dict(sorted(seals.items()))}


def validate_candidate(*, baseline: Mapping[str, bytes], incoming: Mapping[str, bytes],
                       candidate: Mapping[str, bytes],
                       allowed_key_uuids: Mapping[str, frozenset[str]], map_uuid: str,
                       clock_ceiling: Decimal, annotation_writes: Mapping[str, AnnotationWrite],
                       expected_annotation_seals: Mapping[str, str],
                       required_key_uuids: Mapping[str, frozenset[str]] | None = None) -> dict:
    """Independently validate the native merge against its exact admitted input.

    Requires complete location-record union, not just a subset of allowed claims;
    advisory annotations use native effective-cell union (which permits native
    compaction). Unrelated metadata remains byte-identical. The caller must also
    compare these effective locations with the qualified native staging reader.
    ``allowed_key_uuids`` permits deltas; ``required_key_uuids`` independently
    requires final presence of selected proven copies, including no-op merges.
    """
    old, source, new = _tree(baseline), _tree(incoming), _tree(candidate)
    _paths(old, source, new)
    admitted = admit_input(baseline=old, incoming=source, allowed_key_uuids=allowed_key_uuids,
                           map_uuid=map_uuid, clock_ceiling=clock_ceiling,
                           annotation_writes=annotation_writes,
                           expected_annotation_seals=expected_annotation_seals)
    required = _contract({} if required_key_uuids is None else required_key_uuids,
                         map_uuid, clock_ceiling)
    for key, identities in required.items():
        _require(key in allowed_key_uuids and identities <= allowed_key_uuids[key],
                 "PUBLICATION_REQUIRED_LOCATION_UNPROVEN")
    locations, annotations = {}, {}
    for path in sorted(old.keys() | source.keys() | new.keys()):
        if path in annotation_writes:
            _require(path in new, "PUBLICATION_ANNOTATION_MERGE_MISMATCH")
            check_annotation_merge(baseline=old.get(path, b""), admitted_source=source[path],
                                   candidate=new[path])
            annotations[path] = [{"field": field, "value": value,
                                  "clock": _clock(cell.clock), "present": cell.present}
                                 for (field, value), cell in sorted(parse_annotations(new[path]).items())]
            continue
        info = _recognized(path)
        if info and not info[1] and info[0] in allowed_key_uuids:
            _require(path in new, "PUBLICATION_LOCATION_HISTORY_CHANGED")
            before, after = _records(old.get(path)), _records(new[path])
            check_location_delta(before=before, after=after, allowed_uuids=allowed_key_uuids[info[0]],
                                 map_uuid=map_uuid, observed_clock_ceiling=clock_ceiling)
            _require(after == before | _records(source.get(path)),
                     "PUBLICATION_LOCATION_MERGE_MISMATCH")
            locations[info[0]] = sorted(effective_locations(after))
            continue
        _require(new.get(path) == old.get(path), "PUBLICATION_METADATA_UNRELATED_CHANGE", path=path)
    for key, identities in required.items():
        _require(identities <= frozenset(locations.get(key, [])),
                 "PUBLICATION_REQUIRED_LOCATION_MISSING", key=key)
    return {"version": 1, "phase": "VALIDATED_CANDIDATE", "admission_digest": _seal(admitted),
            "baseline_digest": _tree_seal(old), "source_digest": _tree_seal(source),
            "candidate_digest": _tree_seal(new), "effective_locations": locations,
            "required_locations": {key: sorted(value) for key, value in sorted(required.items())},
            "annotations": annotations, "location_additions": admitted["location_additions"]}
