"""PR-09 Gate-2 remediation regressions for findings 33–38."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import _pr09_gate1_fixtures as f
from modelark.core import db
from modelark.proposal import Refusal, graph_write, GraphResult


def _claim(con, out):
    sess = f.session_api()
    sess.claim_worker(
        con,
        session_id=out.session.session_id,
        fencing_token=out.session.fencing_token,
        worker_identity="w-f33",
        controller_identity="c-f33",
    )
    return sess.load_session(con, out.session.session_id) or out.session


def test_finding_33_live_envelope_succeeds_with_owner_fields(tmp_path):
    """Real drive_mutation envelope under claimed session is authorized."""
    from modelark import drive_mutation as dm
    from modelark import drive_fence

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    session = _claim(con, out)

    free = 10**12
    fp = "a" * 64

    def observe(label):
        return dm.Observation(
            identity_proven=True, free_bytes=free, filesystem_capacity=free,
            fingerprint=fp, identity_proof="t", fence_proof="t")

    def reconcile(label, paths, keys):
        return None

    saved = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    db.STATE_DIR = tmp_path / "state"
    try:
        # Use the same connection for session authority; fence namespace under tmp.
        with mock.patch.object(drive_fence, "hold_controller") as hc, \
                mock.patch.object(drive_fence, "hold_drives_sorted") as hd:
            @contextmanager
            def _ctrl(*a, **k):
                yield object()

            @contextmanager
            def _drives(*a, **k):
                h = mock.Mock()
                h.fileno.return_value = 3
                yield [h]

            hc.side_effect = lambda *a, **k: _ctrl()
            hd.side_effect = lambda *a, **k: _drives()
            with dm.drive_mutation(
                con, ["d0"], "fill",
                observe=observe, reconcile=reconcile,
                now=datetime.now(timezone.utc).isoformat(sep=" "),
                session_id=session.session_id,
                fencing_token=int(session.fencing_token),
            ) as writer:
                writer.record_touched("d0", paths=["org/a/model.safetensors"], keys=[])
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = saved

    row = con.execute(
        "SELECT generation, owner_session_id, owner_fencing_token "
        "FROM drive_dirty_generations WHERE drive_label='d0' "
        "ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] >= 2  # seed gen 1 + new dirty
    assert row[1] == session.session_id
    assert int(row[2]) == int(session.fencing_token)


def test_finding_34_cross_catalog_authority_does_not_leak():
    """Authorized session_write on catalog A must not authorize catalog B."""
    from modelark import execution_session as esess

    con_a = f.mem_con()
    con_b = f.mem_con()
    f.seed_plan_selection(con_a, repos=("org/a",))
    f.seed_plan_selection(con_b, repos=("org/b",))
    con_a.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    con_b.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid_a, _ = f.create_and_approve(con_a)
    # Live session only on A
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con_a, pid_a, None, f.default_services()), label="start A")
    session = _claim(con_a, out)

    # Live session on B as well so graph_write would need authority
    _p2, pid_b, _ = f.create_and_approve(con_b)
    out_b = f.require_success(
        sess.start_session(con_b, pid_b, None, f.default_services()), label="start B")
    _claim(con_b, out_b)

    unauthorized_cross = 0

    def write_a(c):
        nonlocal unauthorized_cross
        # Nested: while A's session_write is active, attempt B mutation.
        def op_b(cb):
            cb.execute(
                "INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES('org/leak',1)")
            return GraphResult(proven_noop=False)

        try:
            graph_write(con_b, op_b)
            unauthorized_cross += 1
        except Refusal as exc:
            assert exc.code == "FILL_SESSION_ACTIVE", exc
        return True

    esess.session_write(
        con_a, session.session_id, int(session.fencing_token), write_a)
    assert unauthorized_cross == 0, (
        f"unauthorized_cross_catalog_rows {unauthorized_cross}")
    assert con_b.execute(
        "SELECT count(*) FROM models WHERE repo_id='org/leak'").fetchone()[0] == 0


def test_finding_35_transport_uses_frozen_compression(monkeypatch):
    """fetch.run uses frozen ExecutionConfig compression, not a hostile global reread."""
    from modelark import fetch as fetch_mod
    from modelark.execution_config import ExecutionConfig

    frozen = ExecutionConfig.from_values({
        "capacity_mode": "guaranteed",
        "policy_version": "1",
        "solver_version": "1",
        "compression": {
            "max_compress_ram_gb": 1.5, "stream_compress": True, "threads": 7,
            "enabled": True, "codec": "streamznn", "level": 3,
        },
        "numcopies_default": 1,
    })
    ctx = fetch_mod.RunCtx(con=f.mem_con(), execution_config=frozen)

    # Hostile global: different threads / ram
    monkeypatch.setattr(
        "modelark.wishlist.compression",
        lambda: {"max_compress_ram_gb": 99.0, "stream_compress": False, "threads": 1},
    )
    got = fetch_mod._compression_from_ctx(ctx)
    assert got["threads"] == 7
    assert got["max_compress_ram_gb"] == 1.5
    assert got["stream_compress"] is True


def test_finding_35_derivation_mode_not_ecfg_hash():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, loaded = f.create_and_approve(con)
    assert not str(loaded.get("derivation_mode") or "").startswith("ecfg:")
    assert loaded.get("execution_config_hash") and len(loaded["execution_config_hash"]) == 64
    assert loaded.get("derivation_mode") in (
        "optimized", "state_truncated", "canonical_fallback", None)


def test_finding_36_heartbeat_renews_expiry_and_terminal_clears():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    services = f.default_services(lease_ttl=3600)
    out = f.require_success(
        sess.start_session(con, pid, None, services), label="start")
    session = _claim(con, out)
    before = con.execute(
        "SELECT expires_at FROM execution_sessions WHERE session_id=?",
        [session.session_id]).fetchone()[0]
    # Advance clock for renewal
    services.clock = SimpleNamespace(now=lambda: "2026-06-01T00:00:00Z")
    sess.heartbeat(
        con, session_id=session.session_id,
        fencing_token=int(session.fencing_token), services=services)
    after = con.execute(
        "SELECT expires_at, heartbeat_at FROM execution_sessions WHERE session_id=?",
        [session.session_id]).fetchone()
    assert after[0] != before or after[0] is not None
    assert after[0] and "2026-06-01" in str(after[0])
    assert after[1] is not None

    f.assert_refuses(
        lambda: sess.heartbeat(
            con, session_id=session.session_id,
            fencing_token=int(session.fencing_token) + 99),
        code="SESSION_TOKEN_MISMATCH",
        label="wrong token heartbeat",
    )

    sess.terminalize(
        con, session_id=session.session_id,
        fencing_token=int(session.fencing_token),
        state="done", terminal_code="PLAN_SATISFIED")
    term = con.execute(
        "SELECT state, heartbeat_at, expires_at FROM execution_sessions WHERE session_id=?",
        [session.session_id]).fetchone()
    assert term[0] == "done"
    assert term[1] is None and term[2] is None, term


def test_finding_36_terminalize_no_tokenless_fallback():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    session = _claim(con, out)
    f.assert_refuses(
        lambda: sess.terminalize(
            con, session_id=session.session_id,
            fencing_token=int(session.fencing_token) + 1,
            state="failed", terminal_code="X"),
        code="SESSION_TOKEN_MISMATCH",
        label="wrong token terminalize",
    )
    # Still live — not force-updated without token
    st = con.execute(
        "SELECT state FROM execution_sessions WHERE session_id=?",
        [session.session_id]).fetchone()[0]
    assert st in ("starting", "running")


def test_finding_37_refresh_propagates_refusal():
    from modelark import fill as fill_mod

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    ctx = SimpleNamespace(con=con, lock=mock.MagicMock(
        __enter__=lambda s: None, __exit__=lambda *a: False))

    with mock.patch(
            "modelark.execution_projection.project_pure",
            return_value=Refusal("APPROVED_INPUT_CHANGED", {"probe": True}, ())):
        try:
            fill_mod._refresh_projection(ctx, out)
            raised = None
        except Refusal as exc:
            raised = exc
    assert raised is not None and raised.code == "APPROVED_INPUT_CHANGED"


def test_finding_38_acceptance_rejects_missing_approved_structure(tmp_path):
    from modelark import execution_benchmark as bench

    path = tmp_path / "no_proposal.sqlite"
    con = sqlite3.connect(str(path))
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    for i in range(3):
        repo = f"org/m{i}"
        con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?,?,100,'safetensors','bf16',?)",
            [repo, "model.safetensors", f"{i:064d}"[:64].ljust(64, "0")])
        con.execute(
            "INSERT INTO selection(repo_id,finalized_at) VALUES(?, '2026-01-01')", [repo])
    con.commit()
    con.close()
    actual = bench.recompute_fixture_identity(path)
    desc = {
        **actual,
        "harness_generator_version": "gate2-f38",
        "sqlite_path": str(path),
        "requirement_count": actual["selected_repository_count"],
        "task_count": actual["selected_repository_count"],
    }
    f.assert_refuses(
        lambda: bench.run_acceptance_wall_clock(
            fixture_descriptor=desc,
            operator_approved_identity=dict(desc),
        ),
        code="ACCEPTANCE_FIXTURE_INVALID",
        label="no approved proposal structure",
    )
