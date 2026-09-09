"""Pure folder contract gate: no filesystem adapter or writer is enabled here."""
from dataclasses import replace
import json

import pytest

from modelark.slice.folder_contract import (
    CapacityObservation,
    FolderProfile,
    FolderReview,
    FolderTarget,
    overlaps,
)
from modelark.slice.transaction import TransferPlan, TransferRefusal


def target(**changes):
    values = dict(profile=FolderProfile.NATIVE_EXT4, filesystem_scope="trusted-fs-key",
                  parent_parts=("home", "operator", "exports"),
                  parent_identity=(41, 123456789), child_name="delivery")
    values.update(changes)
    return FolderTarget(**values)


def review(**changes):
    values = dict(target=target(), closure_seal="a" * 64,
                  capacity=CapacityObservation(1_000_000, 100, 4096))
    values.update(changes)
    return FolderReview(**values)


def test_profile_values_and_explicit_resume_claims():
    assert FolderProfile.NATIVE_EXT4.value == "native-ext4-folder.v1"
    assert FolderProfile.FAT32_SESSION.value == "fat32-folder-session.v1"
    assert FolderProfile.NATIVE_EXT4.resume_policy == "authenticated"
    assert FolderProfile.FAT32_SESSION.resume_policy == "new-root"
    assert target(profile="native-ext4-folder.v1").profile is FolderProfile.NATIVE_EXT4
    fat = target(profile="fat32-folder-session.v1", parent_identity="b" * 32)
    assert fat.profile is FolderProfile.FAT32_SESSION
    assert review(target=fat).execution_ready is False
    assert review().execution_ready is False


def test_target_roundtrip_stable_identity_and_filesystem_relative_parts():
    original = target()
    restored = FolderTarget.from_json(original.to_json())
    assert restored == original
    assert restored.to_json() == original.to_json()
    assert restored.target_id == original.target_id
    assert original.parts == ("home", "operator", "exports", "delivery")
    assert json.loads(original.to_json())["version"] == "modelark.slice.folder-target.v1"
    assert target(parent_parts=()).parts == ("delivery",)


@pytest.mark.parametrize("changes", [
    {"filesystem_scope": "other-fs"},
    {"parent_parts": ("home", "different")},
    {"parent_identity": (42, 123456789)},
    {"parent_identity": (41, 123456790)},
    {"child_name": "other"},
    {"profile": FolderProfile.FAT32_SESSION, "parent_identity": "b" * 32},
])
def test_target_identity_binds_scope_path_parent_and_profile(changes):
    assert target(**changes).target_id != target().target_id


def test_capacity_is_approval_evidence_not_target_identity():
    original = review()
    for capacity in (CapacityObservation(999_999, 100, 4096),
                     CapacityObservation(1_000_000, 99, 4096),
                     CapacityObservation(1_000_000, 100, 8192),
                     CapacityObservation(1_000_000, None, 4096)):
        changed = replace(original, capacity=capacity)
        assert changed.target.target_id == original.target.target_id
        assert changed.seal != original.seal
    assert replace(original, closure_seal="b" * 64).seal != original.seal


def test_review_json_roundtrip_and_seal():
    original = review()
    restored = FolderReview.from_json(original.to_json())
    assert restored == original
    assert restored.to_json() == original.to_json()
    assert restored.seal == original.seal
    assert len(original.seal) == 64
    assert all(c in "0123456789abcdef" for c in original.seal)
    assert json.loads(original.to_json())["version"] == "modelark.slice.folder-review.v1"
    assert restored.execution_ready is False


def test_fat_session_identity_roundtrip_and_no_resume():
    original = target(profile=FolderProfile.FAT32_SESSION, parent_identity="0123456789abcdef" * 2)
    assert FolderTarget.from_json(original.to_json()) == original
    assert FolderReview.from_json(review(target=original).to_json()).target == original
    assert original.profile.resume_policy == "new-root"
    assert replace(original, parent_identity="c" * 32).target_id != original.target_id


@pytest.mark.parametrize("left,right,expected", [
    (target(), target(), True),
    (target(), target(parent_identity=(99, 99)), True),
    (target(), target(child_name="sibling"), False),
    (target(), target(child_name="delivery-more"), False),
    (target(), target(filesystem_scope="different-fs"), False),
    (target(), target(parent_parts=("home", "operator", "exports", "delivery"),
                      child_name="nested"), True),
    (target(), target(parent_parts=("home", "operator", "exports", "delivery-more"),
                      child_name="nested"), False),
    (target(parent_parts=(), child_name="home"), target(), True),
])
def test_overlap_uses_component_ancestry_not_ownership_certificate(left, right, expected):
    assert overlaps(left, right) is expected
    assert overlaps(right, left) is expected


def test_same_filesystem_cannot_claim_conflicting_profiles():
    native = target()
    fat = target(profile=FolderProfile.FAT32_SESSION, parent_identity="b" * 32)
    for first, second in ((native, fat), (fat, native)):
        with pytest.raises(TransferRefusal) as exc:
            overlaps(first, second)
        assert exc.value.code == "FOLDER_CONTRACT_INVALID"
    assert overlaps(native, replace(fat, filesystem_scope="other-fs")) is False


def test_fat_exact_path_ancestry_conflicts_despite_session_token_change():
    fat = target(profile=FolderProfile.FAT32_SESSION, parent_identity="b" * 32)
    assert overlaps(fat, replace(fat, parent_identity="c" * 32)) is True
    child = replace(fat, parent_parts=fat.parts, child_name="nested", parent_identity="c" * 32)
    assert overlaps(fat, child) is True
    assert overlaps(child, fat) is True


@pytest.mark.parametrize("child_name", ["DELIVERY", "different", "DELIVE~1", "delivery-more"])
def test_fat_disjointness_needs_qualified_case_and_short_alias_semantics(child_name):
    fat = target(profile=FolderProfile.FAT32_SESSION, parent_identity="b" * 32)
    for first, second in ((fat, replace(fat, child_name=child_name)),
                          (replace(fat, child_name=child_name), fat)):
        with pytest.raises(TransferRefusal) as exc:
            overlaps(first, second)
        assert exc.value.code == "FOLDER_CONTRACT_INVALID"


@pytest.mark.parametrize("invalid", [None, {}, "folder-target-v1:anything"])
def test_overlap_refuses_untyped_targets(invalid):
    with pytest.raises(TransferRefusal):
        overlaps(target(), invalid)
    with pytest.raises(TransferRefusal):
        overlaps(invalid, target())


@pytest.mark.parametrize("field,value", [
    ("filesystem_scope", ""), ("filesystem_scope", " "),
    ("filesystem_scope", " key"), ("filesystem_scope", "key "),
    ("filesystem_scope", None), ("filesystem_scope", 1),
    ("profile", "ext4"), ("profile", "exfat-folder.v1"), ("profile", None),
    ("parent_parts", "/home/exports"), ("parent_parts", None),
    ("parent_parts", (".",)), ("parent_parts", ("..",)),
    ("parent_parts", ("",)), ("parent_parts", ("a/b",)),
    ("parent_parts", ("a\\b",)), ("parent_parts", ("a\x00b",)),
    ("parent_parts", ("a:b",)), ("parent_parts", (1,)),
    ("parent_parts", ("\udcff",)),
    ("child_name", ""), ("child_name", "."), ("child_name", ".."),
    ("child_name", "/absolute"), ("child_name", "a/b"),
    ("child_name", "a\\b"), ("child_name", "a:b"),
    ("child_name", "a\x00b"), ("child_name", "\udcff"),
    ("child_name", None), ("child_name", 7),
    ("parent_identity", None), ("parent_identity", "b" * 32),
    ("parent_identity", (0, 1)), ("parent_identity", (1, 0)),
    ("parent_identity", (-1, 1)), ("parent_identity", (1, -1)),
    ("parent_identity", (True, 1)), ("parent_identity", (1, False)),
    ("parent_identity", (1.0, 2)), ("parent_identity", (1,)),
    ("parent_identity", (1, 2, 3)),
])
def test_malformed_target_constructor_refuses(field, value):
    with pytest.raises(TransferRefusal):
        target(**{field: value})


@pytest.mark.parametrize("identity", [None, (1, 2), "", "a" * 31, "a" * 33,
                                      "A" * 32, "g" * 32, "a" * 31 + " "])
def test_fat_identity_requires_retained_session_token(identity):
    with pytest.raises(TransferRefusal):
        target(profile=FolderProfile.FAT32_SESSION, parent_identity=identity)


@pytest.mark.parametrize("field", ["available_bytes", "free_inodes", "headroom_bytes"])
@pytest.mark.parametrize("value", [-1, True, False, 1.5, "1", float("inf"), float("nan")])
def test_capacity_rejects_nonnegative_integer_impostors(field, value):
    kwargs = dict(available_bytes=100, free_inodes=5, headroom_bytes=0)
    kwargs[field] = value
    with pytest.raises(TransferRefusal):
        CapacityObservation(**kwargs)


def test_capacity_zero_and_unknown_inodes_are_observations_not_guarantees():
    assert CapacityObservation(0, None).headroom_bytes == 0
    assert review(capacity=CapacityObservation(0, 0)).execution_ready is False
    for field in ("available_bytes", "headroom_bytes"):
        kwargs = dict(available_bytes=100, free_inodes=None, headroom_bytes=0)
        kwargs[field] = None
        with pytest.raises(TransferRefusal):
            CapacityObservation(**kwargs)


@pytest.mark.parametrize("closure_seal", [None, "", "a" * 63, "a" * 65,
                                          "A" * 64, "g" * 64, "a" * 63 + " "])
def test_review_requires_exact_closure_seal(closure_seal):
    with pytest.raises(TransferRefusal):
        review(closure_seal=closure_seal)


@pytest.mark.parametrize("changes", [{"target": None}, {"target": {}},
                                      {"capacity": None}, {"capacity": {}}])
def test_review_requires_typed_contract_objects(changes):
    with pytest.raises(TransferRefusal):
        review(**changes)


@pytest.mark.parametrize("factory", [target, review])
@pytest.mark.parametrize("mutation", ["unknown", "missing", "version", "duplicate"])
def test_json_root_schema_is_closed(factory, mutation):
    original = factory()
    obj = json.loads(original.to_json())
    if mutation == "unknown":
        obj["execution_ready"] = True
    elif mutation == "missing":
        del obj["version"]
    elif mutation == "version":
        obj["version"] = "modelark.slice.future.v99"
    if mutation == "duplicate":
        payload = '{"version":' + json.dumps(obj["version"]) + ',' + original.to_json()[1:]
    else:
        payload = json.dumps(obj)
    with pytest.raises(TransferRefusal):
        type(original).from_json(payload)


@pytest.mark.parametrize("payload", ["", "{", "null", "[]", "true", "1", '"text"',
                                    '{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}'])
@pytest.mark.parametrize("contract", [FolderTarget, FolderReview])
def test_malformed_json_refuses_as_contract_error(contract, payload):
    with pytest.raises(TransferRefusal):
        contract.from_json(payload)


@pytest.mark.parametrize("field", ["target", "capacity"])
def test_review_nested_json_unknown_fields_refuse(field):
    obj = json.loads(review().to_json())
    obj[field]["unreviewed_extension"] = True
    with pytest.raises(TransferRefusal):
        FolderReview.from_json(json.dumps(obj))


def test_review_nested_duplicate_fields_refuse():
    obj = json.loads(review().to_json())
    encoded_capacity = json.dumps(obj["capacity"])
    obj["capacity"] = "capacity-placeholder"
    duplicate_capacity = '{"available_bytes":0,' + encoded_capacity[1:]
    payload = json.dumps(obj).replace('"capacity-placeholder"', duplicate_capacity)
    with pytest.raises(TransferRefusal):
        FolderReview.from_json(payload)


def test_review_nonfinite_nested_capacity_refuses():
    obj = json.loads(review().to_json())
    obj["capacity"]["available_bytes"] = float("nan")
    with pytest.raises(TransferRefusal):
        FolderReview.from_json(json.dumps(obj))


def test_folder_review_cannot_enter_legacy_transaction_execution():
    with pytest.raises(TransferRefusal) as exc:
        TransferPlan.from_json(review().to_json())
    assert exc.value.code == "STATE_CORRUPT"
