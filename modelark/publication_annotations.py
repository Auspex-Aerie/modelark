"""Pure validators for native annex-8 per-key advisory model tags.

Unlike location logs, native tag logs compact superseded (field, value) records.
Validate their effective timestamped cells, not byte-line history inclusion.
No tags establish payload identity, presence, ownership or write authority here.
The production publisher must seal the source write proof before importing it.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from decimal import Decimal
import re
from typing import Mapping

from modelark.publication_policy import PublicationRefused, _require

_CLOCK = re.compile(r"[0-9]+(?:\.[0-9]+)?s\Z")
_FIELD = re.compile(r"[a-z][a-z0-9-]*\Z")
MODEL_FIELDS = frozenset({"model", "format", "quant", "params"})


@dataclass(frozen=True)
class AnnotationCell:
    clock: Decimal
    present: bool

    def __post_init__(self):
        _require(isinstance(self.clock, Decimal) and self.clock.is_finite()
                 and self.clock >= 0 and type(self.present) is bool,
                 "PUBLICATION_ANNOTATION_INVALID")


AnnotationState = dict[tuple[str, str], AnnotationCell]


def _insert(state: AnnotationState, field: str, value: str, cell: AnnotationCell):
    previous = state.get((field, value))
    _require(previous is None or previous.clock != cell.clock or previous == cell,
             "PUBLICATION_ANNOTATION_CLOCK_CONFLICT")
    if previous is None or cell.clock > previous.clock:
        state[field, value] = cell


def parse_annotations(data: bytes) -> AnnotationState:
    """Decode the qualified native log grammar; missing log is explicit b''.

    Values with whitespace/non-ASCII use annex's !base64 UTF-8 encoding. The
    limited field grammar is deliberately fail-closed, not a general annex parser.
    """
    _require(isinstance(data, bytes) and (not data or data.endswith(b"\n")),
             "PUBLICATION_ANNOTATION_INVALID")
    state: AnnotationState = {}
    history = {}
    try:
        for line in data.decode("ascii").split("\n")[:-1]:
            tokens = line.split(" ")
            _require(len(tokens) >= 3 and _CLOCK.fullmatch(tokens[0]) is not None,
                     "PUBLICATION_ANNOTATION_INVALID")
            clock = Decimal(tokens[0][:-1])
            field = None
            field_has_value = False
            fields_seen = set()
            cells_seen = set()
            for token in tokens[1:]:
                if _FIELD.fullmatch(token):
                    _require(field is None or field_has_value, "PUBLICATION_ANNOTATION_INVALID")
                    _require(token not in fields_seen, "PUBLICATION_ANNOTATION_INVALID")
                    fields_seen.add(token)
                    field, field_has_value = token, False
                    continue
                _require(field is not None and len(token) > 1 and token[0] in "+-",
                         "PUBLICATION_ANNOTATION_INVALID")
                encoded = token[1:]
                if encoded.startswith("!"):
                    raw = base64.b64decode(encoded[1:], validate=True)
                    _require(base64.b64encode(raw).decode() == encoded[1:],
                             "PUBLICATION_ANNOTATION_INVALID")
                    value = raw.decode("utf-8")
                else:
                    _require(all(33 <= ord(char) <= 126 for char in encoded),
                             "PUBLICATION_ANNOTATION_INVALID")
                    value = encoded
                _require(bool(value) and "\0" not in value and (field, value) not in cells_seen,
                         "PUBLICATION_ANNOTATION_INVALID")
                cells_seen.add((field, value))
                cell = AnnotationCell(clock, token[0] == "+")
                history_key = field, value, clock
                _require(history_key not in history or history[history_key] == cell,
                         "PUBLICATION_ANNOTATION_CLOCK_CONFLICT")
                history[history_key] = cell
                _insert(state, field, value, cell)
                field_has_value = True
            _require(field_has_value, "PUBLICATION_ANNOTATION_INVALID")
    except (UnicodeError, binascii.Error) as exc:
        raise PublicationRefused("PUBLICATION_ANNOTATION_INVALID") from exc
    return state


def check_annotation_write(*, before: bytes, after: bytes, assignments: Mapping[str, str],
                           clock_ceiling: Decimal) -> None:
    """Verify an already-performed, sealed native -s write for the four Fill tags.

    Does not execute the write or confer authority to perform it. Preserves all
    other effective cells, including tombstones. Source admission/guarded native
    command execution must happen before this postcondition can be used.
    """
    old, new = parse_annotations(before), parse_annotations(after)
    _require(isinstance(clock_ceiling, Decimal) and clock_ceiling.is_finite()
             and clock_ceiling >= 0, "PUBLICATION_CLOCK_BOUND_INVALID")
    _require(isinstance(assignments, Mapping) and bool(assignments) and assignments.keys() <= MODEL_FIELDS
             and all(isinstance(v, str) and bool(v) and "\0" not in v for v in assignments.values()),
             "PUBLICATION_ANNOTATION_ASSIGNMENT_INVALID")
    for pair in old.keys() | new.keys():
        previous, current = old.get(pair), new.get(pair)
        if previous == current:
            continue
        field, value = pair
        _require(field in assignments and current is not None, "PUBLICATION_ANNOTATION_UNRELATED_CHANGE")
        _require(current.clock <= clock_ceiling and (previous is None or current.clock > previous.clock),
                 "PUBLICATION_ANNOTATION_CLOCK_CONFLICT")
        if current.present:
            _require(value == assignments[field], "PUBLICATION_ANNOTATION_UNRELATED_CHANGE")
        else:
            _require(previous is not None and previous.present and value != assignments[field],
                     "PUBLICATION_ANNOTATION_UNRELATED_CHANGE")
    for field, value in assignments.items():
        values = {v for (f, v), cell in new.items() if f == field and cell.present}
        _require(values == {value}, "PUBLICATION_ANNOTATION_ASSIGNMENT_MISMATCH")


def check_annotation_merge(*, baseline: bytes, admitted_source: bytes, candidate: bytes) -> None:
    """Verify native merge's effective union, never synthesize an annex log.

    `admitted_source` MUST already have a source write/unchanged-baseline proof;
    parsing attacker-chosen bytes alone is not input admission. Same-key tags from
    other repositories remain advisory set values, matching native annex behavior.
    """
    expected = parse_annotations(baseline)
    for (field, value), cell in parse_annotations(admitted_source).items():
        _insert(expected, field, value, cell)
    _require(parse_annotations(candidate) == expected, "PUBLICATION_ANNOTATION_MERGE_MISMATCH")
