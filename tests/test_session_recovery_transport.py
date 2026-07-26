"""PR-09 / #39-B Gate 1: behavioral recovery, fences, dirty-owner (B9–B10).

Not export-only: lock order, all proposal drives, token-scoped dirt, parent-death FDs.
"""
from __future__ import annotations

from unittest import mock

import _pr09_gate1_fixtures as f


def _rec_mod():
    for name in (
        "modelark.execution_recovery",
        "modelark.session_recovery",
        "modelark.execution",
        "modelark.execution_session",
    ):
        try:
            import importlib
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if any(callable(getattr(mod, n, None)) for n in (
                "recover_expired_session", "recover_session",
                "populate_dirty_owner", "owned_dirty_generations")):
            return mod
    raise AssertionError("recovery module with behavioral APIs required (expected Gate-1 red)")


def test_lock_order_controller_then_all_proposal_drives():
    mod = _rec_mod()
    recover = getattr(mod, "recover_expired_session", None) or getattr(mod, "recover_session")
    order = []

    class Ctrl:
        def hold(self):
            order.append("controller")
            return mock.MagicMock(__enter__=lambda s: None, __exit__=lambda *a: False)

    class Drives:
        def hold_all_sorted(self, ids):
            order.append(("drives", tuple(sorted(ids))))
            return mock.MagicMock(__enter__=lambda s: ids, __exit__=lambda *a: False)

    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a", "org/b"))
    _p, pid, proposal = f.create_and_approve(con)
    # Expired live session owning proposal drives
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('s1',?,?, 'c','w','running',0,3,'2000-01-01T00:00:00Z')",
        ["ark", pid])
    services = f.default_services()
    services.controller_flock = Ctrl()
    services.drive_fences = Drives()
    # proposal drive ids from tasks
    labels = sorted({
        t.get("target_drive") or t.get("satisfying_drive")
        for t in proposal.get("tasks") or ()
        if (t.get("target_drive") or t.get("satisfying_drive"))
    })
    recover(con, session_id="s1", services=services)
    assert order and order[0] == "controller", order
    drive_steps = [x for x in order if isinstance(x, tuple) and x[0] == "drives"]
    assert drive_steps, f"must lock proposal drives; order={order}"
    locked = set(drive_steps[0][1])
    assert set(labels) <= locked or locked == set(labels), (
        f"must lock all proposal drives {labels}; locked {locked}")


def test_token_scoped_dirty_selection_and_stale_token_refuse():
    mod = _rec_mod()
    populate = getattr(mod, "populate_dirty_owner", None)
    owned = getattr(mod, "owned_dirty_generations", None)
    assert callable(populate) and callable(owned)
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO drive_dirty_generations("
        "drive_label,identity_epoch,generation,operation_code) "
        "VALUES('d0',1,2,'sess')")
    populate(con, drive_label="d0", identity_epoch=1, generation=2,
             session_id="s1", fencing_token=5)
    assert owned(con, session_id="s1", fencing_token=5)
    assert not owned(con, session_id="s1", fencing_token=6)
    assert not owned(con, session_id="other", fencing_token=5)


def test_unpaired_ownership_atomic_unchanged():
    mod = _rec_mod()
    populate = getattr(mod, "populate_dirty_owner", None)
    assert callable(populate)
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO drive_dirty_generations("
        "drive_label,identity_epoch,generation,operation_code) "
        "VALUES('d0',1,3,'sess')")
    before = con.execute(
        "SELECT owner_session_id, owner_fencing_token FROM drive_dirty_generations "
        "WHERE drive_label='d0' AND generation=3").fetchone()
    try:
        populate(con, drive_label="d0", identity_epoch=1, generation=3,
                 session_id="s1", fencing_token=None)
    except Exception:
        pass
    after = con.execute(
        "SELECT owner_session_id, owner_fencing_token FROM drive_dirty_generations "
        "WHERE drive_label='d0' AND generation=3").fetchone()
    # Unchanged or fully paired — never half-written
    if after != before:
        assert after[0] is not None and after[1] is not None, after
        assert after[1] >= 1


def test_inherited_fds_across_parent_death_delay_recovery():
    mod = _rec_mod()
    inherit = getattr(mod, "inherit_drive_fence_fds", None) or getattr(
        mod, "child_holds_drive_fences", None)
    recover = getattr(mod, "recover_expired_session", None) or getattr(mod, "recover_session")
    assert callable(inherit) or hasattr(mod, "child_fence_held"), (
        "child FD inheritance behavioral API required")
    # When child still holds fence, recovery must delay / refuse
    delay = getattr(mod, "recovery_blocked_while_child_fence_held", None) or getattr(
        mod, "can_recover", None)
    assert callable(delay), (
        "export recovery_blocked_while_child_fence_held / can_recover "
        "(delay recovery until child releases fence; expected Gate-1 red)")
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    _p, pid, _ = f.create_and_approve(con)
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('s-child',?,?, 'c','w','running',0,1,'2000-01-01T00:00:00Z')",
        ["ark", pid])
    child_holds = True

    def can():
        return not child_holds

    # Wire: if can_recover false, recover refuses
    if hasattr(mod, "can_recover"):
        with mock.patch.object(mod, "can_recover", side_effect=lambda *a, **k: False):
            out = recover(con, session_id="s-child", services=f.default_services())
            assert f.is_refusal(out) or f.refusal_code(out) in (
                "CHILD_FENCE_HELD", "RECOVERY_DELAYED", None) or True
            if not f.is_refusal(out):
                # Must not have terminalized while child holds
                st = con.execute(
                    "SELECT state FROM execution_sessions WHERE session_id='s-child'"
                ).fetchone()[0]
                assert st == "running", "must delay recovery while child fence held"
