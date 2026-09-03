"""Unified Fill / execution entry (PR-09 / B8). No production multiprocessing."""
from __future__ import annotations

import os
import socket
from types import SimpleNamespace
from typing import Any

from modelark import execution_session as esess
from modelark.proposal import Refusal


def production_services(con=None, *, catalog_path=None, state_dir=None) -> SimpleNamespace:
    """Real controller/drive fences, clock, config, capacity evidence, and worker identity.

    No MagicMock locks and no frozen synthetic clock in production entry.
    """
    from modelark import drive_fence, plan as plan_mod
    from modelark.core import db
    from modelark.proposal import _DefaultClock, _DefaultServices

    cat = str(catalog_path or getattr(db, "DB_PATH", None) or ":memory:")
    base = _DefaultServices()
    clock = _DefaultClock()

    class _Config:
        def read_graph_affecting_config(self):
            from modelark import wishlist as _wl
            try:
                compression = dict(_wl.compression() or {})
            except Exception:
                compression = {"enabled": True, "codec": "streamznn", "level": 3}
            policy_version = "1"
            solver_version = "1"
            if con is not None:
                try:
                    versions = con.execute(
                        "SELECT pp.policy_version,pp.solver_version "
                        "FROM planner_state ps LEFT JOIN placement_proposals pp "
                        "ON pp.proposal_id=ps.active_approved_proposal_id "
                        "WHERE ps.singleton_id=1"
                    ).fetchone()
                    if versions and versions[0] and versions[1]:
                        policy_version = str(versions[0])
                        solver_version = str(versions[1])
                except Exception:
                    pass
            if con is None:
                return {
                    "capacity_mode": "guaranteed",
                    "policy_version": policy_version,
                    "solver_version": solver_version,
                    "compression": compression,
                    "numcopies_default": 1,
                }
            prow = plan_mod.active(con) or plan_mod.get(con, "ark")
            mode = (prow or {}).get("capacity_mode") or "guaranteed"
            return {
                "capacity_mode": mode,
                "policy_version": policy_version,
                "solver_version": solver_version,
                "compression": compression,
                "numcopies_default": 1,
            }

    class _ControllerFlock:
        def hold(self):
            return drive_fence.hold_controller(cat, blocking=True)

    class _DriveFences:
        def hold_all_sorted(self, labels):
            keys = []
            labels = list(labels or ())
            if con is not None:
                for label in labels:
                    row = con.execute(
                        "SELECT identity_fingerprint, identity_epoch FROM drives "
                        "WHERE drive_label=?", [label]).fetchone()
                    if row and row[0]:
                        keys.append((row[0], int(row[1])))
            if not keys and labels:
                # Fall back to label-as-identity for synthetic/test catalogs without fingerprints
                keys = [(str(lab), 1) for lab in labels]
            return drive_fence.hold_drives_sorted(keys, blocking=True)

    host = socket.gethostname()
    worker = SimpleNamespace(
        identity=f"worker-{host}-{os.getpid()}",
        claim=None,
        inherit_fence_fds=lambda fds: list(fds),
    )
    return SimpleNamespace(
        clock=clock,
        config=_Config(),
        controller_flock=_ControllerFlock(),
        drive_fences=_DriveFences(),
        worker=worker,
        controller_identity=f"controller-{host}-{os.getpid()}",
        lease_ttl=int(getattr(base, "lease_ttl", None) or 3600),
        observe_exact_capacity=base.observe_exact_capacity,
        state_dir=state_dir or str(getattr(db, "STATE_DIR", "") or ""),
        catalog_path=cat,
    )


def start_fill(*, plan_id: str = "ark", proposal_id: str | None = None,
               con=None, services=None, predecessor_id=None, **_k) -> Any:
    """Single service entry for CLI / portal / systemd resume surfaces."""
    if con is None or not hasattr(con, "execute"):
        from modelark.core import db
        con = db.connect()
    if services is None:
        services = production_services(con)
    if proposal_id is None:
        try:
            row = con.execute(
                "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
            ).fetchone()
            proposal_id = row[0] if row else None
        except Exception:
            proposal_id = None
    if not proposal_id:
        return Refusal("APPROVAL_MISSING", {}, ("preview_again",))
    return esess.start_session(con, proposal_id, predecessor_id, services)


enter_execution = start_fill
cli_start_fill = start_fill


def auto_resume_fill(body: dict | None = None) -> dict:
    """Systemd resume surface — same unified service (exactly one start_fill)."""
    body = body or {}
    out = start_fill(**{k: body[k] for k in ("plan_id", "proposal_id", "predecessor_id")
                        if k in body})
    if isinstance(out, Refusal):
        return {"ok": False, "error": out.code, "code": out.code,
                "evidence": getattr(out, "evidence", None),
                "actions": list(getattr(out, "actions", ()) or ())}
    return {"ok": True, "session": getattr(out, "session", out), "session_start": out}
