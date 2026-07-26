"""PR-09 Gate 1: recovery behavioral contracts — locks, dirt, child FD, atomic pair."""
from __future__ import annotations

from unittest import mock

import _pr09_gate1_fixtures as f


def _rec_mod():
    import importlib
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
        if any(callable(getattr(mod, n, None)) for n in (
                "recover_expired_session", "recover_session",
                "populate_dirty_owner", "owned_dirty_generations")):
            return mod
    raise AssertionError("recovery behavioral APIs required (expected Gate-1 red)")


class _SpyConnection:
    """Delegate to a real sqlite3 connection while recording SQL against lock order.

    ``sqlite3.Connection.execute`` is a read-only C slot (assignment/patch fail). A
    thin proxy is the enforceable observation surface for BEGIN IMMEDIATE timing.
    """

    def __init__(self, real, order, sqlite_during_lock):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_order", order)
        object.__setattr__(self, "_sqlite_during_lock", sqlite_during_lock)

    def execute(self, sql, *a, **k):
        text = sql if isinstance(sql, str) else str(sql)
        if "BEGIN" in text.upper():
            self._sqlite_during_lock.append((list(self._order), text))
        return self._real.execute(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_lock_order_controller_then_all_proposal_drives_no_sqlite_while_waiting():
    mod = _rec_mod()
    recover = getattr(mod, "recover_expired_session", None) or getattr(mod, "recover_session")
    order = []
    sqlite_during_lock = []

    class Ctrl:
        def hold(self):
            # Record only when context is entered (not at construction).
            class _CM:
                def __enter__(self_inner):
                    order.append("controller")
                    return None

                def __exit__(self_inner, *a):
                    return False

            return _CM()

    class Drives:
        def hold_all_sorted(self, ids):
            labels = tuple(sorted(ids))

            class _CM:
                def __enter__(self_inner):
                    order.append(("drives", labels))
                    return labels

                def __exit__(self_inner, *a):
                    return False

            return _CM()

    real = f.mem_con()
    f.seed_plan_selection(real, repos=("org/a", "org/b"))
    _p, pid, proposal = f.create_and_approve(real)
    real.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('s1',?,?, 'c','w','running',0,3,'2000-01-01T00:00:00Z')",
        ["ark", pid])
    services = f.default_services()
    services.controller_flock = Ctrl()
    services.drive_fences = Drives()
    labels = sorted({
        t.get("target_drive") or t.get("satisfying_drive")
        for t in proposal.get("tasks") or ()
        if (t.get("target_drive") or t.get("satisfying_drive"))
    })
    con = _SpyConnection(real, order, sqlite_during_lock)
    f.require_success(
        recover(con, session_id="s1", services=services), label="recover")
    assert order and order[0] == "controller", order
    drive_steps = [x for x in order if isinstance(x, tuple) and x[0] == "drives"]
    assert drive_steps, order
    locked = set(drive_steps[0][1])
    assert set(labels) <= locked, f"proposal drives {labels} must be locked; got {locked}"
    # Controller must precede drives.
    ctrl_i = order.index("controller")
    drive_i = next(i for i, x in enumerate(order) if isinstance(x, tuple) and x[0] == "drives")
    assert ctrl_i < drive_i, order
    # BEGIN IMMEDIATE must be observed and only after both physical locks are held.
    immediates = [
        (held, sql) for held, sql in sqlite_during_lock
        if "IMMEDIATE" in sql.upper()
    ]
    assert immediates, (
        f"must observe BEGIN IMMEDIATE under held locks; "
        f"order={order} sqlite={sqlite_during_lock}")
    for held, sql in immediates:
        assert "controller" in held and any(
            isinstance(x, tuple) and x[0] == "drives" for x in held), (
            f"SQLite IMMEDIATE must not run while waiting only on physical locks; "
            f"held={held} sql={sql}")

def test_token_scoped_dirty_and_stale_token():
    mod = _rec_mod()
    populate = getattr(mod, "populate_dirty_owner")
    owned = getattr(mod, "owned_dirty_generations")
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO drive_dirty_generations("
        "drive_label,identity_epoch,generation,operation_code) "
        "VALUES('d0',1,2,'sess')")
    f.require_success(
        populate(con, drive_label="d0", identity_epoch=1, generation=2,
                 session_id="s1", fencing_token=5),
        label="populate pair",
    )
    assert owned(con, session_id="s1", fencing_token=5)
    assert list(owned(con, session_id="s1", fencing_token=6) or []) == []
    assert list(owned(con, session_id="other", fencing_token=5) or []) == []


def test_unpaired_ownership_refuses_atomically_unchanged():
    mod = _rec_mod()
    populate = getattr(mod, "populate_dirty_owner")
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute(
        "INSERT INTO drive_dirty_generations("
        "drive_label,identity_epoch,generation,operation_code) "
        "VALUES('d0',1,3,'sess')")
    before = con.execute(
        "SELECT owner_session_id, owner_fencing_token FROM drive_dirty_generations "
        "WHERE drive_label='d0' AND generation=3").fetchone()
    f.assert_refuses(
        lambda: populate(con, drive_label="d0", identity_epoch=1, generation=3,
                         session_id="s1", fencing_token=None),
        code="DIRTY_OWNER_PAIR_REQUIRED",
        label="unpaired owner",
    )
    after = con.execute(
        "SELECT owner_session_id, owner_fencing_token FROM drive_dirty_generations "
        "WHERE drive_label='d0' AND generation=3").fetchone()
    assert after == before, f"must remain unchanged; before={before} after={after}"


def test_child_fence_delays_recovery_until_release():
    """Inherited FD held across parent death blocks recovery until child releases."""
    mod = _rec_mod()
    recover = getattr(mod, "recover_expired_session", None) or getattr(mod, "recover_session")
    inherit = getattr(mod, "inherit_drive_fence_fds", None)
    assert callable(inherit), "inherit_drive_fence_fds required"
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    _p, pid, _ = f.create_and_approve(con)
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('s-child',?,?, 'c','w','running',0,1,'2000-01-01T00:00:00Z')",
        ["ark", pid])
    # Simulate parent death with child still holding fence FDs
    child_fds = inherit(session_id="s-child", drive_labels=["d0"])
    assert child_fds is not None
    held = getattr(mod, "child_fence_still_held", None) or getattr(
        mod, "fence_fds_held", None)
    assert callable(held), "child_fence_still_held required"
    with mock.patch.object(mod, held.__name__, return_value=True):
        f.assert_refuses(
            lambda: recover(con, session_id="s-child", services=f.default_services()),
            code="CHILD_FENCE_HELD",
            label="recovery while child holds fence",
        )
    # After release, recovery may proceed
    with mock.patch.object(mod, held.__name__, return_value=False):
        f.require_success(
            recover(con, session_id="s-child", services=f.default_services()),
            label="recovery after child release",
        )
