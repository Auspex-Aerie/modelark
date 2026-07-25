"""PR-08 / #39-A complete graph-writer revision closure (tests-first, A3).

Every currently supported graph-affecting catalog writer must bump planner_revision in the same
transaction unless it proves a canonical no-op. No expected-red writer follow-ups.
"""
from __future__ import annotations

import importlib
import sqlite3

from modelark.core import db


def _mem_v5():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "planner_state" not in tables:
        raise AssertionError(
            "v5 planner_state required for revision-writer contracts (expected Gate-1 red)")
    if con.execute("SELECT count(*) FROM planner_state").fetchone()[0] == 0:
        con.execute(
            "INSERT INTO planner_state(singleton_id,planner_revision,active_approved_proposal_id,"
            "next_fencing_token) VALUES(1,0,NULL,0)")
    return con


def _rev(con) -> int:
    return int(con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])


def _require_bump(con, label, fn):
    before = _rev(con)
    fn()
    after = _rev(con)
    assert after == before + 1, (
        f"{label} must bump planner_revision {before}→{before + 1}; got {after} "
        "(complete writer closure required in PR-08; no expected-red follow-ups)")


def _seed_base(con):
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/m',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('org/m','model.safetensors',100,'safetensors','bf16')")
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch,write_generation) "
        "VALUES('d0',1000,900,'primary',0,'active','enabled',1,0)")
    from modelark import plan
    plan.create(con, "ark", name="Ark")
    plan.add_drive(con, "ark", "d0")
    # create+add_drive may already bump once production wraps them; re-seed revision baseline.
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")


def test_selection_finalize_deselect_clear_bump():
    con = _mem_v5()
    _seed_base(con)
    prop = importlib.import_module("modelark.proposal")
    gw = getattr(prop, "graph_write", None)
    assert gw is not None, "graph_write primitive required (Gate-1 red)"

    def finalize(c):
        c.execute(
            "INSERT INTO selection(repo_id,finalized_at) VALUES('org/m','2026-01-01')")

    _require_bump(con, "selection finalize via graph_write", lambda: gw(con, finalize))

    def deselect(c):
        c.execute("DELETE FROM selection WHERE repo_id='org/m'")

    _require_bump(con, "selection deselect via graph_write", lambda: gw(con, deselect))


def test_replace_files_bumps():
    con = _mem_v5()
    _seed_base(con)
    rows = [{"rfilename": "model.safetensors", "size_bytes": 100, "format": "safetensors",
             "quant": "bf16", "sha256": "1" * 64}]
    _require_bump(con, "db.replace_files", lambda: db.replace_files(con, "org/m", rows))


def test_numcopies_bump():
    con = _mem_v5()
    _seed_base(con)
    # protect / numcopies path — common helper names.
    for mod_name, attr in (
        ("modelark.protect", "set_numcopies"),
        ("modelark.models", "set_numcopies"),
        ("modelark.core.db", "set_numcopies"),
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
        fn = getattr(mod, attr, None)
        if fn is not None:
            _require_bump(con, f"{mod_name}.{attr}", lambda: fn(con, "org/m", 2))
            return
    # Direct SQL is not a supported path; production must expose a wrapped mutator.
    def via_sql():
        con.execute("UPDATE models SET numcopies=2 WHERE repo_id='org/m'")
        # If graph_write is the only entry, call it.
        prop = importlib.import_module("modelark.proposal")
        bump = getattr(prop, "graph_write", None) or getattr(prop, "bump_if_changed", None)
        if bump is None:
            raise AssertionError(
                "numcopies writer must go through graph_write/bump primitive (Gate-1 red)")
        bump(con, lambda c: c.execute("UPDATE models SET numcopies=2 WHERE repo_id='org/m'"))
    try:
        _require_bump(con, "numcopies via graph_write", via_sql)
    except AssertionError:
        raise


def test_plan_membership_and_capacity_mode_bump():
    from modelark import plan
    con = _mem_v5()
    _seed_base(con)
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility) VALUES('d1',1000,900,'replica',0,'active','enabled')")
    _require_bump(con, "plan.add_drive", lambda: plan.add_drive(con, "ark", "d1"))
    _require_bump(con, "plan.remove_drive", lambda: plan.remove_drive(con, "ark", "d1"))
    if hasattr(plan, "set_capacity_mode"):
        _require_bump(con, "plan.set_capacity_mode",
                      lambda: plan.set_capacity_mode(con, "ark", "compression_aware"))
    else:
        # capacity mode column exists on plans
        def set_mode():
            prop = importlib.import_module("modelark.proposal")
            gw = getattr(prop, "graph_write")
            gw(con, lambda c: c.execute(
                "UPDATE plans SET capacity_mode='compression_aware' WHERE plan_id='ark'"))
        _require_bump(con, "capacity_mode via graph_write", set_mode)


def test_plan_bootstrap_bumps_when_membership_changes():
    from modelark import plan
    con = _mem_v5()
    _seed_base(con)
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility) VALUES('d2',1000,900,'primary',0,'active','enabled')")
    # d2 not on plan yet — bootstrap should add and bump.
    _require_bump(con, "plan.bootstrap (new membership)", lambda: plan.bootstrap(con, "ark"))


def test_active_plan_switch_bumps_noop_does_not():
    from modelark import plan
    con = _mem_v5()
    _seed_base(con)
    plan.create(con, "other", name="Other")
    # Real switch
    before = _rev(con)
    plan.set_active(con, "other")
    assert _rev(con) == before + 1, "active-plan switch must bump revision"
    # No-op re-select
    before = _rev(con)
    plan.set_active(con, "other")
    assert _rev(con) == before, "no-op set_active of already-active plan must not bump"


def test_archived_progress_writer_bumps():
    con = _mem_v5()
    _seed_base(con)
    prop = importlib.import_module("modelark.proposal")
    gw = getattr(prop, "graph_write", None)
    assert gw is not None, "graph_write primitive required for archived writers (Gate-1 red)"

    def insert_archived(c):
        c.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,stored_bytes) "
            "VALUES('org/m','model.safetensors','d0',0,100,100)")

    _require_bump(con, "archived insert via graph_write", lambda: gw(con, insert_archived))


def test_dirty_generation_and_clean_anchor_bump():
    con = _mem_v5()
    _seed_base(con)
    prop = importlib.import_module("modelark.proposal")
    gw = getattr(prop, "graph_write", None)
    assert gw is not None

    def dirty(c):
        c.execute(
            "INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
            "VALUES('d0',1,1,'test')")
        c.execute("UPDATE drives SET write_generation=1 WHERE drive_label='d0'")

    _require_bump(con, "dirty generation via graph_write", lambda: gw(con, dirty))

    def anchor(c):
        c.execute(
            "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
            "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,"
            "fence_proof,observed_at) VALUES('d0',1,1,100,1000,?,?,?,?,?)",
            ["a" * 64, "unknown", "p", "p", "now"])

    _require_bump(con, "clean anchor via graph_write", lambda: gw(con, anchor))


def test_lifecycle_eligibility_axis_write_bumps():
    con = _mem_v5()
    _seed_base(con)
    prop = importlib.import_module("modelark.proposal")
    gw = getattr(prop, "graph_write", None)
    assert gw is not None

    def exclude(c):
        c.execute("UPDATE drives SET eligibility='excluded' WHERE drive_label='d0'")

    _require_bump(con, "eligibility axis via graph_write", lambda: gw(con, exclude))


def test_proven_noop_does_not_bump():
    con = _mem_v5()
    _seed_base(con)
    prop = importlib.import_module("modelark.proposal")
    gw = getattr(prop, "graph_write", None)
    assert gw is not None
    before = _rev(con)

    class Result:
        proven_noop = True
        value = None

    # graph_write may accept operation returning proven_noop.
    try:
        gw(con, lambda c: Result())
    except TypeError:
        # Alternate API: bump_if_changed(con, before_facts, after_facts)
        raise AssertionError(
            "graph_write must honor proven_noop without bumping (Gate-1 red until API exists)")
    assert _rev(con) == before


def test_writer_inventory_is_explicit():
    """Production must export the closed inventory of graph-affecting writers for review."""
    prop = importlib.import_module("modelark.proposal")
    inv = getattr(prop, "GRAPH_AFFECTING_WRITERS", None) or getattr(
        prop, "graph_affecting_writers", None)
    assert inv is not None and len(list(inv)) >= 8, (
        "export GRAPH_AFFECTING_WRITERS (or graph_affecting_writers) listing every closed "
        "supported mutator; complete closure required in PR-08 (A3)")


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            failed.append((name, type(exc).__name__, str(exc)[:240]))
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    print("Gate-1 tests-only: revision-writer contracts EXPECTED RED until PR-08 production.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
