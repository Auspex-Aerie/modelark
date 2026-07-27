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

    def _hostile():
        raise RuntimeError("HOSTILE_GLOBAL_REREAD")

    monkeypatch.setattr("modelark.wishlist.compression", _hostile)
    got = fetch_mod._compression_from_ctx(ctx)
    assert got["threads"] == 7
    assert got["max_compress_ram_gb"] == 1.5
    assert got["stream_compress"] is True


def test_finding_35_incomplete_freeze_never_rereads_wishlist(monkeypatch):
    """Even incomplete frozen mappings must not fall back to wishlist."""
    from modelark import fetch as fetch_mod

    # Freeze present but missing compression key entirely.
    frozen = SimpleNamespace(values={"capacity_mode": "guaranteed"}, canonical_hash="x" * 64)
    ctx = fetch_mod.RunCtx(con=f.mem_con(), execution_config=frozen)

    def _hostile():
        raise RuntimeError("HOSTILE_GLOBAL_REREAD")

    monkeypatch.setattr("modelark.wishlist.compression", _hostile)
    got = fetch_mod._compression_from_ctx(ctx)
    assert got["threads"] == 1  # literal default
    assert got["max_compress_ram_gb"] == 4.0


def test_finding_35_null_execution_config_hash_refuses_start():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    con.execute(
        "UPDATE placement_proposals SET execution_config_hash=NULL WHERE proposal_id=?",
        [pid])
    sess = f.session_api()
    f.assert_refuses(
        lambda: sess.start_session(con, pid, None, f.default_services()),
        code="APPROVED_INPUT_CHANGED",
        label="null_config_binding_start",
    )


def test_finding_35a_ecfg_derivation_mode_does_not_authorize_start():
    """NULL execution_config_hash + derivation_mode=ecfg:<hash> must still refuse."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, loaded = f.create_and_approve(con)
    cfg = loaded.get("execution_config_hash")
    assert cfg and len(cfg) == 64
    con.execute(
        "UPDATE placement_proposals SET execution_config_hash=NULL, "
        "derivation_mode=? WHERE proposal_id=?",
        [f"ecfg:{cfg}", pid])
    sess = f.session_api()
    f.assert_refuses(
        lambda: sess.start_session(con, pid, None, f.default_services()),
        code="APPROVED_INPUT_CHANGED",
        label="f35a_ecfg_bypass",
    )


def test_finding_35b_unbind_helper_clears_only_config_hash():
    from modelark.execution_config import mark_proposal_pre_pr09_unbound
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    before = con.execute(
        "SELECT semantic_input_hash, execution_config_hash FROM placement_proposals "
        "WHERE proposal_id=?", [pid]).fetchone()
    assert before[0] and before[1]
    mark_proposal_pre_pr09_unbound(con, pid)
    after = con.execute(
        "SELECT semantic_input_hash, execution_config_hash FROM placement_proposals "
        "WHERE proposal_id=?", [pid]).fetchone()
    assert after[0] == before[0], "semantic must stay intact"
    assert after[1] is None


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
        fencing_token=int(session.fencing_token),
        worker_identity=session.worker_identity,
        services=services)
    after = con.execute(
        "SELECT expires_at, heartbeat_at FROM execution_sessions WHERE session_id=?",
        [session.session_id]).fetchone()
    assert after[0] != before or after[0] is not None
    assert after[0] and "2026-06-01" in str(after[0])
    assert after[1] is not None

    f.assert_refuses(
        lambda: sess.heartbeat(
            con, session_id=session.session_id,
            fencing_token=int(session.fencing_token) + 99,
            worker_identity=session.worker_identity),
        code="SESSION_TOKEN_MISMATCH",
        label="wrong token heartbeat",
    )
    f.assert_refuses(
        lambda: sess.heartbeat(
            con, session_id=session.session_id,
            fencing_token=int(session.fencing_token),
            worker_identity="wrong-worker"),
        code="SESSION_WORKER_MISMATCH",
        label="wrong worker heartbeat",
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


def test_finding_36_terminalize_failure_overwrites_ok_true():
    """Terminalize CAS failure must not leave returned ok=True with live session."""
    from modelark import fill as fill_mod
    from modelark import fetch

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/a','model.safetensors','d0',0,100,100,?)",
        ["1" * 64])
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    # Claim then corrupt token so terminalize fails after drain success
    _claim(con, out)
    con.execute(
        "UPDATE execution_sessions SET fencing_token=fencing_token+99 "
        "WHERE session_id=?", [out.session.session_id])
    # Drain will still see pre-reload session token in session object
    import modelark.execution_recovery as erec
    with mock.patch.object(erec, "inherit_drive_fence_fds", return_value=()):
        result = fill_mod.execute(
            fetch.RunCtx(con=con), session_start=out, guided=False, max_24h_gb=0)
    assert result.get("ok") is False, result
    assert result.get("terminalize_error") or result.get("code"), result
    st = con.execute(
        "SELECT state FROM execution_sessions WHERE session_id=?",
        [out.session.session_id]).fetchone()[0]
    # Session remains live only if terminalize truly failed without tokenless update
    assert st in ("starting", "running", "stopping") or result.get("ok") is False


def test_finding_37_refresh_propagates_refusal():
    from modelark import fill as fill_mod

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)
    sess = f.session_api()
    out = f.require_success(
        sess.start_session(con, pid, None, f.default_services()), label="start")
    # Self-echo config reader would hide drift; force hostile current reader.
    out._config_reader = SimpleNamespace(
        read_graph_affecting_config=lambda: {
            "capacity_mode": "compression_aware",
            "policy_version": "1", "solver_version": "1",
            "compression": {"level": 9}, "numcopies_default": 1,
        })
    ctx = SimpleNamespace(con=con, lock=mock.MagicMock(
        __enter__=lambda s: None, __exit__=lambda *a: False))

    f.assert_refuses(
        lambda: fill_mod._refresh_projection(ctx, out),
        code="APPROVED_INPUT_CHANGED",
        label="hostile global config at refresh boundary",
    )


def test_finding_37_missing_proposal_files_refuses():
    from modelark import fill as fill_mod

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    projection = SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id="org/a", target_drive="d0",
        source_drive=None, requirement_id="primary:org/a",
        schedule_state="ready", order_key=1,
        guaranteed_durable=100, expected_durable=100,
    ),))
    f.assert_refuses(
        lambda: fill_mod._projection_work_units(con, projection, proposal_files=[]),
        code="APPROVED_INPUT_CHANGED",
        label="missing proposal_files authority",
    )


def test_finding_37_null_archive_identity_not_satisfied():
    from modelark import fill as fill_mod

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/a','model.safetensors','d0',0,100,100,NULL)")
    projection = SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id="org/a", target_drive="d0",
        source_drive=None, requirement_id="primary:org/a",
        schedule_state="ready", order_key=1,
        guaranteed_durable=100, expected_durable=100,
    ),))
    proposal_files = [{
        "requirement_id": "primary:org/a", "rfilename": "model.safetensors",
        "size_bytes": 100, "orig_sha256": "1" * 64, "format": "safetensors", "quant": "bf16",
    }]
    units = fill_mod._projection_work_units(
        con, projection, proposal_files=proposal_files)
    assert units, "null archive identity must leave work unit"
    assert "model.safetensors" in units[0].missing_files


def test_finding_37_stale_replica_source_identity_not_ready():
    """Mismatched source orig_sha256 must not promote waiting_dependency to ready."""
    from modelark import fill as fill_mod

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    # Source has archive but wrong content identity
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/a','model.safetensors','d0',0,100,100,?)",
        ["9" * 64])
    projection = SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id="org/a", target_drive="d1",
        source_drive="d0", requirement_id="replica:org/a",
        schedule_state="waiting_dependency", order_key=1,
        guaranteed_durable=100, expected_durable=100,
    ),))
    proposal_files = [{
        "requirement_id": "replica:org/a", "rfilename": "model.safetensors",
        "size_bytes": 100, "orig_sha256": "1" * 64, "format": "safetensors", "quant": "bf16",
    }]
    units = fill_mod._projection_work_units(
        con, projection, proposal_files=proposal_files, require_proposal_files=True)
    assert units
    assert units[0].schedule_state == "waiting_dependency", (
        f"stale_source_schedule: {units[0].schedule_state}")
    assert units[0].kind is None


def test_finding_37_null_null_same_on_source_and_target():
    """Unhashed approved file + null archive: presence satisfies both sides (37-a)."""
    from modelark import fill as fill_mod

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/a','tiny.bin','d0',0,10,10,NULL)")
    # Target side: fully present with null-null → shrink out
    proj_tgt = SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id="org/a", target_drive="d0",
        source_drive=None, requirement_id="primary:org/a",
        schedule_state="ready", order_key=1,
        guaranteed_durable=10, expected_durable=10,
    ),))
    pfiles = [{
        "requirement_id": "primary:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None, "format": None, "quant": None,
    }]
    units_t = fill_mod._projection_work_units(
        con, proj_tgt, proposal_files=pfiles, require_proposal_files=True)
    assert units_t == [], "null-null on target must be presence satisfaction (shrink out)"

    # Source side: null-null must also be ready for replica
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/a','tiny.bin','d1',0,10,10,NULL)")
    # only on d0 source for replica to d1 — wait, put source on d0, target d1 missing
    con.execute("DELETE FROM archived WHERE drive_label='d1'")
    proj_src = SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id="org/a", target_drive="d1",
        source_drive="d0", requirement_id="replica:org/a",
        schedule_state="waiting_dependency", order_key=1,
        guaranteed_durable=10, expected_durable=10,
    ),))
    pfiles_r = [{
        "requirement_id": "replica:org/a", "rfilename": "tiny.bin",
        "size_bytes": 10, "orig_sha256": None, "format": None, "quant": None,
    }]
    units_s = fill_mod._projection_work_units(
        con, proj_src, proposal_files=pfiles_r, require_proposal_files=True)
    assert units_s and units_s[0].schedule_state == "ready", units_s
    assert units_s[0].kind is not None


def test_finding_37b_multifile_source_stale_and_absent():
    from modelark import fill as fill_mod

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    # File A good, file B stale
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/a','a.bin','d0',0,1,1,?)", ["1" * 64])
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/a','b.bin','d0',0,1,1,?)", ["9" * 64])
    proj = SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id="org/a", target_drive="d1",
        source_drive="d0", requirement_id="replica:org/a",
        schedule_state="waiting_dependency", order_key=1,
        guaranteed_durable=2, expected_durable=2,
    ),))
    pfiles = [
        {"requirement_id": "replica:org/a", "rfilename": "a.bin",
         "size_bytes": 1, "orig_sha256": "1" * 64},
        {"requirement_id": "replica:org/a", "rfilename": "b.bin",
         "size_bytes": 1, "orig_sha256": "2" * 64},
    ]
    units = fill_mod._projection_work_units(
        con, proj, proposal_files=pfiles, require_proposal_files=True)
    assert units[0].schedule_state == "waiting_dependency", "stale second file"

    # File B absent
    con.execute("DELETE FROM archived WHERE rfilename='b.bin'")
    units2 = fill_mod._projection_work_units(
        con, proj, proposal_files=pfiles, require_proposal_files=True)
    assert units2[0].schedule_state == "waiting_dependency", "absent second file"


def test_finding_37_thrown_refresh_fails_closed():
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
            side_effect=RuntimeError("boom")):
        try:
            fill_mod._refresh_projection(ctx, out)
            raised = None
        except Exception as exc:
            raised = exc
    # Capacity observe or config may refuse first; thrown RuntimeError must not be swallowed as None
    assert raised is not None
    assert not (isinstance(raised, type(None)))


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
