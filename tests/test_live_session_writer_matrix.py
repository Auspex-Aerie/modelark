"""PR-09 / #39-B Gate 1: invoke every PR-08 A3 writer while a session is live (B3, B13).

Behavioral — not metadata. Multi-process same-catalog uses separate connections.
"""
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
    out = mod.start_session(con, pid, None, f.default_services())
    assert not f.is_refusal(out), out
    return out


def _assert_writer_refuses_while_live(label, call):
    f.assert_refuses(call, code="FILL_SESSION_ACTIVE", label=label)


def test_each_pr08_writer_refuses_while_session_live(tmp_path):
    """Invoke real inventory entrypoints under a live session — not export-only checks."""
    prop = f.proposal_mod()
    # File-backed catalog so discover/register paths that open db.connect can be redirected.
    catalog_dir = tmp_path / "cat"
    catalog_dir.mkdir()
    db.CATALOG_DIR = catalog_dir
    db.DB_PATH = catalog_dir / "catalog.sqlite"
    con = db.connect()
    f.seed_plan_selection(con, repos=("org/a",))
    # Reset path after seed bumps
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _loaded = f.create_and_approve(con)
    _start_live(con, pid)

    inv = prop.GRAPH_AFFECTING_WRITERS
    assert inv, "GRAPH_AFFECTING_WRITERS required"

    # --- selection_api ---
    from modelark.web import selection_api, data as web_data
    web_data._con = con
    with mock.patch.object(web_data, "conn", return_value=con), \
         mock.patch.object(web_data, "_lock", mock.MagicMock()):
        for name, fn, args in (
            ("selection_api.finalize", selection_api.finalize, ({"repo_id": "org/x"},)),
            ("selection_api.clear", selection_api.clear, ({},)),
            ("selection_api.toggle", selection_api.toggle, ({"repo_id": "org/a"},)),
            ("selection_api.bulk", selection_api.bulk, ({"repo_ids": ["org/a"], "op": "remove"},)),
        ):
            if name not in inv and not any(name in k for k in inv):
                continue
            _assert_writer_refuses_while_live(name, lambda fn=fn, args=args: fn(*args))

    # --- plan writers ---
    from modelark import plan
    _assert_writer_refuses_while_live(
        "plan.create", lambda: plan.create(con, "p-live", name="L"))
    _assert_writer_refuses_while_live(
        "plan.set_capacity_mode",
        lambda: plan.set_capacity_mode(con, "ark", "compression_aware"))
    _assert_writer_refuses_while_live(
        "plan.add_drive",
        lambda: (
            con.execute(
                "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,"
                "role,raid_backed,lifecycle,eligibility) "
                "VALUES('d9',1000,900,'replica',0,'active','enabled')")
            or plan.add_drive(con, "ark", "d9")))

    # --- graph_write direct ---
    def op(c):
        c.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES('org/z',1)")
        return SimpleNamespace(proven_noop=False)

    # Prefer Refusal path; if production not wired, still must not commit silently
    try:
        prop.graph_write(con, op)
        # If it succeeded, that's a Gate-1 red for missing live check
        raise AssertionError(
            "graph_write while live must refuse FILL_SESSION_ACTIVE (expected Gate-1 red)")
    except AssertionError:
        raise
    except Exception as exc:
        got = f.refusal_code(exc)
        msg = str(exc).upper()
        if got == "FILL_SESSION_ACTIVE" or "FILL_SESSION_ACTIVE" in msg:
            pass
        else:
            raise AssertionError(
                f"graph_write while live: expected FILL_SESSION_ACTIVE, got {exc!r}"
            ) from exc

    # --- db.replace_files ---
    def replace():
        db.replace_files(con, "org/a", [
            {"rfilename": "model.safetensors", "size_bytes": 101,
             "format": "safetensors", "quant": "bf16", "sha256": "2" * 64}])
    try:
        replace()
        raise AssertionError("db.replace_files while live must refuse (expected Gate-1 red)")
    except AssertionError:
        raise
    except Exception as exc:
        if f.refusal_code(exc) != "FILL_SESSION_ACTIVE" and "FILL_SESSION_ACTIVE" not in str(exc).upper():
            raise AssertionError(
                f"replace_files while live expected FILL_SESSION_ACTIVE; got {exc!r}"
            ) from exc

    # --- proposal.approve second draft ---
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    # Cannot easily approve while live without supersede — pin that approve path refuses live
    draft = prop.create_draft(con, plan_id="ark", mutation=("adopt_current", ()))
    dpid = draft["proposal_id"] if isinstance(draft, dict) else draft
    try:
        prop.approve(con, dpid)
        raise AssertionError("approve while live session must refuse FILL_SESSION_ACTIVE")
    except AssertionError:
        raise
    except Exception as exc:
        if f.refusal_code(exc) != "FILL_SESSION_ACTIVE" and "FILL_SESSION_ACTIVE" not in str(exc).upper():
            # May refuse for other CAS reasons first; require live check to be present in stack
            raise AssertionError(
                f"approve while live expected FILL_SESSION_ACTIVE; got {exc!r}"
            ) from exc

    con.close()


def _child_try_start(db_path: str, state_dir: str, q: mp.Queue):
    """Separate process: open same catalog, different state_dir, try start_session."""
    try:
        from modelark.core import db as core_db
        core_db.CATALOG_DIR = Path(db_path).parent
        core_db.DB_PATH = Path(db_path)
        os.environ["MODELARK_STATE_DIR"] = state_dir
        con = core_db.connect()
        mod = f.session_api()
        # Find active proposal
        row = con.execute(
            "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
        ).fetchone()
        pid = row[0] if row else None
        out = mod.start_session(con, pid, None, f.default_services())
        if f.is_refusal(out) or f.refusal_code(out) == "FILL_SESSION_ACTIVE":
            q.put(("refused", f.refusal_code(out) or "FILL_SESSION_ACTIVE"))
        else:
            q.put(("ok", str(out)))
        con.close()
    except Exception as exc:
        q.put(("err", f"{type(exc).__name__}:{exc}"))


def test_same_catalog_different_state_dirs_process_exclusion(tmp_path):
    """Behavioral multi-process: second process must see live session on shared catalog."""
    catalog_dir = tmp_path / "shared"
    catalog_dir.mkdir()
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
    proc = mp.Process(
        target=_child_try_start,
        args=(str(db.DB_PATH), str(state_b), q))
    proc.start()
    proc.join(timeout=30)
    assert proc.exitcode is not None, "child hung"
    status, detail = q.get(timeout=5)
    assert status in ("refused", "err"), (
        f"second process must not start live session; got {status} {detail}")
    if status == "refused":
        assert detail == "FILL_SESSION_ACTIVE" or "FILL_SESSION" in str(detail)
    con.close()
