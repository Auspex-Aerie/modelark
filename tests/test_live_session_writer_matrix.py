"""PR-09 Gate 1: complete A3 writer matrix while live + multi-process exclusion."""
from __future__ import annotations

import multiprocessing as mp
import os
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

        draft = prop.create_draft(con, plan_id="ark", mutation=("adopt_current", ()))
        dpid = draft["proposal_id"] if isinstance(draft, dict) else draft
        _refuse_live("proposal.approve", lambda: prop.approve(con, dpid))

        con.close()
    finally:
        db.CATALOG_DIR = prev_dir
        db.DB_PATH = prev_path
        try:
            from modelark.web import data as web_data
            web_data._con = prev_web_con
        except Exception:
            pass


def _child_start(db_path: str, state_dir: str, q: mp.Queue):
    try:
        from modelark.core import db as core_db
        import _pr09_gate1_fixtures as fx
        core_db.CATALOG_DIR = Path(db_path).parent
        core_db.DB_PATH = Path(db_path)
        os.environ["MODELARK_STATE_DIR"] = state_dir
        con = core_db.connect()
        mod = fx.session_api()
        row = con.execute(
            "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
        ).fetchone()
        out = mod.start_session(con, row[0], None, fx.default_services())
        code = fx.refusal_code(out)
        if code == "FILL_SESSION_ACTIVE" or (
                isinstance(out, BaseException) and "FILL_SESSION_ACTIVE" in str(out)):
            q.put(("FILL_SESSION_ACTIVE",))
        elif fx.is_refusal(out):
            q.put(("other_refusal", code))
        else:
            q.put(("started", str(out)))
        con.close()
    except Exception as exc:
        code = getattr(exc, "code", None)
        if str(code) == "FILL_SESSION_ACTIVE" or "FILL_SESSION_ACTIVE" in str(exc):
            q.put(("FILL_SESSION_ACTIVE",))
        else:
            q.put(("error", f"{type(exc).__name__}:{exc}"))


def test_same_catalog_different_state_dirs_process_exclusion(tmp_path):
    catalog_dir = tmp_path / "shared"
    catalog_dir.mkdir()
    prev_dir, prev_path = db.CATALOG_DIR, db.DB_PATH
    try:
        db.CATALOG_DIR = catalog_dir
        db.DB_PATH = catalog_dir / "catalog.sqlite"
        con = db.connect()
        f.seed_plan_selection(con, repos=("org/a",))
        con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
        _p, pid, _ = f.create_and_approve(con)
        _start_live(con, pid)

        state_b = tmp_path / "state-b"
        state_b.mkdir()
        q: mp.Queue = mp.Queue()
        proc = mp.Process(target=_child_start, args=(str(db.DB_PATH), str(state_b), q))
        proc.start()
        proc.join(timeout=60)
        assert proc.exitcode == 0, f"child exit {proc.exitcode}"
        msg = q.get(timeout=5)
        assert msg[0] == "FILL_SESSION_ACTIVE", (
            f"child must refuse FILL_SESSION_ACTIVE, not arbitrary error: {msg}")
        con.close()
    finally:
        db.CATALOG_DIR = prev_dir
        db.DB_PATH = prev_path
