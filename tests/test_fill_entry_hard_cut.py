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


def test_portal_systemd_and_second_portal_each_call_unified_once(tmp_path):
    umod = _unified_mod()
    name = _start_name(umod)
    fill_api = importlib.import_module("modelark.web.fill_api")
    server = importlib.import_module("modelark.web.server")

    # Surfaces must bind to the unified symbol (same function or thin wrapper calling it).
    resume = getattr(server, "auto_resume_fill", None)
    assert callable(resume), "server.auto_resume_fill required for systemd resume hard-cut"
    assert callable(fill_api.start), "fill_api.start required"

    marker = tmp_path / "unified_calls.txt"
    if marker.exists():
        marker.unlink()

    def capture(*a, **k):
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write("call\n")
        return {"ok": True, "via": "unified"}

    with mock.patch.object(umod, name, side_effect=capture):
        # Portal (this process)
        fill_api.start({})
        # Systemd resume (this process, distinct adapter)
        resume({})
        # Second portal: real child process must also invoke unified start
        import multiprocessing as mp

        def second_portal(q, marker_path, mod_name, start_name):
            try:
                import importlib as il
                from unittest import mock as child_mock
                um = il.import_module(mod_name)
                fa = il.import_module("modelark.web.fill_api")

                def child_capture(*a, **k):
                    with open(marker_path, "a", encoding="utf-8") as fh:
                        fh.write("call\n")
                    return {"ok": True}

                with child_mock.patch.object(um, start_name, side_effect=child_capture):
                    fa.start({})
                q.put("ok")
            except Exception as exc:
                q.put(f"err:{type(exc).__name__}:{exc}")

        q = mp.Queue()
        proc = mp.Process(
            target=second_portal,
            args=(q, str(marker), umod.__name__, name))
        proc.start()
        proc.join(timeout=60)
        assert proc.exitcode == 0, proc.exitcode
        child_status = q.get(timeout=5)
        assert child_status == "ok", f"second portal process failed: {child_status}"

    lines = marker.read_text().splitlines() if marker.exists() else []
    assert len(lines) == 3, (
        f"expected 1 unified call each for portal, systemd, second-portal process; "
        f"got {len(lines)} marker lines")


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
