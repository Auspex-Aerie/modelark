"""FAT session policy/port fault matrix on disposable Linux dirs, not FAT qualification."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
import errno
import json
import os
import threading
from types import SimpleNamespace

import pytest

from modelark.slice import domain as d, state, transaction as t
from modelark.slice import fat32_destination as destination_module
from modelark.slice.fat32_destination import Fat32Destination
from modelark.slice.fat32_plan import Fat32Intent, Fat32Plan, binding_for, estimate_metadata
from modelark.slice.folder_contract import CapacityObservation, FolderProfile
from modelark.slice.linux import BoundTree
from test_slice_transaction import DATA, Sources, proposal


@pytest.mark.parametrize("observed,expected", [
    ("WAITING_DESTINATION", "WAITING_DESTINATION"),
    ("DESTINATION_CHANGED", "DESTINATION_CHANGED"),
    ("DESTINATION_UNPROVEN", "DESTINATION_CAPACITY_WAIT"),
    (None, "DESTINATION_CAPACITY_WAIT"),
])
def test_io_classification_rechecks_backing_not_only_parent(fat, observed, expected):
    def recheck(tree):
        tree.check()  # A removed backing device can leave the mount/path intact.
        if observed is None:
            raise OSError(errno.EIO, "backing probe unavailable")
        raise t.TransferRefusal(observed, "synthetic backing evidence")
    fat.case.adapter._recheck = recheck
    refusal = fat.case.adapter._io_refusal(OSError(errno.ENOSPC, "original allocation failure"))
    assert refusal.code == expected


@pytest.mark.parametrize("point", ["_live", "_open"])
def test_reader_setup_io_failure_is_structured(fat, monkeypatch, point):
    with t.start(fat.store, fat.case.tx, fat.case.adapter, fat.case.sources):
        def fail(*args):
            raise OSError(errno.EIO, "synthetic reader setup failure")
        monkeypatch.setattr(fat.case.adapter, point, fail)
        with pytest.raises(t.TransferRefusal, match="DESTINATION_IO_FAILED"):
            with fat.case.adapter.read(fat.case.plan.control_path):
                pytest.fail("failed reader setup yielded a stream")


@pytest.fixture
def fat(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "HOST_STATE_DIR", tmp_path / "private")
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o700)
    sibling = parent / "unrelated"
    sibling.write_bytes(b"existing sibling")
    store = state.Store()
    with ExitStack() as stack:
        tree = stack.enter_context(BoundTree(parent, writable=True))

        def create(*, child="delivery", parts=(), scope=None):
            target = Fat32Intent(FolderProfile.FAT32_SESSION, scope or "test-filesystem:" + str(tmp_path),
                                 parts, child)
            catalog, backing = str(tmp_path / "catalog.sqlite"), ("serial:disk-a",)
            binding = binding_for(target, catalog, str(parent), backing)
            original, _, snapshot = proposal()
            preview = d.preview(replace(original.spec, destination_id=target.target_id,
                                        destination_root=child), snapshot)
            capacity = CapacityObservation(64 * 1024 * 1024, None, 0)
            reserve = estimate_metadata(preview, binding, catalog, str(parent), backing,
                                        capacity, store.root)
            plan = Fat32Plan(preview, binding, catalog, str(parent), backing, capacity, reserve)
            approval = d.approve(preview, expected_seal=preview.seal, current_snapshot=snapshot)
            tx = store.create(plan, approval)
            store.approve(tx, expected_seal=plan.seal)
            adapter = stack.enter_context(Fat32Destination(tree, binding, store, tx,
                                                           recheck=lambda tree: None))
            return SimpleNamespace(plan=plan, tx=tx, adapter=adapter, sources=Sources(snapshot, t))

        yield SimpleNamespace(parent=parent, sibling=sibling, tree=tree, store=store,
                              create=create, case=create())


def run(fat, case=None, *, fault=None):
    case = case or fat.case
    with t.start(fat.store, case.tx, case.adapter, case.sources, fault=fault) as session:
        return session.run()


def consumed(fat):
    with fat.store._connection(write=False) as con:
        return con.execute("SELECT consumed_attempt FROM transactions WHERE id=?",
                           (fat.case.tx,)).fetchone()[0]


def assert_restart_refused(fat):
    before = consumed(fat)
    assert isinstance(before, str) and before
    with pytest.raises(t.TransferRefusal, match="FAT32_NEW_ROOT_REQUIRED"):
        t.start(fat.store, fat.case.tx, fat.case.adapter, fat.case.sources)
    assert consumed(fat) == before


def test_live_session_exports_verified_bytes_and_metadata_without_xattrs(fat, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("FAT session must not depend on xattrs")
    monkeypatch.setattr(os, "getxattr", forbidden)
    monkeypatch.setattr(os, "setxattr", forbidden)
    original = fat.case.sources.snapshot
    assert run(fat).state == "complete"
    child = fat.parent / "delivery"
    assert (child / "org/model/model.safetensors").read_bytes() == DATA
    receipt = json.loads((child / ".modelark-slice-receipt.json").read_text())
    assert receipt == fat.store.receipt(fat.case.tx)
    assert Fat32Plan.from_json(json.dumps(receipt["plan"])) == fat.case.plan
    assert receipt["status"] == "export-verified"
    assert receipt["resume_policy"] == "new-root"
    assert receipt["host_transaction_completion"] == "not-claimed"
    assert receipt["receipt_persistence"] == "not-self-certified"
    assert not (fat.parent / ".modelark-slice-owner").exists()
    assert fat.sibling.read_bytes() == b"existing sibling"
    assert fat.case.sources.snapshot == original
    assert consumed(fat)


def test_unrelated_sibling_activity_does_not_break_session(fat):
    def change_sibling(point):
        if point == "chunk_written":
            fat.sibling.write_bytes(b"unrelated" * 4096)
            (fat.parent / "another-sibling").mkdir()
    assert run(fat, fault=change_sibling).state == "complete"
    assert fat.sibling.read_bytes() == b"unrelated" * 4096


def test_stop_consumes_attempt_preserves_partial_tree_and_requires_new_root(fat):
    def stop(point):
        if point == "chunk_written":
            fat.store.request_stop(fat.case.tx)
    assert run(fat, fault=stop).state == "stopped"
    partials = tuple((fat.parent / "delivery/org/model").glob(".slice-*"))
    assert len(partials) == 1 and partials[0].read_bytes() == DATA
    assert_restart_refused(fat)
    sibling = fat.create(child="fresh-delivery")
    assert run(fat, sibling).state == "complete"
    assert partials[0].read_bytes() == DATA
    assert fat.store.receipt(fat.case.tx) is None


def test_fresh_store_and_port_cannot_recover_stopped_output_from_saved_intent(fat):
    case = fat.case
    with t.start(fat.store, case.tx, case.adapter, case.sources):
        pass
    before = consumed(fat)
    fat.store = state.Store()
    with Fat32Destination(fat.tree, case.plan.destination, fat.store, case.tx,
                          recheck=lambda tree: None) as reopened:
        case.adapter = reopened
        assert_restart_refused(fat)
    assert consumed(fat) == before


def test_duplicate_start_observes_live_attempt_without_spending_or_closing_it(fat):
    case = fat.case
    with t.start(fat.store, case.tx, case.adapter, case.sources) as session:
        before = consumed(fat)
        duplicate = t.start(fat.store, case.tx, case.adapter, case.sources)
        assert not duplicate.can_write
        assert consumed(fat) == before
        assert session.can_write
        assert session.run().state == "complete"


@pytest.mark.parametrize("source_code,expected_state", [("WAITING_SOURCE", "waiting_source"),
                                                       ("SOURCE_CHANGED", "blocked_source")])
def test_source_wait_or_block_ends_attempt_without_resume(fat, source_code, expected_state):
    fat.case.sources.status["drive-a"] = source_code
    with t.start(fat.store, fat.case.tx, fat.case.adapter, fat.case.sources) as session:
        assert session.run().state == expected_state
        assert not session.can_write
    fat.case.sources.status.clear()
    assert_restart_refused(fat)


@pytest.mark.parametrize("code", ["WAITING_DESTINATION", "DESTINATION_CAPACITY_WAIT"])
def test_destination_wait_ends_attempt_without_resume(fat, monkeypatch, code):
    case = fat.case
    with t.start(fat.store, case.tx, case.adapter, case.sources) as session:
        original = case.adapter.check
        def wait(*args, **kwargs):
            raise t.TransferRefusal(code)
        monkeypatch.setattr(case.adapter, "check", wait)
        assert session.run().state == "waiting_destination"
        assert not session.can_write
        monkeypatch.setattr(case.adapter, "check", original)
        with pytest.raises(t.TransferRefusal):
            case.adapter.create_directory("delivery/late", "a" * 32)
    assert_restart_refused(fat)


@pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT])
def test_actual_partial_capacity_write_preserved_but_cannot_resume(fat, monkeypatch, code):
    original_write = os.write
    armed, partial = False, False
    def fail_after_partial(fd, data):
        nonlocal partial
        if armed:
            if not partial:
                partial = True
                return original_write(fd, data[:3])
            raise OSError(code, "synthetic capacity failure")
        return original_write(fd, data)
    def arm(point):
        nonlocal armed
        if point == "temporary_created":
            armed = True
    with monkeypatch.context() as patch:
        patch.setattr(os, "write", fail_after_partial)
        assert run(fat, fault=arm).state == "waiting_destination"
    partials = tuple((fat.parent / "delivery/org/model").glob(".slice-*"))
    assert len(partials) == 1 and partials[0].read_bytes() == DATA[:3]
    assert_restart_refused(fat)
    assert partials[0].read_bytes() == DATA[:3]


def test_consumption_before_first_directory_survives_failed_admission(fat, monkeypatch):
    original = fat.case.adapter.check
    def wait(*args, **kwargs):
        raise t.TransferRefusal("WAITING_DESTINATION")
    monkeypatch.setattr(fat.case.adapter, "check", wait)
    with pytest.raises(t.TransferRefusal, match="WAITING_DESTINATION"):
        run(fat)
    assert not (fat.parent / "delivery").exists()
    monkeypatch.setattr(fat.case.adapter, "check", original)
    assert_restart_refused(fat)


FAULTS = ["reserved", "directory_intent", "directory_created", "directory_flushed",
          "directory_parent_flushed", "directory_complete", "temporary_created", "chunk_written"]
FAULTS += [kind + "_" + phase for kind in ("control", "file", "receipt")
           for phase in ("intent", "flushed", "prepared", "published", "parent_flushed", "complete")]
FAULTS += [kind + "_" + phase for kind in ("control", "receipt") for phase in ("created", "chunk_written")]


@pytest.mark.parametrize("point", FAULTS)
def test_every_post_claim_fault_leaves_consumed_nonresumable_attempt(fat, point):
    reached = False
    def crash(current):
        nonlocal reached
        if current == point:
            reached = True
            raise RuntimeError("synthetic fault: " + point)
    with pytest.raises(RuntimeError, match="synthetic fault"):
        run(fat, fault=crash)
    assert reached
    assert_restart_refused(fat)
    assert fat.store.receipt(fat.case.tx) is None
    assert fat.sibling.read_bytes() == b"existing sibling"


def test_failure_before_claim_has_not_consumed_attempt(fat):
    def crash(point):
        if point == "reservation_committed":
            raise RuntimeError("before claim")
    with pytest.raises(RuntimeError, match="before claim"):
        run(fat, fault=crash)
    assert not consumed(fat)
    assert not (fat.parent / "delivery").exists()
    assert run(fat).state == "complete"


@pytest.mark.parametrize("operation", ["receipt_journal", "host_complete"])
def test_host_commit_failure_never_relabels_export_report_as_host_commit(fat, monkeypatch, operation):
    original = fat.store.append if operation == "receipt_journal" else fat.store.complete
    def fail(*args, **kwargs):
        if operation == "host_complete" or args[1] == "receipt":
            raise t.TransferRefusal("STATE_BUSY", "synthetic host commit refusal")
        return original(*args, **kwargs)
    monkeypatch.setattr(fat.store, "append" if operation == "receipt_journal" else "complete", fail)
    with pytest.raises(t.TransferRefusal, match="STATE_BUSY"):
        run(fat)
    report = json.loads((fat.parent / "delivery/.modelark-slice-receipt.json").read_text())
    assert report["status"] == "export-verified"
    assert fat.store.status(fat.case.tx).state != "complete"
    assert_restart_refused(fat)


def test_old_port_cannot_mutate_after_session_close_even_if_port_context_is_open(fat):
    case = fat.case
    with t.start(fat.store, case.tx, case.adapter, case.sources):
        pass
    with pytest.raises(t.TransferRefusal):
        case.adapter.create_directory("delivery/late", "a" * 32)
    assert not (fat.parent / "delivery/late").exists()
    assert_restart_refused(fat)


def test_live_port_cannot_append_published_artifact(fat):
    case = fat.case
    with t.start(fat.store, case.tx, case.adapter, case.sources) as session:
        assert session.step().state == "transferring"
        final = "delivery/org/model/model.safetensors"
        token = case.adapter.inspect(final).token
        with pytest.raises(t.TransferRefusal):
            case.adapter.append(final, token, b"must not append")
        assert (fat.parent / final).read_bytes() == DATA


def test_consumer_oserror_inside_read_context_is_not_reclassified(fat):
    case = fat.case
    failure = OSError(errno.EIO, "consumer-owned failure, not destination I/O")
    with t.start(fat.store, case.tx, case.adapter, case.sources) as session:
        assert session.step().state == "transferring"
        with pytest.raises(OSError) as caught:
            with case.adapter.read("delivery/org/model/model.safetensors") as stream:
                assert stream.read() == DATA
                raise failure
        assert caught.value is failure
        assert session.can_write


def test_final_retained_close_error_prevents_host_completion(fat, monkeypatch):
    case = fat.case
    retained, closed = set(), []
    original_close = os.close
    failure = OSError(errno.EIO, "final retained close reported writeback failure")
    failed = False

    def arm(point):
        if point == "receipt_complete":
            retained.update(obj.fd for obj in case.adapter._objects.values())

    def close(fd):
        nonlocal failed
        original_close(fd)
        if fd in retained:
            closed.append(fd)
            if not failed:
                failed = True
                raise failure

    with monkeypatch.context() as patch:
        patch.setattr(os, "close", close)
        with pytest.raises((OSError, t.TransferRefusal)) as caught:
            run(fat, fault=arm)
    assert caught.value is failure or getattr(caught.value, "code", None) == "DESTINATION_IO_FAILED"
    assert failed and set(closed) == retained and len(closed) == len(retained)
    assert case.adapter._objects == {}
    assert fat.store.status(case.tx).state != "complete"
    report = json.loads((fat.parent / "delivery/.modelark-slice-receipt.json").read_text())
    assert report["status"] == "export-verified"
    assert report["host_transaction_completion"] == "not-claimed"
    assert_restart_refused(fat)


@pytest.mark.parametrize("primary_failure", [False, True])
def test_failed_close_detaches_all_handles_and_cleanup_is_never_retried(fat, monkeypatch, primary_failure):
    case = fat.case
    session = t.start(fat.store, case.tx, case.adapter, case.sources)
    retained = tuple(obj.fd for obj in case.adapter._objects.values())
    assert len(retained) >= 2
    original_close = os.close
    closed = []
    failure = OSError(errno.EIO, "close released its descriptor before reporting failure")

    def close(fd):
        if fd in retained:
            closed.append(fd)
            original_close(fd)
            if fd == retained[0]:
                raise failure
        else:
            original_close(fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "close", close)
            if primary_failure:
                try:
                    raise RuntimeError("primary transfer failure")
                except RuntimeError:
                    case.adapter.end_session()  # Cleanup must not replace the active failure.
            else:
                with pytest.raises(OSError) as caught:
                    case.adapter.end_session()
                assert caught.value is failure
            assert closed == list(retained)
            assert case.adapter._objects == {}
            assert case.adapter.cleanup_errors == (failure,)
            case.adapter.end_session()
            assert closed == list(retained)  # Released descriptor numbers are never retried.
    finally:
        session.close()


def test_retained_descriptor_budget_refuses_before_binding_or_destination_writes(fat, monkeypatch):
    monkeypatch.setattr(destination_module.resource, "getrlimit", lambda resource: (1, 1))
    case = fat.case
    with pytest.raises(t.TransferRefusal, match="DESTINATION_LAYOUT_UNSUPPORTED"):
        Fat32Destination(fat.tree, case.plan.destination, fat.store, case.tx, recheck=lambda tree: None)
    assert not consumed(fat)
    assert not (fat.parent / "delivery").exists()
    assert fat.sibling.read_bytes() == b"existing sibling"


def reserve_race(store, first, second):
    barrier = threading.Barrier(2)
    def reserve(case):
        barrier.wait()
        try:
            store.reserve(case.tx, case.plan.destination.device_id)
            return "reserved"
        except t.TransferRefusal as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        return sorted(pool.map(reserve, (first, second)))


@pytest.mark.parametrize("child,parts", [("DELIVERY", ()), ("nested", ("DELIVERY",))])
def test_case_equivalent_or_nested_claims_race_to_one_owner(fat, child, parts):
    other = fat.create(child=child, parts=parts)
    assert reserve_race(fat.store, fat.case, other) == ["DESTINATION_BUSY", "reserved"]


def test_disjoint_sibling_claims_are_permitted_and_stopped_root_stays_claimed(fat):
    other = fat.create(child="other-delivery")
    assert reserve_race(fat.store, fat.case, other) == ["reserved", "reserved"]
    with t.start(fat.store, fat.case.tx, fat.case.adapter, fat.case.sources):
        pass
    overlapping = fat.create(child="DELIVERY")
    with pytest.raises(t.TransferRefusal, match="DESTINATION_BUSY"):
        fat.store.reserve(overlapping.tx, overlapping.plan.destination.device_id)
    assert run(fat, other).state == "complete"
