"""PR-09 Gate 1: hard Fill entry cut — real surface adapters only (B8).

Retain in-process wiring proofs for:
  - CLI fill entry
  - portal fill_api.start
  - systemd auto_resume_fill

Do **not** invent second_portal_start_fill, FILL_ENTRYPOINTS, or ENTRYPOINT_ADAPTERS.
A second portal is another **cold instance of the same portal entrypoint**, proven at
Gate 2 via exec-style installed-process smoke (same catalog, other state directory,
exact FILL_SESSION_ACTIVE). No production multiprocessing; no fork/spawn selection.
"""
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


def test_cli_adapter_invokes_unified_service_once():
    umod = _unified_mod()
    name = _start_name(umod)
    cli = importlib.import_module("modelark.cli")
    with mock.patch.object(umod, name) as spy:
        spy.return_value = {"ok": True}
        adapter = getattr(cli, "start_fill_via_service", None) or getattr(
            umod, "cli_start_fill", None)
        if callable(adapter):
            adapter(plan_id="ark")
        elif hasattr(cli, "cmd_fill"):
            args = argparse.Namespace(
                plan_id="ark", max_24h_gb=0, guided=True, repos=None)
            cli.cmd_fill(args)
        else:
            raise AssertionError(
                "CLI adapter cmd_fill or start_fill_via_service required")
        assert spy.call_count == 1, (
            f"CLI adapter must call unified {name} exactly once; got {spy.call_count}")


def test_portal_adapter_invokes_unified_service_once():
    """Portal surface is fill_api.start — the same entry a second portal instance uses."""
    umod = _unified_mod()
    name = _start_name(umod)
    fill_api = importlib.import_module("modelark.web.fill_api")
    assert callable(fill_api.start), "fill_api.start required"
    with mock.patch.object(umod, name) as spy:
        spy.return_value = {"ok": True}
        fill_api.start({})
        assert spy.call_count == 1, (
            f"portal fill_api.start must call unified {name} once; got {spy.call_count}")


def test_systemd_resume_adapter_invokes_unified_service_once():
    umod = _unified_mod()
    name = _start_name(umod)
    server = importlib.import_module("modelark.web.server")
    resume = getattr(server, "auto_resume_fill", None)
    assert callable(resume), "server.auto_resume_fill required for systemd resume hard-cut"
    with mock.patch.object(umod, name) as spy:
        spy.return_value = {"ok": True}
        resume({})
        assert spy.call_count == 1, (
            f"systemd auto_resume_fill must call unified {name} once; got {spy.call_count}")


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
