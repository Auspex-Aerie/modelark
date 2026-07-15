"""Portal endpoints for the first-class Plan (#33): the plan list + active plan (the Plans tab #35),
the active plan's live capacity numbers (the two bars #36), the cart-aware graduated gate (#38), and
create / select / capacity-mode mutations. Read-mostly; every call holds data._lock (conn()/plan.*
don't re-acquire it, so this is safe under the lock)."""
from __future__ import annotations

import re

from modelark.web import data

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _api_plan(p: dict) -> dict:
    """Canonical response plus the one-release legacy output alias."""
    return {
        "plan_id": p["plan_id"], "name": p["name"],
        "capacity_mode": p["capacity_mode"],
        "provisioning": p["provisioning"],
        "deprecated_fields": ["provisioning"],
        "is_active": p["is_active"], "n_drives": len(p["drives"]), "drives": p["drives"],
    }


def overview() -> dict:
    """Everything the Plans tab / bars / gate need in one read: the plan list, the active plan id, and
    the active plan's three live numbers over the finalized selection."""
    from modelark import plan
    with data._lock:
        con = data.conn()
        plan.bootstrap(con)                              # ensure `ark` exists + is active
        plans = [_api_plan(p) for p in plan.list_plans(con)]
        active = plan.active(con)
        totals = plan.totals(con, active["plan_id"]) if active else None
    return {"plans": plans, "active": active["plan_id"] if active else None, "totals": totals}


def totals() -> dict:
    """The active plan's live numbers over the finalized selection (the #36 bars, refreshed per shard)."""
    from modelark import plan
    with data._lock:
        con = data.conn()
        active = plan.active(con) or plan.bootstrap(con)
        return plan.totals(con, active["plan_id"])


def cart() -> dict:
    """The active plan's live numbers over the WHOLE cart + the graduated gate tier (#38)."""
    from modelark import plan
    with data._lock:
        con = data.conn()
        active = plan.active(con) or plan.bootstrap(con)
        return plan.cart_totals(con, active["plan_id"])


def shadow_explain() -> dict:
    """Read-only Phase-2 graph/ledger evidence without holding the portal's shared lock."""
    from modelark import plan, reconcile
    from modelark.core import db
    con = db.connect(read_only=True)
    try:
        active = plan.active(con)
        if active is None:
            return {"ok": False, "error": "no active plan"}
        try:
            return reconcile.shadow_report(
                con,
                active["plan_id"],
                capacity_mode=active["capacity_mode"],
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": {
                    "code": "SHADOW_CAPACITY_ERROR",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            }
    finally:
        con.close()


def select(body: dict) -> dict:
    from modelark import plan
    with data._lock:
        p = plan.set_active(data.conn(), body["plan_id"])
    return {"ok": True, "active": p["plan_id"]}


def create(body: dict) -> dict:
    pid = (body.get("plan_id") or "").strip().lower()
    if not _SLUG.match(pid):
        return {"ok": False, "error": "plan id must be a slug: a-z 0-9 _ - (≤32 chars)"}
    from modelark import plan
    with data._lock:
        con = data.conn()
        if plan.get(con, pid) is not None:
            return {"ok": False, "error": f"plan '{pid}' already exists"}
        canonical = body.get("capacity_mode")
        legacy = body.get("provisioning")
        try:
            if canonical is not None and legacy is not None:
                if plan.normalize_capacity_mode(canonical) != plan.normalize_capacity_mode(legacy):
                    return {"ok": False, "error": "capacity_mode and provisioning disagree"}
            mode = canonical if canonical is not None else (
                legacy if legacy is not None else "guaranteed"
            )
            p = plan.create(con, pid, name=(body.get("name") or pid), capacity_mode=mode)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    out = {"ok": True, **_api_plan(p)}
    if legacy is not None:
        out["warnings"] = ["provisioning is deprecated; use capacity_mode"]
    return out


def set_capacity_mode(body: dict) -> dict:
    from modelark import plan
    with data._lock:
        con = data.conn()
        pid = body.get("plan_id") or (plan.active(con) or plan.bootstrap(con))["plan_id"]
        raw = body.get("capacity_mode")
        legacy_used = raw is None
        if raw is None:
            raw = body.get("mode", body.get("provisioning"))
        if raw is None:
            return {"ok": False, "error": "capacity_mode is required"}
        try:
            p = plan.set_capacity_mode(con, pid, raw)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
    out = {"ok": True, **_api_plan(p)}
    if legacy_used:
        out["warnings"] = ["mode/provisioning is deprecated; use capacity_mode"]
    return out


def set_provisioning(body: dict) -> dict:
    """Deprecated endpoint adapter retained for one compatibility release."""
    adapted = dict(body)
    if "capacity_mode" not in adapted:
        adapted["provisioning"] = adapted.get("provisioning", adapted.get("mode"))
    result = set_capacity_mode(adapted)
    result.setdefault("warnings", []).append(
        "/api/plan/provisioning is deprecated; use /api/plan/capacity-mode"
    )
    return result
