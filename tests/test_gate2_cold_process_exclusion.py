"""PR-09 Gate 2: cold exec-style installed-process exclusion proof (B8).

Start one live session against a shared on-disk catalog, then launch a second
**cold** process of the same installed package entry path with a different
state directory. Exact FILL_SESSION_ACTIVE is required.

No production multiprocessing and no fork/spawn selection — only independent
exec of the same entrypoint (``python -m modelark`` package path / console script).
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import _pr09_gate1_fixtures as f
from modelark.core import db


_CHILD_SCRIPT = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path
    from types import SimpleNamespace
    from unittest import mock
    from modelark.core import db
    from modelark import execution_session as esess
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
        worker=SimpleNamespace(identity="cold-child-worker", claim=lambda **k: None),
        lease_ttl=3600,
        state_dir=str(state_dir),
    )
    try:
        out = esess.start_session(con, proposal_id, None, services)
    except Refusal as exc:
        print(json.dumps({"ok": False, "code": exc.code}))
        raise SystemExit(2)
    if isinstance(out, Refusal):
        print(json.dumps({"ok": False, "code": out.code}))
        raise SystemExit(2)
    sid = getattr(getattr(out, "session", out), "session_id", None)
    print(json.dumps({"ok": True, "session_id": sid}))
    raise SystemExit(0)
    """
)


def _cold_python(*args: str) -> subprocess.CompletedProcess[str]:
    """Cold process via the same interpreter that runs the installed package."""
    return subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cold_second_process_same_catalog_other_state_dir_refuses(tmp_path):
    catalog_dir = tmp_path / "data"
    catalog_dir.mkdir()
    state_a = tmp_path / "state-a"
    state_b = tmp_path / "state-b"
    state_a.mkdir()
    state_b.mkdir()

    prev_dir, prev_path, prev_state = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
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
        # Parent holds live row; leave connection open so WAL readers see it.
        child = _cold_python(str(catalog_dir), str(state_b), pid)
        assert child.returncode != 0, (
            f"cold child must refuse while live; stdout={child.stdout!r} stderr={child.stderr!r}"
        )
        # Parse last JSON line from stdout
        lines = [ln for ln in child.stdout.strip().splitlines() if ln.strip().startswith("{")]
        assert lines, f"child produced no JSON; stdout={child.stdout!r} stderr={child.stderr!r}"
        payload = json.loads(lines[-1])
        assert payload.get("ok") is False
        assert str(payload.get("code") or "").upper() == "FILL_SESSION_ACTIVE", payload
        con.close()
    finally:
        db.CATALOG_DIR = prev_dir
        db.DB_PATH = prev_path
        db.STATE_DIR = prev_state


def test_cold_console_script_entry_available():
    """Installed console script ``modelark`` must resolve (packaging smoke companion)."""
    # Prefer the venv sibling of sys.executable (installed editable in Gate-2 runs).
    candidate = Path(sys.executable).with_name("modelark")
    assert candidate.exists(), f"expected installed modelark at {candidate}"
    proc = subprocess.run(
        [str(candidate), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "modelark" in (proc.stdout + proc.stderr).lower()
