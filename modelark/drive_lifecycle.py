"""Operator-authorized drive lifecycle transitions.

Physical observation is evidence, never lifecycle authority.  In particular, a registered drive
that is not currently attached remains ``active`` until an operator explicitly declares the exact
catalog identity lost.  The transition preserves plan membership and every residency/provenance
row so historical claims remain reviewable, while excluding the identity from new placement.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping

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

    active = plan.active(con)
    label = _next_drive_label(con)
    nodes = [dict(node) for node in topology.get("nodes") or []]
    volumes = [node for node in nodes if node.get("fstype")]
    volume = None
    blockers = []
    if active is None:
        blockers.append("ACTIVE_PLAN_REQUIRED")
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
        if raw_volume.get("registration_receipt") is not None:
            volume["registration_receipt"] = raw_volume.get("registration_receipt")
        for key in (
            "archive_parent_writable",
            "archive_parent_uid",
            "archive_parent_gid",
            "archive_parent_mode",
        ):
            if key in raw_volume:
                volume[key] = raw_volume.get(key)
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
        elif volume["archive_state"] == "prepared_registration":
            expected_receipt = {
                "state": "prepared",
                "label": label,
                "fs_uuid": volume["fs_uuid"],
                "serial": serial,
                "volume_dev": volume["dev"],
            }
            if volume.get("registration_receipt") != expected_receipt:
                blockers.append("PREPARED_REGISTRATION_MISMATCH")
        elif volume["archive_state"] == "annex":
            blockers.append("ANNEX_IDENTITY_PRESENT")
        elif volume["archive_state"] != "absent":
            blockers.append("ARCHIVE_NAMESPACE_OCCUPIED")
        elif volume.get("archive_parent_writable") is False:
            blockers.append("ARCHIVE_PARENT_NOT_WRITABLE")
        elif volume.get("archive_parent_writable") is not True:
            blockers.append("ARCHIVE_PARENT_WRITE_UNPROVEN")

    if "SYSTEM_DEVICE" in blockers:
        next_action = "refuse_system_device"
    elif "ACTIVE_PLAN_REQUIRED" in blockers:
        next_action = "select_active_plan"
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
    elif "PREPARED_REGISTRATION_MISMATCH" in blockers:
        next_action = "review_prepared_registration"
    elif "ARCHIVE_PARENT_NOT_WRITABLE" in blockers:
        next_action = "prepare_archive_permissions"
    elif "ARCHIVE_PARENT_WRITE_UNPROVEN" in blockers:
        next_action = "prove_archive_permissions"
    elif "MOUNT_REQUIRED" in blockers:
        next_action = "mount_volume"
    else:
        next_action = "review_registration"

    mountpoint = volume["mountpoints"][0] if volume and volume["mountpoints"] else None
    registration_binding = {
        "planner_revision": planner_revision(con),
        "dev": dev,
        "serial": serial,
        "volume_dev": volume["dev"] if volume else None,
        "fs_uuid": volume["fs_uuid"] if volume else None,
        "mount": mountpoint,
        "archive_path": volume["archive_path"] if volume else None,
        "archive_state": volume["archive_state"] if volume else None,
        "label": label,
        "plan_id": active["plan_id"] if active else None,
        "role": "primary",
    }
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
        "confirmation": f"REGISTER NEW {label}",
        "registration_binding": registration_binding,
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


_REGISTRATION_BINDING_KEYS = (
    "planner_revision",
    "dev",
    "serial",
    "volume_dev",
    "fs_uuid",
    "mount",
    "archive_path",
    "archive_state",
    "label",
    "plan_id",
    "role",
)


def _exact_existing_registration(
    con,
    *,
    expected: Mapping[str, Any],
    observed_device: Mapping[str, Any],
    topology: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Recognize only a fully committed replay of the same registration request."""
    row = con.execute(
        "SELECT fs_uuid,annex_uuid,serial,role,lifecycle,eligibility FROM drives "
        "WHERE drive_label=?",
        [expected["label"]],
    ).fetchone()
    if row is None:
        return None
    volumes = [dict(node) for node in topology.get("nodes") or [] if node.get("fstype")]
    volume = volumes[0] if len(volumes) == 1 else {}
    memberships = [
        str(item[0])
        for item in con.execute(
            "SELECT plan_id FROM plan_drives WHERE drive_label=? ORDER BY plan_id",
            [expected["label"]],
        ).fetchall()
    ]
    current = {
        "fs_uuid": row[0],
        "annex_uuid": row[1],
        "serial": row[2],
        "role": row[3],
        "lifecycle": row[4],
        "eligibility": row[5],
        "observed_dev": observed_device.get("dev"),
        "volume_dev": volume.get("dev"),
        "volume_fs_uuid": volume.get("fs_uuid"),
        "volume_annex_uuid": volume.get("annex_uuid"),
        "mounted": bool(volume.get("mountpoints")),
        "mountpoints": volume.get("mountpoints") or [],
        "archive_path": volume.get("archive_path"),
        "archive_state": volume.get("archive_state"),
        "registration_receipt": volume.get("registration_receipt"),
        "plans": memberships,
    }
    expected_receipt = {
        "state": "prepared",
        "label": expected["label"],
        "fs_uuid": expected["fs_uuid"],
        "serial": expected["serial"],
        "volume_dev": expected["volume_dev"],
    }
    exact = (
        row[0] == expected["fs_uuid"]
        and bool(row[1])
        and row[1] == volume.get("annex_uuid")
        and row[2] == expected["serial"] == observed_device.get("serial")
        and row[3] == expected["role"]
        and row[4] == "active"
        and row[5] == "enabled"
        and observed_device.get("dev") == expected["dev"]
        and volume.get("dev") == expected["volume_dev"]
        and volume.get("fs_uuid") == expected["fs_uuid"]
        and volume.get("mountpoints") == [expected["mount"]]
        and volume.get("archive_path") == expected["archive_path"]
        and volume.get("archive_state") == "prepared_registration"
        and volume.get("registration_receipt") == expected_receipt
        and expected["plan_id"] in memberships
    )
    if not exact:
        raise proposal.Refusal(
            "DRIVE_REGISTRATION_IDENTITY_COLLISION",
            {"expected": dict(expected), "current": current},
            ("review_registered_identity",),
        )
    return {
        "changed": False,
        "already_registered": True,
        "drive_label": expected["label"],
        "planner_revision": planner_revision(con),
        "plan_id": expected["plan_id"],
        "archive_path": expected["archive_path"],
        "annex_uuid": row[1],
        "approval_invalidated": False,
        "capacity_evidence": "unknown_until_reconcile",
        "reconciliation_required": True,
        "inherited_from_lost_identity": [],
    }


def register_new_identity(
    con,
    observed_device: Mapping[str, Any],
    topology: Mapping[str, Any],
    *,
    expected_binding: Mapping[str, Any],
    confirmation: str,
    prepare_archive: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    """Prepare and register one exact new identity under the central graph-write boundary."""
    if not isinstance(expected_binding, Mapping):
        raise proposal.Refusal(
            "DRIVE_REGISTRATION_BINDING_INVALID",
            {"reason": "binding_not_an_object"},
            ("refresh_onboarding_preview",),
        )
    missing = [key for key in _REGISTRATION_BINDING_KEYS if key not in expected_binding]
    if missing:
        raise proposal.Refusal(
            "DRIVE_REGISTRATION_BINDING_INVALID",
            {"missing": missing},
            ("refresh_onboarding_preview",),
        )
    expected = {key: expected_binding[key] for key in _REGISTRATION_BINDING_KEYS}
    if (not isinstance(expected["planner_revision"], int)
            or isinstance(expected["planner_revision"], bool)
            or not re.fullmatch(r"drive-\d+", str(expected["label"] or ""))):
        raise proposal.Refusal(
            "DRIVE_REGISTRATION_BINDING_INVALID",
            {"binding": expected},
            ("refresh_onboarding_preview",),
        )
    required_confirmation = f"REGISTER NEW {expected['label']}"
    if confirmation != required_confirmation:
        raise proposal.Refusal(
            "DRIVE_REGISTRATION_CONFIRMATION_MISMATCH",
            {"required": required_confirmation},
            ("type_exact_confirmation",),
        )

    already = _exact_existing_registration(
        con,
        expected=expected,
        observed_device=observed_device,
        topology=topology,
    )
    if already is not None:
        return already

    current_preview = onboarding_preview(con, observed_device, topology)
    current_binding = current_preview["registration_binding"]
    if current_binding != expected:
        raise proposal.Refusal(
            "DRIVE_REGISTRATION_PREVIEW_STALE",
            {"expected": expected, "current": current_binding},
            ("refresh_onboarding_preview",),
        )
    if not current_preview["ready_for_registration"]:
        raise proposal.Refusal(
            "DRIVE_REGISTRATION_BLOCKED",
            {
                "blockers": current_preview["blockers"],
                "next_action": current_preview["next_action"],
            },
            (current_preview["next_action"],),
        )
    volume = current_preview["volume"]
    approval_invalidated = False

    def op(c):
        nonlocal approval_invalidated
        current_revision = planner_revision(c)
        active = plan.active(c)
        current_catalog_binding = {
            "planner_revision": current_revision,
            "label": _next_drive_label(c),
            "plan_id": active["plan_id"] if active else None,
        }
        expected_catalog_binding = {
            "planner_revision": expected["planner_revision"],
            "label": expected["label"],
            "plan_id": expected["plan_id"],
        }
        if current_catalog_binding != expected_catalog_binding:
            raise proposal.Refusal(
                "DRIVE_REGISTRATION_PREVIEW_STALE",
                {"expected": expected_catalog_binding, "current": current_catalog_binding},
                ("refresh_onboarding_preview",),
            )
        for column, value in (
            ("drive_label", expected["label"]),
            ("serial", expected["serial"]),
            ("fs_uuid", expected["fs_uuid"]),
        ):
            collision = c.execute(
                f"SELECT drive_label FROM drives WHERE {column}=? ORDER BY drive_label", [value]
            ).fetchall()
            if collision:
                raise proposal.Refusal(
                    "DRIVE_REGISTRATION_IDENTITY_COLLISION",
                    {column: value, "registered_labels": [str(row[0]) for row in collision]},
                    ("review_registered_identity",),
                )
        try:
            prepared = dict(prepare_archive(
                volume_dev=expected["volume_dev"],
                mount=expected["mount"],
                archive_path=expected["archive_path"],
                label=expected["label"],
                fs_uuid=expected["fs_uuid"],
                fstype=volume["fstype"],
                serial=expected["serial"],
                model=observed_device.get("model"),
                role=expected["role"],
            ))
        except proposal.Refusal:
            raise
        except Exception as exc:
            raise proposal.Refusal(
                "DRIVE_REGISTRATION_PREPARATION_INCOMPLETE",
                {"archive_path": expected["archive_path"], "error": str(exc)},
                ("refresh_onboarding_preview", "review_prepared_namespace"),
            ) from exc
        annex_uuid = str(prepared.get("annex_uuid") or "")
        if not annex_uuid or prepared.get("archive_path") != expected["archive_path"]:
            raise proposal.Refusal(
                "DRIVE_REGISTRATION_PREPARATION_INCOMPLETE",
                {"prepared": prepared},
                ("refresh_onboarding_preview", "review_prepared_namespace"),
            )
        annex_collision = c.execute(
            "SELECT drive_label FROM drives WHERE annex_uuid=? ORDER BY drive_label",
            [annex_uuid],
        ).fetchall()
        if annex_collision:
            raise proposal.Refusal(
                "DRIVE_REGISTRATION_IDENTITY_COLLISION",
                {
                    "annex_uuid": annex_uuid,
                    "registered_labels": [str(row[0]) for row in annex_collision],
                },
                ("review_registered_identity",),
            )

        c.execute(
            "INSERT INTO drives("
            "drive_label,fs_uuid,annex_uuid,capacity_bytes,free_bytes,hw_model,serial,role,"
            "raid_backed,health,last_seen,notes,identity_epoch,write_generation,"
            "filesystem_capacity_bytes,identity_fingerprint,write_authority,lifecycle,eligibility"
            ") VALUES(?,?,?,?,?,?,?,?,0,'unchecked',CURRENT_TIMESTAMP,?,1,0,NULL,NULL,'unknown',"
            "'active','enabled')",
            [
                expected["label"],
                expected["fs_uuid"],
                annex_uuid,
                int(volume["size_bytes"]),
                int(prepared.get("free_bytes") or 0),
                observed_device.get("model"),
                expected["serial"],
                expected["role"],
                "Portal new-identity registration; SMART/format/mount were not run. "
                "Capacity evidence remains unknown until explicit reconciliation.",
            ],
        )
        c.execute(
            "INSERT INTO plan_drives(plan_id,drive_label) VALUES(?,?)",
            [expected["plan_id"], expected["label"]],
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
        approval_invalidated = active_approval is not None or approved_rows > 0
        return proposal.GraphResult(
            proven_noop=False,
            value={
                "archive_path": prepared["archive_path"],
                "annex_uuid": annex_uuid,
            },
        )

    written = proposal.graph_write(con, op)
    return {
        "changed": True,
        "already_registered": False,
        "drive_label": expected["label"],
        "planner_revision": planner_revision(con),
        "plan_id": expected["plan_id"],
        "archive_path": written.value["archive_path"],
        "annex_uuid": written.value["annex_uuid"],
        "approval_invalidated": approval_invalidated,
        "capacity_evidence": "unknown_until_reconcile",
        "reconciliation_required": True,
        "inherited_from_lost_identity": [],
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
