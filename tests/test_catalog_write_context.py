"""Real adapter transactions: revision markers are atomic, scoped and single-bump."""
from contextvars import copy_context
import sqlite3

import pytest

from modelark import catalog_write_context as writes
from modelark.execution_session import session_write
from modelark.proposal import GraphResult, Refusal, graph_write


GRAPH = writes.WriteIdentity("graph")
SESSION = writes.WriteIdentity("session", "test-session", 2)


@pytest.fixture
def con():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.executescript("""
        CREATE TABLE planner_state(singleton_id INTEGER PRIMARY KEY, planner_revision INTEGER, updated_at TEXT);
        INSERT INTO planner_state VALUES(1,10,NULL);
        CREATE TABLE execution_sessions(session_id TEXT PRIMARY KEY, state TEXT, fencing_token INTEGER,
            bound_planner_revision INTEGER, controller_identity TEXT, worker_identity TEXT);
        CREATE TABLE test_markers(id INTEGER PRIMARY KEY, revision INTEGER);
    """)
    yield connection
    connection.close()


def activate(con):
    con.execute("INSERT INTO execution_sessions VALUES('test-session','running',2,10,'test','test')")


def revision(con):
    return con.execute("SELECT planner_revision FROM planner_state").fetchone()[0]


def marker(con, owner, number=1):
    writes.require_write(con, owner)
    con.execute("INSERT INTO test_markers VALUES(?,NULL)", [number])

    def finish(rev):
        assert con.in_transaction
        assert revision(con) == rev
        if owner == SESSION:
            assert con.execute("SELECT bound_planner_revision FROM execution_sessions").fetchone()[0] == rev
        con.execute("UPDATE test_markers SET revision=? WHERE id=?", [rev, number])

    writes.defer_revision(con, owner, finish)


@pytest.mark.parametrize("mode", ["graph", "session"])
def test_two_markers_share_exactly_one_owned_revision(con, mode):
    if mode == "session":
        activate(con)
    statements = []
    con.set_trace_callback(statements.append)

    def op(c):
        marker(c, SESSION if mode == "session" else GRAPH, 1)
        marker(c, SESSION if mode == "session" else GRAPH, 2)
        return "result"

    result = session_write(con, "test-session", 2, op) if mode == "session" else graph_write(con, op)
    assert result == "result"
    assert revision(con) == 11
    assert con.execute("SELECT revision FROM test_markers").fetchall() == [(11,), (11,)]
    assert statements.count("BEGIN IMMEDIATE") == statements.count("COMMIT") == 1
    with pytest.raises(writes.WriteContextError, match="AUTHORITY_MISSING"):
        writes.require_write(con, SESSION if mode == "session" else GRAPH)


@pytest.mark.parametrize("mode", ["graph", "session"])
def test_late_marker_failure_rolls_back_data_markers_and_all_revisions(con, mode):
    owner = SESSION if mode == "session" else GRAPH
    if mode == "session":
        activate(con)

    def fail(_):
        raise RuntimeError("marker CAS conflict")

    def op(c):
        marker(c, owner)
        writes.defer_revision(c, owner, fail)

    with pytest.raises(RuntimeError, match="marker CAS conflict"):
        if mode == "session":
            session_write(con, "test-session", 2, op)
        else:
            graph_write(con, op)
    assert not con.in_transaction and revision(con) == 10
    assert not con.execute("SELECT * FROM test_markers").fetchall()
    if mode == "session":
        assert con.execute("SELECT bound_planner_revision FROM execution_sessions").fetchone()[0] == 10
    with pytest.raises(writes.WriteContextError):
        writes.require_write(con, owner)


def test_raw_transaction_is_not_owned_authority_and_remains_caller_owned(con):
    con.execute("BEGIN IMMEDIATE")
    with pytest.raises(writes.WriteContextError, match="AUTHORITY_MISSING"):
        marker(con, GRAPH)
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        graph_write(con, lambda c: marker(c, GRAPH))
    assert con.in_transaction
    con.execute("ROLLBACK")
    assert revision(con) == 10


def test_wrong_session_token_does_not_execute_callback(con):
    activate(con)
    called = []
    with pytest.raises(Refusal, match="SESSION_TOKEN_MISMATCH"):
        session_write(con, "test-session", 1, lambda c: called.append(c))
    assert not called and not con.in_transaction


def test_session_adapter_does_not_adopt_or_rollback_unrelated_transaction(con):
    activate(con)
    con.execute("BEGIN IMMEDIATE")
    con.execute("INSERT INTO test_markers VALUES(99,NULL)")
    with pytest.raises(sqlite3.OperationalError, match="within a transaction"):
        session_write(con, "test-session", 2, lambda c: marker(c, SESSION))
    assert con.in_transaction
    assert con.execute("SELECT id FROM test_markers").fetchall() == [(99,)]
    with pytest.raises(writes.WriteContextError):
        writes.require_write(con, SESSION)
    con.execute("ROLLBACK")


def test_session_cannot_be_relabelled_graph_or_another_token(con):
    activate(con)

    def op(c):
        for owner in (GRAPH, writes.WriteIdentity("session", "test-session", 1)):
            with pytest.raises(writes.WriteContextError, match="AUTHORITY_MISMATCH"):
                writes.require_write(c, owner)
        marker(c, SESSION)

    session_write(con, "test-session", 2, op)


def test_other_connection_not_authorized_by_ambient_context(con):
    other = sqlite3.connect(":memory:", isolation_level=None)
    try:
        other.execute("BEGIN IMMEDIATE")

        def op(c):
            with pytest.raises(writes.WriteContextError, match="AUTHORITY_MISSING"):
                writes.defer_revision(other, GRAPH, lambda _: None)
            marker(c, GRAPH)

        graph_write(con, op)
    finally:
        other.close()


@pytest.mark.parametrize("mode", ["graph", "session"])
def test_proven_noop_cannot_commit_new_marker(con, mode):
    owner = SESSION if mode == "session" else GRAPH
    if mode == "session":
        activate(con)
    run = (lambda op: session_write(con, "test-session", 2, op)) if mode == "session" else (lambda op: graph_write(con, op))
    def op(c):
        marker(c, owner)
        return GraphResult(proven_noop=True)

    with pytest.raises(writes.WriteContextError, match="NOOP_HAS_MARKERS"):
        run(op)
    assert revision(con) == 10 and not con.execute("SELECT * FROM test_markers").fetchall()
    run(lambda _: GraphResult(proven_noop=True))
    assert revision(con) == 10
    if mode == "session":
        assert con.execute("SELECT bound_planner_revision FROM execution_sessions").fetchone()[0] == 10


def test_finalizer_cannot_register_another_finalizer(con):
    def op(c):
        writes.defer_revision(c, GRAPH, lambda _: writes.defer_revision(c, GRAPH, lambda _: None))

    with pytest.raises(writes.WriteContextError, match="AUTHORITY_MISMATCH"):
        graph_write(con, op)
    assert revision(con) == 10


def test_copied_context_cannot_reuse_authority_after_adapter_exit(con):
    snapshots = []
    graph_write(con, lambda c: snapshots.append(copy_context()))
    con.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(writes.WriteContextError, match="AUTHORITY_MISSING"):
            snapshots[0].run(writes.require_write, con, GRAPH)
    finally:
        con.execute("ROLLBACK")


@pytest.mark.parametrize("mode", ["graph", "session"])
def test_same_finalizer_key_with_conflicting_binding_rolls_back_owning_write(con, mode):
    owner = SESSION if mode == "session" else GRAPH
    if mode == "session":
        activate(con)
    called = []
    def op(c):
        c.execute("INSERT INTO test_markers VALUES(1,NULL)")
        writes.defer_revision_once(c, owner, key=("operation", 1), binding=(10, False), finalize=called.append)
        writes.defer_revision_once(c, owner, key=("operation", 1), binding=(None, True), finalize=called.append)
    with pytest.raises(writes.WriteContextError, match="FINALIZER_CONFLICT"):
        if mode == "session":
            session_write(con, "test-session", 2, op)
        else:
            graph_write(con, op)
    assert called == [] and revision(con) == 10
    assert con.execute("SELECT count(*) FROM test_markers").fetchone()[0] == 0
    if mode == "session":
        assert con.execute("SELECT bound_planner_revision FROM execution_sessions").fetchone()[0] == 10
