"""Unified Fill / execution entry (PR-09 / B8). No production multiprocessing."""
from __future__ import annotations

from typing import Any

from modelark import execution_session as esess
from modelark.proposal import Refusal


def start_fill(*, plan_id: str = "ark", proposal_id: str | None = None,
               con=None, services=None, **_k) -> Any:
    """Single service entry for CLI / portal / systemd resume surfaces."""
    if con is None:
        from modelark.core import db
        con = db.connect()
    if services is None:
        # Minimal services for production entry; tests inject mocks via patch
        from types import SimpleNamespace
        from unittest import mock
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
            worker=SimpleNamespace(identity="worker-local"),
            lease_ttl=3600,
        )
    if proposal_id is None:
        row = con.execute(
            "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
        ).fetchone()
        proposal_id = row[0] if row else None
    if not proposal_id:
        return Refusal("APPROVAL_MISSING", {}, ("preview_again",))
    return esess.start_session(con, proposal_id, None, services)


enter_execution = start_fill
cli_start_fill = start_fill


def auto_resume_fill(body: dict | None = None) -> dict:
    """Systemd resume surface — same unified service."""
    body = body or {}
    out = start_fill(**{k: body[k] for k in ("plan_id", "proposal_id") if k in body})
    if isinstance(out, Refusal):
        return {"ok": False, "error": out.code, "code": out.code}
    return {"ok": True, "session": getattr(out, "session", out)}
