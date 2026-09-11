"""FAT assembly on synthetic evidence and disposable Linux directories, not kernel FAT qualification."""
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from modelark.slice import fat32_operator as fat, folder_operator as folder, operator, state, transaction as t
from modelark.slice.fat32_observation import Fat32FolderEvidence, Fat32Tree
from modelark.slice.folder_contract import CapacityObservation
from test_slice_transaction import DATA, Sources, proposal


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "HOST_STATE_DIR", tmp_path / "private")
    parent = tmp_path / "exports"
    parent.mkdir(mode=0o700)
    _, _, snapshot = proposal()
    fixture = SimpleNamespace(parent=parent, catalog=tmp_path / "catalog.sqlite", snapshot=snapshot,
                              calls=[], backing=("filesystem:fixture-uuid", "serial:fixture-disk"),
                              available=None, source_status={})

    class Observer:
        def observe(self, destination, *, archives, protected_paths=()):
            destination = Path(destination)
            fixture.calls.append((destination, protected_paths))
            if destination.exists():
                raise t.TransferRefusal("OUTPUT_COLLISION")
            with Fat32Tree(destination.parent) as tree:
                capacity = os.fstatvfs(tree.fd)
                available = (capacity.f_bavail * capacity.f_frsize
                             if fixture.available is None else fixture.available)
                return Fat32FolderEvidence(str(destination.parent), str(destination), "fixture-uuid",
                                           "fixture-scope:" + str(tmp_path), (), destination.name,
                                           fixture.backing, tree.mount_id, "fixture-major-minor",
                                           CapacityObservation(available, None), 255, capacity.f_frsize)

        def recheck(self, tree, evidence, **kwargs):
            tree.check()
            available = (evidence.capacity.available_bytes
                         if fixture.available is None else fixture.available)
            return replace(evidence, capacity=CapacityObservation(available, None))

    def sources(*args):
        result = Sources(fixture.snapshot, t)
        result.status.update(fixture.source_status)
        return result

    monkeypatch.setattr(folder, "_filesystem_type", lambda parent: "vfat")
    monkeypatch.setattr(fat, "Fat32FolderObserver", Observer)
    monkeypatch.setattr(fat, "read_catalog", lambda *args: fixture.snapshot)
    monkeypatch.setattr(folder, "read_catalog", lambda *args: fixture.snapshot)
    monkeypatch.setattr(operator, "read_catalog", lambda *args: fixture.snapshot)
    monkeypatch.setattr(operator, "_archives", lambda *args: ())
    monkeypatch.setattr(fat, "FencedSources", sources)
    monkeypatch.setattr(folder, "NativeFolderObserver", lambda: pytest.fail("FAT plan entered native observer"))
    monkeypatch.setattr(operator, "LinuxObserver", lambda: pytest.fail("FAT plan entered legacy USB observer"))
    return fixture


def preview(setup, child="delivery"):
    return operator.preview(setup.catalog, setup.parent / child, ("org/model",), None)


def approved(setup, child="delivery"):
    result = preview(setup, child)
    operator.approve(result["transaction_id"], result["seal"])
    return result


def test_preview_and_approve_only_write_private_state(setup):
    result = approved(setup)
    assert list(setup.parent.iterdir()) == []
    assert result["ownership"] == "new-output-folder-only"
    assert result["parent_continuity"] == "fresh-at-start-not-persistently-identified"
    assert result["resume_policy"] == "new-root-after-any-ended-attempt"
    assert result["capacity_policy"] == "advisory-shared-space-not-reserved"
    assert result["executable"] is False
    assert setup.calls
    assert not state.Store().attempt_consumed(result["transaction_id"])


@pytest.mark.parametrize("point", ["open", "recheck", "close"])
def test_preview_io_failure_is_structured_without_output(setup, monkeypatch, point):
    original = fat.Fat32Tree

    class FailingTree(original):
        def __init__(self, *args, **kwargs):
            if point == "open":
                raise OSError("synthetic preview reopen failure")
            super().__init__(*args, **kwargs)

        def __exit__(self, *args):
            super().__exit__(*args)
            if point == "close":
                raise OSError("synthetic preview close failure")

    monkeypatch.setattr(fat, "Fat32Tree", FailingTree)
    if point == "recheck":
        def fail(*args, **kwargs):
            raise OSError("synthetic preview recheck failure")
        monkeypatch.setattr(fat.Fat32FolderObserver, "recheck", fail)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_UNPROVEN"):
        preview(setup)
    assert not list(setup.parent.iterdir())


def test_complete_export_receipt_and_control_stay_inside_child(setup, monkeypatch):
    result = approved(setup)
    (setup.parent / "unrelated").write_bytes(b"untouched")
    done = operator.start(result["transaction_id"], setup.parent / "delivery", {})
    assert done["state"] == "complete" and done["ok"] and not done["can_write"]
    assert done["new_root_required"] is False
    child = setup.parent / "delivery"
    assert (child / "org/model/model.safetensors").read_bytes() == DATA
    assert (child / ".modelark-slice-owner").exists()
    assert not (setup.parent / ".modelark-slice-owner").exists()
    receipt = json.loads((child / ".modelark-slice-receipt.json").read_text())
    assert receipt["status"] == "export-verified"
    assert receipt["host_transaction_completion"] == "not-claimed"
    assert (setup.parent / "unrelated").read_bytes() == b"untouched"
    monkeypatch.setattr(fat, "Fat32FolderObserver", lambda: pytest.fail("completed Start observed destination"))
    assert operator.start(result["transaction_id"], "/absent", {})["state"] == "complete"


@pytest.mark.parametrize("code,expected", [("SOURCE_BLOCKED", "blocked_source"),
                                           ("WAITING_SOURCE", "waiting_source"),
                                           ("stop-race", "stopped")])
def test_fat_preflight_returns_source_status_without_spending_attempt(setup, monkeypatch, code, expected):
    result = approved(setup)
    tx = result["transaction_id"]
    def refuse(self, proposal, check):
        check()
        if code == "stop-race":
            state.Store().request_stop(tx)
        raise t.TransferRefusal("SOURCE_BLOCKED" if code == "stop-race" else code,
                                "preflight source unavailable")
    monkeypatch.setattr(Sources, "preflight", refuse, raising=False)
    outcome = operator.start(tx, setup.parent / "delivery", {})
    assert outcome["state"] == expected and not outcome["can_write"]
    assert outcome["new_root_required"] is False
    assert state.Store().events(tx) == [] and not (setup.parent / "delivery").exists()
    assert not state.Store().attempt_consumed(tx)
    if code == "stop-race":
        assert state.Store().stop_requested(tx) and outcome["reason"] == "STOPPED"
    else:
        assert not outcome["ok"] and code in outcome["reason"]
    monkeypatch.setattr(Sources, "preflight", lambda self, proposal, check: check())
    assert operator.start(tx, setup.parent / "delivery", {})["state"] == "complete"


def test_unapproved_plan_refuses_before_observation(setup, monkeypatch):
    result = preview(setup)
    monkeypatch.setattr(fat, "Fat32FolderObserver", lambda: pytest.fail("unapproved Start observed destination"))
    with pytest.raises(t.TransferRefusal, match="APPROVAL_MISSING"):
        operator.start(result["transaction_id"], setup.parent / "delivery", {})
    assert not list(setup.parent.iterdir())


def test_incorrect_seal_cannot_approve_or_create_output(setup):
    result = preview(setup)
    with pytest.raises(t.TransferRefusal, match="PREVIEW_STALE"):
        operator.approve(result["transaction_id"], "0" * 64)
    assert state.Store().status(result["transaction_id"]).state == "ready"
    assert not list(setup.parent.iterdir())


@pytest.mark.parametrize("substitution", ["path", "backing"])
def test_start_refuses_unapproved_path_or_backing_without_consumption(setup, substitution):
    result = approved(setup)
    destination = setup.parent / "delivery"
    if substitution == "path":
        destination = setup.parent / "other"
    else:
        setup.backing = ("filesystem:fixture-uuid", "serial:different-disk")
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CHANGED"):
        operator.start(result["transaction_id"], destination, {})
    assert not state.Store().attempt_consumed(result["transaction_id"])
    assert not list(setup.parent.iterdir())


def test_parent_replacement_between_preview_and_start_is_allowed_with_same_path_backing(setup):
    result = approved(setup)
    old_identity = setup.parent.stat().st_ino
    setup.parent.rename(setup.parent.with_name("old-exports"))
    setup.parent.mkdir(mode=0o700)
    assert setup.parent.stat().st_ino != old_identity
    assert operator.start(result["transaction_id"], setup.parent / "delivery", {})["state"] == "complete"
    assert not list(setup.parent.with_name("old-exports").iterdir())


def test_new_root_collision_after_approval_is_preserved_without_attempt(setup):
    result = approved(setup)
    child = setup.parent / "delivery"
    child.mkdir(mode=0o700)
    (child / "foreign").write_bytes(b"keep")
    with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
        operator.start(result["transaction_id"], child, {})
    assert (child / "foreign").read_bytes() == b"keep"
    assert not state.Store().attempt_consumed(result["transaction_id"])


def test_stop_requires_fresh_transaction_and_different_folder(setup, monkeypatch):
    result = approved(setup)
    original_run = t.Session.run
    def stopped(session):
        session.store.request_stop(session.transaction_id)
        return original_run(session)
    with monkeypatch.context() as patch:
        patch.setattr(t.Session, "run", stopped)
        outcome = operator.start(result["transaction_id"], setup.parent / "delivery", {})
    assert outcome["state"] == "stopped" and outcome["new_root_required"]
    assert outcome["ok"]  # The graceful Stop request was acknowledged successfully.
    with pytest.raises(t.TransferRefusal, match="FAT32_NEW_ROOT_REQUIRED"):
        operator.start(result["transaction_id"], setup.parent / "delivery", {})
    new = approved(setup, "fresh-delivery")
    assert operator.start(new["transaction_id"], setup.parent / "fresh-delivery", {})["state"] == "complete"
    assert (setup.parent / "delivery/.modelark-slice-owner").exists()


def test_keyboard_interrupt_reports_non_success_and_requires_new_root(setup, monkeypatch):
    result = approved(setup)
    def interrupted(session):
        raise KeyboardInterrupt
    monkeypatch.setattr(t.Session, "run", interrupted)
    outcome = operator.start(result["transaction_id"], setup.parent / "delivery", {})
    assert not outcome["ok"] and not outcome["can_write"]
    assert outcome["stop_requested"] and outcome["new_root_required"]
    with pytest.raises(t.TransferRefusal, match="FAT32_NEW_ROOT_REQUIRED"):
        operator.start(result["transaction_id"], setup.parent / "delivery", {})


@pytest.mark.parametrize("point", ["directory_intent", "chunk_written"])
@pytest.mark.parametrize("error", [KeyboardInterrupt, RuntimeError])
def test_abort_inside_engine_records_ended_state_before_lease_drop(setup, monkeypatch, point, error):
    result = approved(setup)
    original_start = t.start
    def abort(current):
        if current == point:
            raise error("synthetic engine abort")
    monkeypatch.setattr(t, "start", lambda *args, **kwargs: original_start(*args, **kwargs, fault=abort))
    if error is KeyboardInterrupt:
        outcome = operator.start(result["transaction_id"], setup.parent / "delivery", {})
        assert not outcome["ok"] and outcome["new_root_required"] and outcome["stop_requested"]
    else:
        with pytest.raises(RuntimeError, match="synthetic engine abort"):
            operator.start(result["transaction_id"], setup.parent / "delivery", {})
    store = state.Store()
    assert store.status(result["transaction_id"]).state == "stopped"
    assert operator.status(result["transaction_id"])["state"] == "stopped"
    if error is KeyboardInterrupt:
        with store._connection(write=False) as con:
            serial, acknowledged = con.execute(
                "SELECT stop_serial,acknowledged_stop_serial FROM transactions WHERE id=?",
                (result["transaction_id"],)).fetchone()
        assert serial == acknowledged and serial > 0
    with pytest.raises(t.TransferRefusal, match="FAT32_NEW_ROOT_REQUIRED"):
        operator.start(result["transaction_id"], setup.parent / "delivery", {})


def test_consumed_intent_refuses_before_destination_observation(setup, monkeypatch):
    result = approved(setup)
    setup.source_status["drive-a"] = "WAITING_SOURCE"
    outcome = operator.start(result["transaction_id"], setup.parent / "delivery", {})
    assert outcome["state"] == "waiting_source" and outcome["new_root_required"]
    assert not outcome["ok"]
    monkeypatch.setattr(fat, "Fat32FolderObserver", lambda: pytest.fail("consumed intent observed destination"))
    with pytest.raises(t.TransferRefusal, match="FAT32_NEW_ROOT_REQUIRED"):
        operator.start(result["transaction_id"], setup.parent / "delivery", {})


def test_insufficient_fresh_capacity_before_claim_does_not_consume_attempt(setup):
    result = approved(setup)
    setup.available = 0
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CAPACITY_WAIT"):
        operator.start(result["transaction_id"], setup.parent / "delivery", {})
    assert not state.Store().attempt_consumed(result["transaction_id"])
    assert not (setup.parent / "delivery").exists()
    setup.available = None
    assert operator.start(result["transaction_id"], setup.parent / "delivery", {})["state"] == "complete"


def test_capacity_wait_after_claim_consumes_attempt_and_returns_new_root_policy(setup, monkeypatch):
    result = approved(setup)
    def wait(*args):
        raise t.TransferRefusal("DESTINATION_CAPACITY_WAIT")
    monkeypatch.setattr(fat.Fat32Destination, "check", wait)
    outcome = operator.start(result["transaction_id"], setup.parent / "delivery", {})
    assert outcome["state"] == "waiting_destination" and outcome["new_root_required"]
    assert not outcome["ok"] and not outcome["can_write"]
    assert state.Store().attempt_consumed(result["transaction_id"])
    assert not (setup.parent / "delivery").exists()


def test_source_gap_returns_before_filesystem_hint_or_destination_observation(setup, monkeypatch):
    setup.snapshot = replace(setup.snapshot, copies=())
    monkeypatch.setattr(folder, "_filesystem_type", lambda *args: pytest.fail("source gap probed filesystem"))
    monkeypatch.setattr(fat, "Fat32FolderObserver", lambda: pytest.fail("source gap observed destination"))
    outcome = preview(setup)
    assert outcome["state"] == "blocked" and not outcome["ok"] and outcome["gaps"]
    assert not list(setup.parent.iterdir())
