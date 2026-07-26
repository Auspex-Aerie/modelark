"""PR-09 Gate 2: cold installed CLI / portal exclusion (B8).

Parent holds a live session on a shared on-disk catalog. A second cold process
invokes the **installed** CLI entrypoint (``modelark session start``) and the
portal ``fill_api.start`` surface without monkey-patching the execution service.
Exact ``FILL_SESSION_ACTIVE`` is required.

No production multiprocessing and no fork/spawn selection.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import _pr09_gate1_fixtures as f
from modelark.core import db


def _modelark_bin() -> Path:
    candidate = Path(sys.executable).with_name("modelark")
    assert candidate.exists(), f"expected installed modelark at {candidate}"
    return candidate


def _parse_json_line(text: str) -> dict:
    lines = [ln for ln in (text or "").strip().splitlines() if ln.strip().startswith("{")]
    assert lines, f"no JSON line in output: {text!r}"
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


def test_cold_installed_cli_session_start_refuses(tmp_path):
    """Installed ``modelark session start`` against live catalog → FILL_SESSION_ACTIVE."""
    ctx = _seed_live_parent(tmp_path)
    try:
        proc = subprocess.run(
            [
                str(_modelark_bin()),
                "--data-dir", str(ctx["catalog_dir"]),
                "--state-dir", str(ctx["state_b"]),
                "session", "start",
                "--proposal-id", ctx["pid"],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0, (
            f"installed CLI must refuse; stdout={proc.stdout!r} stderr={proc.stderr!r}")
        payload = _parse_json_line(proc.stdout + "\n" + proc.stderr)
        assert payload.get("ok") is False
        assert str(payload.get("code") or "").upper() == "FILL_SESSION_ACTIVE", payload
        ctx["con"].close()
    finally:
        _restore_db(ctx["prev"])


_PORTAL_CHILD = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from modelark.core import db
    from modelark.web import data as web_data, fill_api

    data_dir = Path(sys.argv[1])
    state_dir = Path(sys.argv[2])
    proposal_id = sys.argv[3]
    db.configure(data_dir=data_dir, state_dir=state_dir)
    con = db.connect()
    web_data._con = con
    # No monkey-patch of execution_service — production services + catalog authority.
    out = fill_api.start({"plan_id": "ark", "proposal_id": proposal_id})
    if isinstance(out, dict) and (
            out.get("ok") is False or out.get("code") or out.get("error")):
        code = out.get("code") or out.get("error")
        print(json.dumps({"ok": False, "code": code, "surface": "portal"}))
        raise SystemExit(2)
    print(json.dumps({"ok": True, "surface": "portal"}))
    raise SystemExit(0)
    """
)


def test_cold_portal_fill_api_start_refuses_without_service_patch(tmp_path):
    """Cold portal fill_api.start (no service monkey-patch) → FILL_SESSION_ACTIVE."""
    ctx = _seed_live_parent(tmp_path)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PORTAL_CHILD,
             str(ctx["catalog_dir"]), str(ctx["state_b"]), ctx["pid"]],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode != 0, (
            f"portal child must refuse; stdout={proc.stdout!r} stderr={proc.stderr!r}")
        payload = _parse_json_line(proc.stdout)
        assert payload.get("ok") is False
        assert str(payload.get("code") or "").upper() == "FILL_SESSION_ACTIVE", payload
        ctx["con"].close()
    finally:
        _restore_db(ctx["prev"])


def test_installed_console_script_session_help():
    proc = subprocess.run(
        [str(_modelark_bin()), "session", "start", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "proposal" in (proc.stdout + proc.stderr).lower()
