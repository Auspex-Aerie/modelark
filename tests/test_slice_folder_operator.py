"""Native assembly using synthetic source evidence and disposable real filesystem IO."""
from dataclasses import replace
from pathlib import Path
import os

import pytest

from modelark.slice import folder_operator as f, operator, state, transaction as t
from modelark.slice.folder_contract import CapacityObservation, FolderProfile, FolderTarget
from modelark.slice.folder_observation import NativeFolderEvidence
from modelark.slice.linux import BoundTree
from test_slice_transaction import proposal, Sources, DATA


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "HOST_STATE_DIR", tmp_path / "private")
    parent = tmp_path / "exports"
    parent.mkdir(mode=0o700)
    catalog = tmp_path / "catalog.sqlite"
    _, _, snapshot = proposal()
    calls = []

    class Observer:
        def observe(self, destination, *, archives, protected_paths=(), allow_existing=False):
            destination = Path(destination)
            calls.append((destination, allow_existing, protected_paths))
            if destination.exists() and not allow_existing:
                raise t.TransferRefusal("OUTPUT_COLLISION")
            with BoundTree(destination.parent) as tree:
                target = FolderTarget(FolderProfile.NATIVE_EXT4, "fixture-scope", (),
                                      tree.identity(tree.fd)[1:], destination.name)
                capacity = os.fstatvfs(tree.fd)
                self.evidence = NativeFolderEvidence(target, str(parent), str(destination), "fixture-uuid",
                                                     ("filesystem:fixture-uuid", "serial:fixture-disk"),
                                                     tree.mount_id, "fixture-major-minor",
                                                     CapacityObservation(capacity.f_bavail * capacity.f_frsize,
                                                                         capacity.f_favail),
                                                     255, capacity.f_frsize)
                return self.evidence

        def recheck(self, tree, evidence, **kwargs):
            tree.check()
            assert tree.identity(tree.fd)[1:] == evidence.target.parent_identity
            return evidence

    monkeypatch.setattr(f, "NativeFolderObserver", Observer)
    monkeypatch.setattr(f, "read_catalog", lambda *a: snapshot)
    monkeypatch.setattr(operator, "read_catalog", lambda *a: snapshot)
    monkeypatch.setattr(operator, "_archives", lambda *a: ())
    monkeypatch.setattr(f, "FencedSources", lambda *a: Sources(snapshot, t))
    monkeypatch.setattr(operator, "LinuxObserver", lambda: pytest.fail("native plan entered USB observer"))
    return parent, catalog, calls


def approved(setup):
    parent, catalog, _ = setup
    result = operator.preview(catalog, parent / "delivery", ("org/model",), None)
    operator.approve(result["transaction_id"], result["seal"])
    return result


def test_preview_and_approve_write_only_private_state(setup):
    parent, _, calls = setup
    result = approved(setup)
    assert list(parent.iterdir()) == []
    assert result["ownership"] == "new-output-folder-only"
    assert result["capacity_policy"] == "advisory-shared-space-not-reserved"
    assert result["resume_policy"] == "authenticated"
    assert result["executable"] is False
    assert calls and all(not item[1] for item in calls)


@pytest.mark.parametrize("point", ["open", "recheck", "close"])
def test_preview_io_failure_is_structured_without_output(setup, monkeypatch, point):
    parent, catalog, _ = setup
    original = f.BoundTree

    class FailingTree(original):
        def __init__(self, *args, **kwargs):
            if point == "open":
                raise OSError("synthetic preview reopen failure")
            super().__init__(*args, **kwargs)

        def __exit__(self, *args):
            super().__exit__(*args)
            if point == "close":
                raise OSError("synthetic preview close failure")

    monkeypatch.setattr(f, "BoundTree", FailingTree)
    if point == "recheck":
        def fail(*args, **kwargs):
            raise OSError("synthetic preview recheck failure")
        monkeypatch.setattr(f.NativeFolderObserver, "recheck", fail)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_UNPROVEN"):
        operator.preview(catalog, parent / "delivery", ("org/model",), None)
    assert not list(parent.iterdir())


@pytest.mark.parametrize("free,code", [(1, "DESTINATION_CAPACITY_WAIT"),
                                      (None, "DESTINATION_CAPACITY_UNPROVEN")])
def test_preview_requires_remaining_inode_budget(setup, monkeypatch, free, code):
    parent, catalog, _ = setup
    original = f.NativeFolderObserver.recheck
    def recheck(*args, **kwargs):
        value = original(*args, **kwargs)
        return replace(value, capacity=replace(value.capacity, free_inodes=free))
    monkeypatch.setattr(f.NativeFolderObserver, "recheck", recheck)
    with pytest.raises(t.TransferRefusal, match=code):
        operator.preview(catalog, parent / "delivery", ("org/model",), None)
    assert not list(parent.iterdir())


def test_native_operator_roundtrip_and_completed_start_need_no_usb_observer(setup, monkeypatch):
    parent, _, _ = setup
    result = approved(setup)
    (parent / "unrelated").write_bytes(b"untouched")
    tx = result["transaction_id"]
    done = operator.start(tx, parent / "delivery", {})
    assert done["state"] == "complete" and not done["can_write"]
    assert (parent / "delivery/org/model/model.safetensors").read_bytes() == DATA
    assert (parent / "delivery/.modelark-slice-owner").exists()
    assert not (parent / ".modelark-slice-owner").exists()
    assert (parent / "unrelated").read_bytes() == b"untouched"
    monkeypatch.setattr(f, "NativeFolderObserver", lambda: pytest.fail("completed Start observes destination"))
    assert operator.start(tx, "/absent", {})["state"] == "complete"


@pytest.mark.parametrize("code,expected", [("SOURCE_BLOCKED", "blocked_source"),
                                           ("WAITING_SOURCE", "waiting_source")])
def test_native_preflight_returns_source_status_without_output(setup, monkeypatch, code, expected):
    parent, _, _ = setup
    result = approved(setup)
    tx = result["transaction_id"]
    def refuse(self, proposal, check):
        check()
        raise t.TransferRefusal(code, "preflight source unavailable")
    monkeypatch.setattr(Sources, "preflight", refuse, raising=False)
    outcome = operator.start(tx, parent / "delivery", {})
    assert outcome["state"] == expected and not outcome["ok"] and not outcome["can_write"]
    assert code in outcome["reason"]
    assert state.Store().events(tx) == [] and not (parent / "delivery").exists()
    monkeypatch.setattr(Sources, "preflight", lambda self, proposal, check: check())
    assert operator.start(tx, parent / "delivery", {})["state"] == "complete"


def test_native_preflight_interrupt_returns_acknowledged_stop_without_reclaim(setup, monkeypatch):
    parent, _, _ = setup
    tx = approved(setup)["transaction_id"]
    def interrupted(self, proposal, check):
        raise KeyboardInterrupt
    monkeypatch.setattr(Sources, "preflight", interrupted, raising=False)
    with monkeypatch.context() as patch:
        patch.setattr(state.Store, "claim", lambda *a: pytest.fail("interrupted preflight claimed an attempt"))
        outcome = operator.start(tx, parent / "delivery", {})
    assert outcome["state"] == "stopped" and outcome["stop_requested"] and not outcome["can_write"]
    assert state.Store().events(tx) == [] and not (parent / "delivery").exists()
    with state.Store()._connection(write=False) as con:
        assert con.execute("SELECT stop_serial,acknowledged_stop_serial FROM transactions WHERE id=?", (tx,)).fetchone() == (1, 1)
    monkeypatch.setattr(Sources, "preflight", lambda self, proposal, check: check())
    assert operator.start(tx, parent / "delivery", {})["state"] == "complete"


def test_unapproved_native_plan_refuses_before_observation(setup, monkeypatch):
    parent, catalog, _ = setup
    result = operator.preview(catalog, parent / "delivery", ("org/model",), None)
    monkeypatch.setattr(f, "NativeFolderObserver", lambda: pytest.fail("unapproved Start observes destination"))
    with pytest.raises(t.TransferRefusal, match="APPROVAL_MISSING"):
        operator.start(result["transaction_id"], parent / "delivery", {})
    assert not (parent / "delivery").exists()


def test_start_wrong_folder_refuses_without_creation(setup):
    parent, _, _ = setup
    result = approved(setup)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CHANGED"):
        operator.start(result["transaction_id"], parent / "other", {})
    assert not list(parent.iterdir())


def test_root_collision_after_approval_is_never_adopted(setup):
    parent, _, _ = setup
    result = approved(setup)
    (parent / "delivery").mkdir()
    (parent / "delivery/foreign").write_bytes(b"keep")
    with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
        operator.start(result["transaction_id"], parent / "delivery", {})
    assert (parent / "delivery/foreign").read_bytes() == b"keep"
    assert sorted(p.name for p in (parent / "delivery").iterdir()) == ["foreign"]


def test_ctrl_c_acknowledges_before_lease_release_then_resumes(setup, monkeypatch):
    parent, _, _ = setup
    result = approved(setup)
    tx = result["transaction_id"]
    run = t.Session.run
    raised = False

    def interrupted(session):
        nonlocal raised
        if not raised:
            raised = True
            raise KeyboardInterrupt
        return run(session)

    monkeypatch.setattr(t.Session, "run", interrupted)
    stopped = operator.start(tx, parent / "delivery", {})
    assert stopped["state"] == "stopped" and stopped["stop_requested"]
    assert operator.start(tx, parent / "delivery", {})["state"] == "complete"


def test_capacity_after_approval_waits_without_creating_root(setup, monkeypatch):
    parent, _, _ = setup
    result = approved(setup)
    original = f.NativeFolderDestination.check

    def insufficient(*a):
        raise t.TransferRefusal("DESTINATION_CAPACITY_WAIT")

    monkeypatch.setattr(f.NativeFolderDestination, "check", insufficient)
    waiting = operator.start(result["transaction_id"], parent / "delivery", {})
    assert waiting["state"] == "waiting_destination" and not waiting["can_write"]
    assert not (parent / "delivery").exists()
    monkeypatch.setattr(f.NativeFolderDestination, "check", original)
    assert operator.start(result["transaction_id"], parent / "delivery", {})["state"] == "complete"


def test_native_full_layout_rejects_generated_name_and_path_limits(setup):
    parent, _, _ = setup
    result = approved(setup)
    plan = state.Store().load(result["transaction_id"])
    evidence = f.NativeFolderObserver().observe(parent / "another", archives=())
    long_file = replace(plan.proposal.closure[0], rfilename="x" * 256)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_LAYOUT_UNSUPPORTED"):
        f._layout(replace(plan.proposal, closure=(long_file,)), evidence)
    reserved = replace(long_file, rfilename=".slice-user-data")
    with pytest.raises(t.TransferRefusal, match="DESTINATION_LAYOUT_UNSUPPORTED"):
        f._layout(replace(plan.proposal, closure=(reserved,)), evidence)
