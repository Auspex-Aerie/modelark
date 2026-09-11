"""Unclaimed admission owns stop/resume outcomes and never spends FAT authority."""
# ruff: noqa: F811 -- imported pytest fixtures deliberately name parameters
import io
import resource
from contextlib import contextmanager

import pytest

from modelark import artifact_preflight
from modelark.artifact_policy import qualified_policy
from modelark.codec_resources import CodecResourceRefusal
from modelark.slice import transaction as t
from modelark.slice.authority import Lease, PreclaimInterrupted
from test_slice_fat32_transactions import fat  # noqa: F401
from test_slice_guarded_decoding import header


def stop_serials(store, tx):
    with store._connection(write=False) as con:
        return con.execute("SELECT stop,stop_serial,acknowledged_stop_serial FROM transactions WHERE id=?",
                           (tx,)).fetchone()


def start(fat):
    return t.start(fat.store, fat.case.tx, fat.case.adapter, fat.case.sources)


def acknowledge(fat, monkeypatch):
    monkeypatch.setattr(fat.case.sources, "preflight", lambda proposal, check: check(), raising=False)
    fat.store.request_stop(fat.case.tx)
    with pytest.raises(t.TransferRefusal, match="STOPPED"):
        start(fat)
    assert stop_serials(fat.store, fat.case.tx) == (1, 1, 1)


def assert_unspent(fat):
    assert not fat.store.attempt_consumed(fat.case.tx)
    assert fat.store.events(fat.case.tx) == []
    assert not (fat.parent / "delivery").exists()
    lease = Lease(fat.case.plan.destination.device_id, "probe-after-preflight")
    lease.close()


@pytest.mark.parametrize("code,expected", [("SOURCE_BLOCKED", "blocked_source"),
                                           ("WAITING_SOURCE", "waiting_source")])
@pytest.mark.parametrize("new_stop", [False, True])
def test_acknowledged_resume_preserves_source_outcome_unless_new_stop(fat, monkeypatch, code, expected, new_stop):
    acknowledge(fat, monkeypatch)
    def refusal(proposal, check):
        check()  # Old acknowledged Stop permits this explicit resume.
        if new_stop:
            fat.store.request_stop(fat.case.tx)
        raise t.TransferRefusal(code, "source unavailable")
    monkeypatch.setattr(fat.case.sources, "preflight", refusal)
    result = start(fat)
    assert result.state == ("stopped" if new_stop else expected)
    assert stop_serials(fat.store, fat.case.tx) == ((1, 2, 2) if new_stop else (0, 1, 1))
    assert_unspent(fat)
    if not new_stop:
        # A second wait keeps the actionable result; it does not resurrect Stop.
        assert start(fat).state == expected
    monkeypatch.setattr(fat.case.sources, "preflight", lambda proposal, check: check())
    with start(fat) as session:
        assert session.run().state == "complete"


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("new_stop", [False, True])
@pytest.mark.parametrize("point", ["first-check", "source", "last-check"])
def test_preclaim_interrupt_is_acknowledged_before_lease_release(fat, monkeypatch, resume, new_stop, point):
    if resume:
        acknowledge(fat, monkeypatch)
    guard = fat.store.guard_preclaim
    checks = []
    def checked(*args):
        checks.append(1)
        guard(*args)
        if (point == "first-check" and len(checks) == 1) or (point == "last-check" and len(checks) == 2):
            if new_stop:
                fat.store.request_stop(fat.case.tx)
            raise KeyboardInterrupt
    monkeypatch.setattr(fat.store, "guard_preclaim", checked)
    def preflight(proposal, check):
        if point == "source":
            if new_stop:
                fat.store.request_stop(fat.case.tx)
            raise KeyboardInterrupt
    monkeypatch.setattr(fat.case.sources, "preflight", preflight, raising=False)
    close = Lease.close
    outcomes_at_release = []
    def closing(lease):
        if lease.device == fat.case.plan.destination.device_id:
            outcomes_at_release.append((fat.store.status(fat.case.tx).state,
                                        stop_serials(fat.store, fat.case.tx)))
        close(lease)
    monkeypatch.setattr(Lease, "close", closing)
    with pytest.raises(PreclaimInterrupted) as exc:
        start(fat)
    serial = 2 if resume else 1
    assert exc.value.outcome == fat.store.status(fat.case.tx)
    assert exc.value.outcome.state == "stopped" and exc.value.outcome.reason == "STOPPED"
    assert outcomes_at_release == [("stopped", (1, serial, serial))]
    assert_unspent(fat)
    monkeypatch.setattr(fat.store, "guard_preclaim", guard)
    monkeypatch.setattr(fat.case.sources, "preflight", lambda proposal, check: check())
    with start(fat) as session:
        assert session.run().state == "complete"


def test_start_reserved_before_stop_ack_cannot_adopt_later_resume(fat, monkeypatch):
    # Another request can be issued while no attempt owns the destination. Its
    # reservation must not gain resume rights merely by waiting for exclusion.
    fat.store.request_stop(fat.case.tx)
    stale = fat.store.reserve(fat.case.tx, fat.case.plan.destination.device_id)
    monkeypatch.setattr(fat.case.sources, "preflight", lambda proposal, check: check(), raising=False)
    with pytest.raises(t.TransferRefusal, match="STOPPED"):
        start(fat)
    reserve = fat.store.reserve
    monkeypatch.setattr(fat.store, "reserve", lambda *a: stale)
    with pytest.raises(t.TransferRefusal, match="STOPPED"):
        start(fat)
    assert_unspent(fat)
    assert stop_serials(fat.store, fat.case.tx) == (1, 1, 1)
    monkeypatch.setattr(fat.store, "reserve", reserve)
    with start(fat) as session:
        assert session.run().state == "complete"


@pytest.mark.parametrize("phase", ["owner-lost", "terminal", "write-fails"])
def test_interrupt_cannot_claim_acknowledgment_without_persisting(fat, monkeypatch, phase):
    def preflight(proposal, check):
        if phase in {"owner-lost", "terminal"}:
            with fat.store._connection() as con:
                if phase == "owner-lost":
                    con.execute("DELETE FROM owners")
                else:
                    con.execute("UPDATE transactions SET state='invalidated' WHERE id=?", (fat.case.tx,))
        else:
            original = fat.store._connection
            @contextmanager
            def failed(*, write=True):
                if write:
                    raise t.TransferRefusal("STATE_BUSY")
                with original(write=False) as con:
                    yield con
            monkeypatch.setattr(fat.store, "_connection", failed)
        raise KeyboardInterrupt
    monkeypatch.setattr(fat.case.sources, "preflight", preflight, raising=False)
    code = {"owner-lost": "EXECUTION_FENCE_LOST", "terminal": "NOT_RESUMABLE", "write-fails": "STATE_BUSY"}[phase]
    with pytest.raises(t.TransferRefusal, match=code):
        start(fat)
    assert_unspent(fat)


def test_inherited_limit_refusal_does_not_spend_fat_and_can_retry(fat, monkeypatch):
    from modelark.slice.sources import FencedSources
    policy = qualified_policy()
    limits = [1 << 30, 1 << 30]
    monkeypatch.setattr(resource, "getrlimit", lambda which: tuple(limits))
    monkeypatch.setattr(resource, "setrlimit", lambda *a: pytest.fail("preflight changed parent limits"))
    monkeypatch.setattr(artifact_preflight, "available_memory", lambda: {"available_bytes": 16 << 30})
    # Exercise the real alternative/error aggregator with synthetic source IO.
    sources = FencedSources("unused", type("Reader", (), {"policy": policy})())
    @contextmanager
    def inspect(*args, **kwargs):
        try:
            artifact_preflight.inspect_original(io.BytesIO(header() + b"abcd"), compressed=True,
                                                expected_bytes=128, stored_bytes=36, policy=policy,
                                                check=kwargs["check"])
        except CodecResourceRefusal as exc:
            raise t.TransferRefusal("SOURCE_DECODE_RESOURCE", str(exc)) from exc
        yield
    monkeypatch.setattr(sources, "_open", inspect)
    monkeypatch.setattr(fat.case.sources, "preflight", sources.preflight, raising=False)
    result = start(fat)
    assert result.state == "blocked_source" and "inherited AS ceiling" in result.reason
    assert_unspent(fat)
    limits[:] = [resource.RLIM_INFINITY, resource.RLIM_INFINITY]
    with start(fat) as session:
        assert session.run().state == "complete"
