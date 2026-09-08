"""Shared attempt invariants and the existing Fill persistence adapter boundary."""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace

import pytest

from modelark import execution_authority as authority
from modelark import execution_recovery, execution_session
from modelark.proposal import Refusal


@pytest.mark.parametrize("token", [7, "attempt-7"])
def test_exact_attempt_in_allowed_state_is_current(token):
    authority.require_current(authority.Attempt("owner", token),
                              authority.Attempt("owner", token), "running", {"running"})


@pytest.mark.parametrize("actual", [authority.Attempt("other", 7),
                                  authority.Attempt("owner", 8),
                                  authority.Attempt("owner", "7")])
def test_same_state_does_not_authorize_another_owner_or_attempt(actual):
    with pytest.raises(authority.AuthorityLost):
        authority.require_current(authority.Attempt("owner", 7), actual, "running", {"running"})


def test_same_attempt_does_not_authorize_a_terminal_state():
    with pytest.raises(authority.AuthorityLost):
        authority.require_current(authority.Attempt("owner", 7),
                                  authority.Attempt("owner", 7), "done", {"running"})


def _connection():
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute("CREATE TABLE execution_sessions(session_id TEXT PRIMARY KEY, state TEXT,"
                "fencing_token INTEGER, bound_planner_revision INTEGER, expires_at TEXT,"
                "approved_proposal_id TEXT, terminal_code TEXT, terminal_at TEXT)")
    con.execute("INSERT INTO execution_sessions VALUES('fill','running',7,0,"
                "'2000-01-01T00:00:00Z','proposal',NULL,NULL)")
    return con


def test_fill_graph_write_checks_shared_attempt_inside_transaction(monkeypatch):
    con = _connection()
    seen = []
    original = authority.require_current

    def check(expected, actual, state, allowed_states):
        assert con.in_transaction
        seen.append((expected, actual, state))
        original(expected, actual, state, allowed_states)

    monkeypatch.setattr(authority, "require_current", check)
    monkeypatch.setattr(execution_session, "bump_revision", lambda connection: 1)
    try:
        assert execution_session.session_write(con, "fill", 7, lambda connection: "written") == "written"
        assert seen == [(authority.Attempt("fill", 7), authority.Attempt("fill", 7), "running")]
        with pytest.raises(Refusal) as error:
            execution_session.session_write(con, "fill", 8, lambda connection: pytest.fail("stale writer"))
        assert error.value.code == "SESSION_TOKEN_MISMATCH"
    finally:
        con.close()


@pytest.mark.parametrize("change", ["fencing_token=8", "state='done'"])
def test_fill_graph_write_rejects_authority_changed_after_preflight(change):
    con = _connection()

    class RacingConnection:
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                con.execute("UPDATE execution_sessions SET " + change)
            return con.execute(sql, *args)

    try:
        with pytest.raises(Refusal) as error:
            execution_session.session_write(
                RacingConnection(), "fill", 7, lambda connection: pytest.fail("lost authority"))
        assert error.value.code == "SESSION_TOKEN_MISMATCH"
        assert not con.in_transaction
    finally:
        con.close()


@pytest.mark.parametrize(("change", "code"), [
    ("fencing_token=8", "SESSION_TOKEN_MISMATCH"),
    ("state='done'", "SESSION_STATE_INVALID"),
])
def test_fill_recovery_rejects_authority_changed_before_fenced_transaction(monkeypatch, change, code):
    con = _connection()

    @contextmanager
    def controller():
        con.execute("UPDATE execution_sessions SET " + change)
        yield

    @contextmanager
    def drives(*args):
        yield

    monkeypatch.setattr(execution_recovery, "child_fence_still_held", lambda **kw: False)
    monkeypatch.setattr(execution_recovery, "owned_dirty_generations", lambda *args, **kw: ())
    services = SimpleNamespace(controller_flock=SimpleNamespace(hold=controller),
                               drive_fences=SimpleNamespace(hold_all_sorted=drives))
    try:
        with pytest.raises(Refusal) as error:
            execution_recovery.recover_expired_session(con, session_id="fill", services=services)
        assert error.value.code == code
        assert not con.in_transaction
    finally:
        con.close()


def test_fill_recovery_checks_shared_attempt_under_fences_and_transaction(monkeypatch):
    con = _connection()
    held = []
    seen = []
    original = authority.require_current

    @contextmanager
    def hold(*args):
        held.append(True)
        try:
            yield
        finally:
            held.pop()

    def check(expected, actual, state, allowed_states):
        assert con.in_transaction and len(held) == 2
        seen.append((expected, actual, state))
        original(expected, actual, state, allowed_states)

    monkeypatch.setattr(authority, "require_current", check)
    monkeypatch.setattr(execution_recovery, "child_fence_still_held", lambda **kw: False)
    monkeypatch.setattr(execution_recovery, "owned_dirty_generations", lambda *args, **kw: ())
    monkeypatch.setattr(execution_recovery, "release_child_fences", lambda *args: None)
    services = SimpleNamespace(controller_flock=SimpleNamespace(hold=hold),
                               drive_fences=SimpleNamespace(hold_all_sorted=hold))
    try:
        assert execution_recovery.recover_expired_session(con, session_id="fill", services=services)
        assert len(seen) == 2
        assert all(item == (authority.Attempt("fill", 7), authority.Attempt("fill", 7), "running")
                   for item in seen)
        assert con.execute("SELECT state,terminal_code FROM execution_sessions").fetchone() == (
            "failed", "EXPIRED_RECOVERED")
    finally:
        con.close()
