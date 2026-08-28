"""Operator-authorized drive lifecycle transitions.

Physical observation is evidence, never lifecycle authority.  In particular, a registered drive
that is not currently attached remains ``active`` until an operator explicitly declares the exact
catalog identity lost.  The transition preserves plan membership and every residency/provenance
row so historical claims remain reviewable, while excluding the identity from new placement.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from modelark import plan, proposal


def planner_revision(con) -> int:
    row = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()
    if row is None:
        raise RuntimeError("planner_state singleton missing")
    return int(row[0])


def _drive(con, drive_label: str) -> dict[str, Any]:
    row = con.execute(
        "SELECT drive_label,lifecycle,eligibility,identity_epoch,identity_fingerprint,"
        "serial,hw_model,capacity_bytes,filesystem_capacity_bytes,last_seen "
        "FROM drives WHERE drive_label=?",
        [drive_label],
    ).fetchone()
    if row is None:
        raise proposal.Refusal(
            "DRIVE_NOT_REGISTERED",
            {"drive_label": drive_label},
            ("refresh_drive_inventory",),
        )
    return dict(zip(
        (
            "drive_label", "lifecycle", "eligibility", "identity_epoch",
            "identity_fingerprint", "serial", "hw_model", "capacity_bytes",
            "filesystem_capacity_bytes", "last_seen",
        ),
        row,
    ))


def loss_preview(con, drive_label: str) -> dict[str, Any]:
    """Return the exact catalog state an operator must bind before declaring loss."""
    drive = _drive(con, drive_label)
    memberships = [
        {"plan_id": str(row[0]), "is_active": bool(row[1])}
        for row in con.execute(
            "SELECT p.plan_id,p.is_active FROM plan_drives pd "
            "JOIN plans p USING(plan_id) WHERE pd.drive_label=? ORDER BY p.plan_id",
            [drive_label],
        ).fetchall()
    ]
    archived = con.execute(
        "SELECT count(*),count(DISTINCT repo_id),coalesce(sum(stored_bytes),0) "
        "FROM archived WHERE drive_label=?",
        [drive_label],
    ).fetchone()
    replicas = con.execute(
        "SELECT count(*),coalesce(sum(CASE WHEN present THEN 1 ELSE 0 END),0) "
        "FROM replicas WHERE drive_label=?",
        [drive_label],
    ).fetchone()
    active_plan = plan.active(con)
    active_capacity = (
        plan.capacity(con, active_plan["plan_id"]) if active_plan is not None else 0
    )
    return {
        **drive,
        "planner_revision": planner_revision(con),
        "plans": memberships,
        "active_plan_id": active_plan["plan_id"] if active_plan else None,
        "active_plan_capacity_bytes": int(active_capacity),
        "archived_rows": int(archived[0]),
        "archived_repositories": int(archived[1]),
        "archived_stored_bytes": int(archived[2]),
        "replica_rows": int(replicas[0]),
        "replicas_recorded_present": int(replicas[1]),
        "confirmation": f"DECLARE LOST {drive_label}",
        "warning": (
            "Not currently observed means offline or missing only. ModelArk never promotes "
            "that observation to lost automatically."
        ),
    }


def observe_registered(
    con,
    devices: Iterable[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Map passive hardware inventory to registered identities by unique exact serial only.

    The mapping is deliberately read-only.  Unmatched devices are reported as unregistered and
    never inherit a label, lifecycle, plan membership, or archived facts.
    """
    observed = [dict(item) for item in devices]
    by_serial: dict[str, list[dict[str, Any]]] = {}
    for item in observed:
        serial = str(item.get("serial") or "").strip()
        if serial and serial != "—":
            by_serial.setdefault(serial, []).append(item)

    registered_rows = con.execute(
        "SELECT drive_label,lifecycle,eligibility,identity_epoch,identity_fingerprint,"
        "serial,hw_model,capacity_bytes,last_seen FROM drives ORDER BY drive_label"
    ).fetchall()
    registered_serials: dict[str, int] = {}
    for row in registered_rows:
        serial = str(row[5] or "").strip()
        if serial and serial != "—":
            registered_serials[serial] = registered_serials.get(serial, 0) + 1
    registered: list[dict[str, Any]] = []
    matched_device_ids: set[int] = set()
    for row in registered_rows:
        drive = dict(zip(
            (
                "drive_label", "lifecycle", "eligibility", "identity_epoch",
                "identity_fingerprint", "serial", "hw_model", "capacity_bytes", "last_seen",
            ),
            row,
        ))
        serial = str(drive.get("serial") or "").strip()
        matches = by_serial.get(serial, []) if serial and serial != "—" else []
        if registered_serials.get(serial, 0) > 1:
            observation = "ambiguous_serial"
            device = None
            matched_device_ids.update(id(item) for item in matches)
        elif len(matches) == 1:
            observation = "attached_exact_serial"
            device = matches[0]
            matched_device_ids.add(id(device))
        elif len(matches) > 1:
            observation = "ambiguous_serial"
            device = None
            matched_device_ids.update(id(item) for item in matches)
        elif serial:
            observation = "not_attached"
            device = None
        else:
            observation = "identity_unproven"
            device = None
        registered.append({**drive, "observation": observation, "device": device})

    unregistered = [
        {**item, "observation": "unregistered", "action_taken": False}
        for item in observed
        if id(item) not in matched_device_ids
    ]
    return {"registered": registered, "unregistered": unregistered}


def declare_lost(
    con,
    drive_label: str,
    *,
    expected_revision: int,
    expected_identity_epoch: int,
    expected_identity_fingerprint: str | None,
    confirmation: str,
) -> dict[str, Any]:
    """Atomically mark an exact registered identity lost+excluded and invalidate approval.

    The drive row, identity epoch/fingerprint, plan membership, archived rows, and replica evidence
    are retained.  A live Fill refuses the graph write through ``proposal.graph_write``.
    """
    required_confirmation = f"DECLARE LOST {drive_label}"
    if confirmation != required_confirmation:
        raise proposal.Refusal(
            "DRIVE_LOSS_CONFIRMATION_MISMATCH",
            {"required": required_confirmation},
            ("type_exact_confirmation",),
        )
    if (not isinstance(expected_revision, int) or isinstance(expected_revision, bool)
            or not isinstance(expected_identity_epoch, int)
            or isinstance(expected_identity_epoch, bool)):
        raise proposal.Refusal(
            "DRIVE_LOSS_BINDING_INVALID",
            {"expected_revision": expected_revision,
             "expected_identity_epoch": expected_identity_epoch},
            ("refresh_loss_preview",),
        )

    def op(c):
        current_revision = planner_revision(c)
        drive = _drive(c, drive_label)
        current_binding = {
            "planner_revision": current_revision,
            "identity_epoch": int(drive["identity_epoch"]),
            "identity_fingerprint": drive["identity_fingerprint"],
        }
        expected_binding = {
            "planner_revision": expected_revision,
            "identity_epoch": expected_identity_epoch,
            "identity_fingerprint": expected_identity_fingerprint,
        }
        if current_binding != expected_binding:
            raise proposal.Refusal(
                "DRIVE_LOSS_PREVIEW_STALE",
                {"expected": expected_binding, "current": current_binding},
                ("refresh_loss_preview",),
            )
        if drive["lifecycle"] == "retired":
            raise proposal.Refusal(
                "DRIVE_ALREADY_RETIRED",
                {"drive_label": drive_label},
                ("review_drive_history",),
            )
        if drive["lifecycle"] == "lost" and drive["eligibility"] == "excluded":
            return proposal.GraphResult(
                proven_noop=True,
                value={"changed": False, "approval_invalidated": False},
            )

        active_approval = c.execute(
            "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
        ).fetchone()[0]
        approved_rows = int(c.execute(
            "SELECT count(*) FROM placement_proposals WHERE lifecycle='approved'"
        ).fetchone()[0])
        c.execute(
            "UPDATE placement_proposals SET lifecycle='superseded', "
            "superseded_at=CURRENT_TIMESTAMP WHERE lifecycle='approved'"
        )
        c.execute(
            "UPDATE planner_state SET active_approved_proposal_id=NULL WHERE singleton_id=1"
        )
        c.execute(
            "UPDATE drives SET lifecycle='lost',eligibility='excluded' WHERE drive_label=?",
            [drive_label],
        )
        return proposal.GraphResult(
            proven_noop=False,
            value={
                "changed": True,
                "approval_invalidated": active_approval is not None or approved_rows > 0,
            },
        )

    result = proposal.graph_write(con, op)
    drive = _drive(con, drive_label)
    return {
        "drive_label": drive_label,
        "lifecycle": drive["lifecycle"],
        "eligibility": drive["eligibility"],
        "identity_epoch": int(drive["identity_epoch"]),
        "identity_fingerprint": drive["identity_fingerprint"],
        "planner_revision": planner_revision(con),
        **(result.value or {}),
    }
