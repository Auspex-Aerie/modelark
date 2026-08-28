"""Read-only drive discovery and operator-confirmed lifecycle portal surfaces."""
from __future__ import annotations

from typing import Callable

from modelark import drive_lifecycle, plan, planning, proposal
from modelark.web import data, disk_api


def _refused(exc: proposal.Refusal) -> dict:
    return {
        "ok": False,
        "refused": {
            "code": exc.code,
            "evidence": exc.evidence,
            "actions": list(exc.actions),
        },
    }


def overview(*, inventory: dict | None = None) -> dict:
    """Registered identities plus passive attached inventory; never changes either."""
    if inventory is None:
        inventory = disk_api.attached_inventory()
    available = bool(inventory.get("available"))
    devices = inventory.get("devices") or []
    with data._lock:
        con = data.conn()
        mapped = drive_lifecycle.observe_registered(con, devices if available else [])
        if not available:
            for item in mapped["registered"]:
                item["observation"] = "inventory_unavailable"
        memberships: dict[str, list[dict]] = {}
        for drive_label, plan_id, is_active in con.execute(
            "SELECT pd.drive_label,p.plan_id,p.is_active FROM plan_drives pd "
            "JOIN plans p USING(plan_id) ORDER BY pd.drive_label,p.plan_id"
        ).fetchall():
            memberships.setdefault(str(drive_label), []).append({
                "plan_id": str(plan_id), "is_active": bool(is_active),
            })
        for item in mapped["registered"]:
            item["plans"] = memberships.get(str(item["drive_label"]), [])
        revision = drive_lifecycle.planner_revision(con)
    return {
        "ok": True,
        "planner_revision": revision,
        "inventory_available": available,
        "observation_authority": "advisory_only",
        "registered": mapped["registered"],
        "unregistered": mapped["unregistered"] if available else [],
        "message": (
            "Attached inventory is observation only. Missing never means lost, and unregistered "
            "hardware is never assigned a role automatically."
        ),
    }


def loss_preview(drive_label: str) -> dict:
    try:
        with data._lock:
            preview = drive_lifecycle.loss_preview(data.conn(), drive_label)
        return {"ok": True, "preview": preview}
    except proposal.Refusal as exc:
        return _refused(exc)


def onboarding_preview(
    dev: str,
    serial: str,
    *,
    inventory: dict | None = None,
    topology: dict | None = None,
) -> dict:
    """Rebind one passive observation and return a read-only new-label preview."""
    if not dev or not serial:
        return _refused(proposal.Refusal(
            "DRIVE_ONBOARDING_REQUEST_INCOMPLETE",
            {"required": ["dev", "serial"]},
            ("refresh_drive_inventory",),
        ))
    inventory = inventory if inventory is not None else disk_api.attached_inventory()
    if not inventory.get("available"):
        return _refused(proposal.Refusal(
            "DRIVE_INVENTORY_UNAVAILABLE",
            {},
            ("refresh_drive_inventory",),
        ))
    matches = [
        item for item in inventory.get("devices") or []
        if str(item.get("dev") or "") == dev and str(item.get("serial") or "") == serial
    ]
    if len(matches) != 1:
        return _refused(proposal.Refusal(
            "DRIVE_ONBOARDING_OBSERVATION_STALE",
            {"dev": dev, "serial": serial, "matches": len(matches)},
            ("refresh_drive_inventory",),
        ))
    topology = topology if topology is not None else disk_api.registration_topology(dev)
    try:
        with data._lock:
            preview = drive_lifecycle.onboarding_preview(data.conn(), matches[0], topology)
        return {"ok": True, "preview": preview}
    except proposal.Refusal as exc:
        return _refused(exc)


def declare_lost(body: dict, *, observe: Callable[[str], object | None] | None = None) -> dict:
    """Apply the exact loss transition, then show one canonical post-change replan."""
    required = {
        "drive_label", "expected_revision", "expected_identity_epoch",
        "expected_identity_fingerprint", "confirmation",
    }
    missing = sorted(required - body.keys())
    if missing:
        return _refused(proposal.Refusal(
            "DRIVE_LOSS_REQUEST_INCOMPLETE",
            {"missing": missing},
            ("refresh_loss_preview",),
        ))
    try:
        with data._lock:
            con = data.conn()
            before = drive_lifecycle.loss_preview(con, str(body["drive_label"]))
            changed = drive_lifecycle.declare_lost(
                con,
                str(body["drive_label"]),
                expected_revision=body["expected_revision"],
                expected_identity_epoch=body["expected_identity_epoch"],
                expected_identity_fingerprint=body["expected_identity_fingerprint"],
                confirmation=body["confirmation"],
            )
            active = plan.active(con)
            totals = None
            replan = None
            replan_error = None
            if active is not None:
                try:
                    totals = plan.totals(con, active["plan_id"])
                    result = planning.preview(
                        con,
                        active["plan_id"],
                        **({"observe": observe} if observe is not None else {}),
                    )
                    targets: dict[str, int] = {}
                    for task in result.capacity.tasks:
                        targets[task.target_drive] = targets.get(task.target_drive, 0) + 1
                    replan = {
                        "plan_id": active["plan_id"],
                        "planner_revision": result.planner_revision,
                        "capacity_mode": result.policy.capacity_mode.value,
                        "feasible": result.feasible,
                        "root_code": result.root_code,
                        "blocking_codes": list(result.blocking_codes),
                        "executable_tasks": len(result.capacity.tasks),
                        "target_counts": targets,
                    }
                except Exception as exc:
                    replan_error = f"{type(exc).__name__}: {exc}"
        return {
            "ok": True,
            "transition": changed,
            "before": {
                "planner_revision": before["planner_revision"],
                "active_plan_id": before["active_plan_id"],
                "active_plan_capacity_bytes": before["active_plan_capacity_bytes"],
            },
            "after": {"totals": totals, "replan": replan, "replan_error": replan_error},
            "preserved": {
                "plan_membership": before["plans"],
                "archived_rows": before["archived_rows"],
                "archived_repositories": before["archived_repositories"],
                "archived_stored_bytes": before["archived_stored_bytes"],
                "replica_rows": before["replica_rows"],
            },
        }
    except proposal.Refusal as exc:
        return _refused(exc)
