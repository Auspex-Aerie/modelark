"""Pure FAT32 path/backing intent and reporting contracts; no device IO."""
from dataclasses import asdict, replace
import hashlib
import json

import pytest

from modelark.slice import domain as d, fat32_layout as layout
from modelark.slice.fat32_plan import Fat32Binding, Fat32Intent, Fat32Plan, binding_for, estimate_metadata
from modelark.slice.folder_contract import CapacityObservation, FolderProfile, FolderTarget
from modelark.slice.folder_plan import NativePlan
from modelark.slice.transaction import TransferPlan, TransferRefusal
from test_slice_domain import facts, spec
from test_slice_folder_plan import plan as native_plan


def intent(**changes):
    values = dict(profile=FolderProfile.FAT32_SESSION, filesystem_scope="fat32-fs:disk:uuid",
                  parent_parts=("exports",), child_name="delivery")
    values.update(changes)
    return Fat32Intent(**values)


def plan(**changes):
    binding = binding_for(intent(), "/catalog/catalog.sqlite", "/media/USB/exports", ("serial:disk",))
    proposal = d.preview(replace(spec(d), destination_id=binding.device_id,
                                 destination_root=binding.target.child_name), facts(d))
    values = dict(proposal=proposal, destination=binding, catalog="/catalog/catalog.sqlite",
                  parent_path="/media/USB/exports", backing_ids=("serial:disk",),
                  capacity=CapacityObservation(1_000_000, None, 4096), metadata_reserve_bytes=256 * 1024)
    values.update(changes)
    return Fat32Plan(**values)


def proposal_with_files(original, files):
    closure = tuple(replace(original.proposal.closure[0], rfilename=name, size_bytes=size)
                    for name, size in files)
    changed = replace(original.proposal, closure=closure)
    return replace(changed, seal=hashlib.sha256(changed.canonical_bytes()).hexdigest())


def test_intent_roundtrip_is_path_backing_only_not_saved_live_ownership():
    original = intent()
    restored = Fat32Intent.from_json(original.to_json())
    assert restored == original
    assert restored.target_id == original.target_id
    assert restored.parts == ("exports", "delivery")
    assert not hasattr(restored, "parent_identity")
    assert not hasattr(restored, "parent_token")
    assert set(asdict(restored)) == {"profile", "filesystem_scope", "parent_parts", "child_name"}
    assert json.loads(restored.to_json())["version"] == "modelark.slice.fat32-intent.v1"
    assert intent(parent_parts=()).parts == ("delivery",)


@pytest.mark.parametrize("changes", [{"filesystem_scope": "other-fs"}, {"parent_parts": ("other",)},
                                      {"child_name": "other"}, {"child_name": "DELIVERY"}])
def test_intent_seals_reviewed_spelling_and_scope(changes):
    assert intent(**changes).target_id != intent().target_id


def test_intent_copies_input_components():
    components = ["exports"]
    original = intent(parent_parts=components)
    components.append("changed")
    assert original.parent_parts == ("exports",)


@pytest.mark.parametrize("value", ["a~1", "CON", "trailing.", "a/b", "café", "", None, ".."])
def test_intended_fat_parent_and_child_components_use_candidate_grammar(value):
    for changes in ({"parent_parts": (value,)}, {"child_name": value}):
        with pytest.raises(TransferRefusal, match="DESTINATION_LAYOUT_UNSUPPORTED"):
            intent(**changes)


@pytest.mark.parametrize("changes", [
    {"profile": FolderProfile.NATIVE_EXT4}, {"profile": "unknown"}, {"profile": None},
    {"parent_parts": "exports"}, {"parent_parts": None}, {"filesystem_scope": ""},
    {"filesystem_scope": " scope"}, {"filesystem_scope": "scope\0"},
])
def test_intent_invalid_shapes_refuse(changes):
    with pytest.raises(TransferRefusal, match="FAT32_PLAN_INVALID"):
        intent(**changes)


@pytest.mark.parametrize("key,value", [("parent_identity", [1, 2]), ("parent_token", "a" * 32),
                                      ("inode", 1), ("unknown", True)])
def test_serialized_parent_identity_is_never_accepted_as_intent(key, value):
    record = json.loads(intent().to_json())
    record[key] = value
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        Fat32Intent.from_json(json.dumps(record))


def test_plan_roundtrip_has_its_own_tag_and_session_only_policy():
    original = plan()
    restored = Fat32Plan.from_json(original.to_json())
    assert restored == original
    assert restored.to_json() == original.to_json()
    assert restored.seal == original.seal
    assert restored.version == "modelark.slice.fat32-transaction.v1"
    assert restored.session_only and restored.is_folder
    assert restored.control_path == "delivery/.modelark-slice-owner"
    assert restored.destination.device_id == restored.destination.target.target_id
    assert restored.destination.filesystem_id == restored.destination.target.filesystem_scope
    assert restored.destination.mount_id == restored.destination.admission_id
    assert not hasattr(restored.destination, "available_bytes")


def test_observation_changes_do_not_change_intent_or_stable_admission():
    original = plan()
    changed = replace(original, capacity=CapacityObservation(0, 0, 8192))
    assert changed.destination == original.destination
    assert changed.seal != original.seal
    assert changed.required_bytes("/private/state") == (
        original.proposal.total_bytes + original.metadata_reserve_bytes + 8192)


@pytest.mark.parametrize("field,value", [("catalog", "/other/catalog"),
                                        ("parent_path", "/other/exports"),
                                        ("backing_ids", ("serial:other",))])
def test_admission_seals_all_stable_fields(field, value):
    original = plan()
    with pytest.raises(TransferRefusal, match="ADMISSION_CORRUPT"):
        replace(original, **{field: value})
    values = dict(target=original.destination.target, catalog=original.catalog,
                  parent_path=original.parent_path, backing_ids=original.backing_ids)
    values[field] = value
    assert binding_for(**values) != original.destination


def test_profiles_and_envelope_decoders_never_ducktype_each_other():
    original = plan()
    for decoder in (TransferPlan.from_json, NativePlan.from_json):
        with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
            decoder(original.to_json())
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        Fat32Plan.from_json(native_plan().to_json())
    legacy_fat = FolderTarget(FolderProfile.FAT32_SESSION, "scope", (), "a" * 32, "delivery")
    with pytest.raises(TransferRefusal, match="FAT32_PLAN_INVALID"):
        binding_for(legacy_fat, "/catalog", "/exports", ("serial:disk",))
    with pytest.raises(TransferRefusal, match="FAT32_PLAN_INVALID"):
        Fat32Binding(legacy_fat, "fat32-folder-v1:" + "a" * 64)


@pytest.mark.parametrize("identifier", [None, "", "fat32-folder-v1:", "fat32-folder-v1:" + "A" * 64,
                                       "native-folder-v1:" + "a" * 64])
def test_binding_requires_exact_fat_tag(identifier):
    with pytest.raises(TransferRefusal, match="FAT32_PLAN_INVALID"):
        Fat32Binding(intent(), identifier)


@pytest.mark.parametrize("changes", [{"proposal": None}, {"destination": None}, {"capacity": {}},
                                      {"metadata_reserve_bytes": True}, {"metadata_reserve_bytes": -1},
                                      {"metadata_reserve_bytes": 1.0}, {"version": "other"}])
def test_plan_typed_fields_and_version_required(changes):
    with pytest.raises(TransferRefusal, match="FAT32_PLAN_INVALID"):
        plan(**changes)


def test_closure_must_match_intent_and_be_verified_source_ready():
    original = plan()
    for changes in ({"destination_id": "other"}, {"destination_root": "other"}):
        proposal = d.preview(replace(original.proposal.spec, **changes), facts(d))
        with pytest.raises(TransferRefusal, match="DESTINATION_CHANGED"):
            replace(original, proposal=proposal)
    blocked = d.preview(original.proposal.spec, replace(facts(d), copies=()))
    with pytest.raises(TransferRefusal, match="PREVIEW_BLOCKED"):
        replace(original, proposal=blocked)
    with pytest.raises(d.SliceRefusal, match="PREVIEW_TAMPERED"):
        replace(original, proposal=replace(original.proposal, seal="b" * 64))


@pytest.mark.parametrize("files", [(("a.bin", 1), ("A.bin", 1)), (("a", 1), ("a/b", 1)),
                                   (("a~1.bin", 1),), (("CON.bin", 1),), (("café.bin", 1),),
                                   (("large.bin", 1 << 32),)])
def test_plan_admits_entire_fat_tree_not_only_binding(files):
    original = plan()
    with pytest.raises(TransferRefusal, match="DESTINATION_LAYOUT_UNSUPPORTED"):
        replace(original, proposal=proposal_with_files(original, files))


def test_exact_control_and_report_sizes_are_admitted(monkeypatch):
    original = plan()
    seen = []
    original_admit = layout.admit_layout
    def record(files, **options):
        seen.append(options)
        return original_admit(files, **options)
    monkeypatch.setattr(layout, "admit_layout", record)
    original.required_bytes("/private/state")
    control_size, receipt_size = original._metadata_sizes("/private/state")
    assert seen[-1]["control_size"] == control_size
    assert seen[-1]["receipt_size"] == receipt_size
    assert control_size > 0 and receipt_size > control_size
    # Reduce the qualified bound to test oversized report rejection without a huge allocation.
    monkeypatch.setattr(layout, "MAX_FILE_BYTES", receipt_size - 1)
    with pytest.raises(TransferRefusal, match="DESTINATION_LAYOUT_UNSUPPORTED"):
        original.required_bytes("/private/state")


def test_metadata_reserve_required_and_estimate_covers_actual_report():
    original = plan(capacity=CapacityObservation(0, None))
    with pytest.raises(TransferRefusal, match="DESTINATION_CAPACITY_UNPROVEN"):
        replace(original, metadata_reserve_bytes=1)
    reserve = estimate_metadata(original.proposal, original.destination, original.catalog, original.parent_path,
                                original.backing_ids, original.capacity, "/private/state")
    estimated = replace(original, metadata_reserve_bytes=reserve)
    assert reserve > sum(estimated._metadata_sizes("/private/state"))
    assert estimated.required_bytes("/private/state") > estimated.capacity.available_bytes


def test_report_does_not_certify_host_completion_its_own_flush_or_resume():
    original = plan()
    report = original.receipt("a" * 32, [])
    assert report["status"] == "export-verified"
    assert report["host_transaction_completion"] == "not-claimed"
    assert report["receipt_persistence"] == "not-self-certified"
    assert report["resume_policy"] == "new-root"
    assert report["verification"]["layout"] == "live-session-authenticated"
    assert report["verification"]["content"] == "sha256-original-bytes"
    assert report["destination"] == asdict(original.destination)
    assert "archive" not in report and "physical_verification" not in report


@pytest.mark.parametrize("location", [(), ("destination",), ("destination", "target"), ("capacity",),
                                     ("proposal",), ("proposal", "spec")])
def test_unknown_mixed_fields_rejected(location):
    record = json.loads(plan().to_json())
    current = record
    for part in location:
        current = current[part]
    current["parent_identity"] = "never-authority"
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        Fat32Plan.from_json(json.dumps(record))


@pytest.mark.parametrize("location,key", [((), "version"), (("destination",), "target"),
                                         (("destination", "target"), "profile"), (("capacity",), "free_inodes")])
def test_missing_fields_rejected(location, key):
    record = json.loads(plan().to_json())
    current = record
    for part in location:
        current = current[part]
    del current[key]
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        Fat32Plan.from_json(json.dumps(record))


@pytest.mark.parametrize("decoder", [Fat32Intent.from_json, Fat32Plan.from_json])
@pytest.mark.parametrize("payload", ["null", "[]", "{}", "bad-json", None,
                                    '{"version":1,"version":2}', '{"version":Infinity}'])
def test_malformed_or_duplicate_json_refuses(decoder, payload):
    with pytest.raises(TransferRefusal, match="STATE_CORRUPT"):
        decoder(payload)
