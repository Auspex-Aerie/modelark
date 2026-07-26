"""PR-09 Gate 1: hard Fill entry cut — adapter wiring only (no fork/spawn).

Each surface adapter (CLI, portal, systemd resume, second-portal adapter) must
invoke the unified start_fill / enter_execution exactly once. Cold installed
CLI/portal subprocess smoke is Gate-2 — not a PR-09 multiprocessing contract.
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


def test_second_portal_adapter_invokes_unified_service_once():
    """Second portal is a second *adapter entry*, not a fork/spawn process.

    Production may export second_portal_start_fill or re-export the same portal
    adapter under an explicit multi-portal entry name. Gate-2 owns cold subprocess
    smoke of a second installed portal process.
    """
    umod = _unified_mod()
    name = _start_name(umod)
    adapter = (
        getattr(umod, "second_portal_start_fill", None)
        or getattr(umod, "portal_start_fill", None)
    )
    fill_api = importlib.import_module("modelark.web.fill_api")
    if not callable(adapter):
        # Accept explicit multi-portal binding: second portal uses same fill_api.start
        # only if production documents FILL_ENTRYPOINTS includes second_portal → start_fill.
        entries = getattr(umod, "FILL_ENTRYPOINTS", None) or getattr(
            umod, "fill_entrypoints", None)
        assert entries is not None, (
            "export FILL_ENTRYPOINTS including second_portal, or second_portal_start_fill")
        names = {str(k).lower() for k in (
            entries.keys() if isinstance(entries, dict) else entries)}
        assert any("second" in n or "portal" in n for n in names), names
        adapter = fill_api.start
    with mock.patch.object(umod, name) as spy:
        spy.return_value = {"ok": True}
        adapter({})
        assert spy.call_count == 1, (
            f"second-portal adapter must call unified {name} once; got {spy.call_count}")


def test_all_surface_adapters_share_same_unified_target():
    """Adapter wiring pin: every surface resolves to one unified callable (in-process)."""
    umod = _unified_mod()
    name = _start_name(umod)
    unified = getattr(umod, name)
    server = importlib.import_module("modelark.web.server")
    resume = getattr(server, "auto_resume_fill", None)
    assert callable(resume)
    # Production may wrap; require FILL_ENTRYPOINTS map or identity of underlying target
    entries = getattr(umod, "FILL_ENTRYPOINTS", None) or getattr(umod, "ENTRYPOINT_ADAPTERS", None)
    assert entries is not None, (
        "export FILL_ENTRYPOINTS / ENTRYPOINT_ADAPTERS mapping "
        "cli|portal|systemd|second_portal → unified start (expected Gate-1 red)")
    mapping = entries if isinstance(entries, dict) else {}
    required = {"cli", "portal", "systemd"}
    keys = {str(k).lower() for k in mapping}
    missing = [r for r in required if not any(r in k for k in keys)]
    assert not missing, f"FILL_ENTRYPOINTS missing {missing}; have={sorted(keys)}"
    # All mapped callables must be the unified start (or name equal)
    for key, target in mapping.items():
        if not callable(target):
            continue
        assert target is unified or getattr(target, "__name__", "") in (
            name, "start_fill", "enter_execution", "start"), (
            f"entrypoint {key} must target unified {name}; got {target!r}")


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
