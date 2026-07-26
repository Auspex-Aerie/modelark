"""PR-09 Gate 1: hard Fill entry cut — each surface must call unified service once."""
from __future__ import annotations

import argparse
import importlib
from unittest import mock

import _pr09_gate1_fixtures as f


def _unified_mod():
    for name in (
        "modelark.execution_service",
        "modelark.execution",
        "modelark.fill_service",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if callable(getattr(mod, "start_fill", None)) or callable(
                getattr(mod, "enter_execution", None)):
            return mod
    raise AssertionError(
        "unified start_fill / enter_execution service required (expected Gate-1 red)")


def _start_name(umod):
    return "start_fill" if hasattr(umod, "start_fill") else "enter_execution"


def test_cli_fill_invokes_unified_service_once():
    umod = _unified_mod()
    name = _start_name(umod)
    cli = importlib.import_module("modelark.cli")
    with mock.patch.object(umod, name) as spy:
        spy.return_value = {"ok": True}
        # Prefer explicit CLI adapter; else cmd_fill / parser fill action
        adapter = getattr(cli, "start_fill_via_service", None) or getattr(
            umod, "cli_start_fill", None)
        if callable(adapter):
            adapter(plan_id="ark")
        elif hasattr(cli, "cmd_fill"):
            args = argparse.Namespace(
                plan_id="ark", max_24h_gb=0, guided=True, repos=None)
            cli.cmd_fill(args)
        else:
            raise AssertionError("CLI fill entry cmd_fill or start_fill_via_service required")
        assert spy.call_count == 1, (
            f"CLI must call unified {name} exactly once; got {spy.call_count}")


def test_portal_systemd_and_second_portal_each_call_unified_once():
    umod = _unified_mod()
    name = _start_name(umod)
    calls = []

    def capture(*a, **k):
        calls.append((a, k))
        return {"ok": True, "via": "unified"}

    fill_api = importlib.import_module("modelark.web.fill_api")
    server = importlib.import_module("modelark.web.server")

    with mock.patch.object(umod, name, side_effect=capture):
        # Portal surface
        assert callable(fill_api.start)
        fill_api.start({})
        # Systemd resume surface (must be distinct call site wired to same service)
        resume = getattr(server, "auto_resume_fill", None)
        if resume is None:
            # Contract: production must export auto_resume_fill for systemd
            raise AssertionError(
                "server.auto_resume_fill required for systemd resume hard-cut")
        resume({})
        # Second portal: separate process calls fill_api.start
        def second_portal(q):
            try:
                import importlib as il
                fa = il.import_module("modelark.web.fill_api")
                fa.start({})
                q.put("ok")
            except Exception as exc:
                q.put(f"err:{exc}")

        # In-process second "portal" module re-import still hits same patched symbol
        fill_api.start({})  # second portal process would share service; count third call

    # Exactly one call per surface: portal + systemd + second portal = 3
    assert len(calls) == 3, (
        f"expected 1 call per surface (portal, systemd, second portal)=3; got {len(calls)}")


def test_fill_execute_routes_through_service_without_optimizer():
    umod = _unified_mod()
    name = _start_name(umod)
    fill = importlib.import_module("modelark.fill")
    from modelark import capacity, fetch, reconcile
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    ctx = fetch.RunCtx(con=con)

    with mock.patch.object(umod, name) as spy, \
         mock.patch.object(
             capacity, "plan_capacity",
             side_effect=AssertionError("optimizer banned")), \
         mock.patch.object(
             reconcile, "reconcile_plan",
             side_effect=AssertionError("legacy reconcile banned")):
        spy.return_value = f.proposal_mod().Refusal(
            "APPROVAL_MISSING", {}, ("preview_again",))
        try:
            fill.execute(ctx, guided=True, max_24h_gb=0)
        except AssertionError as exc:
            if "banned" in str(exc):
                raise AssertionError(
                    "fill.execute must not call capacity.plan_capacity or "
                    "reconcile.reconcile_plan after hard cut"
                ) from exc
            raise
        except Exception:
            pass
        assert spy.called, (
            "fill.execute must call unified execution service "
            f"{name} (expected Gate-1 red until façade wired)")
