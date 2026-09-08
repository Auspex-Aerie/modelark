"""Delivery authority regressions: disposable state, devices and original bytes only."""
import multiprocessing
import os
from contextlib import contextmanager

import pytest

from test_slice_transaction import api as api, setup as setup, d


def test_stale_acquisition_confuses_a_delayed_completed_starter(setup, monkeypatch):
    t, store, plan, first, destination, sources = setup
    second = store.create(plan, d.approve(plan.proposal, expected_seal=plan.proposal.seal,
                                        current_snapshot=sources.snapshot))
    store.approve(first, expected_seal=plan.seal)
    store.approve(second, expected_seal=plan.seal)
    observed = []
    def delayed(point):
        if point != "reservation_committed":
            return
        with t.start(store, first, destination, sources) as winner:
            assert winner.run().state == "complete"
        def transient_failure(*args):
            raise t.TransferRefusal("STATE_BUSY")
        with monkeypatch.context() as patch:
            patch.setattr(destination, "check", transient_failure)
            with pytest.raises(t.TransferRefusal, match="STATE_BUSY"):
                t.start(store, second, destination, sources)
        assert store.owner(plan.destination.device_id) == second
        status = store.status
        def after_old_starter_binds(tx):
            if tx == first and not observed:
                try:
                    observed.append(t.start(store, second, destination, sources))
                except t.TransferRefusal as exc:
                    observed.append(exc)
            return status(tx)
        monkeypatch.setattr(store, "status", after_old_starter_binds)
    assert t.start(store, first, destination, sources, fault=delayed).state == "complete"
    assert len(observed) == 1
    assert isinstance(observed[0], t.TransferRefusal), repr(observed[0])
    assert observed[0].code == "DESTINATION_BUSY"


def test_completion_between_status_and_reservation_returns_terminal_status(setup, monkeypatch):
    t, store, plan, tx, destination, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    reserve = store.reserve
    def completion_wins(*args):
        monkeypatch.setattr(store, "reserve", reserve)
        with t.start(store, tx, destination, sources) as winner:
            assert winner.run().state == "complete"
        return reserve(*args)
    monkeypatch.setattr(store, "reserve", completion_wins)
    outcome = t.start(store, tx, destination, sources)
    assert outcome.state == "complete" and not outcome.can_write


def _die_after_operator_stop(store, tx, destination, sources):
    from modelark.slice import transaction as t
    def die(point):
        if point == "reserved":
            store.request_stop(tx)
            os._exit(73)
    with t.start(store, tx, destination, sources, fault=die):
        pass


def test_recovery_preserves_unacknowledged_stop_after_writer_death(setup):
    t, store, plan, tx, destination, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    process = multiprocessing.get_context("fork").Process(
        target=_die_after_operator_stop, args=(store, tx, destination, sources))
    process.start()
    process.join()
    assert process.exitcode == 73
    assert store.status(tx).state == "transferring" and store.stop_requested(tx)
    assert not list(destination.root.iterdir())
    with pytest.raises(t.TransferRefusal, match="STOPPED"):
        with t.start(store, tx, destination, sources):
            pass
    assert not list(destination.root.iterdir())
    with t.start(store, tx, destination, sources) as resumed:
        assert resumed.run().state == "complete"


def test_marker_queries_do_not_queue_and_release_precedes_device(api):
    from modelark.slice.authority import Lease
    lease = Lease("authority-marker-test", "transaction")
    try:
        for _ in range(1000):
            assert Lease.retained(lease.device, lease.attempt)
        lease.marker.close()
        assert not Lease.retained(lease.device, lease.attempt)
        with pytest.raises(api[0].TransferRefusal, match="DESTINATION_BUSY"):
            Lease(lease.device, "another")
    finally:
        lease.close()


def test_replaced_attempt_cannot_publish_state_or_journal(setup):
    t, store, plan, tx, destination, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    first = t.start(store, tx, destination, sources)
    old = first.authority.attempt
    first.close()
    with t.start(store, tx, destination, sources) as current:
        before = store.head(tx)
        for operation in (
            lambda: store.transition(old, plan.destination.device_id, "failed"),
            lambda: store.append(tx, "hostile", {}, attempt=old, device=plan.destination.device_id),
        ):
            with pytest.raises(t.TransferRefusal, match="EXECUTION_FENCE_LOST"):
                operation()
        assert store.head(tx) == before
        assert store.status(tx).state == "transferring"
        assert current.run().state == "complete"


def test_stopped_attempt_cannot_restart_without_new_claim(setup):
    t, store, plan, tx, destination, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    with t.start(store, tx, destination, sources) as session:
        session.authority.transition("stopped")
        for operation in (
            lambda: session.authority.transition("transferring"),
            lambda: session.authority.append("hostile", {}, expected_head=store.head(tx)),
        ):
            with pytest.raises(t.TransferRefusal, match="STATE_TRANSITION_INVALID"):
                operation()


@pytest.mark.parametrize("legacy_state", ["starting", "stopped"])
def test_v4_migration_never_treats_process_history_as_live_identity(setup, api, legacy_state):
    t, store, plan, tx, destination, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    store.reserve(tx, plan.destination.device_id)
    store.request_stop(tx)
    with store._connection() as con:
        con.execute("UPDATE transactions SET state=?", (legacy_state,))
        con.execute("UPDATE owners SET process_seen=1")
        con.execute("ALTER TABLE owners DROP COLUMN attempt")
        con.execute("ALTER TABLE transactions DROP COLUMN acknowledged_stop_serial")
        con.execute("PRAGMA user_version=4")
    upgraded = api[1].Store()
    assert upgraded.current_attempt(plan.destination.device_id) is None
    unrelated = t._Lease(plan.destination.device_id)
    try:
        for _ in range(2):
            with pytest.raises(t.TransferRefusal, match="DESTINATION_BUSY"):
                t.start(upgraded, tx, destination, sources)
    finally:
        unrelated.close()
    with pytest.raises(t.TransferRefusal, match="STOPPED"):
        t.start(upgraded, tx, destination, sources)
    assert not list(destination.root.iterdir())
    with t.start(upgraded, tx, destination, sources) as resumed:
        assert resumed.run().state == "complete"


def test_start_issued_before_stop_acknowledgment_cannot_resume_later(setup):
    t, store, plan, tx, destination, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    with t.start(store, tx, destination, sources) as first:
        store.request_stop(tx)
        assert first.run().state == "stopped"
    store.request_stop(tx)  # New request while stopped, not yet acknowledged.
    before = tuple(destination.root.rglob("*"))
    def acknowledge_from_another_caller(point):
        if point == "reservation_committed":
            with pytest.raises(t.TransferRefusal, match="STOPPED"):
                t.start(store, tx, destination, sources)
    with pytest.raises(t.TransferRefusal, match="STOPPED"):
        t.start(store, tx, destination, sources, fault=acknowledge_from_another_caller)
    assert tuple(destination.root.rglob("*")) == before
    assert store.stop_requested(tx)
    with t.start(store, tx, destination, sources) as resumed:
        assert resumed.run().state == "complete"


def test_first_reservation_keeps_snapshot_while_waiting_for_writer(setup, monkeypatch):
    t, store, plan, tx, destination, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    connection = store._connection
    intervened = False
    @contextmanager
    def delayed_writer(*, write=True):
        nonlocal intervened
        if write and not intervened:
            intervened = True
            def request_during_initialization(point):
                if point == "reservation_committed":
                    store.request_stop(tx)
            with pytest.raises(t.TransferRefusal, match="STOPPED"):
                t.start(store, tx, destination, sources, fault=request_during_initialization)
        with connection(write=write) as con:
            yield con
    monkeypatch.setattr(store, "_connection", delayed_writer)
    with pytest.raises(t.TransferRefusal, match="STOPPED"):
        t.start(store, tx, destination, sources)
    assert intervened and not list(destination.root.iterdir())
    with t.start(store, tx, destination, sources) as resumed:
        assert resumed.run().state == "complete"
