"""Native plan serialization and shared-space evidence do not grant authority."""
from dataclasses import asdict, replace
import json

import pytest

from modelark.slice import domain as d
from modelark.slice.folder_contract import CapacityObservation, FolderProfile, FolderTarget
from modelark.slice.folder_plan import NativeBinding, NativePlan, binding_for, estimate_metadata
from modelark.slice.transaction import TransferPlan, TransferRefusal
from test_slice_domain import facts, spec


def parts(**changes):
    target = FolderTarget(FolderProfile.NATIVE_EXT4, "filesystem:disk:uuid", ("exports",),
                          (42, 123456789), "delivery")
    values = dict(target=target, catalog="/catalog/catalog.sqlite", parent_path="/exports",
                  backing_ids=("serial:disk", "wwn:disk"), capacity=CapacityObservation(1_000_000, 100, 4096))
    values.update(changes)
    return values


def plan(**changes):
    values = parts()
    binding = binding_for(values["target"], values["catalog"], values["parent_path"], values["backing_ids"])
    proposal = d.preview(replace(spec(d), destination_id=binding.device_id,
                                 destination_root=values["target"].child_name), facts(d))
    kwargs = dict(proposal=proposal, destination=binding, catalog=values["catalog"],
                  parent_path=values["parent_path"], backing_ids=values["backing_ids"],
                  capacity=values["capacity"], metadata_reserve_bytes=256 * 1024)
    kwargs.update(changes)
    return NativePlan(**kwargs)


def test_native_roundtrip_and_stable_versioned_seal():
    original = plan()
    restored = NativePlan.from_json(original.to_json())
    assert restored == original
    assert restored.to_json() == original.to_json()
    assert restored.seal == original.seal
    assert len(restored.seal) == 64
    assert restored.is_folder is True
    assert restored.control_path == "delivery/.modelark-slice-owner"
    assert restored.version == "modelark.slice.native-transaction.v1"
    assert restored.destination.device_id == restored.destination.target.target_id
    assert restored.destination.filesystem_id == restored.destination.target.filesystem_scope
    assert restored.destination.mount_id == restored.destination.admission_id
    assert not hasattr(restored.destination, "available_bytes")


def test_changed_advisory_space_changes_review_not_binding():
    original = plan()
    changed = replace(original, capacity=CapacityObservation(0, 0, 8192))
    assert changed.destination == original.destination
    assert changed.seal != original.seal
    assert changed.required_bytes("/private/state") == (
        changed.proposal.total_bytes + changed.metadata_reserve_bytes + 8192)
    assert changed.required_bytes("/private/state") > changed.capacity.available_bytes


@pytest.mark.parametrize("field,value", [
    ("catalog", "/elsewhere/catalog.sqlite"), ("parent_path", "/elsewhere"),
    ("backing_ids", ("serial:other",)),
])
def test_admission_seals_stable_policy_not_only_folder(field, value):
    original = plan()
    with pytest.raises(TransferRefusal, match="ADMISSION_CORRUPT"):
        replace(original, **{field: value})
    values = parts(**{field: value})
    assert binding_for(values["target"], values["catalog"], values["parent_path"], values["backing_ids"]) != original.destination


def test_legacy_decoder_refuses_native_and_native_refuses_legacy():
    original = plan()
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        TransferPlan.from_json(original.to_json())
    record = json.loads(original.to_json())
    record["version"] = "modelark.slice.transaction.v3"
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        NativePlan.from_json(json.dumps(record))


@pytest.mark.parametrize("value", ["native-folder-v1:", "native-folder-v1:" + "A" * 64,
                                  "direct-v1:" + "a" * 64, None, 7])
def test_binding_rejects_unknown_or_malformed_policy_id(value):
    with pytest.raises(TransferRefusal, match="FOLDER_PLAN_INVALID"):
        NativeBinding(parts()["target"], value)


def test_fat_policy_cannot_enter_native_plan():
    target = replace(parts()["target"], profile=FolderProfile.FAT32_SESSION, parent_identity="a" * 32)
    with pytest.raises(TransferRefusal, match="FOLDER_PLAN_INVALID"):
        binding_for(target, "/catalog", "/exports", ("serial:disk",))
    with pytest.raises(TransferRefusal, match="FOLDER_PLAN_INVALID"):
        NativeBinding(target, "native-folder-v1:" + "a" * 64)


@pytest.mark.parametrize("value", ["relative", "", "/a/../b", "/a/./b", "/a//b", "//a/b",
                                  "/a/", "/a\0b", "/a\udcff", None])
@pytest.mark.parametrize("field", ["catalog", "parent_path"])
def test_admission_requires_canonical_absolute_paths(field, value):
    values = parts(**{field: value})
    with pytest.raises(TransferRefusal, match="FOLDER_PLAN_INVALID"):
        binding_for(values["target"], values["catalog"], values["parent_path"], values["backing_ids"])


@pytest.mark.parametrize("values", [(), [], None, "serial:disk", ("b", "a"), ("a", "a"),
                                    ("",), (" a",), ("a\0",), (1,)])
def test_backing_scope_is_explicit_sorted_unique(values):
    with pytest.raises(TransferRefusal, match="FOLDER_PLAN_INVALID"):
        binding_for(parts()["target"], "/catalog", "/exports", values)


def test_plan_closure_must_match_target_and_be_source_ready():
    original = plan()
    for change in ({"destination_id": "other"}, {"destination_root": "nested/delivery"}):
        proposal = d.preview(replace(original.proposal.spec, **change), facts(d))
        with pytest.raises(TransferRefusal, match="DESTINATION_CHANGED"):
            replace(original, proposal=proposal)
    proposal = d.preview(original.proposal.spec, replace(facts(d), copies=()))
    with pytest.raises(TransferRefusal, match="PREVIEW_BLOCKED"):
        replace(original, proposal=proposal)
    with pytest.raises(d.SliceRefusal, match="PREVIEW_TAMPERED"):
        replace(original, proposal=replace(original.proposal, seal="a" * 64))


@pytest.mark.parametrize("changes", [
    {"proposal": None}, {"destination": None}, {"capacity": {}},
    {"metadata_reserve_bytes": -1}, {"metadata_reserve_bytes": True},
    {"metadata_reserve_bytes": 1.0}, {"version": "anything"},
])
def test_typed_native_contract_required(changes):
    with pytest.raises(TransferRefusal, match="FOLDER_PLAN_INVALID"):
        plan(**changes)


def test_metadata_budget_counts_control_and_maximal_receipt():
    original = plan()
    minimum = original._metadata_minimum("")
    exact = replace(original, metadata_reserve_bytes=minimum)
    # Serialized integer length can change the minimum; these bounds are far apart.
    assert exact.required_bytes("") >= original.proposal.total_bytes + minimum
    with pytest.raises(TransferRefusal, match="DESTINATION_CAPACITY_UNPROVEN"):
        replace(original, metadata_reserve_bytes=1)
    with pytest.raises(TransferRefusal, match="DESTINATION_CAPACITY_UNPROVEN"):
        original.required_bytes("/" + "long-state-path" * 100_000)


def test_metadata_estimate_covers_real_serialized_evidence_without_capacity_guarantee():
    original = plan(capacity=CapacityObservation(0, None))
    reserve = estimate_metadata(original.proposal, original.destination, original.catalog,
                                original.parent_path, original.backing_ids, original.capacity, "/private/state")
    estimated = replace(original, metadata_reserve_bytes=reserve)
    assert estimated.required_bytes("/private/state") > estimated.capacity.available_bytes
    assert reserve > estimated._metadata_minimum("/private/state")


def test_receipt_distinguishes_folder_shared_space_and_verification_claims():
    original = plan()
    receipt = original.receipt("a" * 32, [])
    assert receipt["version"] == original.version
    assert receipt["topology"] == "folder"
    assert receipt["resume_policy"] == "authenticated"
    assert receipt["capacity_evidence"] == "advisory-shared-space-not-reserved"
    assert receipt["profile"] == "native-ext4-folder.v1"
    assert receipt["destination"] == asdict(original.destination)
    assert receipt["verification"] == {"content": "sha256-original-bytes", "layout": "authenticated",
                                       "result": "verified", "file_count": 0}
    assert "archive" not in receipt and "physical_verification" not in receipt


@pytest.mark.parametrize("location,key", [((), "version"), ((), "proposal"), (("destination",), "target"),
                                         (("destination", "target"), "profile"), (("capacity",), "free_inodes")])
def test_missing_fields_rejected(location, key):
    record = json.loads(plan().to_json())
    subrecord = record
    for component in location:
        subrecord = subrecord[component]
    del subrecord[key]
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        NativePlan.from_json(json.dumps(record))


@pytest.mark.parametrize("location", [(), ("destination",), ("destination", "target"), ("capacity",), ("proposal",)])
def test_extra_or_mixed_envelope_fields_rejected(location):
    record = json.loads(plan().to_json())
    subrecord = record
    for component in location:
        subrecord = subrecord[component]
    subrecord["legacy_extra"] = "not allowed"
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        NativePlan.from_json(json.dumps(record))


@pytest.mark.parametrize("location", [(), ("copy",), ("drive",), ("anchor",)])
def test_unknown_nested_source_fields_are_not_silently_discarded(location):
    record = json.loads(plan().to_json())
    source = record["proposal"]["closure"][0]["sources"][0]
    for component in location:
        source = source[component]
    source["unsealed_extra"] = "unknown policy"
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        NativePlan.from_json(json.dumps(record))


@pytest.mark.parametrize("payload", ["{}", "[]", "null", "no-json", '{"version":1,"version":2}',
                                    '{"version":NaN}', None])
def test_malformed_json_refuses(payload):
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        NativePlan.from_json(payload)
