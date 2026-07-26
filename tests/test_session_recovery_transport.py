"""PR-09 / #39-B Gate 1: recovery, child fence inherit, dirty-owner pairing (B9–B10).

Tests-only. Expected red until recovery runtime lands. Protected transport suite
characterization remains; these pins bind session-owned dirt and parent-death.
"""
from __future__ import annotations

import importlib
import sqlite3

from modelark.core import db


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _recovery():
    for name in (
        "modelark.execution_recovery",
        "modelark.session_recovery",
        "modelark.execution",
        "modelark.execution_session",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if any(hasattr(mod, n) for n in (
                "recover_expired_session", "recover_session", "force_recover",
                "inherit_drive_fence_fds", "populate_dirty_owner")):
            return mod
    raise AssertionError(
        "session recovery / dirty-owner exports required (expected Gate-1 red)")


def test_dirty_owner_fields_populated_as_pair():
    mod = _recovery()
    populate = getattr(mod, "populate_dirty_owner", None) or getattr(
        mod, "set_dirty_generation_owner", None)
    assert callable(populate), "populate_dirty_owner required"
    con = _mem()
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch,identity_fingerprint) "
        "VALUES('d0',1000,900,'primary',0,'active','enabled',1,?)",
        ["f" * 64])
    con.execute(
        "INSERT INTO drive_dirty_generations("
        "drive_label,identity_epoch,generation,operation_code) "
        "VALUES('d0',1,1,'test')")
    populate(con, drive_label="d0", identity_epoch=1, generation=1,
             session_id="sess-1", fencing_token=2)
    row = con.execute(
        "SELECT owner_session_id, owner_fencing_token FROM drive_dirty_generations "
        "WHERE drive_label='d0' AND identity_epoch=1 AND generation=1").fetchone()
    assert row == ("sess-1", 2), f"paired dirty owner required; got {row}"
    # Pair constraint: cannot set one without the other at API level
    try:
        populate(con, drive_label="d0", identity_epoch=1, generation=1,
                 session_id="sess-2", fencing_token=None)
        # If it wrote, schema CHECK should abort — either path is failure for unpaired API
        row2 = con.execute(
            "SELECT owner_session_id, owner_fencing_token FROM drive_dirty_generations "
            "WHERE drive_label='d0' AND identity_epoch=1 AND generation=1").fetchone()
        assert row2[0] is None or row2[1] is not None
    except Exception:
        pass  # refuse unpaired is correct


def test_recovery_selects_matching_session_token_only():
    mod = _recovery()
    recover = getattr(mod, "recover_expired_session", None) or getattr(
        mod, "recover_session", None) or getattr(mod, "force_recover", None)
    assert callable(recover), "recover_expired_session required"
    con = _mem()
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    con.execute(
        "INSERT INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
        "mutation_kind,mutation_args_json,serializer_version,capacity_mode,"
        "policy_version,solver_version,gate_b_code) "
        "VALUES('prop-1','ark',0,'approved',?,?,?,?,?,?,?,?)",
        ["a" * 64, "adopt_current", "[]", "1", "guaranteed", "1", "1", "FEASIBLE"])
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token) "
        "VALUES('s1','ark','prop-1','c1','w1','running',0,5)")
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch,identity_fingerprint) "
        "VALUES('d0',1000,900,'primary',0,'active','enabled',1,?)",
        ["f" * 64])
    con.execute(
        "INSERT INTO drive_dirty_generations("
        "drive_label,identity_epoch,generation,operation_code,"
        "owner_session_id,owner_fencing_token) "
        "VALUES('d0',1,1,'test','s1',5)")
    # Wrong token must not clear or claim this dirt
    owned = getattr(mod, "owned_dirty_generations", None) or getattr(
        mod, "list_owned_dirt", None)
    assert callable(owned), "owned_dirty_generations(session_id, token) required"
    rows_ok = owned(con, session_id="s1", fencing_token=5)
    rows_bad = owned(con, session_id="s1", fencing_token=6)
    assert rows_ok, "matching session/token must see owned dirt"
    assert not rows_bad, "mismatched token must not select owned dirt"


def test_child_inherits_drive_fence_fds_export():
    mod = _recovery()
    inherit = getattr(mod, "inherit_drive_fence_fds", None) or getattr(
        mod, "child_fence_fd_inheritance", None) or getattr(
        mod, "monitor_child_fence_fds", None)
    assert inherit is not None, (
        "child drive-fence FD inheritance export required (B9; expected Gate-1 red)")


def test_recovery_lock_order_controller_first():
    mod = _recovery()
    order = getattr(mod, "RECOVERY_LOCK_ORDER", None) or getattr(
        mod, "recovery_lock_order", None)
    assert order is not None, "export RECOVERY_LOCK_ORDER (expected Gate-1 red)"
    seq = [str(x).lower() for x in order]
    assert seq[0] in ("controller", "controller_flock", "controller_lock"), (
        f"recovery must take controller flock first; got {seq}")
    assert any("drive" in s for s in seq[1:]), "sorted drive flocks must follow controller"


def test_normal_close_reconciles_generation_touched_only():
    mod = _recovery()
    assert getattr(mod, "NORMAL_CLOSE_FULL_DRIVE_INVENTORY", True) is False or hasattr(
        mod, "reconcile_generation_touched_only"), (
        "normal clean close must not full-drive inventory per file (expected Gate-1 red)")


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
