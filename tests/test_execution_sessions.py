"""PR-09 Gate 1: start_session lifecycle — no false greens; exact bound revision."""
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


def test_start_binds_exact_planner_revision_and_token():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    rev = int(con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])
    out = f.require_success(_start(mod, con, pid, None), label="start")
    session = f.session_fields(out)
    assert f.get_field(session, "state") == "starting"
    assert int(f.get_field(session, "fencing_token")) >= 1
    assert int(f.get_field(session, "bound_planner_revision")) == rev, (
        f"bound_planner_revision must equal current planner_revision {rev}, "
        f"got {f.get_field(session, 'bound_planner_revision')}")
    # Services must expose worker
    assert f.default_services().worker.identity


def test_start_refuses_while_current_proposal_review_is_pending():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    pending = f.proposal_mod().create_draft(
        con, plan_id="ark", mutation=("adopt_current", ())
    )

    f.assert_refuses(
        lambda: _start(mod, con, pid, None),
        code="PROPOSAL_REVIEW_PENDING",
        label="start while exact review is pending",
    )
    assert con.execute(
        "SELECT count(*) FROM execution_sessions "
        "WHERE state IN ('starting','running','stopping')"
    ).fetchone()[0] == 0
    assert con.execute(
        "SELECT lifecycle FROM placement_proposals WHERE proposal_id=?",
        [pending["proposal_id"]],
    ).fetchone()[0] == "draft"


def test_second_live_start_refuses_fill_session_active():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    f.require_success(_start(mod, con, pid, None), label="first start")
    f.assert_refuses(
        lambda: _start(mod, con, pid, None),
        code="FILL_SESSION_ACTIVE",
        label="second live start",
    )


def test_resume_from_each_resumable_state():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    for state in sorted(RESUMABLE):
        # Ensure no live row
        con.execute(
            "UPDATE execution_sessions SET state='stopped', "
            "terminal_at=COALESCE(terminal_at, CURRENT_TIMESTAMP) "
            "WHERE state IN ('starting','running','stopping')")
        pred = f"pred-{state}"
        con.execute("DELETE FROM execution_sessions WHERE session_id=?", [pred])
        con.execute(
            "INSERT INTO execution_sessions("
            "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
            "state,bound_planner_revision,fencing_token,terminal_at) "
            "VALUES(?,?,?,'ctrl','worker',?,0,4,CURRENT_TIMESTAMP)",
            [pred, "ark", pid, state])
        out = f.require_success(_start(mod, con, pid, pred), label=f"resume from {state}")
        session = f.session_fields(out)
        assert f.get_field(session, "session_id") != pred
        assert int(f.get_field(session, "fencing_token")) > 4
        assert f.get_field(session, "resumed_from_session_id") == pred
        pred_state = con.execute(
            "SELECT state FROM execution_sessions WHERE session_id=?", [pred]).fetchone()[0]
        assert pred_state == state


def test_resume_from_done_refuses():
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
    f.require_success(_start(mod, con, pid, "s-pause"), label="first successor")
    f.assert_refuses(
        lambda: _start(mod, con, pid, "s-pause"),
        code="FILL_SESSION_ACTIVE",
        label="competing successor",
    )


def test_worker_claim_requires_running_and_distinct_identity():
    mod = _mod()
    claim = getattr(mod, "claim_worker", None) or getattr(mod, "transition_to_running", None)
    assert callable(claim), "claim_worker required"
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    out = f.require_success(_start(mod, con, pid, None), label="start for claim")
    session = f.session_fields(out)
    sid = f.get_field(session, "session_id")
    token = f.get_field(session, "fencing_token")
    f.require_success(
        claim(con, session_id=sid, fencing_token=token,
              worker_identity="worker-pid-9", controller_identity="ctrl-A"),
        label="claim worker",
    )
    row = con.execute(
        "SELECT state, worker_identity, controller_identity FROM execution_sessions "
        "WHERE session_id=?", [sid]).fetchone()
    assert row[0] == "running"
    assert row[1] == "worker-pid-9"
    assert row[2] != row[1]


def test_heartbeat_only_when_running_validates_token():
    """RFC: heartbeat authority is running/stopping — not starting."""
    mod = _mod()
    heartbeat = getattr(mod, "heartbeat", None) or getattr(mod, "session_heartbeat", None)
    claim = getattr(mod, "claim_worker", None) or getattr(mod, "transition_to_running", None)
    assert callable(heartbeat) and callable(claim)
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    out = f.require_success(_start(mod, con, pid, None), label="start")
    session = f.session_fields(out)
    sid = f.get_field(session, "session_id")
    token = f.get_field(session, "fencing_token")
    # Heartbeat while starting must refuse
    f.assert_refuses(
        lambda: heartbeat(con, session_id=sid, fencing_token=token),
        code="SESSION_STATE_INVALID",
        label="heartbeat while starting",
    )
    f.require_success(
        claim(con, session_id=sid, fencing_token=token,
              worker_identity="w1", controller_identity="c1"),
        label="to running",
    )
    before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    f.require_success(
        heartbeat(con, session_id=sid, fencing_token=token), label="heartbeat running")
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
    claim = getattr(mod, "claim_worker", None) or getattr(mod, "transition_to_running", None)
    assert callable(terminalize) and callable(claim)
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    out = f.require_success(_start(mod, con, pid, None), label="start")
    session = f.session_fields(out)
    sid = f.get_field(session, "session_id")
    token = f.get_field(session, "fencing_token")
    f.require_success(
        claim(con, session_id=sid, fencing_token=token,
              worker_identity="w1", controller_identity="c1"),
        label="running",
    )
    before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    f.require_success(
        terminalize(con, session_id=sid, fencing_token=token, state="stopped",
                    terminal_code="OPERATOR_STOP"),
        label="terminalize",
    )
    after = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert after == before
    f.assert_refuses(
        lambda: terminalize(con, session_id=sid, fencing_token=token, state="running",
                            terminal_code=None),
        code="SESSION_TERMINAL_IMMUTABLE",
        label="reactivate terminal",
    )


def test_expired_recovery_succeeds_unexpired_refuses():
    mod = _mod()
    recover = getattr(mod, "recover_expired_session", None) or getattr(mod, "recover_session")
    assert callable(recover)
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    # Single expired live row only (no dual insert)
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('s-exp',?,?, 'c','w','running',0,7,'2000-01-01T00:00:00Z')",
        ["ark", pid])
    f.require_success(
        recover(con, session_id="s-exp", services=f.default_services()),
        label="recover expired",
    )
    # Unexpired alone
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


def test_start_failure_refuses_and_leaves_no_live_row():
    mod = _mod()
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    bad = f.default_services()
    bad.config = SimpleNamespace(read_graph_affecting_config=lambda: {"hostile": True})
    f.assert_refuses(
        lambda: _start(mod, con, pid, None, services=bad),
        code="APPROVED_INPUT_CHANGED",
        label="hostile config start",
    )
    live = con.execute(
        "SELECT count(*) FROM execution_sessions "
        "WHERE state IN ('starting','running','stopping')").fetchone()[0]
    assert live == 0, "failed start must leave zero live sessions"


def test_session_write_archive_path_not_numcopies():
    mod = _mod()
    session_write = getattr(mod, "session_write", None)
    claim = getattr(mod, "claim_worker", None) or getattr(mod, "transition_to_running", None)
    assert callable(session_write) and callable(claim)
    con = f.mem_con()
    pid, _ = _approve_ready(con)
    out = f.require_success(_start(mod, con, pid, None), label="start")
    session = f.session_fields(out)
    sid = f.get_field(session, "session_id")
    token = f.get_field(session, "fencing_token")
    f.require_success(
        claim(con, session_id=sid, fencing_token=token,
              worker_identity="w1", controller_identity="c1"),
        label="running",
    )

    def archive_op(c):
        c.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
            "stored_bytes,orig_sha256) VALUES('org/a','model.safetensors','d0',0,100,100,?)",
            ["1" * 64])
        return SimpleNamespace(proven_noop=False)

    before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    f.require_success(
        session_write(con, sid, token, archive_op), label="session_write archive")
    after = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0]
    assert after == before + 1
    assert con.execute(
        "SELECT count(*) FROM archived WHERE repo_id='org/a'").fetchone()[0] == 1
    f.assert_refuses(
        lambda: session_write(con, sid, int(token) + 1, archive_op),
        code="SESSION_TOKEN_MISMATCH",
        label="stale token session_write",
    )
