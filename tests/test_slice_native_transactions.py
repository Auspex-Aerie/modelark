"""Real disposable folder ports and durable authority; no device observation or media."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import errno
import json
import os
import threading
from types import SimpleNamespace

import pytest

from modelark.slice import domain as d
from modelark.slice import state, transaction as t
from modelark.slice.folder_contract import CapacityObservation, FolderProfile, FolderTarget
from modelark.slice.folder_destination import NativeFolderDestination
from modelark.slice.folder_plan import NativePlan, binding_for, estimate_metadata
from modelark.slice.linux import BoundTree
from test_slice_transaction import DATA, Sources, proposal


@pytest.fixture
def native(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "HOST_STATE_DIR", tmp_path / "private")
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o700)
    sibling = parent / "unrelated"
    sibling.write_bytes(b"existing sibling")
    store = state.Store()
    with ExitStack() as stack:
        tree = stack.enter_context(BoundTree(parent, writable=True))

        def create(*, child="delivery", parts=(), identity=None, scope="test-filesystem",
                   backing=("serial:disk-a",)):
            target = FolderTarget(FolderProfile.NATIVE_EXT4, scope, parts,
                                  identity or tree.identity(tree.fd)[1:], child)
            catalog = str(tmp_path / "read-only-catalog.sqlite")
            binding = binding_for(target, catalog, str(parent), backing)
            original, _, snapshot = proposal()
            preview = d.preview(replace(original.spec, destination_id=target.target_id,
                                        destination_root=child), snapshot)
            capacity = CapacityObservation(64 * 1024 * 1024, 10000, 0)
            reserve = estimate_metadata(preview, binding, catalog, str(parent), backing,
                                        capacity, store.root)
            plan = NativePlan(preview, binding, catalog, str(parent), backing, capacity, reserve)
            approval = d.approve(preview, expected_seal=preview.seal, current_snapshot=snapshot)
            tx = store.create(plan, approval)
            store.approve(tx, expected_seal=plan.seal)
            adapter = NativeFolderDestination(tree, binding, store, tx, recheck=lambda tree: None)
            return SimpleNamespace(plan=plan, tx=tx, adapter=adapter, sources=Sources(snapshot, t))

        yield SimpleNamespace(parent=parent, sibling=sibling, store=store, tree=tree,
                              create=create, case=create())


def run(native, case=None, *, fault=None):
    case = case or native.case
    with t.start(native.store, case.tx, case.adapter, case.sources, fault=fault) as session:
        return session.run()


def stop_after_chunk(native):
    def stop(point):
        if point == "chunk_written":
            native.store.request_stop(native.case.tx)
    assert run(native, fault=stop).state == "stopped"


def test_native_completion_keeps_control_receipt_and_verified_bytes_inside_child(native):
    case = native.case
    assert run(native).state == "complete"
    child = native.parent / "delivery"
    assert (child / "org/model/model.safetensors").read_bytes() == DATA
    assert (child / ".modelark-slice-owner").is_file()
    assert not (native.parent / ".modelark-slice-owner").exists()
    receipt = json.loads((child / ".modelark-slice-receipt.json").read_text())
    assert receipt == native.store.receipt(case.tx)
    assert NativePlan.from_json(json.dumps(receipt["plan"])) == case.plan
    assert receipt["topology"] == "folder"
    assert receipt["resume_policy"] == "authenticated"
    assert receipt["capacity_evidence"] == "advisory-shared-space-not-reserved"
    assert receipt["verification"]["result"] == "verified"
    assert native.sibling.read_bytes() == b"existing sibling"


def test_sibling_bytes_and_parent_inode_growth_do_not_invalidate_transfer(native):
    def unrelated_writes(point):
        if point == "chunk_written":
            native.sibling.write_bytes(b"new unrelated content" * 4096)
            for number in range(100):
                (native.parent / f"sibling-{number}").touch()
    assert run(native, fault=unrelated_writes).state == "complete"
    assert native.sibling.read_bytes().startswith(b"new unrelated content")
    assert len(tuple(native.parent.glob("sibling-*"))) == 100


def test_group_writable_parent_is_refused_before_output_creation(native):
    native.parent.chmod(0o775)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_NOT_WRITABLE"):
        run(native)
    assert not (native.parent / "delivery").exists()
    assert native.sibling.read_bytes() == b"existing sibling"
    assert native.store.receipt(native.case.tx) is None


def test_native_stop_and_fresh_adapter_resume_same_certified_root(native):
    stop_after_chunk(native)
    child = native.parent / "delivery"
    identity = child.stat().st_ino
    assert not (child / "org/model/model.safetensors").exists()
    assert tuple((child / "org/model").glob(".slice-*"))
    case = native.case
    case.adapter = NativeFolderDestination(native.tree, case.plan.destination, native.store,
                                           case.tx, recheck=lambda tree: None)
    assert run(native).state == "complete"
    assert child.stat().st_ino == identity
    assert (child / "org/model/model.safetensors").read_bytes() == DATA


def test_known_inode_shortfall_refuses_before_root_creation_and_can_retry(native, monkeypatch):
    original = os.fstatvfs
    def scarce(fd):
        observed = original(fd)
        return SimpleNamespace(f_bavail=observed.f_bavail, f_frsize=observed.f_frsize,
                               f_favail=1, f_ffree=1)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fstatvfs", scarce)
        with pytest.raises(t.TransferRefusal, match="DESTINATION_CAPACITY_WAIT"):
            run(native)
    assert not (native.parent / "delivery").exists()
    assert run(native).state == "complete"


def test_remaining_inode_budget_deduplicates_publication_links_and_allows_zero_at_finish(native, monkeypatch):
    def stop_published(point):
        if point == "file_published":
            native.store.request_stop(native.case.tx)
    assert run(native, fault=stop_published).state == "stopped"
    final = native.parent / "delivery/org/model/model.safetensors"
    temporary = next(final.parent.glob(".slice-*"))
    assert final.stat().st_ino == temporary.stat().st_ino
    original = os.fstatvfs
    available = 0  # Receipt is the one remaining inode, despite two payload links.
    def observed(fd):
        value = original(fd)
        return SimpleNamespace(f_bavail=value.f_bavail, f_frsize=value.f_frsize,
                               f_favail=available, f_ffree=available)
    monkeypatch.setattr(os, "fstatvfs", observed)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CAPACITY_WAIT"):
        run(native)
    available = 1
    def last_inode(point):
        nonlocal available
        if point == "receipt_created":
            available = 0
    assert run(native, fault=last_inode).state == "complete"
    assert available == 0


@pytest.mark.parametrize("replace_root", [False, True])
def test_missing_or_replaced_certified_root_is_never_recreated_or_adopted(native, replace_root):
    stop_after_chunk(native)
    child = native.parent / "delivery"
    child.rename(native.parent / "retained-original")
    if replace_root:
        child.mkdir()
        (child / "foreign").write_bytes(b"do not touch")
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CHANGED|OUTPUT_COLLISION"):
        run(native)
    assert native.store.receipt(native.case.tx) is None
    if replace_root:
        assert tuple(path.name for path in child.iterdir()) == ("foreign",)
        assert (child / "foreign").read_bytes() == b"do not touch"
    else:
        assert not child.exists()


@pytest.mark.parametrize("method,args", [
    ("inspect", ("unrelated",)),
    ("create_directory", ("elsewhere", "a" * 32)),
    ("create_file", (".slice-" + "a" * 32, "a" * 32)),
    ("append", (".slice-" + "a" * 32, "a" * 32, b"foreign")),
    ("discard_temporary", (".slice-" + "a" * 32, "a" * 32)),
    ("read", ("unrelated",)),
    ("flush", ("unrelated",)),
    ("list_paths", ("unrelated",)),
])
def test_native_port_rejects_paths_outside_approved_child(native, method, args):
    with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
        value = getattr(native.case.adapter, method)(*args)
        if method == "read":
            with value:
                pass
    assert native.sibling.read_bytes() == b"existing sibling"
    assert tuple(path.name for path in native.parent.iterdir()) == ("unrelated",)


def test_native_publication_rejects_foreign_final_path(native):
    adapter = native.case.adapter
    adapter.create_directory("delivery", "b" * 32)
    temporary = "delivery/.slice-" + "a" * 32
    adapter.create_file(temporary, "a" * 32)
    adapter.append(temporary, "a" * 32, DATA)
    with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
        adapter.publish(temporary, "unrelated", "a" * 32)
    assert native.sibling.read_bytes() == b"existing sibling"
    assert (native.parent / temporary).read_bytes() == DATA


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT])
def test_partial_capacity_failure_ends_attempt_and_fresh_start_recovers(native, monkeypatch, code):
    original_write = os.write
    armed = False
    partial = False

    def fail_after_partial(fd, data):
        nonlocal partial
        if armed:
            if not partial:
                partial = True
                return original_write(fd, data[:3])
            raise OSError(code, "synthetic exhausted shared capacity")
        return original_write(fd, data)

    def arm(point):
        nonlocal armed
        if point == "temporary_created":
            armed = True

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail_after_partial)
        case = native.case
        with t.start(native.store, case.tx, case.adapter, case.sources, fault=arm) as session:
            assert session.run().state == "waiting_destination"
            assert not session.can_write
            with pytest.raises(t.TransferRefusal, match="NOT_RESUMABLE"):
                session.step()
    temporary = tuple((native.parent / "delivery/org/model").glob(".slice-*"))
    assert len(temporary) == 1 and temporary[0].read_bytes() == DATA[:3]
    assert native.store.receipt(native.case.tx) is None
    assert run(native).state == "complete"
    assert (native.parent / "delivery/org/model/model.safetensors").read_bytes() == DATA


def reserve_concurrently(store, cases):
    barrier = threading.Barrier(2)

    def reserve(case):
        barrier.wait()
        try:
            store.reserve(case.tx, case.plan.destination.device_id)
            return "reserved"
        except t.TransferRefusal as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        return sorted(pool.map(reserve, cases))


@pytest.mark.parametrize("nested", [False, True])
def test_simultaneous_overlapping_native_claims_have_one_durable_winner(native, nested):
    first = native.case
    second = native.create(parts=("delivery",), child="nested") if nested else native.create()
    assert reserve_concurrently(native.store, (first, second)) == ["DESTINATION_BUSY", "reserved"]
    with native.store._connection(write=False) as con:
        assert con.execute("SELECT COUNT(*) FROM owners").fetchone()[0] == 1


def test_simultaneous_sibling_claims_are_both_permitted(native):
    first, second = native.case, native.create(child="delivery-other")
    assert reserve_concurrently(native.store, (first, second)) == ["reserved", "reserved"]
    assert native.store.owner(first.plan.destination.device_id) == first.tx
    assert native.store.owner(second.plan.destination.device_id) == second.tx
    assert run(native, first).state == "complete"
    assert run(native, second).state == "complete"


@pytest.mark.parametrize("legacy_identity", ["serial:disk-a", "serial:DISK-A"])
def test_simultaneous_native_and_legacy_drive_claims_exclude_each_other(native, legacy_identity):
    original, _, snapshot = proposal()
    preview = d.preview(replace(original.spec, destination_id=legacy_identity), snapshot)
    binding = t.DestinationBinding(legacy_identity, "test-filesystem", "legacy-mount", 10_000_000)
    plan = t.TransferPlan(preview, binding, metadata_reserve_bytes=65536)
    approval = d.approve(preview, expected_seal=preview.seal, current_snapshot=snapshot)
    tx = native.store.create(plan, approval)
    native.store.approve(tx, expected_seal=plan.seal)
    legacy = SimpleNamespace(plan=plan, tx=tx)
    assert reserve_concurrently(native.store, (native.case, legacy)) == ["DESTINATION_BUSY", "reserved"]
    assert not (native.parent / "delivery").exists()
