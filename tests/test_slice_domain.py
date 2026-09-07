"""Slice 1 contract: archive evidence -> immutable domain preview -> explicit approval.

These contracts intentionally precede implementation. Hardware preflight and execution are later
slices; a domain approval must never claim that a destination is ready for writes.
"""
from dataclasses import FrozenInstanceError, replace
import importlib
import random
from unittest import mock

import pytest


@pytest.fixture
def domain():
    try:
        return importlib.import_module("modelark.slice.domain")
    except ModuleNotFoundError as exc:
        if exc.name not in {"modelark.slice", "modelark.slice.domain"}:
            raise
        pytest.fail("Slice 1 domain preview/approval behavior is not implemented yet")


SHA = "a" * 64
OTHER = "b" * 64


def facts(d):
    from modelark.capacity_evidence import identity_fingerprint_v1

    fingerprint = identity_fingerprint_v1(
        fs_uuid="fs-a", annex_uuid="annex-a", serial="serial-a", filesystem_capacity_bytes=1000)
    drive = d.DriveFact("drive-a", "fs-a", "annex-a", "serial-a", 1, 2,
                        fingerprint, 1000, "dedicated_local", "active", "enabled")
    anchor = d.AnchorFact("drive-a", 1, 2, fingerprint, 1000, "dedicated_local", "anchor-2")
    file = d.FileFact("org/model", "model.safetensors", 12, SHA, "safetensors", "bf16")
    copy = d.CopyFact("org/model", file.rfilename, "drive-a", file.rfilename,
                      12, 12, SHA, "ingestion_computed", f"SHA256E-s12--{SHA}", False)
    return d.CatalogSnapshot("fixture", (file,), (copy,), (drive,), (anchor,))


def spec(d, **kwargs):
    return d.SliceSpec(repo_ids=("org/model",), destination_id="usb-destination",
                       destination_root="models", **kwargs)


def test_offline_archive_is_source_ready_without_io(domain):
    snapshot = facts(domain)
    with mock.patch("pathlib.Path.resolve", side_effect=AssertionError("no source resolution")), \
         mock.patch("subprocess.run", side_effect=AssertionError("no annex or mount")), \
         mock.patch("socket.socket", side_effect=AssertionError("no acquisition")):
        p = domain.preview(spec(domain), snapshot)
        assert p.source_ready and not p.execution_ready
        assert p.total_bytes == 12 and p.required_drives == ("drive-a",)
        assert p.closure[0].sha256 == SHA
        assert p.closure[0].sources[0].copy.annex_key == f"SHA256E-s12--{SHA}"
        assert p.closure[0].sources[0].anchor.anchor_id == "anchor-2"


@pytest.mark.parametrize("lifecycle,eligibility,ready", [
    ("active", "enabled", True), ("active", "excluded", True),
    ("lost", "enabled", False), ("retired", "excluded", False),
])
def test_lifecycle_and_placement_eligibility_are_orthogonal(domain, lifecycle, eligibility, ready):
    s = facts(domain)
    s = replace(s, drives=(replace(s.drives[0], lifecycle=lifecycle, eligibility=eligibility),))
    p = domain.preview(spec(domain), s)
    assert p.source_ready is ready
    if not ready:
        assert p.gaps[0].code == "SOURCE_INACTIVE"
        assert p.gaps[0].drive_label == "drive-a"


@pytest.mark.parametrize("change", ["missing", "old_generation", "old_epoch", "fingerprint", "authority"])
def test_clean_anchor_must_bind_current_drive(domain, change):
    s = facts(domain)
    anchors = s.anchors
    if change == "missing":
        anchors = ()
    else:
        field, value = {
            "old_generation": ("generation", 1), "old_epoch": ("identity_epoch", 2),
            "fingerprint": ("identity_fingerprint", OTHER),
            "authority": ("write_authority", "unknown"),
        }[change]
        anchors = (replace(anchors[0], **{field: value}),)
    p = domain.preview(spec(domain), replace(s, anchors=anchors))
    assert not p.source_ready
    assert p.gaps[0].code == "SOURCE_RECONCILIATION_REQUIRED"
    assert (p.gaps[0].repo_id, p.gaps[0].rfilename) == ("org/model", "model.safetensors")


def test_catalog_only_and_partial_archives_report_exact_files(domain):
    s = facts(domain)
    config = domain.FileFact("org/model", "nested/config.json", 4, OTHER, "aux", None)
    p = domain.preview(spec(domain), replace(s, files=(*s.files, config)))
    assert not p.source_ready
    assert [(g.rfilename, g.code) for g in p.gaps] == [(config.rfilename, "ARCHIVE_MISSING")]
    assert p.closure[0].rfilename == "model.safetensors"
    p = domain.preview(spec(domain), replace(s, copies=()))
    assert p.gaps[0].code == "ARCHIVE_MISSING"


@pytest.mark.parametrize("field,value,code", [
    ("orig_bytes", 13, "SOURCE_SIZE_MISMATCH"),
    ("orig_sha256", OTHER, "SOURCE_DIGEST_CONFLICT"),
    ("present", False, "SOURCE_COPY_ABSENT"),
    ("stored_relpath", "../escape", "SOURCE_PATH_UNSAFE"),
])
def test_unproven_copy_cannot_satisfy_archive_evidence(domain, field, value, code):
    s = facts(domain)
    p = domain.preview(spec(domain), replace(s, copies=(replace(s.copies[0], **{field: value}),)))
    assert not p.source_ready and p.gaps[0].code == code


def test_legacy_digest_without_independent_proof_is_blocked(domain):
    s = facts(domain)
    copy = replace(s.copies[0], provenance="legacy_unknown", compressed=True,
                   annex_key=f"SHA256E-s8--{OTHER}", stored_bytes=8)
    p = domain.preview(spec(domain), replace(s, copies=(copy,)))
    assert p.gaps[0].code == "SOURCE_PROVENANCE_UNPROVEN"


def test_raw_annex_digest_is_independent_proof_not_a_catalog_mutation(domain):
    s = facts(domain)
    copy = replace(s.copies[0], provenance=None, orig_sha256=None)
    p = domain.preview(spec(domain), replace(s, copies=(copy,)))
    assert p.source_ready
    assert p.closure[0].sources[0].digest_provenance == "annex_key"
    assert copy.orig_sha256 is None and copy.provenance is None


def test_compressed_annex_hash_is_not_original_byte_evidence(domain):
    s = facts(domain)
    copy = replace(s.copies[0], orig_sha256=None, provenance=None, compressed=True)
    p = domain.preview(spec(domain), replace(s, copies=(copy,)))
    assert not p.source_ready


def alternatives(d):
    from modelark.capacity_evidence import identity_fingerprint_v1

    s = facts(d)
    fingerprint = identity_fingerprint_v1(
        fs_uuid="fs-b", annex_uuid="annex-b", serial="serial-b", filesystem_capacity_bytes=1000)
    drive = replace(s.drives[0], drive_label="drive-b", fs_uuid="fs-b", annex_uuid="annex-b",
                    serial="serial-b", identity_fingerprint=fingerprint)
    anchor = replace(s.anchors[0], drive_label="drive-b", identity_fingerprint=fingerprint,
                     anchor_id="anchor-b")
    return replace(s, drives=(*s.drives, drive), anchors=(*s.anchors, anchor),
                   copies=(*s.copies, replace(s.copies[0], drive_label="drive-b")))


def test_valid_alternatives_survive_bad_preferred_source(domain):
    s = alternatives(domain)
    p = domain.preview(spec(domain), replace(s, drives=(replace(s.drives[0], lifecycle="lost"), s.drives[1])))
    assert p.source_ready and p.required_drives == ("drive-b",)


def test_missing_catalog_hash_requires_unambiguous_archived_content(domain):
    s = alternatives(domain)
    s = replace(s, files=(replace(s.files[0], sha256=None),),
                copies=(s.copies[0], replace(s.copies[1], orig_sha256=OTHER,
                                           annex_key=f"SHA256E-s12--{OTHER}")))
    p = domain.preview(spec(domain), s)
    assert not p.source_ready and p.gaps[0].code == "SOURCE_DIGEST_AMBIGUOUS"


def test_preview_seal_is_order_independent_and_facts_are_immutable(domain):
    s = alternatives(domain)
    p = domain.preview(spec(domain), s)
    for seed in range(10):
        rng = random.Random(seed)
        shuffled = {name: tuple(rng.sample(getattr(s, name), len(getattr(s, name))))
                    for name in ("files", "copies", "drives", "anchors")}
        assert domain.preview(spec(domain), replace(s, **shuffled)) == p
    assert tuple(src.drive.drive_label for src in p.closure[0].sources) == ("drive-a", "drive-b")
    with pytest.raises(FrozenInstanceError):
        p.closure[0].sources[0].drive.lifecycle = "lost"
    file_list = list(s.files)
    copied = replace(s, files=file_list)
    file_list.clear()
    assert copied.files == s.files


@pytest.mark.parametrize("path", ["/absolute", "../escape", "a/../b", "a\\b", "a//b", "a/./b", "x\0y"])
def test_unsafe_artifact_paths_block_domain_preview(domain, path):
    s = facts(domain)
    p = domain.preview(spec(domain), replace(s, files=(replace(s.files[0], rfilename=path),)))
    assert not p.source_ready and p.gaps[0].code == "ARTIFACT_PATH_UNSAFE"


def test_file_directory_collisions_are_rejected(domain):
    s = facts(domain)
    nested = replace(s.files[0], rfilename="model.safetensors/config.json")
    p = domain.preview(spec(domain), replace(s, files=(*s.files, nested)))
    assert not p.source_ready and "OUTPUT_COLLISION" in {g.code for g in p.gaps}


def test_missing_sizes_and_repositories_are_not_silently_omitted(domain):
    s = facts(domain)
    p = domain.preview(spec(domain), replace(s, files=(replace(s.files[0], size_bytes=None),)))
    assert p.gaps[0].code == "ARTIFACT_SIZE_UNKNOWN"
    p = domain.preview(replace(spec(domain), repo_ids=("missing/model",)), s)
    assert not p.source_ready and p.gaps[0].repo_id == "missing/model"


def test_explicit_approval_binds_reviewed_seal_and_never_starts_execution(domain):
    s = facts(domain)
    p = domain.preview(spec(domain), s)
    a = domain.approve(p, expected_seal=p.seal, current_snapshot=s)
    assert a.preview_seal == p.seal and a.stage == "domain"
    assert domain.validate_approval(p, a) is None
    assert not p.execution_ready
    with pytest.raises(domain.SliceRefusal, match="PREVIEW_STALE"):
        domain.approve(p, expected_seal=OTHER, current_snapshot=s)
    with pytest.raises(domain.SliceRefusal, match="PREVIEW_STALE"):
        domain.approve(p, expected_seal=p.seal, current_snapshot=replace(s, copies=()))


def test_tampered_preview_and_approval_are_refused(domain):
    s = facts(domain)
    p = domain.preview(spec(domain), s)
    a = domain.approve(p, expected_seal=p.seal, current_snapshot=s)
    tampered = replace(p, spec=replace(p.spec, destination_id="other-usb"))
    with pytest.raises(domain.SliceRefusal, match="PREVIEW_TAMPERED"):
        domain.approve(tampered, expected_seal=p.seal, current_snapshot=s)
    with pytest.raises(domain.SliceRefusal, match="APPROVAL_MISMATCH"):
        domain.validate_approval(p, replace(a, preview_seal=OTHER))
    with pytest.raises(domain.SliceRefusal, match="PREVIEW_BLOCKED"):
        blocked = domain.preview(spec(domain), replace(s, copies=()))
        domain.approve(blocked, expected_seal=blocked.seal, current_snapshot=replace(s, copies=()))


def test_profile_and_destination_intent_are_part_of_seal(domain):
    s = facts(domain)
    p = domain.preview(spec(domain), s)
    assert domain.preview(replace(p.spec, destination_root="different"), s).seal != p.seal
    with pytest.raises(domain.SliceRefusal, match="UNSUPPORTED_PROFILE"):
        domain.preview(spec(domain, consumer_profile="unknown"), s)
    with pytest.raises(domain.SliceRefusal, match="INVALID_SPEC"):
        domain.preview(replace(p.spec, destination_root="../outside"), s)


def test_duplicate_conflicting_evidence_is_not_order_dependent(domain):
    s = facts(domain)
    with pytest.raises(domain.SliceRefusal, match="INVALID_SNAPSHOT"):
        domain.preview(spec(domain), replace(s, drives=(*s.drives, replace(s.drives[0], lifecycle="lost"))))


def test_snapshot_rejects_mutable_scalar_fields(domain):
    s = facts(domain)
    with pytest.raises(domain.SliceRefusal, match="INVALID_SNAPSHOT"):
        replace(s, files=(replace(s.files[0], quant=[]),))
    with pytest.raises(domain.SliceRefusal, match="INVALID_SPEC"):
        replace(spec(domain), repo_ids=iter(("org/model",)))


def test_zero_bytes_are_known_and_malformed_hashes_are_not_proof(domain):
    s = facts(domain)
    zero = replace(s, files=(replace(s.files[0], size_bytes=0),),
                   copies=(replace(s.copies[0], orig_bytes=0, stored_bytes=0,
                                   annex_key=f"SHA256E-s0--{SHA}"),))
    assert domain.preview(spec(domain), zero).source_ready
    bad = replace(s, files=(replace(s.files[0], sha256="not-a-digest"),))
    assert domain.preview(spec(domain), bad).gaps[0].code == "ARTIFACT_DIGEST_INVALID"


def test_catalog_identity_and_source_evidence_are_bound_to_approval(domain):
    s = facts(domain)
    p = domain.preview(spec(domain), s)
    for changed in (replace(s, catalog_id="other-catalog"),
                    replace(s, copies=(replace(s.copies[0], stored_relpath="different"),)),
                    replace(s, anchors=())):
        with pytest.raises(domain.SliceRefusal, match="PREVIEW_STALE"):
            domain.approve(p, expected_seal=p.seal, current_snapshot=changed)


def test_approval_validation_does_not_replan_for_later_source_changes(domain):
    s = facts(domain)
    p = domain.preview(spec(domain), s)
    a = domain.approve(p, expected_seal=p.seal, current_snapshot=s)
    changed = replace(s, drives=(replace(s.drives[0], lifecycle="lost"),))
    assert not domain.preview(spec(domain), changed).source_ready
    assert domain.validate_approval(p, a) is None  # Later execution must apply source-use gates.


def test_unrelated_fleet_activity_does_not_stale_slice_approval(domain):
    s = alternatives(domain)
    # drive-b has no copy of this slice. Its ongoing Fill generation is not slice evidence.
    s = replace(s, copies=(s.copies[0],))
    p = domain.preview(spec(domain), s)
    changed = replace(s, drives=(s.drives[0], replace(s.drives[1], write_generation=99)),
                      anchors=(s.anchors[0],))
    assert domain.preview(spec(domain), changed).seal == p.seal
    assert domain.approve(p, expected_seal=p.seal, current_snapshot=changed).preview_seal == p.seal


def test_placement_exclusion_alone_does_not_stale_read_approval(domain):
    s = facts(domain)
    p = domain.preview(spec(domain), s)
    changed = replace(s, drives=(replace(s.drives[0], eligibility="excluded"),))
    assert domain.preview(spec(domain), changed).seal == p.seal
    assert domain.approve(p, expected_seal=p.seal, current_snapshot=changed).preview_seal == p.seal


def test_real_candidate_evidence_change_still_stales_approval(domain):
    s = alternatives(domain)
    p = domain.preview(spec(domain), s)
    changed = replace(s, drives=(s.drives[0], replace(s.drives[1], write_generation=99)))
    with pytest.raises(domain.SliceRefusal, match="PREVIEW_STALE"):
        domain.approve(p, expected_seal=p.seal, current_snapshot=changed)


@pytest.mark.parametrize("fs_uuid,annex_uuid", [("fs-a", None), (None, "annex-a")])
def test_either_proven_uuid_can_bind_a_clean_source(domain, fs_uuid, annex_uuid):
    from modelark.capacity_evidence import identity_fingerprint_v1

    s = facts(domain)
    fingerprint = identity_fingerprint_v1(fs_uuid=fs_uuid, annex_uuid=annex_uuid,
                                          serial="serial-a", filesystem_capacity_bytes=1000)
    s = replace(s, drives=(replace(s.drives[0], fs_uuid=fs_uuid, annex_uuid=annex_uuid,
                                   identity_fingerprint=fingerprint),),
                anchors=(replace(s.anchors[0], identity_fingerprint=fingerprint),))
    assert domain.preview(spec(domain), s).source_ready


def test_serial_alone_never_proves_source_identity(domain):
    s = facts(domain)
    s = replace(s, drives=(replace(s.drives[0], fs_uuid=None, annex_uuid=None),))
    assert domain.preview(spec(domain), s).gaps[0].code == "SOURCE_IDENTITY_UNPROVEN"


# 2026-09-07 review correction: absence of an annex key alone is not missing identity.
# A confined stored path on a proven drive may carry independent durable digest provenance.
@pytest.mark.parametrize("key", [None, "WORM-s12-m1--model.safetensors", "URL--recorded-object"])
@pytest.mark.parametrize("provenance", ["ingestion_computed", "hub_confirmed", "annex_key", "archive-head-blob"])
def test_durable_original_proof_does_not_require_sha256_annex_backend(domain, key, provenance):
    s = facts(domain)
    s = replace(s, copies=(replace(s.copies[0], annex_key=key, provenance=provenance),))
    p = domain.preview(spec(domain), s)
    assert p.source_ready and p.closure[0].sha256 == SHA
    assert p.closure[0].sources[0].digest_provenance == provenance


def test_keyless_copy_without_durable_original_proof_is_still_blocked(domain):
    s = facts(domain)
    s = replace(s, copies=(replace(s.copies[0], annex_key=None, provenance="legacy_unknown"),))
    assert domain.preview(spec(domain), s).gaps[0].code == "SOURCE_PROVENANCE_UNPROVEN"


def test_compressed_hub_provenance_survives_missing_catalog_digest(domain):
    s = facts(domain)
    s = replace(s, files=(replace(s.files[0], sha256=None),),
                copies=(replace(s.copies[0], provenance="hub_confirmed", compressed=True,
                                stored_bytes=8, annex_key=f"SHA256E-s8--{OTHER}"),))
    p = domain.preview(spec(domain), s)
    assert p.source_ready and p.closure[0].sha256 == SHA
    assert p.closure[0].sources[0].digest_provenance == "hub_confirmed"
