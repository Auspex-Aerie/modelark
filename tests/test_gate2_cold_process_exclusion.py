"""PR-09 Gate 2: cold exec-style installed-process exclusion proof (B8).

Start one live session against a shared on-disk catalog, then launch a second
**cold** process through the same **unified Fill entry surfaces** (CLI adapter and
portal ``fill_api.start``), each with a different state directory. Exact
``FILL_SESSION_ACTIVE`` is required.

No production multiprocessing and no fork/spawn selection — only independent
exec of the installed package entry path.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import _pr09_gate1_fixtures as f
from modelark.core import db


# Cold child: enter via CLI adapter (start_fill_via_service → execution_service.start_fill).
_CHILD_CLI_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from types import SimpleNamespace
    from unittest import mock
    from modelark.core import db
    from modelark.cli import start_fill_via_service
    from modelark.proposal import Refusal

    data_dir = Path(sys.argv[1])
    state_dir = Path(sys.argv[2])
    proposal_id = sys.argv[3]
    db.configure(data_dir=data_dir, state_dir=state_dir)
    con = db.connect()
    services = SimpleNamespace(
        clock=SimpleNamespace(now=lambda: "2026-01-01T00:00:00Z"),
        config=SimpleNamespace(read_graph_affecting_config=lambda: {
            "capacity_mode": "guaranteed", "policy_version": "1",
            "solver_version": "1",
            "compression": {"enabled": True, "codec": "streamznn", "level": 3},
            "numcopies_default": 1,
        }),
        controller_flock=SimpleNamespace(
            hold=lambda: mock.MagicMock(
                __enter__=lambda s: None, __exit__=lambda *a: False)),
        drive_fences=SimpleNamespace(
            hold_all_sorted=lambda ids: mock.MagicMock(
                __enter__=lambda s: tuple(ids), __exit__=lambda *a: False)),
        worker=SimpleNamespace(identity="cold-cli-worker", claim=lambda **k: None),
        lease_ttl=3600,
        state_dir=str(state_dir),
    )
    try:
        out = start_fill_via_service(
            plan_id="ark", proposal_id=proposal_id, con=con, services=services)
    except Refusal as exc:
        print(json.dumps({"ok": False, "code": exc.code, "surface": "cli"}))
        raise SystemExit(2)
    if isinstance(out, Refusal):
        print(json.dumps({"ok": False, "code": out.code, "surface": "cli"}))
        raise SystemExit(2)
    if isinstance(out, dict) and (out.get("code") or out.get("error")):
        print(json.dumps({
            "ok": False,
            "code": out.get("code") or out.get("error"),
            "surface": "cli",
        }))
        raise SystemExit(2)
    sid = getattr(getattr(out, "session", out), "session_id", None)
    print(json.dumps({"ok": True, "session_id": sid, "surface": "cli"}))
    raise SystemExit(0)
    """
)


# Cold child: enter via portal fill_api.start (same entrypoint a second portal uses).
_CHILD_PORTAL_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from types import SimpleNamespace
    from unittest import mock
    from modelark.core import db
    from modelark.web import data as web_data, fill_api
    from modelark import execution_service
    from modelark.proposal import Refusal

    data_dir = Path(sys.argv[1])
    state_dir = Path(sys.argv[2])
    proposal_id = sys.argv[3]
    db.configure(data_dir=data_dir, state_dir=state_dir)
    con = db.connect()
    web_data._con = con
    services = SimpleNamespace(
        clock=SimpleNamespace(now=lambda: "2026-01-01T00:00:00Z"),
        config=SimpleNamespace(read_graph_affecting_config=lambda: {
            "capacity_mode": "guaranteed", "policy_version": "1",
            "solver_version": "1",
            "compression": {"enabled": True, "codec": "streamznn", "level": 3},
            "numcopies_default": 1,
        }),
        controller_flock=SimpleNamespace(
            hold=lambda: mock.MagicMock(
                __enter__=lambda s: None, __exit__=lambda *a: False)),
        drive_fences=SimpleNamespace(
            hold_all_sorted=lambda ids: mock.MagicMock(
                __enter__=lambda s: tuple(ids), __exit__=lambda *a: False)),
        worker=SimpleNamespace(identity="cold-portal-worker", claim=lambda **k: None),
        lease_ttl=3600,
        state_dir=str(state_dir),
    )

    # fill_api.start always hits execution_service.start_fill first; inject services
    # by wrapping the unified entry so the cold portal process uses the same surface.
    _real_start = execution_service.start_fill

    def _start_with_services(**kw):
        kw.setdefault("services", services)
        kw.setdefault("con", con)
        kw.setdefault("proposal_id", proposal_id)
        return _real_start(**kw)

    execution_service.start_fill = _start_with_services
    try:
        out = fill_api.start({"plan_id": "ark", "proposal_id": proposal_id})
    except Refusal as exc:
        print(json.dumps({"ok": False, "code": exc.code, "surface": "portal"}))
        raise SystemExit(2)
    if isinstance(out, dict) and (
            out.get("ok") is False or out.get("code") or out.get("error")):
        code = out.get("code") or out.get("error") or "UNKNOWN"
        print(json.dumps({"ok": False, "code": code, "surface": "portal"}))
        raise SystemExit(2)
    print(json.dumps({"ok": True, "surface": "portal", "out": str(type(out))}))
    raise SystemExit(0)
    """
)


def _cold_python(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Cold process via the same interpreter that runs the installed package."""
    return subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_json_line(proc: subprocess.CompletedProcess[str]) -> dict:
    lines = [ln for ln in (proc.stdout or "").strip().splitlines()
             if ln.strip().startswith("{")]
    assert lines, f"child produced no JSON; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return json.loads(lines[-1])


def _seed_live_parent(tmp_path):
    catalog_dir = tmp_path / "data"
    catalog_dir.mkdir()
    state_a = tmp_path / "state-a"
    state_b = tmp_path / "state-b"
    state_a.mkdir()
    state_b.mkdir()

    prev_dir, prev_path, prev_state = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    db.configure(data_dir=catalog_dir, state_dir=state_a)
    con = db.connect()
    f.seed_plan_selection(con, repos=("org/a",))
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, pid, _ = f.create_and_approve(con)

    svc_a = f.default_services()
    svc_a.state_dir = str(state_a)
    mod = f.session_api()
    f.require_success(
        mod.start_session(con, pid, None, svc_a),
        label="parent live session",
    )
    return {
        "catalog_dir": catalog_dir,
        "state_a": state_a,
        "state_b": state_b,
        "con": con,
        "pid": pid,
        "prev": (prev_dir, prev_path, prev_state),
    }


def _restore_db(prev):
    db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = prev


def test_cold_cli_adapter_same_catalog_other_state_dir_refuses(tmp_path):
    """Cold CLI Fill adapter (start_fill_via_service) must see FILL_SESSION_ACTIVE."""
    ctx = _seed_live_parent(tmp_path)
    try:
        child = _cold_python(
            _CHILD_CLI_SCRIPT,
            str(ctx["catalog_dir"]), str(ctx["state_b"]), ctx["pid"],
        )
        assert child.returncode != 0, (
            f"cold CLI child must refuse while live; "
            f"stdout={child.stdout!r} stderr={child.stderr!r}")
        payload = _parse_json_line(child)
        assert payload.get("ok") is False
        assert str(payload.get("code") or "").upper() == "FILL_SESSION_ACTIVE", payload
        assert payload.get("surface") == "cli"
        ctx["con"].close()
    finally:
        _restore_db(ctx["prev"])


def test_cold_portal_entrypoint_same_catalog_other_state_dir_refuses(tmp_path):
    """Cold portal fill_api.start (same entrypoint) must see FILL_SESSION_ACTIVE."""
    ctx = _seed_live_parent(tmp_path)
    try:
        child = _cold_python(
            _CHILD_PORTAL_SCRIPT,
            str(ctx["catalog_dir"]), str(ctx["state_b"]), ctx["pid"],
        )
        assert child.returncode != 0, (
            f"cold portal child must refuse while live; "
            f"stdout={child.stdout!r} stderr={child.stderr!r}")
        payload = _parse_json_line(child)
        assert payload.get("ok") is False
        assert str(payload.get("code") or "").upper() == "FILL_SESSION_ACTIVE", payload
        assert payload.get("surface") == "portal"
        ctx["con"].close()
    finally:
        _restore_db(ctx["prev"])


def test_installed_console_script_resolves_and_imports_fill_adapter():
    """Installed ``modelark`` console script + CLI Fill adapter must be importable."""
    candidate = Path(sys.executable).with_name("modelark")
    assert candidate.exists(), f"expected installed modelark at {candidate}"
    help_proc = subprocess.run(
        [str(candidate), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0, help_proc.stderr
    assert "modelark" in (help_proc.stdout + help_proc.stderr).lower()
    # Same installed interpreter must expose the CLI Fill adapter entry.
    import_proc = subprocess.run(
        [sys.executable, "-c",
         "from modelark.cli import start_fill_via_service; "
         "from modelark.web.fill_api import start; "
         "assert callable(start_fill_via_service) and callable(start); "
         "print('fill-adapters-ok')"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert import_proc.returncode == 0, import_proc.stderr
    assert "fill-adapters-ok" in import_proc.stdout
