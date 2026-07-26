"""PR-09 / #39-B Gate 1: execution session lifecycle, fencing, uniqueness (B3–B6).

Tests-only. Expected red until session runtime writers land.
Exact state sets: live starting|running|stopping; resumable paused|blocked|stopped|failed;
not resumable: done. Resume never reactivates a row.
"""
from __future__ import annotations

import importlib
import sqlite3

from modelark.core import db

LIVE = frozenset({"starting", "running", "stopping"})
RESUMABLE = frozenset({"paused", "blocked", "stopped", "failed"})
NOT_RESUMABLE = frozenset({"done"})


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _sessions():
    for name in (
        "modelark.execution_session",
        "modelark.execution_sessions",
        "modelark.session",
        "modelark.execution",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if any(hasattr(mod, n) for n in (
                "start_session", "start", "resume_session", "resume",
                "session_write", "allocate_fencing_token")):
            return mod
    raise AssertionError(
        "execution session module required "
        "(modelark.execution_session / execution; expected Gate-1 red)")


def _require(mod, *names):
    for n in names:
        fn = getattr(mod, n, None)
        if callable(fn):
            return fn
    raise AssertionError(
        f"export one of {names} (expected Gate-1 red until session runtime)")


def test_live_resumable_done_state_sets_exported():
    mod = _sessions()
    live = getattr(mod, "LIVE_STATES", None) or getattr(mod, "live_states", None)
    resumable = getattr(mod, "RESUMABLE_STATES", None) or getattr(mod, "resumable_states", None)
    assert live is not None and set(live) == LIVE, f"LIVE_STATES must be {LIVE}; got {live}"
    assert resumable is not None and set(resumable) == RESUMABLE, (
        f"RESUMABLE_STATES must be {RESUMABLE}; got {resumable}")
    done = getattr(mod, "NOT_RESUMABLE_STATES", None) or getattr(mod, "terminal_done_states", None)
    if done is not None:
        assert set(done) == NOT_RESUMABLE or "done" in set(done)


def test_start_allocates_token_and_starting_row():
    mod = _sessions()
    start = _require(mod, "start_session", "start")
    con = _mem()
    # Minimal approved proposal row so FK can succeed when production inserts sessions.
    _seed_approved(con)
    session = start(
        con, plan_id="ark", proposal_id="prop-1",
        controller_identity="ctrl-1", bound_revision=0)
    sid = session["session_id"] if isinstance(session, dict) else session.session_id
    state = session["state"] if isinstance(session, dict) else session.state
    token = session["fencing_token"] if isinstance(session, dict) else session.fencing_token
    assert state == "starting"
    assert int(token) >= 1
    row = con.execute(
        "SELECT state, fencing_token, resumed_from_session_id FROM execution_sessions "
        "WHERE session_id=?", [sid]).fetchone()
    assert row is not None, "start must persist execution_sessions row"
    assert row[0] == "starting" and int(row[1]) == int(token) and row[2] is None


def test_global_live_uniqueness_refuses_second_start():
    mod = _sessions()
    start = _require(mod, "start_session", "start")
    con = _mem()
    _seed_approved(con)
    start(con, plan_id="ark", proposal_id="prop-1", controller_identity="c1", bound_revision=0)
    try:
        start(con, plan_id="ark", proposal_id="prop-1", controller_identity="c2", bound_revision=0)
        raise AssertionError("second live start must refuse")
    except Exception as exc:
        msg = str(exc).upper()
        assert any(k in msg for k in (
            "LIVE", "SESSION", "UNIQUE", "ACTIVE", "FILL", "REFUS")), exc


def test_resume_from_done_refuses():
    mod = _sessions()
    resume = _require(mod, "resume_session", "resume")
    con = _mem()
    _seed_approved(con)
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,terminal_at) "
        "VALUES('s-done','ark','prop-1','c1','w1','done',0,1,CURRENT_TIMESTAMP)")
    try:
        resume(con, predecessor_id="s-done", controller_identity="c1")
        raise AssertionError("resume from done must refuse")
    except Exception as exc:
        msg = str(exc).upper()
        assert any(k in msg for k in ("DONE", "NOT_RESUMABLE", "RESUME", "REFUS", "TERMINAL")), exc


def test_resume_from_paused_new_id_greater_token_immutable_predecessor():
    mod = _sessions()
    resume = _require(mod, "resume_session", "resume")
    con = _mem()
    _seed_approved(con)
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,terminal_at) "
        "VALUES('s-pause','ark','prop-1','c1','w1','paused',0,3,CURRENT_TIMESTAMP)")
    nxt = con.execute(
        "SELECT next_fencing_token FROM planner_state WHERE singleton_id=1").fetchone()[0]
    con.execute(
        "UPDATE planner_state SET next_fencing_token=? WHERE singleton_id=1",
        [max(int(nxt or 0), 3)])
    session = resume(con, predecessor_id="s-pause", controller_identity="c1")
    sid = session["session_id"] if isinstance(session, dict) else session.session_id
    token = int(session["fencing_token"] if isinstance(session, dict) else session.fencing_token)
    prev = session.get("resumed_from_session_id") if isinstance(session, dict) else (
        session.resumed_from_session_id)
    assert sid != "s-pause", "resume must allocate a new session id"
    assert token > 3, f"fencing token must be strictly greater than predecessor 3; got {token}"
    assert prev == "s-pause"
    pred = con.execute(
        "SELECT state FROM execution_sessions WHERE session_id='s-pause'").fetchone()
    assert pred[0] == "paused", "predecessor must remain immutable (not reactivated)"


def test_heartbeat_does_not_bump_planner_revision():
    mod = _sessions()
    start = _require(mod, "start_session", "start")
    heartbeat = _require(mod, "heartbeat", "session_heartbeat", "touch_heartbeat")
    con = _mem()
    _seed_approved(con)
    session = start(
        con, plan_id="ark", proposal_id="prop-1",
        controller_identity="c1", bound_revision=0)
    sid = session["session_id"] if isinstance(session, dict) else session.session_id
    token = session["fencing_token"] if isinstance(session, dict) else session.fencing_token
    before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    heartbeat(con, session_id=sid, fencing_token=token)
    after = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert after == before, "heartbeat must not bump planner_revision"


def test_session_write_validates_token_and_bumps_bound_revision():
    mod = _sessions()
    session_write = _require(mod, "session_write")
    start = _require(mod, "start_session", "start")
    con = _mem()
    _seed_approved(con)
    session = start(
        con, plan_id="ark", proposal_id="prop-1",
        controller_identity="c1", bound_revision=0)
    sid = session["session_id"] if isinstance(session, dict) else session.session_id
    token = session["fencing_token"] if isinstance(session, dict) else session.fencing_token

    def op(c):
        c.execute("UPDATE models SET numcopies=2 WHERE repo_id='org/m'")
        return True

    # Ensure model exists for the mutation
    con.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES('org/m',1)")
    before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    session_write(con, session_id=sid, fencing_token=token, operation=op)
    after = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert after == before + 1, "session_write graph mutation must bump planner_revision"
    try:
        session_write(con, session_id=sid, fencing_token=int(token) + 99, operation=op)
        raise AssertionError("wrong fencing token must refuse")
    except Exception as exc:
        msg = str(exc).upper()
        assert any(k in msg for k in ("TOKEN", "FENCE", "SESSION", "REFUS", "INVALID")), exc


def _seed_approved(con):
    con.execute("INSERT OR IGNORE INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    con.execute(
        "INSERT OR IGNORE INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
        "mutation_kind,mutation_args_json,serializer_version,capacity_mode,"
        "policy_version,solver_version,gate_b_code) "
        "VALUES('prop-1','ark',0,'approved',?,?,?,?,?,?,?,?)",
        ["a" * 64, "adopt_current", "[]", "1", "guaranteed", "1", "1", "FEASIBLE"])
    con.execute(
        "UPDATE planner_state SET active_approved_proposal_id='prop-1' WHERE singleton_id=1")


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            print(f"FAIL  {name}: {exc}")
            failed.append(name)
    print(f"\n{len(tests) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
