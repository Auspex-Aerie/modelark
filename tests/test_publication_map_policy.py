"""Pure map admission checks do not execute native tools or confer authority."""
from dataclasses import replace
from decimal import Decimal
import json

import pytest

from modelark.publication_map_policy import AnnotationWrite, admit_input, validate_candidate
from modelark.publication_policy import PublicationRefused, sha256_key

DRIVE = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
MAP = "00000000-0000-4000-8000-000000000003"
UNKNOWN = "00000000-0000-4000-8000-000000000004"
KEY = sha256_key(7, "a" * 64)
OTHER_KEY = sha256_key(9, "b" * 64)
PATH = f"abc/def/{KEY}.log"
OTHER_PATH = f"123/456/{OTHER_KEY}.log"
TAGS = PATH + ".met"


def log(clock=2, identity=DRIVE, present=1):
    return f"{clock}s {present} {identity}\n".encode()


def arguments(**kwargs):
    values = {"baseline": {}, "incoming": {PATH: log()},
              "allowed_key_uuids": {KEY: frozenset({DRIVE})}, "map_uuid": MAP,
              "clock_ceiling": Decimal(5), "annotation_writes": {}, "expected_annotation_seals": {}}
    return values | kwargs


def annotation(before=b"", after=b"2s model +org/model\n", assignments=(("model", "org/model"),)):
    return AnnotationWrite(TAGS, before, after, assignments, "c" * 64)


def with_annotation(proof=None, **kwargs):
    proof = proof or annotation()
    return arguments(incoming={PATH: log(), TAGS: proof.after}, annotation_writes={TAGS: proof},
                     expected_annotation_seals={TAGS: proof.seal()}, **kwargs)


def test_selected_positive_source_and_exact_final_union():
    old = {PATH: log(1, OTHER), "trust.log": b"trusted baseline\n"}
    args = arguments(baseline=old)
    admitted = admit_input(**args)
    assert admitted["location_additions"][KEY] == [{"uuid": DRIVE, "clock": "2", "present": True}]
    candidate = old | {PATH: log(2) + log(1, OTHER)}
    checked = validate_candidate(candidate=candidate, **args)
    assert checked["effective_locations"] == {KEY: [DRIVE, OTHER]}
    assert json.loads(json.dumps(checked)) == checked
    assert validate_candidate(candidate=dict(reversed(list(candidate.items()))), **args) == checked


@pytest.mark.parametrize("source,error", [
    (log(identity=MAP), "MAP_CONTENT_CLAIM"),
    (log(identity=UNKNOWN), "UUID_UNPROVEN"),
    (log(present=0), "DROP_CLAIM"),
    (log(clock=99), "FUTURE_CLOCK"),
    (log() + log(present=0), "CLOCK_CONFLICT"),
    (b"not a native log\n", "LOG_INVALID"),
    (log().rstrip(), "LOG_INVALID"),
])
def test_quarantine_refuses_unproven_or_conflicting_location_claims(source, error):
    with pytest.raises(PublicationRefused, match=error):
        admit_input(**arguments(incoming={PATH: source}))


@pytest.mark.parametrize("clock", [0, 1])
def test_new_nonadvancing_source_history_is_not_accepted(clock):
    with pytest.raises(PublicationRefused, match="CLOCK_CONFLICT"):
        admit_input(**arguments(baseline={PATH: log(1, present=0)}, incoming={PATH: log(clock)}))


def test_old_map_presence_drop_history_and_future_history_are_preserved_not_created():
    prior = log(100, MAP) + log(1, OTHER, 0)
    args = arguments(baseline={PATH: prior})
    result = validate_candidate(candidate={PATH: prior + log()}, **args)
    assert result["effective_locations"][KEY] == [DRIVE, MAP]
    with pytest.raises(PublicationRefused, match="HISTORY_CHANGED"):
        validate_candidate(candidate={PATH: log()}, **args)


def test_candidate_cannot_invent_a_permitted_extra_record_or_omit_admitted_record():
    args = arguments(baseline={PATH: log(1)})
    for candidate in ({PATH: log(1)}, {PATH: log(1) + log() + log(3)}):
        with pytest.raises(PublicationRefused, match="MERGE_MISMATCH"):
            validate_candidate(candidate=candidate, **args)


def test_source_subset_does_not_authorize_final_history_deletion():
    old = {PATH: log(1, OTHER) + log(), OTHER_PATH: log(1, OTHER)}
    args = arguments(baseline=old, incoming={PATH: log(), OTHER_PATH: log(1, OTHER)})
    assert admit_input(**args)["location_additions"] == {}
    validate_candidate(candidate=old, **args)
    with pytest.raises(PublicationRefused, match="HISTORY_CHANGED"):
        validate_candidate(candidate={PATH: log(), OTHER_PATH: old[OTHER_PATH]}, **args)


def test_unselected_location_subset_is_admitted_but_no_new_record_or_final_rewrite():
    old = {OTHER_PATH: log(1, OTHER) + log(2, OTHER)}
    args = arguments(baseline=old, incoming={OTHER_PATH: log(2, OTHER)})
    admit_input(**args)
    validate_candidate(candidate=old, **args)
    with pytest.raises(PublicationRefused, match="UUID_UNPROVEN"):
        admit_input(**(args | {"incoming": {OTHER_PATH: log(3, OTHER)}}))
    with pytest.raises(PublicationRefused, match="UNRELATED_CHANGE"):
        validate_candidate(candidate={OTHER_PATH: log(2, OTHER) + log(1, OTHER)}, **args)


@pytest.mark.parametrize("path", ["trust.log", "remote.log", "config", "uuid.log", TAGS,
                                 "future-format.log"])
def test_unknown_or_unbound_metadata_cannot_enter_native_merge(path):
    with pytest.raises(PublicationRefused, match="INPUT_UNADMITTED"):
        admit_input(**arguments(incoming={path: b"unexpected\n"}))


def test_registered_uuid_descriptions_may_only_be_subset_at_source():
    first, second = f"{DRIVE} archive\n".encode(), f"{OTHER} offline\n".encode()
    args = arguments(baseline={"uuid.log": first + second}, incoming={"uuid.log": first})
    admit_input(**args)
    validate_candidate(candidate=args["baseline"], **args)
    for changed in [second + first, first, first + second + b"new\n"]:
        with pytest.raises(PublicationRefused, match="UNRELATED_CHANGE"):
            validate_candidate(candidate={"uuid.log": changed}, **args)
    for source in [first.rstrip(), first + b"\n", first + b"\0\n", b"unknown\n"]:
        with pytest.raises(PublicationRefused, match="INPUT_UNADMITTED"):
            admit_input(**(args | {"incoming": {"uuid.log": source}}))


@pytest.mark.parametrize("change", [{}, {"trust.log": b"modified\n"},
                                    {"trust.log": b"old\n", "remote.log": b"new\n"}])
def test_candidate_unrelated_metadata_must_be_byte_identical(change):
    with pytest.raises(PublicationRefused, match="UNRELATED_CHANGE"):
        validate_candidate(candidate=change | {PATH: log()},
                           **arguments(baseline={"trust.log": b"old\n"}))


def test_sealed_annotation_write_and_native_merge_preserve_other_repository_tags():
    old = {TAGS: b"1s model +other/owner\n"}
    args = with_annotation(baseline=old)
    admitted = admit_input(**args)
    assert admitted["annotation_seals"] == args["expected_annotation_seals"]
    candidate = {PATH: log(), TAGS: old[TAGS] + args["incoming"][TAGS]}
    result = validate_candidate(candidate=candidate, **args)
    assert [cell["value"] for cell in result["annotations"][TAGS]] == ["org/model", "other/owner"]
    with pytest.raises(PublicationRefused, match="MERGE_MISMATCH"):
        validate_candidate(candidate=args["incoming"], **args)


def test_annotation_compaction_uses_semantics_not_byte_history_union():
    old = b"1s model +org/old params +7\n"
    compacted = b"1s params +7\n2s model +org/model -org/old\n"
    proof = annotation(before=old, after=old + compacted)
    args = with_annotation(proof, baseline={TAGS: old})
    result = validate_candidate(candidate={PATH: log(), TAGS: compacted}, **args)
    assert {cell["value"] for cell in result["annotations"][TAGS] if cell["present"]} == {"7", "org/model"}


@pytest.mark.parametrize("change,error", [
    ({"incoming": {PATH: log()}}, "SOURCE_CHANGED"),
    ({"incoming": {PATH: log(), TAGS: b"3s model +hijacked\n"}}, "SOURCE_CHANGED"),
    ({"expected_annotation_seals": {}}, "PROOF_SET_MISMATCH"),
    ({"annotation_writes": {}}, "PROOF_SET_MISMATCH"),
    ({"expected_annotation_seals": {TAGS: "0" * 64}}, "PROOF_CHANGED"),
    ({"allowed_key_uuids": {}}, "KEY_UNSELECTED"),
])
def test_annotation_proof_and_exact_source_binding_required(change, error):
    with pytest.raises(PublicationRefused, match=error):
        admit_input(**(with_annotation() | change))


def test_changed_operation_binding_cannot_reuse_sealed_annotation_write():
    args = with_annotation()
    args["annotation_writes"] = {TAGS: replace(args["annotation_writes"][TAGS], binding_digest="d" * 64)}
    with pytest.raises(PublicationRefused, match="PROOF_CHANGED"):
        admit_input(**args)


@pytest.mark.parametrize("after", [b"2s model +org/model operator +hijacked\n",
                                   b"2s model +wrong\n", b"99s model +org/model\n"])
def test_even_sealed_annotation_values_must_match_native_write_policy(after):
    with pytest.raises(PublicationRefused):
        admit_input(**with_annotation(annotation(after=after)))


def test_unbound_unchanged_annotation_is_not_a_blanket_annotation_exception():
    old = {TAGS: b"1s model +org/old\n"}
    args = arguments(baseline=old, incoming=old)
    validate_candidate(candidate=old, **args)
    with pytest.raises(PublicationRefused, match="UNRELATED_CHANGE"):
        validate_candidate(candidate={TAGS: b"2s model +org/old\n"}, **args)


def test_annotation_equal_clock_conflict_cannot_be_hidden_in_merge():
    args = with_annotation(baseline={TAGS: b"2s model -org/model\n"})
    with pytest.raises(PublicationRefused, match="CLOCK_CONFLICT"):
        admit_input(**args)
    with pytest.raises(PublicationRefused, match="CLOCK_CONFLICT"):
        validate_candidate(candidate=args["incoming"], **args)


def test_key_cannot_hide_records_in_alternate_metadata_bucket():
    alternate = f"fff/fff/{KEY}.log"
    for source in [{PATH: log(), alternate: log()}, {alternate: log()}]:
        with pytest.raises(PublicationRefused, match="KEY_PATH_CONFLICT"):
            admit_input(**arguments(baseline={PATH: log(1)}, incoming=source))
    proof = replace(annotation(), path=alternate + ".met")
    with pytest.raises(PublicationRefused, match="KEY_PATH_CONFLICT"):
        admit_input(**arguments(incoming={PATH: log(), proof.path: proof.after},
                                annotation_writes={proof.path: proof},
                                expected_annotation_seals={proof.path: proof.seal()}))


@pytest.mark.parametrize("allowed", [{KEY: {DRIVE}}, {KEY: frozenset()}, {KEY: frozenset({MAP})},
                                     {KEY: frozenset({"not-uuid"})},
                                     {KEY.replace("SHA256-", "SHA256E-") + ".unknown": frozenset({DRIVE})}])
def test_invalid_or_unqualified_allowlist_refuses(allowed):
    with pytest.raises(PublicationRefused):
        admit_input(**arguments(allowed_key_uuids=allowed))


@pytest.mark.parametrize("change", [{"map_uuid": "not-uuid"}, {"clock_ceiling": Decimal("NaN")},
                                    {"clock_ceiling": 5}, {"incoming": {"../config": b"x"}},
                                    {"incoming": {PATH: "not bytes"}}])
def test_contract_and_tree_types_refuse(change):
    with pytest.raises(PublicationRefused):
        admit_input(**arguments(**change))


def test_explicit_empty_input_and_unchanged_candidate_are_noop_not_missing_evidence():
    args = arguments(baseline={"config": b"known baseline\n"}, incoming={}, allowed_key_uuids={})
    result = validate_candidate(candidate=args["baseline"], **args)
    assert result["location_additions"] == result["effective_locations"] == {}
    assert result["candidate_digest"] == result["baseline_digest"]


def test_record_summary_clock_is_canonical_without_decimal_context_rounding():
    clock = "123456789012345678901234567890.123456789"
    args = arguments(incoming={PATH: log(clock=clock) + log(clock=clock + "00")},
                     clock_ceiling=Decimal(clock))
    assert admit_input(**args)["location_additions"][KEY][0]["clock"] == clock


@pytest.mark.parametrize("assignments", [(("model", "x"), ("model", "y")),
                                         (("quant", "none"), ("model", "x")),
                                         (("model", 1),), {"model": "x"}])
def test_annotation_proof_assignment_record_is_immutable_unique_and_sorted(assignments):
    with pytest.raises(PublicationRefused, match="PROOF_INVALID"):
        replace(annotation(), assignments=assignments).seal()


def test_source_annotation_proof_cannot_smuggle_unrelated_baseline_field():
    prior = b"1s operator +arbitrary\n"
    proof = annotation(before=prior, after=prior + b"2s model +org/model\n")
    with pytest.raises(PublicationRefused, match="UNRELATED_CHANGE"):
        admit_input(**with_annotation(proof))
    # Known baseline fields are preserved, not manufactured by the publication.
    admit_input(**with_annotation(proof, baseline={TAGS: prior}))


def test_preserved_source_annotation_is_not_permission_to_import_future_clock():
    prior = b"99s params +7\n"
    proof = annotation(before=prior, after=prior + b"2s model +org/model\n")
    with pytest.raises(PublicationRefused, match="FUTURE_CLOCK"):
        admit_input(**with_annotation(proof))
    admit_input(**with_annotation(proof, baseline={TAGS: prior}))


def test_required_locations_distinct_from_permission_and_satisfied_by_noop():
    args = arguments(baseline={PATH: log()}, incoming={PATH: log()})
    result = validate_candidate(candidate={PATH: log()}, required_key_uuids={KEY: frozenset({DRIVE})}, **args)
    assert result["required_locations"] == {KEY: [DRIVE]}
    assert result["location_additions"] == {}
    assert result["candidate_digest"] == result["baseline_digest"]


@pytest.mark.parametrize("baseline", [{}, {PATH: log(1, present=0)}, {PATH: log(1, OTHER)}])
def test_allowed_but_absent_copy_is_not_completed_required_copy(baseline):
    args = arguments(baseline=baseline, incoming={})
    validate_candidate(candidate=baseline, **args)
    with pytest.raises(PublicationRefused, match="REQUIRED_LOCATION_MISSING"):
        validate_candidate(candidate=baseline, required_key_uuids={KEY: frozenset({DRIVE})}, **args)


@pytest.mark.parametrize("required", [{KEY: frozenset({OTHER})}, {OTHER_KEY: frozenset({OTHER})}])
def test_required_copy_cannot_exceed_physically_proven_allowlist(required):
    with pytest.raises(PublicationRefused, match="REQUIRED_LOCATION_UNPROVEN"):
        validate_candidate(candidate={PATH: log()}, required_key_uuids=required, **arguments())
