"""Operator-authorized drive lifecycle transitions.

Physical observation is evidence, never lifecycle authority.  In particular, a registered drive
that is not currently attached remains ``active`` until an operator explicitly declares the exact
catalog identity lost.  The transition preserves plan membership and every residency/provenance
row so historical claims remain reviewable, while excluding the identity from new placement.
"""
from __future__ import annotations

import re
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


def _next_drive_label(con) -> str:
    numbers = []
    for row in con.execute("SELECT drive_label FROM drives").fetchall():
        match = re.fullmatch(r"drive-(\d+)", str(row[0]))
        if match:
            numbers.append(int(match.group(1)))
    number = max(numbers, default=0) + 1
    return f"drive-{number:02d}"


def _lost_identity_summaries(con) -> list[dict[str, Any]]:
    summaries = []
    rows = con.execute(
        "SELECT drive_label,identity_epoch,identity_fingerprint FROM drives "
        "WHERE lifecycle='lost' AND eligibility='excluded' ORDER BY drive_label"
    ).fetchall()
    for drive_label, identity_epoch, identity_fingerprint in rows:
        archived_rows = int(con.execute(
            "SELECT count(*) FROM archived WHERE drive_label=?", [drive_label]
        ).fetchone()[0])
        replica_rows = int(con.execute(
            "SELECT count(*) FROM replicas WHERE drive_label=?", [drive_label]
        ).fetchone()[0])
        plans = [
            {"plan_id": str(plan_id), "is_active": bool(is_active)}
            for plan_id, is_active in con.execute(
                "SELECT p.plan_id,p.is_active FROM plan_drives pd "
                "JOIN plans p USING(plan_id) WHERE pd.drive_label=? ORDER BY p.plan_id",
                [drive_label],
            ).fetchall()
        ]
        summaries.append({
            "drive_label": str(drive_label),
            "identity_epoch": int(identity_epoch),
            "identity_fingerprint": identity_fingerprint,
            "archived_rows": archived_rows,
            "replica_rows": replica_rows,
            "plans": plans,
            "relationship": "not_inherited",
        })
    return summaries


def onboarding_preview(
    con,
    observed_device: Mapping[str, Any],
    topology: Mapping[str, Any],
) -> dict[str, Any]:
    """Explain a new-label registration candidate without probing SMART or changing state."""
    device = dict(observed_device)
    dev = str(device.get("dev") or "")
    serial = str(device.get("serial") or "").strip()
    if not dev or not serial or serial == "—":
        raise proposal.Refusal(
            "DRIVE_ONBOARDING_IDENTITY_UNPROVEN",
            {"dev": dev, "serial": serial or None},
            ("refresh_drive_inventory",),
        )
    collisions = [
        str(row[0])
        for row in con.execute(
            "SELECT drive_label FROM drives WHERE serial=? ORDER BY drive_label", [serial]
        ).fetchall()
    ]
    if collisions:
        raise proposal.Refusal(
            "DRIVE_ONBOARDING_IDENTITY_COLLISION",
            {"serial": serial, "registered_labels": collisions},
            ("review_registered_identity",),
        )
    if str(topology.get("requested_dev") or "") != dev:
        raise proposal.Refusal(
            "DRIVE_ONBOARDING_OBSERVATION_STALE",
            {"observed_dev": dev, "topology_dev": topology.get("requested_dev")},
            ("refresh_drive_inventory",),
        )

    nodes = [dict(node) for node in topology.get("nodes") or []]
    volumes = [node for node in nodes if node.get("fstype")]
    volume = None
    blockers = []
    if topology.get("system_backing"):
        blockers.append("SYSTEM_DEVICE")
    if not topology.get("available"):
        blockers.append("TOPOLOGY_UNAVAILABLE")
    elif not volumes:
        blockers.append("FILESYSTEM_NOT_FOUND")
    elif len(volumes) != 1:
        blockers.append("MULTIPLE_FILESYSTEMS")
    else:
        raw_volume = volumes[0]
        mountpoints = [str(item) for item in raw_volume.get("mountpoints") or [] if item]
        volume = {
            "dev": str(raw_volume.get("dev") or ""),
            "type": raw_volume.get("type"),
            "size_bytes": int(raw_volume.get("size_bytes") or 0),
            "fstype": str(raw_volume.get("fstype") or ""),
            "fs_uuid": raw_volume.get("fs_uuid"),
            "mountpoints": mountpoints,
            "mounted": bool(mountpoints),
            "archive_path": raw_volume.get("archive_path"),
            "archive_state": raw_volume.get("archive_state") or (
                "unmounted" if not mountpoints else "unrecognized"
            ),
            "annex_uuid": raw_volume.get("annex_uuid"),
        }
        if not volume["fs_uuid"]:
            blockers.append("FILESYSTEM_IDENTITY_UNPROVEN")
        fs_collisions = [
            str(row[0])
            for row in con.execute(
                "SELECT drive_label FROM drives WHERE fs_uuid=? ORDER BY drive_label",
                [volume["fs_uuid"]],
            ).fetchall()
        ] if volume["fs_uuid"] else []
        if fs_collisions:
            raise proposal.Refusal(
                "DRIVE_ONBOARDING_IDENTITY_COLLISION",
                {"fs_uuid": volume["fs_uuid"], "registered_labels": fs_collisions},
                ("review_registered_identity",),
            )
        annex_collisions = [
            str(row[0])
            for row in con.execute(
                "SELECT drive_label FROM drives WHERE annex_uuid=? ORDER BY drive_label",
                [volume["annex_uuid"]],
            ).fetchall()
        ] if volume["annex_uuid"] else []
        if annex_collisions:
            raise proposal.Refusal(
                "DRIVE_ONBOARDING_IDENTITY_COLLISION",
                {"annex_uuid": volume["annex_uuid"], "registered_labels": annex_collisions},
                ("review_registered_identity",),
            )
        if volume["fstype"] not in {"ext4", "xfs"}:
            blockers.append("UNSUPPORTED_FILESYSTEM")
        if not volume["mounted"]:
            blockers.append("MOUNT_REQUIRED")
        elif volume["archive_state"] == "annex":
            blockers.append("ANNEX_IDENTITY_PRESENT")
        elif volume["archive_state"] != "absent":
            blockers.append("ARCHIVE_NAMESPACE_OCCUPIED")

    active = plan.active(con)
    label = _next_drive_label(con)
    if "SYSTEM_DEVICE" in blockers:
        next_action = "refuse_system_device"
    elif "TOPOLOGY_UNAVAILABLE" in blockers:
        next_action = "refresh_topology"
    elif "MULTIPLE_FILESYSTEMS" in blockers:
        next_action = "choose_volume"
    elif "FILESYSTEM_NOT_FOUND" in blockers or "UNSUPPORTED_FILESYSTEM" in blockers:
        next_action = "review_filesystem"
    elif "FILESYSTEM_IDENTITY_UNPROVEN" in blockers:
        next_action = "establish_filesystem_identity"
    elif "ARCHIVE_NAMESPACE_OCCUPIED" in blockers:
        next_action = "review_archive_namespace"
    elif "ANNEX_IDENTITY_PRESENT" in blockers:
        next_action = "review_existing_annex"
    elif "MOUNT_REQUIRED" in blockers:
        next_action = "mount_volume"
    else:
        next_action = "review_registration"

    mountpoint = volume["mountpoints"][0] if volume and volume["mountpoints"] else None
    return {
        "planner_revision": planner_revision(con),
        "observation_authority": "read_only",
        "device": device,
        "volume": volume,
        "suggested_label": label,
        "label_policy": "new_label_required",
        "blockers": blockers,
        "ready_for_registration": not blockers,
        "next_action": next_action,
        "registration_preview": {
            "dev": volume["dev"] if volume else None,
            "label": label,
            "mount": mountpoint,
            "format": None,
            "role": "primary",
            "adds_to_active_plan": active["plan_id"] if active else None,
            "requires_reconcile_after_registration": True,
            "inherited_from_lost_identity": [],
        },
        "separate_lost_identities": _lost_identity_summaries(con),
    }


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
