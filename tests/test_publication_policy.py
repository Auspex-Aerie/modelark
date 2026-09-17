"""Pure publication policy: no archive, service, database or external process."""
from decimal import Decimal
import hashlib

import pytest

from modelark import publication_policy as policy


DRIVE = "11111111-1111-4111-8111-111111111111"
MAP = "22222222-2222-4222-8222-222222222222"
DIGEST = hashlib.sha256(b"payload").hexdigest()
KEY = policy.sha256_key(7, DIGEST)
OBJECT = f".git/annex/objects/Ab/01/{KEY}/{KEY}"


@pytest.mark.parametrize("name", [".gitignore", ".gitattributes", "nested/.gitignore", ".eval/test.yaml"])
def test_hidden_payload_is_mapped_by_exact_logical_name(name):
    assert policy.payload_relative_path(name) == (
        f"{policy.NAMESPACE}/p-{hashlib.sha256(name.encode()).hexdigest()}.blob")


def test_ordinary_payload_path_is_unchanged():
    assert policy.payload_relative_path("weights/model.safetensors") == "weights/model.safetensors"


@pytest.mark.parametrize("name", ["", "/absolute", "../escape", "x//y", "x/./y", "a/.git/config",
                                 "C:/file", "a\\b", "a\0b", "__modelark_payload_v1__/thing"])
def test_unsafe_or_reserved_names_refuse(name):
    with pytest.raises(policy.PublicationRefused):
        policy.payload_relative_path(name)


def test_unencodable_logical_name_is_typed_refusal():
    with pytest.raises(policy.PublicationRefused):
        policy.payload_relative_path(".bad\ud800")


def test_direct_location_records_cannot_bypass_clock_or_type_validation():
    with pytest.raises(policy.PublicationRefused):
        policy.LocationRecord(DRIVE, Decimal("NaN"), True)
    records = frozenset({policy.LocationRecord(DRIVE, Decimal(1), True),
                         policy.LocationRecord(DRIVE, Decimal(1), False)})
    with pytest.raises(policy.PublicationRefused, match="CLOCK_CONFLICT"):
        policy.effective_locations(records)
    with pytest.raises(policy.PublicationRefused, match="CLOCK_CONFLICT"):
        policy.check_location_delta(before=frozenset(), after=records,
                                    allowed_uuids=frozenset({DRIVE}), map_uuid=MAP,
                                    observed_clock_ceiling=Decimal(2))


@pytest.mark.parametrize("mode,blob", [
    ("120000", ("../../../" + OBJECT).encode()),
    ("100644", f"/annex/objects/{KEY}\n".encode()),
])
def test_exact_locked_and_unlocked_pointer(mode, blob):
    policy.check_committed_pointer(mode=mode, blob=blob, stored_path="org/repo/payload/x",
                                   key=KEY, qualified_object_path=OBJECT)


@pytest.mark.parametrize("mode,blob", [
    ("120000", OBJECT.encode()), ("120000", ("/" + OBJECT).encode()),
    ("120000", ("../../../" + OBJECT + "/suffix").encode()),
    ("100644", b"payload"), ("100755", f"/annex/objects/{KEY}\n".encode()),
    ("120000", ("../../../" + OBJECT.replace("Ab/01", "Cd/02")).encode()),
])
def test_pointer_is_not_a_key_substring_check(mode, blob):
    with pytest.raises(policy.PublicationRefused):
        policy.check_committed_pointer(mode=mode, blob=blob, stored_path="org/repo/payload/x",
                                       key=KEY, qualified_object_path=OBJECT)


def log(*rows):
    return policy.parse_location_log("".join(f"{clock}s {state} {identity}\n"
                                            for clock, state, identity in rows).encode())


def test_location_history_retains_absence_and_native_later_presence():
    old = log((1, 0, DRIVE))
    new = log((1, 0, DRIVE), (2, 1, DRIVE))
    assert policy.effective_locations(new) == {DRIVE}
    assert policy.check_location_delta(before=old, after=new, allowed_uuids=frozenset({DRIVE}),
                                       map_uuid=MAP, observed_clock_ceiling=Decimal(3)) == new - old
    assert policy.check_location_delta(before=new, after=new, allowed_uuids=frozenset(),
                                       map_uuid=MAP, observed_clock_ceiling=Decimal(3)) == frozenset()


@pytest.mark.parametrize("rows", [((1, 0, DRIVE), (1, 1, DRIVE)), (("nan", 1, DRIVE),),
                                   ((1, 2, DRIVE),), ((1, 1, "unknown"),)])
def test_unknown_log_or_equal_clock_conflict_refuses(rows):
    with pytest.raises(policy.PublicationRefused):
        log(*rows)


@pytest.mark.parametrize("clock,present,identity,code", [
    (2, 1, MAP, "PUBLICATION_MAP_CONTENT_CLAIM"),
    (2, 0, DRIVE, "PUBLICATION_DROP_CLAIM"),
    (9, 1, DRIVE, "PUBLICATION_FUTURE_CLOCK"),
    (0, 1, DRIVE, "PUBLICATION_LOCATION_CLOCK_CONFLICT"),
])
def test_bad_location_delta_refuses(clock, present, identity, code):
    old = log((1, 0, DRIVE))
    new = old | log((clock, present, identity))
    with pytest.raises(policy.PublicationRefused, match=code):
        policy.check_location_delta(before=old, after=new, allowed_uuids=frozenset({DRIVE}),
                                    map_uuid=MAP, observed_clock_ceiling=Decimal(3))


def test_source_quarantine_can_omit_history_but_final_merge_cannot():
    old, new = log((1, 0, DRIVE)), log((2, 1, DRIVE))
    kwargs = dict(before=old, after=new, allowed_uuids=frozenset({DRIVE}), map_uuid=MAP,
                  observed_clock_ceiling=Decimal(3))
    policy.check_location_delta(**kwargs, preserve_history=False)
    with pytest.raises(policy.PublicationRefused, match="HISTORY_CHANGED"):
        policy.check_location_delta(**kwargs)


def test_old_index_partial_tree_is_valid_but_mixed_index_is_not():
    old = {"org/repo/.gitattributes": ("100644", DIGEST)}
    new = {"org/repo/payload/x": ("120000", DIGEST)}
    base = dict(old=old, new=new, directories=frozenset({"org", "org/repo"}))
    policy.check_replay_state(**base, index=old, worktree={})
    policy.check_replay_state(**base, index=new, worktree=new)
    for index, worktree in (({}, {}), (new, old), (new, {}), (old, {"extra": ("100644", DIGEST)})):
        with pytest.raises(policy.PublicationRefused):
            policy.check_replay_state(**base, index=index, worktree=worktree)
    with pytest.raises(policy.PublicationRefused, match="UNRELATED_DIRECTORY"):
        policy.check_replay_state(old=old, new=new, index=old, worktree=old,
                                  directories=frozenset({"unrelated-empty"}))
