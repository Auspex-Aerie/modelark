"""PR-09 Gate-2 regressions: successful-session hard cut, drift refuse, atomic token TX."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import _pr09_gate1_fixtures as f


def test_successful_session_execute_never_calls_reconcile_or_plan_capacity():
    """Finding 24: drain projection.tasks only — optimizer must not run on success path."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    from modelark import fill, capacity, reconcile, fetch

    calls = {"reconcile": 0, "plan_capacity": 0, "reconcile_plan": 0}

    def boom_reconcile(*a, **k):
        calls["reconcile"] += 1
        raise AssertionError("LEGACY_OPTIMIZER_CALLED")

    def boom_pc(*a, **k):
        calls["plan_capacity"] += 1
        raise AssertionError("LEGACY_OPTIMIZER_CALLED")

    def boom_rp(*a, **k):
        calls["reconcile_plan"] += 1
        raise AssertionError("LEGACY_OPTIMIZER_CALLED")

    import modelark.execution_recovery as erec
    with mock.patch.object(fill, "_reconcile", side_effect=boom_reconcile), \
         mock.patch.object(capacity, "plan_capacity", side_effect=boom_pc), \
         mock.patch.object(reconcile, "reconcile_plan", side_effect=boom_rp), \
         mock.patch.object(erec, "inherit_drive_fence_fds", return_value=()), \
         mock.patch.object(fill.fetch, "run", return_value={
             "stored_repos": ["org/a"], "failed_repos": [], "capacity_failure": None,
             "terminal_failure": None, "terminal_repo": None, "throttled": False,
             "stopped": False, "drive_unwritable": False, "gated_repos": [],
         }), \
         mock.patch.object(fill, "_await_drive", return_value=True), \
         mock.patch.object(fill, "_mounted", return_value=(True, True)):
        result = fill.execute(fetch.RunCtx(con=con), session_start=out, guided=True, max_24h_gb=0)
    assert calls["reconcile"] == 0 and calls["plan_capacity"] == 0 and calls["reconcile_plan"] == 0
    assert result.get("code") != "LEGACY_OPTIMIZER_CALLED"
    assert result["ok"] or result.get("code") in (
        "PLAN_SATISFIED", "WAITING_DEPENDENCY", None) or result.get("state") in (
        "done", "paused", "blocked")


def test_file_sha_drift_after_approval_refuses_start():
    """Finding 25: current manifests from catalog — SHA change refuses SessionStart."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    # Hostile catalog drift: change file SHA after approval.
    con.execute(
        "UPDATE files SET sha256=? WHERE repo_id='org/a'",
        ["9" * 64])
    sess = f.session_api()
    f.assert_refuses(
        lambda: sess.start_session(con, pid, None, f.default_services()),
        code="APPROVED_INPUT_CHANGED",
        label="sha drift after approval",
    )


def test_graph_affecting_config_drift_after_approval_refuses_start():
    """Finding 25: capacity_mode / config drift refuses start."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    services = f.default_services()
    services.config = SimpleNamespace(
        read_graph_affecting_config=lambda: {
            "capacity_mode": "compression_aware",
            "policy_version": "1",
            "solver_version": "1",
            "compression": {"enabled": True, "codec": "streamznn", "level": 3},
            "numcopies_default": 1,
        })
    sess = f.session_api()
    f.assert_refuses(
        lambda: sess.start_session(con, pid, None, services),
        code="APPROVED_INPUT_CHANGED",
        label="config drift after approval",
    )


def test_token_allocation_rolls_back_when_session_insert_fails():
    """Finding 25: token + session insert are one BEGIN IMMEDIATE — failed insert rolls back."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    before = int(con.execute(
        "SELECT next_fencing_token FROM planner_state WHERE singleton_id=1").fetchone()[0])
    sess = f.session_api()
    services = f.default_services()
    real_execute = con.execute

    def flaky(sql, *a, **k):
        text = sql if isinstance(sql, str) else str(sql)
        if "INSERT INTO execution_sessions" in text:
            raise RuntimeError("forced insert failure")
        return real_execute(sql, *a, **k)

    # Proxy connection so we can intercept INSERT
    class Proxy:
        def execute(self, sql, *a, **k):
            return flaky(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(con, name)

    try:
        sess.start_session(Proxy(), pid, None, services)
        raised = False
    except Exception:
        raised = True
    assert raised, "insert failure must surface"
    after = int(con.execute(
        "SELECT next_fencing_token FROM planner_state WHERE singleton_id=1").fetchone()[0])
    assert after == before, (
        f"token must not advance when session insert fails; before={before} after={after}")
    live = con.execute(
        "SELECT count(*) FROM execution_sessions "
        "WHERE state IN ('starting','running','stopping')").fetchone()[0]
    assert live == 0


def test_worker_claim_failure_fail_closed():
    """Finding 25: claim failure blocks execute — no silent continue."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    # Force starting state
    con.execute(
        "UPDATE execution_sessions SET state='starting', worker_identity=NULL "
        "WHERE session_id=?", [out.session.session_id])
    out.session.state = "starting"
    from modelark import fill, fetch
    from modelark.proposal import Refusal
    import modelark.execution_session as esess

    def boom_claim(*a, **k):
        raise Refusal("SESSION_TOKEN_MISMATCH", {}, ())

    with mock.patch.object(esess, "claim_worker", side_effect=boom_claim):
        result = fill.execute(
            fetch.RunCtx(con=con), session_start=out, guided=True, max_24h_gb=0)
    assert result["ok"] is False
    assert str(result.get("code") or "").upper() in (
        "SESSION_TOKEN_MISMATCH", "SESSION_CLAIM_FAILED")
