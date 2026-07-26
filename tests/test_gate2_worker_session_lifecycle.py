"""PR-09 Gate-2: live worker session_write, terminal cleanup, config/baseline authority."""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import _pr09_gate1_fixtures as f


def test_live_worker_session_write_allows_archived_insert():
    """Finding 31: RunCtx.write under live session uses session_write, not FILL_SESSION_ACTIVE."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    sess.claim_worker(
        con, session_id=out.session.session_id,
        fencing_token=out.session.fencing_token,
        worker_identity="w-live", controller_identity="c-live")
    from modelark.fetch import RunCtx
    ctx = RunCtx(
        con=con,
        session_id=out.session.session_id,
        fencing_token=int(out.session.fencing_token),
    )

    def insert_archived(c):
        c.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
            "stored_bytes,orig_sha256) VALUES('org/a','model.safetensors','d0',0,100,100,?)",
            ["1" * 64])
        return True

    # Must not raise FILL_SESSION_ACTIVE
    assert ctx.write(insert_archived) is True
    assert con.execute(
        "SELECT count(*) FROM archived WHERE repo_id='org/a'").fetchone()[0] == 1


def test_live_worker_graph_write_without_session_token_refuses():
    """Unauthorized graph_write while live still refuses FILL_SESSION_ACTIVE."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    from modelark.proposal import graph_write, GraphResult, Refusal

    def op(c):
        c.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES('org/z',1)")
        return GraphResult(proven_noop=False)

    try:
        graph_write(con, op)
        raised = None
    except Refusal as exc:
        raised = exc
    assert raised is not None and raised.code == "FILL_SESSION_ACTIVE"


def test_execute_terminalizes_satisfied_projection():
    """Finding 32: PLAN_SATISFIED leaves session terminal (done), not running."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    # Pre-satisfy so projection drains empty
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/a','model.safetensors','d0',0,100,100,?)",
        ["1" * 64])
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    from modelark import fill, fetch
    import modelark.execution_recovery as erec
    with mock.patch.object(erec, "inherit_drive_fence_fds", return_value=()):
        result = fill.execute(
            fetch.RunCtx(con=con), session_start=out, guided=False, max_24h_gb=0)
    assert result.get("code") == "PLAN_SATISFIED" or result.get("ok")
    row = con.execute(
        "SELECT state FROM execution_sessions WHERE session_id=?",
        [out.session.session_id]).fetchone()
    assert row is not None
    assert row[0] not in ("starting", "running", "stopping"), row
    assert row[0] in ("done", "stopped", "paused", "blocked", "failed"), row


def test_compression_config_drift_refuses_start():
    """Finding 25: complete config hash includes compression — level change refuses."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, loaded = f.create_and_approve(con)
    # Finding 35: config binding is execution_config_hash; derivation_mode is placement audit.
    row = con.execute(
        "SELECT derivation_mode, execution_config_hash FROM placement_proposals "
        "WHERE proposal_id=?",
        [pid]).fetchone()
    assert row is not None, pid
    dm, cfg_hash = row[0], row[1]
    assert dm in (None, "optimized", "state_truncated", "canonical_fallback") or (
        isinstance(dm, str) and not str(dm).startswith("ecfg:")), dm
    assert cfg_hash and len(str(cfg_hash)) == 64, cfg_hash
    services = f.default_services()
    services.config = SimpleNamespace(
        read_graph_affecting_config=lambda: {
            "capacity_mode": "guaranteed",
            "policy_version": "1",
            "solver_version": "1",
            "compression": {"enabled": True, "codec": "streamznn", "level": 9},
            "numcopies_default": 1,
        })
    sess = f.session_api()
    f.assert_refuses(
        lambda: sess.start_session(con, pid, None, services),
        code="APPROVED_INPUT_CHANGED",
        label="compression config drift",
    )


def test_baseline_archive_loss_refuses_start():
    """Finding 25: removing archived copy for baseline_satisfied refuses start."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/b",), with_archive_on=[("org/b", "d0")])
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, proposal = f.create_and_approve(con)
    # Ensure we have a baseline row; if all executable, force by deleting archived after approve
    baselines = [
        t for t in (proposal.get("tasks") or ())
        if t.get("row_kind") == "baseline_satisfied"
    ]
    if not baselines:
        # Approve path may produce executable only; still delete archive and change task?
        # If no baseline, seed a synthetic baseline on the stored proposal.
        con.execute(
            "UPDATE proposal_tasks SET row_kind='baseline_satisfied', "
            "satisfying_drive='d0', target_drive=NULL, "
            "baseline_certificate=? WHERE proposal_id=? AND repo_id='org/b'",
            ["c" * 64, pid])
    con.execute("DELETE FROM archived WHERE repo_id='org/b'")
    sess = f.session_api()
    f.assert_refuses(
        lambda: sess.start_session(con, pid, None, f.default_services()),
        code="APPROVAL_PROJECTION_VIOLATION",
        label="baseline archive loss",
    )
