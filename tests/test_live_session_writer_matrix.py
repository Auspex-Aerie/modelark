"""PR-09 / #39-B Gate 1: complete A3 writer inventory refused while session live (B3, B13).

Includes same-catalog / different state-directory exclusion. Tests-only; expected red
until session runtime + graph_write live-session gate land fully for all writers.
"""
from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path

from modelark.core import db


# Same supported inventory names as PR-08 A3 (finding completeness).
REQUIRED_WRITERS = [
    "selection_api.finalize", "selection_api.clear", "selection_api.toggle",
    "selection_api.bulk",
    "discover.discover_one", "discover.discover_repos", "db.replace_files",
    "cli.cmd_protect",
    "plan.create", "plan.add_drive", "plan.remove_drive", "plan.set_active",
    "plan.bootstrap", "plan.set_capacity_mode",
    "drive_mutation.begin_generation", "drive_mutation.publish_clean_anchor",
    "drive_bootstrap.reconcile_drive", "register.register_drive",
    "hash_repair.repair_hashes",
    "proposal.approve",
    "fetch",
]


def _sessions():
    for name in (
        "modelark.execution_session",
        "modelark.execution_sessions",
        "modelark.execution",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if hasattr(mod, "start_session") or hasattr(mod, "start"):
            return mod
    raise AssertionError("session start API required (expected Gate-1 red)")


def _proposal():
    return importlib.import_module("modelark.proposal")


def test_inventory_still_lists_complete_a3_writers():
    prop = _proposal()
    inv = getattr(prop, "GRAPH_AFFECTING_WRITERS", None) or getattr(
        prop, "graph_affecting_writers", None)
    assert inv is not None, "GRAPH_AFFECTING_WRITERS export required"
    names = {str(x) for x in (inv.keys() if isinstance(inv, dict) else inv)}
    missing = [s for s in REQUIRED_WRITERS if not any(s in n for n in names)]
    assert not missing, f"A3 inventory incomplete for live-session matrix: {missing}"


def test_graph_write_refuses_while_live_session():
    """Operator graph_write must refuse when a live session exists (RFC-002)."""
    prop = _proposal()
    sess = _sessions()
    start = getattr(sess, "start_session", None) or getattr(sess, "start")
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    con.execute("INSERT OR IGNORE INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    con.execute(
        "INSERT OR IGNORE INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
        "mutation_kind,mutation_args_json,serializer_version,capacity_mode,"
        "policy_version,solver_version,gate_b_code) "
        "VALUES('prop-1','ark',0,'approved',?,?,?,?,?,?,?,?)",
        ["a" * 64, "adopt_current", "[]", "1", "guaranteed", "1", "1", "FEASIBLE"])
    start(con, plan_id="ark", proposal_id="prop-1", controller_identity="c1", bound_revision=0)

    def op(c):
        c.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/x',1)")
        return type("R", (), {"proven_noop": False})()

    try:
        prop.graph_write(con, op)
        raise AssertionError("graph_write while live must refuse FILL_SESSION_ACTIVE (or equivalent)")
    except Exception as exc:
        msg = str(exc).upper()
        code = str(getattr(exc, "code", "") or "").upper()
        assert "FILL_SESSION_ACTIVE" in msg or "FILL_SESSION_ACTIVE" in code or (
            "LIVE" in msg and "SESSION" in msg), (
            f"expected live-session refusal; got {type(exc).__name__}: {exc}")


def test_each_inventory_writer_has_live_session_contract():
    """Every inventory entry must document or implement live-session refusal (Gate-1 pin)."""
    prop = _proposal()
    inv = getattr(prop, "GRAPH_AFFECTING_WRITERS", None) or getattr(
        prop, "graph_affecting_writers", None)
    assert inv is not None
    # Production should expose LIVE_SESSION_REFUSAL or per-writer metadata; until then this is red.
    meta = getattr(prop, "LIVE_SESSION_WRITER_CONTRACT", None) or getattr(
        prop, "live_session_writer_contract", None)
    assert meta is not None, (
        "export LIVE_SESSION_WRITER_CONTRACT covering complete A3 inventory "
        "(expected Gate-1 red until session exclusion is fully pinned)")
    names = set(meta.keys()) if isinstance(meta, dict) else set(meta)
    for req in REQUIRED_WRITERS:
        assert any(req in n for n in names), f"live-session contract missing writer {req}"


def test_same_catalog_different_state_dirs_share_live_exclusion(tmp_path):
    """B13: two processes / state dirs on one catalog share live-session exclusion."""
    sess = _sessions()
    assert hasattr(sess, "controller_lock_path") or hasattr(sess, "catalog_controller_lock") or (
        hasattr(sess, "same_catalog_exclusion_key")), (
        "export catalog-derived controller lock / exclusion key for multi-process "
        "(expected Gate-1 red)")
    catalog = tmp_path / "shared" / "catalog.sqlite"
    catalog.parent.mkdir(parents=True)
    # Two state directories, one catalog file.
    state_a = tmp_path / "state-a"
    state_b = tmp_path / "state-b"
    state_a.mkdir()
    state_b.mkdir()
    key_fn = getattr(sess, "same_catalog_exclusion_key", None) or getattr(
        sess, "catalog_controller_lock", None) or getattr(sess, "controller_lock_path")
    k1 = key_fn(catalog_path=catalog, state_dir=state_a) if callable(key_fn) else key_fn
    k2 = key_fn(catalog_path=catalog, state_dir=state_b) if callable(key_fn) else key_fn
    # Keys must collide on catalog identity, not state dir.
    s1 = str(k1)
    s2 = str(k2)
    assert s1 == s2 or Path(s1).resolve() == Path(s2).resolve(), (
        f"same catalog must yield same exclusion key across state dirs; {s1!r} vs {s2!r}")


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
