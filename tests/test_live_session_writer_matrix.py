"""PR-09 Gate 1: complete A3 writer matrix while live + catalog/state-dir exclusion.

No production multiprocessing and no fork/spawn selection in PR-09.

Gate 1: independent SQLite connections + distinct state_dir service inputs with exact
FILL_SESSION_ACTIVE (covers “second portal” as another catalog client, not a new API).

Gate 2 (not this file): exec-style cold installed process — start one real portal/session,
launch the same installed portal or CLI entrypoint independently against the same catalog
with another state directory, require exact FILL_SESSION_ACTIVE.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import _pr09_gate1_fixtures as f
from modelark.core import db


def _start_live(con, pid):
    mod = f.session_api()
    return f.require_success(
        mod.start_session(con, pid, None, f.default_services()), label="start live")


def _refuse_live(label, call):
    f.assert_refuses(call, code="FILL_SESSION_ACTIVE", label=label)


def test_complete_a3_writer_matrix_while_live(tmp_path):
    prop = f.proposal_mod()
    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir()
    prev_dir, prev_path = db.CATALOG_DIR, db.DB_PATH
    prev_web_con = None
    try:
        db.CATALOG_DIR = catalog_dir
        db.DB_PATH = catalog_dir / "catalog.sqlite"
        con = db.connect()
        f.seed_plan_selection(con, repos=("org/a",))
        con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
        _p, pid, _ = f.create_and_approve(con)
        _start_live(con, pid)

        from modelark.web import selection_api, data as web_data
        prev_web_con = getattr(web_data, "_con", None)
        web_data._con = con
        with mock.patch.object(web_data, "conn", return_value=con), \
             mock.patch.object(web_data, "_lock", mock.MagicMock()):
            for name, fn, kwargs in (
                ("selection_api.finalize", selection_api.finalize, {"repo_id": "org/x"}),
                ("selection_api.clear", selection_api.clear, {}),
                ("selection_api.toggle", selection_api.toggle, {"repo_id": "org/a"}),
                ("selection_api.bulk", selection_api.bulk,
                 {"repo_ids": ["org/a"], "op": "remove"}),
            ):
                try:
                    _refuse_live(name, lambda fn=fn, kwargs=kwargs: fn(kwargs))
                except TypeError:
                    _refuse_live(name, lambda fn=fn, kwargs=kwargs: fn(**kwargs))

        from modelark import plan
        _refuse_live("plan.create", lambda: plan.create(con, "p-live", name="L"))
        _refuse_live("plan.set_capacity_mode",
                     lambda: plan.set_capacity_mode(con, "ark", "compression_aware"))
        con.execute(
            "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "lifecycle,eligibility,identity_epoch,identity_fingerprint,write_authority,"
            "filesystem_capacity_bytes) "
            "VALUES('d9',1000,900,'replica',0,'active','enabled',1,?,'dedicated_local',1000)",
            ["c" * 64])
        _refuse_live("plan.add_drive", lambda: plan.add_drive(con, "ark", "d9"))
        _refuse_live("plan.remove_drive", lambda: plan.remove_drive(con, "ark", "d0"))
        _refuse_live("plan.set_active", lambda: plan.set_active(con, "ark"))
        _refuse_live("plan.bootstrap", lambda: plan.bootstrap(con, "ark"))

        def op(c):
            c.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES('org/z',1)")
            return SimpleNamespace(proven_noop=False)

        _refuse_live("proposal.graph_write", lambda: prop.graph_write(con, op))

        _refuse_live(
            "db.replace_files",
            lambda: db.replace_files(con, "org/a", [{
                "rfilename": "model.safetensors", "size_bytes": 101,
                "format": "safetensors", "quant": "bf16", "sha256": "2" * 64}]))

        from modelark import discover
        api = mock.Mock()
        api.model_info.side_effect = RuntimeError("should not reach hub")
        _refuse_live(
            "discover.discover_one",
            lambda: discover.discover_one(api, con, "org/new"))

        from modelark import cli
        args = SimpleNamespace(repo_id="org/a", numcopies=2, con=con)
        assert hasattr(cli, "cmd_protect"), "cli.cmd_protect required in A3 matrix"
        _refuse_live("cli.cmd_protect", lambda: cli.cmd_protect(args))

        from modelark import drive_mutation
        assert hasattr(drive_mutation, "begin_generation")
        _refuse_live(
            "drive_mutation.begin_generation",
            lambda: drive_mutation.begin_generation(
                con, "d0", identity_epoch=1, operation_code="test"))
        assert hasattr(drive_mutation, "publish_clean_anchor")
        _refuse_live(
            "drive_mutation.publish_clean_anchor",
            lambda: drive_mutation.publish_clean_anchor(
                con, "d0", identity_epoch=1, generation=1,
                anchor_free_bytes=1000, filesystem_capacity_bytes=1000,
                identity_fingerprint=f.DRIVE_IDS["d0"]["fingerprint"],
                write_authority="dedicated_local",
                identity_proof="p", fence_proof="p",
                observed_at="2026-01-01T00:00:00Z"))

        from modelark import drive_bootstrap
        assert hasattr(drive_bootstrap, "reconcile_drive")
        _refuse_live(
            "drive_bootstrap.reconcile_drive",
            lambda: drive_bootstrap.reconcile_drive(
                con, "d0", now="2026-01-01T00:00:00Z"))

        from modelark import register
        assert hasattr(register, "register_drive")
        _refuse_live(
            "register.register_drive",
            lambda: register.register_drive(con, "d-new", path=str(tmp_path / "drv")))

        from modelark import hash_repair
        assert hasattr(hash_repair, "repair_hashes")
        _refuse_live("hash_repair.repair_hashes", lambda: hash_repair.repair_hashes(con))

        from modelark import fetch
        ctx = fetch.RunCtx(con=con)

        def write_archived(c):
            c.execute(
                "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
                "stored_bytes,orig_sha256) VALUES('org/a','x.bin','d0',0,1,1,?)",
                ["3" * 64])
            return None

        _refuse_live("fetch.RunCtx.write", lambda: ctx.write(write_archived))

        _refuse_live(
            "proposal.create_draft",
            lambda: prop.create_draft(con, plan_id="ark", mutation=("adopt_current", ())),
        )

        con.close()
    finally:
        db.CATALOG_DIR = prev_dir
        db.DB_PATH = prev_path
        try:
            from modelark.web import data as web_data
            web_data._con = prev_web_con
        except Exception:
            pass


def test_same_catalog_independent_connections_different_state_dirs():
    """Gate-1 exclusion: two independent connections + distinct state_dir inputs.

    No multiprocessing. Live session on connection A must make start_session on
    connection B refuse with exact FILL_SESSION_ACTIVE even when state directories differ.
    Cold multi-process smoke is Gate-2 (installed CLI/portal), not PR-09 Gate-1.
    """
    import sqlite3
    from modelark.core import db as core_db

    # Shared on-disk catalog; two independent Connection objects (not shared memory).
    with __import__("tempfile").TemporaryDirectory() as td:
        catalog = Path(td) / "catalog.sqlite"
        # Build catalog via schema apply (not process-global db.DB_PATH mutation for peers).
        con_a = sqlite3.connect(str(catalog), isolation_level=None)
        for stmt in core_db._statements(core_db.SCHEMA_PATH.read_text()):
            con_a.execute(stmt)
        if con_a.execute(
                "SELECT count(*) FROM planner_state WHERE singleton_id=1").fetchone()[0] == 0:
            con_a.execute(
                "INSERT INTO planner_state(singleton_id,planner_revision,"
                "active_approved_proposal_id,next_fencing_token) VALUES(1,0,NULL,0)")
        f.seed_plan_selection(con_a, repos=("org/a",))
        con_a.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
        _p, pid, _ = f.create_and_approve(con_a)

        state_a = Path(td) / "state-a"
        state_b = Path(td) / "state-b"
        state_a.mkdir()
        state_b.mkdir()
        svc_a = f.default_services()
        svc_a.state_dir = str(state_a)
        svc_b = f.default_services()
        svc_b.state_dir = str(state_b)
        # Distinct controller identities simulate two portals on one catalog.
        svc_a.worker = SimpleNamespace(identity="worker-a", claim=lambda **k: None)
        svc_b.worker = SimpleNamespace(identity="worker-b", claim=lambda **k: None)

        mod = f.session_api()
        f.require_success(
            mod.start_session(con_a, pid, None, svc_a),
            label="start on connection A / state-a",
        )

        con_b = sqlite3.connect(str(catalog), isolation_level=None)
        f.assert_refuses(
            lambda: mod.start_session(con_b, pid, None, svc_b),
            code="FILL_SESSION_ACTIVE",
            label="connection B / state-b must see live session on same catalog",
        )
        con_b.close()
        con_a.close()
