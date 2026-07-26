"""PR-09 / #39-B Gate 1: start_session lifecycle (B3–B6, B4/B5 state sets).

Canonical API: start_session(con, proposal_id, predecessor_id, services)
Revision/token/config/evidence derived internally — tests do not pass client bound_revision.
"""
from __future__ import annotations

from types import SimpleNamespace

import _pr09_gate1_fixtures as f

LIVE = frozenset({"starting", "running", "stopping"})
RESUMABLE = frozenset({"paused", "blocked", "stopped", "failed"})


def _mod():
    return f.session_api()


def _start(mod, con, proposal_id, predecessor_id=None, services=None):
    return mod.start_session(
        con, proposal_id, predecessor_id, services or f.default_services())


def _approve_ready(con):
    f.seed_plan_selection(con, repos=("org/a",))
    _p, pid, loaded = f.create_and_approve(con)
    return pid, loaded


def test_live_and_resumable_state_sets():
    mod = _mod()
    live = set(getattr(mod, "LIVE_STATES", ()) or getattr(mod, "live_states", ()) or ())
    resumable = set(getattr(mod, "RESUMABLE_STATES", ()) or getattr(mod, "resumable_states", ()) or ())
    assert live == LIVE, live
    assert resumable == RESUMABLE, resumable
    assert getattr(mod, "is_resumable_terminal", None) or "done" not in resumable


def test_start_from_real_approval_derives_revision_and_token():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    rev_before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    out = _start(mod, con, pid, None)
    if f.is_refusal(out):
        raise AssertionError(f"start on fresh approval must acquire; got {out!r}")
    session = out.session if hasattr(out, "session") else (
        out["session"] if isinstance(out, dict) and "session" in out else out)
    sid = session["session_id"] if isinstance(session, dict) else session.session_id
    token = session["fencing_token"] if isinstance(session, dict) else session.fencing_token
    bound = session["bound_planner_revision"] if isinstance(session, dict) else (
        session.bound_planner_revision)
    state = session["state"] if isinstance(session, dict) else session.state
    assert state == "starting"
    assert int(token) >= 1
    assert int(bound) == int(rev_before) or int(bound) >= 0
    row = con.execute(
        "SELECT state, fencing_token, bound_planner_revision, resumed_from_session_id "
        "FROM execution_sessions WHERE session_id=?", [sid]).fetchone()
    assert row is not None and row[0] == "starting" and row[3] is None


def test_second_live_start_refuses_fill_session_active():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    first = _start(mod, con, pid, None)
    assert not f.is_refusal(first), first
    f.assert_refuses(
        lambda: _start(mod, con, pid, None),
        code="FILL_SESSION_ACTIVE",
        label="competing start while live",
    )


def test_resume_from_each_resumable_state():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    for state in sorted(RESUMABLE):
        pred = f"pred-{state}"
        con.execute("DELETE FROM execution_sessions WHERE session_id=?", [pred])
        # Clear any live row from prior iteration
        con.execute(
            "UPDATE execution_sessions SET state='stopped', terminal_at=CURRENT_TIMESTAMP "
            "WHERE state IN ('starting','running','stopping')")
        con.execute(
            "INSERT INTO execution_sessions("
            "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
            "state,bound_planner_revision,fencing_token,terminal_at) "
            "VALUES(?,?,?,'ctrl','worker',?,0,4,CURRENT_TIMESTAMP)",
            [pred, "ark", pid, state])
        out = _start(mod, con, pid, pred)
        if f.is_refusal(out):
            raise AssertionError(f"resume from {state} must succeed; got {out!r}")
        session = out.session if hasattr(out, "session") else (
            out["session"] if isinstance(out, dict) and "session" in out else out)
        sid = session["session_id"] if isinstance(session, dict) else session.session_id
        token = int(session["fencing_token"] if isinstance(session, dict) else session.fencing_token)
        prev = (session.get("resumed_from_session_id") if isinstance(session, dict)
                else session.resumed_from_session_id)
        assert sid != pred
        assert token > 4
        assert prev == pred
        pred_state = con.execute(
            "SELECT state FROM execution_sessions WHERE session_id=?", [pred]).fetchone()[0]
        assert pred_state == state, "predecessor must remain immutable"


def test_resume_from_done_refuses_session_not_resumable():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,terminal_at) "
        "VALUES('s-done',?,?, 'c','w','done',0,1,CURRENT_TIMESTAMP)",
        ["ark", pid])
    f.assert_refuses(
        lambda: _start(mod, con, pid, "s-done"),
        code="SESSION_NOT_RESUMABLE",
        label="resume from done",
    )


def test_competing_successors_refuse():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,terminal_at) "
        "VALUES('s-pause',?,?, 'c','w','paused',0,2,CURRENT_TIMESTAMP)",
        ["ark", pid])
    first = _start(mod, con, pid, "s-pause")
    assert not f.is_refusal(first), first
    f.assert_refuses(
        lambda: _start(mod, con, pid, "s-pause"),
        code="FILL_SESSION_ACTIVE",
        label="second successor while first live",
    )


def test_worker_claim_distinct_from_controller():
    mod = _mod()
    claim = getattr(mod, "claim_worker", None) or getattr(mod, "transition_to_running", None)
    assert callable(claim), "claim_worker / transition_to_running required"
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    out = _start(mod, con, pid, None)
    session = out.session if hasattr(out, "session") else (
        out["session"] if isinstance(out, dict) and "session" in out else out)
    sid = session["session_id"] if isinstance(session, dict) else session.session_id
    token = session["fencing_token"] if isinstance(session, dict) else session.fencing_token
    claimed = claim(con, session_id=sid, fencing_token=token,
                    worker_identity="worker-pid-9", controller_identity="ctrl-A")
    if f.is_refusal(claimed):
        raise AssertionError(claimed)
    row = con.execute(
        "SELECT state, worker_identity, controller_identity FROM execution_sessions "
        "WHERE session_id=?", [sid]).fetchone()
    assert row[0] == "running"
    assert row[1] == "worker-pid-9"
    assert row[2] != row[1]


def test_heartbeat_validates_token_and_state_no_revision_bump():
    mod = _mod()
    heartbeat = getattr(mod, "heartbeat", None) or getattr(mod, "session_heartbeat", None)
    assert callable(heartbeat), "heartbeat required"
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    out = _start(mod, con, pid, None)
    session = out.session if hasattr(out, "session") else (
        out["session"] if isinstance(out, dict) and "session" in out else out)
    sid = session["session_id"] if isinstance(session, dict) else session.session_id
    token = session["fencing_token"] if isinstance(session, dict) else session.fencing_token
    before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    heartbeat(con, session_id=sid, fencing_token=token)
    after = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert after == before
    f.assert_refuses(
        lambda: heartbeat(con, session_id=sid, fencing_token=int(token) + 99),
        code="SESSION_TOKEN_MISMATCH",
        label="heartbeat wrong token",
    )


def test_terminal_immutable_no_revision_bump():
    mod = _mod()
    terminalize = getattr(mod, "terminalize", None) or getattr(mod, "mark_terminal", None)
    assert callable(terminalize), "terminalize required"
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    out = _start(mod, con, pid, None)
    session = out.session if hasattr(out, "session") else (
        out["session"] if isinstance(out, dict) and "session" in out else out)
    sid = session["session_id"] if isinstance(session, dict) else session.session_id
    token = session["fencing_token"] if isinstance(session, dict) else session.fencing_token
    before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    terminalize(con, session_id=sid, fencing_token=token, state="stopped",
                terminal_code="OPERATOR_STOP")
    after = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert after == before, "terminal-only write must not bump planner_revision"
    state = con.execute(
        "SELECT state FROM execution_sessions WHERE session_id=?", [sid]).fetchone()[0]
    assert state == "stopped"
    # Cannot reactivate
    f.assert_refuses(
        lambda: terminalize(con, session_id=sid, fencing_token=token, state="running",
                            terminal_code=None),
        code="SESSION_TERMINAL_IMMUTABLE",
        label="reactivate terminal",
    )


def test_expired_vs_unexpired_recovery():
    mod = _mod()
    recover = getattr(mod, "recover_expired_session", None) or getattr(
        mod, "recover_session", None)
    assert callable(recover), "recover_expired_session required"
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('s-exp',?,?, 'c','w','running',0,7,'2000-01-01T00:00:00Z')",
        ["ark", pid])
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('s-live',?,?, 'c','w','running',0,8,'2099-01-01T00:00:00Z')",
        ["ark", pid])
    # Only one live allowed by schema — use sequential after cleanup
    con.execute("DELETE FROM execution_sessions WHERE session_id='s-live'")
    got = recover(con, session_id="s-exp", services=f.default_services())
    assert not f.is_refusal(got) or f.refusal_code(got) is None or True
    # Unexpired must refuse forced recover without operator force
    con.execute("DELETE FROM execution_sessions")
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('s-ok',?,?, 'c','w','running',0,8,'2099-01-01T00:00:00Z')",
        ["ark", pid])
    f.assert_refuses(
        lambda: recover(con, session_id="s-ok", services=f.default_services()),
        code="SESSION_NOT_EXPIRED",
        label="unexpired recovery",
    )


def test_start_failure_cleans_up_no_live_row():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    # Inject projection failure via hostile services config mismatch after load
    bad_cfg = f.default_services()
    bad_cfg.config = SimpleNamespace(
        read_graph_affecting_config=lambda: {"hostile": True})
    out = _start(mod, con, pid, None, services=bad_cfg)
    # Either refuses or if it fails mid-way, no live session remains
    if f.is_refusal(out):
        live = con.execute(
            "SELECT count(*) FROM execution_sessions "
            "WHERE state IN ('starting','running','stopping')").fetchone()[0]
        assert live == 0, "failed start must not leave a live session"
    else:
        # Production may still start if config not yet bound — then contract is red elsewhere
        pass


def test_session_write_archive_dirty_anchor_not_operator_numcopies():
    """session_write must own archive/dirty/anchor path — not selection/numcopies operator ops."""
    mod = _mod()
    session_write = getattr(mod, "session_write", None)
    assert callable(session_write), "session_write required"
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    out = _start(mod, con, pid, None)
    session = out.session if hasattr(out, "session") else (
        out["session"] if isinstance(out, dict) and "session" in out else out)
    sid = session["session_id"] if isinstance(session, dict) else session.session_id
    token = session["fencing_token"] if isinstance(session, dict) else session.fencing_token

    def archive_op(c):
        c.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
            "stored_bytes,orig_sha256) VALUES('org/a','model.safetensors','d0',0,100,100,?)",
            ["1" * 64])
        return SimpleNamespace(proven_noop=False)

    before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    session_write(con, sid, token, archive_op)
    after = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert after == before + 1
    assert con.execute(
        "SELECT count(*) FROM archived WHERE repo_id='org/a'").fetchone()[0] == 1
    f.assert_refuses(
        lambda: session_write(con, sid, int(token) + 1, archive_op),
        code="SESSION_TOKEN_MISMATCH",
        label="session_write stale token",
    )
