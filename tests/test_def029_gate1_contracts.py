"""DEF-029 Gate 1: replacement onboarding starts with a read-only identity preview.

The preview may explain an attached, unregistered device and propose a never-reused label.  It may
not infer that hardware inherits a lost identity or begin SMART, initialization, registration,
plan mutation, reconciliation, or Fill.
"""
from __future__ import annotations

import sqlite3
from unittest import mock

import pytest

from modelark import drive_lifecycle, proposal, register
from modelark.core import db


def _catalog() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute(
        "INSERT INTO plans(plan_id,name,capacity_mode,is_active) "
        "VALUES('ark','Ark','guaranteed',1)"
    )
    for number in range(7):
        label = f"drive-{number:02d}"
        lost = number == 2
        con.execute(
            "INSERT INTO drives("
            "drive_label,serial,hw_model,capacity_bytes,role,lifecycle,eligibility,"
            "identity_epoch,identity_fingerprint"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            [
                label,
                "FAILED-SERIAL" if lost else f"SERIAL-{number}",
                "old disk" if lost else "fleet disk",
                4_000_000_000_000,
                "primary",
                "lost" if lost else "active",
                "excluded" if lost else "enabled",
                3 if lost else 1,
                "b" * 64 if lost else None,
            ],
        )
        con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/model',1)")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/model','model.safetensors',100,'safetensors','bf16',?)",
        ["a" * 64],
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_bytes,compressed) "
        "VALUES('org/model','model.safetensors','drive-02',90,0)"
    )
    con.execute(
        "INSERT INTO replicas(repo_id,rfilename,drive_label,present) "
        "VALUES('org/model','model.safetensors','drive-02',1)"
    )
    con.execute("UPDATE planner_state SET planner_revision=11 WHERE singleton_id=1")
    return con


def _device(**overrides) -> dict:
    value = {
        "dev": "/dev/mock-seagate",
        "size": "7.3T",
        "model": "Seagate 8TB",
        "serial": "NEW-SEAGATE",
        "bus": "usb",
        "spinning": True,
    }
    value.update(overrides)
    return value


def _topology(**overrides) -> dict:
    value = {
        "available": True,
        "requested_dev": "/dev/mock-seagate",
        "system_backing": False,
        "nodes": [
            {
                "dev": "/dev/mock-seagate",
                "type": "disk",
                "size_bytes": 8_000_000_000_000,
                "fstype": None,
                "fs_uuid": None,
                "mountpoints": [],
            },
            {
                "dev": "/dev/mock-seagate1",
                "type": "part",
                "size_bytes": 7_999_000_000_000,
                "fstype": "ext4",
                "fs_uuid": "NEW-FS-UUID",
                "mountpoints": [],
                "archive_path": None,
                "archive_state": "unmounted",
                "annex_uuid": None,
            },
        ],
    }
    value.update(overrides)
    return value


def test_preview_is_read_only_suggests_new_label_and_preserves_lost_dependencies():
    con = _catalog()
    before_changes = con.total_changes
    before_rows = con.execute(
        "SELECT drive_label,lifecycle,eligibility FROM drives ORDER BY drive_label"
    ).fetchall()

    preview = drive_lifecycle.onboarding_preview(con, _device(), _topology())

    assert preview["planner_revision"] == 11
    assert preview["observation_authority"] == "read_only"
    assert preview["suggested_label"] == "drive-07"
    assert preview["label_policy"] == "new_label_required"
    assert preview["volume"] == {
        "dev": "/dev/mock-seagate1",
        "type": "part",
        "size_bytes": 7_999_000_000_000,
        "fstype": "ext4",
        "fs_uuid": "NEW-FS-UUID",
        "mountpoints": [],
        "mounted": False,
        "archive_path": None,
        "archive_state": "unmounted",
        "annex_uuid": None,
    }
    assert preview["ready_for_registration"] is False
    assert preview["next_action"] == "mount_volume"
    assert preview["registration_preview"] == {
        "dev": "/dev/mock-seagate1",
        "label": "drive-07",
        "mount": None,
        "format": None,
        "role": "primary",
        "adds_to_active_plan": "ark",
        "requires_reconcile_after_registration": True,
        "inherited_from_lost_identity": [],
    }
    assert preview["separate_lost_identities"] == [{
        "drive_label": "drive-02",
        "identity_epoch": 3,
        "identity_fingerprint": "b" * 64,
        "archived_rows": 1,
        "replica_rows": 1,
        "plans": [{"plan_id": "ark", "is_active": True}],
        "relationship": "not_inherited",
    }]
    assert con.total_changes == before_changes
    assert con.execute(
        "SELECT drive_label,lifecycle,eligibility FROM drives ORDER BY drive_label"
    ).fetchall() == before_rows
    con.close()


def test_preview_refuses_registered_identity_collision_and_system_device():
    con = _catalog()
    with pytest.raises(proposal.Refusal) as collision:
        drive_lifecycle.onboarding_preview(
            con,
            _device(serial="SERIAL-5"),
            _topology(),
        )
    assert collision.value.code == "DRIVE_ONBOARDING_IDENTITY_COLLISION"

    system = drive_lifecycle.onboarding_preview(
        con,
        _device(dev="/dev/mock-system", serial="NEW-SYSTEM"),
        _topology(requested_dev="/dev/mock-system", system_backing=True),
    )
    assert system["ready_for_registration"] is False
    assert system["next_action"] == "refuse_system_device"
    assert "SYSTEM_DEVICE" in system["blockers"]
    con.close()


def test_preview_refuses_annex_collision_or_unrecognized_archive_namespace():
    con = _catalog()
    con.execute("UPDATE drives SET annex_uuid='KNOWN-ANNEX' WHERE drive_label='drive-05'")
    mounted_node = {
        **_topology()["nodes"][1],
        "mountpoints": ["/media/test/seagate"],
        "archive_path": "/media/test/seagate/modelark",
        "archive_state": "annex",
        "annex_uuid": "KNOWN-ANNEX",
    }
    with pytest.raises(proposal.Refusal) as collision:
        drive_lifecycle.onboarding_preview(
            con,
            _device(),
            _topology(nodes=[_topology()["nodes"][0], mounted_node]),
        )
    assert collision.value.code == "DRIVE_ONBOARDING_IDENTITY_COLLISION"

    occupied_node = {
        **mounted_node,
        "annex_uuid": None,
        "archive_state": "unrecognized",
    }
    occupied = drive_lifecycle.onboarding_preview(
        con,
        _device(),
        _topology(nodes=[_topology()["nodes"][0], occupied_node]),
    )
    assert occupied["ready_for_registration"] is False
    assert occupied["next_action"] == "review_archive_namespace"
    assert "ARCHIVE_NAMESPACE_OCCUPIED" in occupied["blockers"]
    con.close()


def test_portal_preview_rebinds_exact_observation_without_smart_or_mutation():
    con = _catalog()
    from modelark.web import data, disk_api, drive_api

    data._con = con
    inventory = {"available": True, "devices": [_device()]}
    with mock.patch.object(disk_api, "attached_inventory", return_value=inventory), \
            mock.patch.object(disk_api, "registration_topology", return_value=_topology()), \
            mock.patch.object(disk_api, "disk") as smart, \
            mock.patch.object(register, "register_drive") as register_drive:
        result = drive_api.onboarding_preview(
            "/dev/mock-seagate",
            "NEW-SEAGATE",
        )
    assert result["ok"] is True
    assert result["preview"]["suggested_label"] == "drive-07"
    smart.assert_not_called()
    register_drive.assert_not_called()
    assert con.execute("SELECT count(*) FROM drives").fetchone()[0] == 7
    assert con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == 11
    data._con = None
    con.close()


def test_drives_ui_keeps_review_and_registration_as_separate_actions():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "modelark" / "web" / "static"
    html = (root / "index.html").read_text()
    script = (root / "disk.js").read_text()
    assert 'id="driveOnboardingModal"' in html
    assert "Review onboarding" in script
    assert 'api("/api/drive/onboarding-preview?' in script
    assert 'post("/api/drive/register-new"' in script
    assert "/api/drive/onboarding-apply" not in script
