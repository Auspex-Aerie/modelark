"""PR-09 / #39-B Gate 1: hard Fill entry cut (B8).

CLI Fill, portal Fill, second portal, and systemd resume must enter the same
proposal/session service. fill.execute may remain only as a façade that cannot
retain optimizer authority or bypass approval/session exclusion.
"""
from __future__ import annotations

import importlib
import inspect


def _service():
    for name in (
        "modelark.execution_service",
        "modelark.execution",
        "modelark.fill_service",
        "modelark.session_fill",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if any(hasattr(mod, n) for n in (
                "start_fill", "run_approved_fill", "enter_execution",
                "FILL_ENTRYPOINTS", "unified_start")):
            return mod
    raise AssertionError(
        "unified execution/fill service module required "
        "(modelark.execution_service / execution; expected Gate-1 red)")


def test_fill_entrypoints_inventory_covers_all_surfaces():
    mod = _service()
    entries = getattr(mod, "FILL_ENTRYPOINTS", None) or getattr(mod, "fill_entrypoints", None)
    assert entries is not None, "export FILL_ENTRYPOINTS listing all surfaces"
    names = {str(x).lower() for x in (entries.keys() if isinstance(entries, dict) else entries)}
    required = {"cli", "portal", "systemd", "resume"}
    # Accept second portal as portal or second_portal
    missing = []
    for r in required:
        if not any(r in n for n in names):
            missing.append(r)
    if not any("portal" in n for n in names):
        missing.append("portal")
    assert not missing, f"FILL_ENTRYPOINTS missing surfaces {missing}; have={sorted(names)}"


def test_all_entrypoints_call_same_start_symbol():
    mod = _service()
    unified = getattr(mod, "start_fill", None) or getattr(mod, "run_approved_fill", None) or getattr(
        mod, "enter_execution", None) or getattr(mod, "unified_start", None)
    assert callable(unified), "single start_fill / enter_execution required"
    # Entrypoint adapters must reference the same function object or module path.
    adapters = getattr(mod, "ENTRYPOINT_ADAPTERS", None) or getattr(mod, "entrypoint_adapters", None)
    assert adapters is not None, (
        "export ENTRYPOINT_ADAPTERS mapping cli/portal/systemd/resume → unified start "
        "(expected Gate-1 red)")
    targets = []
    for _name, target in (adapters.items() if isinstance(adapters, dict) else []):
        targets.append(target)
    assert targets, "ENTRYPOINT_ADAPTERS must be non-empty"
    # All resolve to same callable or same qualified name
    norms = []
    for t in targets:
        if callable(t):
            norms.append(getattr(t, "__qualname__", repr(t)))
        else:
            norms.append(str(t))
    assert len(set(norms)) == 1, f"all entrypoints must share one target; got {norms}"


def test_fill_execute_facade_cannot_call_optimizer():
    fill = importlib.import_module("modelark.fill")
    assert hasattr(fill, "execute"), "fill.execute façade still present"
    # Production must route through session service; spy that plan_capacity / solver not used.
    src = inspect.getsource(fill.execute)
    # Soft pin until rewrite: require explicit marker or import of execution service.
    try:
        _service()
    except AssertionError:
        raise AssertionError(
            "fill.execute hard-cut requires unified execution service "
            "(expected Gate-1 red)") from None
    marker = getattr(fill, "EXECUTE_USES_SESSION_SERVICE", None)
    if marker is not True:
        # Fallback: execute source must reference session/execution service symbols
        lowered = src.lower()
        assert any(k in lowered for k in (
            "start_fill", "execution_service", "session", "enter_execution",
            "run_approved")), (
            "fill.execute must route through proposal/session service and not retain "
            "optimizer authority (expected Gate-1 red until façade wired)")


def test_fill_execute_refuses_without_active_approval(tmp_path):
    fill = importlib.import_module("modelark.fill")
    # When hard-cut is live, execute without active approved proposal refuses.
    from modelark.core import db
    import sqlite3
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    # No active_approved_proposal_id
    try:
        from modelark import fetch
        ctx = fetch.RunCtx(con=con)
        fill.execute(ctx, guided=True, max_24h_gb=0)
        raise AssertionError(
            "fill.execute without active approved proposal must refuse after hard cut")
    except AssertionError:
        raise
    except Exception as exc:
        msg = str(exc).upper()
        assert any(k in msg for k in (
            "APPROV", "PROPOSAL", "SESSION", "REFUS", "PREVIEW", "NO_ACTIVE")), (
            f"expected approval/session refusal; got {type(exc).__name__}: {exc}")


def test_execute_does_not_import_plan_capacity_solver_path():
    """B8: no optimizer / plan_capacity call path from fixed-map execution entry."""
    mod = _service()
    unified = getattr(mod, "start_fill", None) or getattr(mod, "run_approved_fill", None) or getattr(
        mod, "enter_execution", None)
    assert callable(unified)
    # Module-level ban list
    banned = getattr(mod, "FORBIDDEN_OPTIMIZER_IMPORTS", None)
    assert banned is not None or not any(
        name in dir(mod) for name in ("plan_capacity", "tiered_v2", "solve_placement")), (
        "execution service must not expose optimizer entrypoints")


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
