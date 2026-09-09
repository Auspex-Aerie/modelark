"""Folder contracts cannot acquire the legacy USB writer's authority."""
import json

import pytest

from modelark.slice import operator, state, transaction as t
from modelark.slice.folder_contract import CapacityObservation, FolderProfile, FolderReview, FolderTarget
from test_slice_transaction import proposal


def review():
    return FolderReview(FolderTarget(FolderProfile.NATIVE_EXT4, "qualified-filesystem", ("exports",),
                                     (123, 456), "demo"), "a" * 64, CapacityObservation(1000000, 20))


def legacy(version="v3"):
    p, approval, _ = proposal()
    binding = t.DestinationBinding("test-device", "test-filesystem", "test-mount", 1000000)
    plan = t.TransferPlan(p, binding, "modelark.slice.transaction." + version,
                          65536 if version == "v3" else None)
    return plan, approval


@pytest.mark.parametrize("version,seal", [
    ("v1", "93d75ef078d1bab667979b70a9d08312512c6618522c8dc71c3febad92dafede"),
    ("v2", "9462a45754c8f32cbd5d1867e6079764e1c41701871f81e86a8dac9e2ac6481e"),
    ("v3", "45aca766c2bad8f0cfaa6f2da3fc85d5fc3ff77f383396a0a61a7a8c3de67bf0"),
])
def test_legacy_seals_match_reviewed_6fd87c3_baseline(version, seal):
    plan, _ = legacy(version)
    assert plan.seal == seal
    decoded = t.TransferPlan.from_json(plan.to_json())
    assert decoded.to_json() == plan.to_json()
    assert decoded.seal == seal
    assert decoded.receipt("fixture", []) == plan.receipt("fixture", [])


@pytest.mark.parametrize("addition", ["target", "capacity", "profile", "created_root", "execution_ready"])
def test_legacy_decoder_rejects_mixed_folder_fields(addition):
    plan, _ = legacy()
    payload = json.loads(plan.to_json())
    payload[addition] = None
    with pytest.raises(t.TransferRefusal, match="STATE_CORRUPT"):
        t.TransferPlan.from_json(json.dumps(payload))


@pytest.mark.parametrize("payload", ["[]", "null", "42", '"legacy"', '{}'])
def test_non_envelopes_fail_with_typed_refusal(payload):
    with pytest.raises(t.TransferRefusal, match="STATE_CORRUPT"):
        t.TransferPlan.from_json(payload)


def test_folder_record_cannot_be_created_in_legacy_store(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "HOST_STATE_DIR", tmp_path / "host-state")
    store = state.Store()
    with pytest.raises(t.TransferRefusal, match="LEGACY_PLAN"):
        store.create(review(), None)
    with store._connection(write=False) as con:
        assert con.execute("SELECT COUNT(*) FROM transactions").fetchone() == (0,)
        assert con.execute("SELECT COUNT(*) FROM owners").fetchone() == (0,)
        assert con.execute("PRAGMA user_version").fetchone() == (8,)


def test_injected_folder_envelope_never_reaches_destination_or_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "HOST_STATE_DIR", tmp_path / "host-state")
    store = state.Store()
    plan, approval = legacy()
    tx = store.create(plan, approval)
    record = review()
    with store._connection() as con:
        con.execute("UPDATE transactions SET plan=?,seal=?,state='approved' WHERE id=?",
                    (record.to_json(), record.seal, tx))

    def forbidden(*args, **kwargs):
        pytest.fail("folder record reached a legacy runtime boundary")

    monkeypatch.setattr(operator, "_store", lambda: store)
    monkeypatch.setattr(operator, "LinuxObserver", forbidden)
    monkeypatch.setattr(operator, "BoundTree", forbidden)
    monkeypatch.setattr(t, "_Lease", forbidden)
    with pytest.raises(t.TransferRefusal, match="STATE_CORRUPT"):
        operator.start(tx, "/must-not-open", {})
    with store._connection(write=False) as con:
        assert con.execute("SELECT COUNT(*) FROM owners").fetchone() == (0,)


def test_folder_review_is_not_legacy_admission():
    plan, _ = legacy()
    with pytest.raises(t.TransferRefusal, match="ADMISSION_CORRUPT"):
        state.Store._validate_admission(plan, json.loads(review().to_json()))
