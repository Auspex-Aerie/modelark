"""DEF-029 Gate 2: exact, recoverable new-identity registration.

Registration is a separate operator-confirmed action bound to the read-only preview.  It may
initialize only an absent namespace, insert one new catalog identity, add that identity to the
active plan, invalidate stale approval, and bump the planner revision once.  Capacity admission
and reconciliation remain later operations.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
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
    con.execute("UPDATE planner_state SET planner_revision=11 WHERE singleton_id=1")
    con.execute(
        "UPDATE planner_state SET active_approved_proposal_id='old-approved' "
        "WHERE singleton_id=1"
    )
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


def _mounted_topology(
    *, archive_state="absent", annex_uuid=None, receipt=None,
    archive_parent_writable=True,
) -> dict:
    return {
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
                "mountpoints": ["/media/test/seagate"],
                "archive_path": "/media/test/seagate/modelark",
                "archive_state": archive_state,
                "annex_uuid": annex_uuid,
                "registration_receipt": receipt,
                "archive_parent_writable": archive_parent_writable,
                "archive_parent_uid": 1000,
                "archive_parent_gid": 1000,
                "archive_parent_mode": "0755",
            },
        ],
    }


def _preview(con, topology=None) -> dict:
    return drive_lifecycle.onboarding_preview(
        con,
        _device(),
        topology or _mounted_topology(),
    )


def test_mounted_preview_exposes_exact_registration_binding_and_confirmation():
    con = _catalog()
    preview = _preview(con)

    assert preview["ready_for_registration"] is True
    assert preview["confirmation"] == "REGISTER NEW drive-07"
    assert preview["registration_binding"] == {
        "planner_revision": 11,
        "dev": "/dev/mock-seagate",
        "serial": "NEW-SEAGATE",
        "volume_dev": "/dev/mock-seagate1",
        "fs_uuid": "NEW-FS-UUID",
        "mount": "/media/test/seagate",
        "archive_path": "/media/test/seagate/modelark",
        "archive_state": "absent",
        "label": "drive-07",
        "plan_id": "ark",
        "role": "primary",
    }
    con.close()


def test_mounted_preview_blocks_unwritable_archive_parent_before_registration():
    con = _catalog()

    preview = _preview(con, _mounted_topology(archive_parent_writable=False))

    assert preview["ready_for_registration"] is False
    assert preview["next_action"] == "prepare_archive_permissions"
    assert "ARCHIVE_PARENT_NOT_WRITABLE" in preview["blockers"]
    assert preview["volume"]["archive_parent_writable"] is False
    con.close()


def test_topology_reports_service_write_access_without_writing(tmp_path):
    from modelark.web import disk_api

    mount = tmp_path / "mounted-volume"
    mount.mkdir()
    lsblk = {
        "blockdevices": [{
            "path": "/dev/mock-seagate",
            "type": "disk",
            "size": 8_000_000_000_000,
            "fstype": None,
            "uuid": None,
            "mountpoints": [None],
            "children": [{
                "path": "/dev/mock-seagate1",
                "type": "part",
                "size": 7_999_000_000_000,
                "fstype": "ext4",
                "uuid": "NEW-FS-UUID",
                "mountpoints": [str(mount)],
            }],
        }],
    }

    def probe(args, **_kwargs):
        if args[0] == "lsblk":
            return subprocess.CompletedProcess(args, 0, json.dumps(lsblk), "")
        if args[:3] == ["findmnt", "-nro", "SOURCE"]:
            return subprocess.CompletedProcess(args, 0, "/dev/root-system\n", "")
        return subprocess.CompletedProcess(args, 1, "", "not configured")

    with mock.patch.object(disk_api.subprocess, "run", side_effect=probe), \
            mock.patch.object(disk_api.os, "access", return_value=False) as access:
        topology = disk_api.registration_topology("/dev/mock-seagate")

    volume = next(node for node in topology["nodes"] if node["fstype"] == "ext4")
    mount_stat = os.stat(mount)
    assert volume["archive_parent_writable"] is False
    assert volume["archive_parent_uid"] == mount_stat.st_uid
    assert volume["archive_parent_gid"] == mount_stat.st_gid
    assert volume["archive_parent_mode"] == "0755"
    access.assert_called_once_with(str(mount), os.W_OK | os.X_OK)


def test_exact_registration_adds_new_identity_once_without_admitting_capacity():
    con = _catalog()
    preview = _preview(con)
    prepared = []

    def prepare_archive(**kwargs):
        prepared.append(kwargs)
        return {
            "archive_path": kwargs["archive_path"],
            "annex_uuid": "NEW-ANNEX-UUID",
            "free_bytes": 7_500_000_000_000,
            "recovered_preparation": False,
        }

    result = drive_lifecycle.register_new_identity(
        con,
        _device(),
        _mounted_topology(),
        expected_binding=preview["registration_binding"],
        confirmation=preview["confirmation"],
        prepare_archive=prepare_archive,
    )

    assert result == {
        "changed": True,
        "already_registered": False,
        "drive_label": "drive-07",
        "planner_revision": 12,
        "plan_id": "ark",
        "archive_path": "/media/test/seagate/modelark",
        "annex_uuid": "NEW-ANNEX-UUID",
        "approval_invalidated": True,
        "capacity_evidence": "unknown_until_reconcile",
        "reconciliation_required": True,
        "inherited_from_lost_identity": [],
    }
    assert len(prepared) == 1
    assert prepared[0]["label"] == "drive-07"
    assert prepared[0]["fs_uuid"] == "NEW-FS-UUID"
    assert prepared[0]["serial"] == "NEW-SEAGATE"
    row = con.execute(
        "SELECT fs_uuid,annex_uuid,serial,hw_model,capacity_bytes,free_bytes,role,"
        "lifecycle,eligibility,identity_epoch,write_generation,filesystem_capacity_bytes,"
        "identity_fingerprint,write_authority,health FROM drives WHERE drive_label='drive-07'"
    ).fetchone()
    assert row == (
        "NEW-FS-UUID", "NEW-ANNEX-UUID", "NEW-SEAGATE", "Seagate 8TB",
        7_999_000_000_000, 7_500_000_000_000, "primary", "active", "enabled",
        1, 0, None, None, "unknown", "unchecked",
    )
    assert con.execute(
        "SELECT count(*) FROM plan_drives WHERE plan_id='ark' AND drive_label='drive-07'"
    ).fetchone()[0] == 1
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None
    assert con.execute(
        "SELECT lifecycle,eligibility FROM drives WHERE drive_label='drive-02'"
    ).fetchone() == ("lost", "excluded")
    assert con.execute(
        "SELECT count(*) FROM drive_clean_anchors WHERE drive_label='drive-07'"
    ).fetchone()[0] == 0

    receipt = {
        "state": "prepared",
        "label": "drive-07",
        "fs_uuid": "NEW-FS-UUID",
        "serial": "NEW-SEAGATE",
        "volume_dev": "/dev/mock-seagate1",
    }
    replay = drive_lifecycle.register_new_identity(
        con,
        _device(),
        _mounted_topology(
            archive_state="prepared_registration",
            annex_uuid="NEW-ANNEX-UUID",
            receipt=receipt,
        ),
        expected_binding=preview["registration_binding"],
        confirmation=preview["confirmation"],
        prepare_archive=lambda **_kwargs: pytest.fail("idempotent replay must not write"),
    )
    assert replay["changed"] is False
    assert replay["already_registered"] is True
    assert replay["planner_revision"] == 12
    assert con.execute(
        "SELECT count(*) FROM plan_drives WHERE plan_id='ark' AND drive_label='drive-07'"
    ).fetchone()[0] == 1
    con.close()


@pytest.mark.parametrize(
    ("binding_change", "confirmation", "code"),
    [
        ({"planner_revision": 10}, None, "DRIVE_REGISTRATION_PREVIEW_STALE"),
        ({"fs_uuid": "OTHER-FS"}, None, "DRIVE_REGISTRATION_PREVIEW_STALE"),
        ({"mount": "/media/other"}, None, "DRIVE_REGISTRATION_PREVIEW_STALE"),
        ({"label": "drive-02"}, "REGISTER NEW drive-02", "DRIVE_REGISTRATION_IDENTITY_COLLISION"),
        ({}, "register it", "DRIVE_REGISTRATION_CONFIRMATION_MISMATCH"),
    ],
)
def test_stale_or_unconfirmed_registration_refuses_before_physical_work(
    binding_change, confirmation, code
):
    con = _catalog()
    preview = _preview(con)
    binding = {**preview["registration_binding"], **binding_change}
    prepare = mock.Mock()

    with pytest.raises(proposal.Refusal) as refused:
        drive_lifecycle.register_new_identity(
            con,
            _device(),
            _mounted_topology(),
            expected_binding=binding,
            confirmation=confirmation or preview["confirmation"],
            prepare_archive=prepare,
        )
    assert refused.value.code == code
    prepare.assert_not_called()
    assert con.execute("SELECT count(*) FROM drives").fetchone()[0] == 7
    assert drive_lifecycle.planner_revision(con) == 11
    con.close()


def test_preparation_failure_rolls_back_catalog_and_revision_without_cleanup_guessing():
    con = _catalog()
    preview = _preview(con)

    def fail_after_possible_filesystem_work(**_kwargs):
        raise RuntimeError("simulated map sync failure")

    with pytest.raises(proposal.Refusal) as refused:
        drive_lifecycle.register_new_identity(
            con,
            _device(),
            _mounted_topology(),
            expected_binding=preview["registration_binding"],
            confirmation=preview["confirmation"],
            prepare_archive=fail_after_possible_filesystem_work,
        )
    assert refused.value.code == "DRIVE_REGISTRATION_PREPARATION_INCOMPLETE"
    assert "simulated map sync failure" in str(refused.value.evidence)
    assert con.execute("SELECT count(*) FROM drives").fetchone()[0] == 7
    assert drive_lifecycle.planner_revision(con) == 11
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == "old-approved"
    con.close()


def test_live_fill_refuses_before_physical_preparation():
    con = _catalog()
    preview = _preview(con)
    prepare = mock.Mock()
    with mock.patch("modelark.execution_session.live_session_exists", return_value=True), \
            mock.patch("modelark.execution_session.live_owner", return_value={"session_id": "live"}):
        with pytest.raises(proposal.Refusal) as refused:
            drive_lifecycle.register_new_identity(
                con,
                _device(),
                _mounted_topology(),
                expected_binding=preview["registration_binding"],
                confirmation=preview["confirmation"],
                prepare_archive=prepare,
            )
    assert refused.value.code == "FILL_SESSION_ACTIVE"
    prepare.assert_not_called()
    assert drive_lifecycle.planner_revision(con) == 11
    con.close()


def test_portal_apply_reprobes_exact_observation_without_smart_format_or_mount():
    from modelark.web import data, disk_api, drive_api

    con = _catalog()
    preview = _preview(con)
    data._con = con
    inventory = {"available": True, "devices": [_device()]}
    prepared = {
        "archive_path": "/media/test/seagate/modelark",
        "annex_uuid": "NEW-ANNEX-UUID",
        "free_bytes": 7_500_000_000_000,
        "recovered_preparation": False,
    }
    with mock.patch.object(disk_api, "attached_inventory", return_value=inventory), \
            mock.patch.object(disk_api, "registration_topology", return_value=_mounted_topology()), \
            mock.patch.object(disk_api, "disk") as smart, \
            mock.patch.object(register, "smart_baseline") as legacy_smart, \
            mock.patch.object(register, "_mkfs") as mkfs, \
            mock.patch.object(register, "_mount") as mount, \
            mock.patch.object(register, "prepare_new_identity_archive", return_value=prepared):
        result = drive_api.register_new({
            **preview["registration_binding"],
            "confirmation": preview["confirmation"],
        })
    assert result["ok"] is True
    assert result["registration"]["drive_label"] == "drive-07"
    smart.assert_not_called()
    legacy_smart.assert_not_called()
    mkfs.assert_not_called()
    mount.assert_not_called()
    data._con = None
    con.close()


def test_physical_preparation_is_receipted_and_exactly_retryable(tmp_path):
    library = tmp_path / "library"
    mount = tmp_path / "mount"
    library.mkdir()
    mount.mkdir()
    register._run("git", "-C", str(library), "init", "-q")
    register._run("git", "-C", str(library), "config", "user.name", "ModelArk Test")
    register._run("git", "-C", str(library), "config", "user.email", "modelark@example.invalid")
    register._run("git", "-C", str(library), "annex", "init", "map")
    (library / "README.md").write_text("# map\n")
    register._run("git", "-C", str(library), "add", "README.md")
    register._run("git", "-C", str(library), "commit", "-qm", "init map")
    original_run = register._run

    def exact_mount_probe(*args, **kwargs):
        if args == ("findmnt", "-nro", "SOURCE", "/"):
            return subprocess.CompletedProcess(args, 0, "/dev/root-system\n", "")
        if args[:3] == ("findmnt", "-nro", "SOURCE"):
            return subprocess.CompletedProcess(args, 0, "/dev/mock-seagate1\n", "")
        if args[:3] == ("findmnt", "-nro", "FSTYPE"):
            return subprocess.CompletedProcess(args, 0, "ext4\n", "")
        if args[:3] == ("findmnt", "-nro", "UUID"):
            return subprocess.CompletedProcess(args, 0, "NEW-FS-UUID\n", "")
        if args[:3] == ("lsblk", "-nro", "PKNAME"):
            return subprocess.CompletedProcess(args, 0, "mock-seagate\n", "")
        if args[:3] == ("lsblk", "-dno", "SERIAL"):
            return subprocess.CompletedProcess(args, 0, "NEW-SEAGATE\n", "")
        return original_run(*args, **kwargs)

    arguments = {
        "volume_dev": "/dev/mock-seagate1",
        "mount": str(mount),
        "archive_path": str(mount / "modelark"),
        "label": "drive-07",
        "fs_uuid": "NEW-FS-UUID",
        "fstype": "ext4",
        "serial": "NEW-SEAGATE",
        "model": "Seagate 8TB",
        "role": "primary",
        "library": str(library),
    }
    with mock.patch.object(register, "_run", side_effect=exact_mount_probe), \
            mock.patch.object(register, "smart_baseline") as smart, \
            mock.patch.object(register, "_mkfs") as mkfs, \
            mock.patch.object(register, "_mount") as mount_drive:
        first = register.prepare_new_identity_archive(**arguments)
        second = register.prepare_new_identity_archive(**arguments)

    assert first["annex_uuid"]
    assert first["recovered_preparation"] is False
    assert second["annex_uuid"] == first["annex_uuid"]
    assert second["recovered_preparation"] is True
    assert register.registration_receipt(mount / "modelark") == {
        "state": "prepared",
        "label": "drive-07",
        "fs_uuid": "NEW-FS-UUID",
        "serial": "NEW-SEAGATE",
        "volume_dev": "/dev/mock-seagate1",
    }
    assert not (mount / ".modelark.registering-drive-07").exists()
    assert register._git(library, "remote", "get-url", "drive-07") == str(mount / "modelark")
    smart.assert_not_called()
    mkfs.assert_not_called()
    mount_drive.assert_not_called()


def test_physical_preparation_refuses_unknown_namespace_without_deleting_it(tmp_path):
    mount = tmp_path / "mount"
    archive = mount / "modelark"
    archive.mkdir(parents=True)
    marker = archive / "operator-data"
    marker.write_text("keep")

    def probe(*args, **_kwargs):
        values = {"SOURCE": "/dev/mock-seagate1\n", "FSTYPE": "ext4\n", "UUID": "NEW-FS-UUID\n"}
        if args == ("findmnt", "-nro", "SOURCE", "/"):
            return subprocess.CompletedProcess(args, 0, "/dev/root-system\n", "")
        if args[:2] == ("findmnt", "-nro"):
            return subprocess.CompletedProcess(args, 0, values[args[2]], "")
        if args[:3] == ("lsblk", "-nro", "PKNAME"):
            return subprocess.CompletedProcess(args, 0, "mock-seagate\n", "")
        if args[:3] == ("lsblk", "-dno", "SERIAL"):
            return subprocess.CompletedProcess(args, 0, "NEW-SEAGATE\n", "")
        return subprocess.CompletedProcess(args, 1, "", "not configured")

    with mock.patch.object(register, "_run", side_effect=probe), \
            mock.patch.object(register, "_is_annex", return_value=True):
        with pytest.raises(RuntimeError, match="receipt|namespace"):
            register.prepare_new_identity_archive(
                volume_dev="/dev/mock-seagate1",
                mount=str(mount),
                archive_path=str(archive),
                label="drive-07",
                fs_uuid="NEW-FS-UUID",
                fstype="ext4",
                serial="NEW-SEAGATE",
                model="Seagate 8TB",
                role="primary",
                library=str(tmp_path / "library"),
            )
    assert marker.read_text() == "keep"


def test_physical_preparation_rechecks_hardware_serial_before_any_namespace_write(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()

    def swapped_serial(*args, **_kwargs):
        values = {"SOURCE": "/dev/mock-seagate1\n", "FSTYPE": "ext4\n", "UUID": "NEW-FS-UUID\n"}
        if args[:2] == ("findmnt", "-nro"):
            return subprocess.CompletedProcess(args, 0, values[args[2]], "")
        if args[:3] == ("lsblk", "-nro", "PKNAME"):
            return subprocess.CompletedProcess(args, 0, "mock-seagate\n", "")
        if args[:3] == ("lsblk", "-dno", "SERIAL"):
            return subprocess.CompletedProcess(args, 0, "SWAPPED-SERIAL\n", "")
        return subprocess.CompletedProcess(args, 1, "", "not configured")

    with mock.patch.object(register, "_run", side_effect=swapped_serial):
        with pytest.raises(RuntimeError, match="serial changed"):
            register.prepare_new_identity_archive(
                volume_dev="/dev/mock-seagate1",
                mount=str(mount),
                archive_path=str(mount / "modelark"),
                label="drive-07",
                fs_uuid="NEW-FS-UUID",
                fstype="ext4",
                serial="NEW-SEAGATE",
                model="Seagate 8TB",
                role="primary",
                library=str(tmp_path / "library"),
            )
    assert not (mount / "modelark").exists()
    assert not (mount / ".modelark.registering-drive-07").exists()


def test_physical_preparation_rechecks_mount_writability_before_git_clone(tmp_path):
    mount = tmp_path / "mount"
    mount.mkdir()
    calls = []

    def exact_identity(*args, **_kwargs):
        calls.append(args)
        values = {"SOURCE": "/dev/mock-seagate1\n", "FSTYPE": "ext4\n", "UUID": "NEW-FS-UUID\n"}
        if args == ("findmnt", "-nro", "SOURCE", "/"):
            return subprocess.CompletedProcess(args, 0, "/dev/root-system\n", "")
        if args[:2] == ("findmnt", "-nro"):
            return subprocess.CompletedProcess(args, 0, values[args[2]], "")
        if args[:3] == ("lsblk", "-nro", "PKNAME"):
            return subprocess.CompletedProcess(args, 0, "mock-seagate\n", "")
        if args[:3] == ("lsblk", "-dno", "SERIAL"):
            return subprocess.CompletedProcess(args, 0, "NEW-SEAGATE\n", "")
        return subprocess.CompletedProcess(args, 1, "", "not configured")

    with mock.patch.object(register, "_run", side_effect=exact_identity), \
            mock.patch.object(register.os, "access", return_value=False):
        with pytest.raises(RuntimeError, match="not writable"):
            register.prepare_new_identity_archive(
                volume_dev="/dev/mock-seagate1",
                mount=str(mount),
                archive_path=str(mount / "modelark"),
                label="drive-07",
                fs_uuid="NEW-FS-UUID",
                fstype="ext4",
                serial="NEW-SEAGATE",
                model="Seagate 8TB",
                role="primary",
                library=str(tmp_path / "library"),
            )

    assert not any(args[:2] == ("git", "clone") for args in calls)
    assert not (mount / "modelark").exists()
    assert not (mount / ".modelark.registering-drive-07").exists()


def test_registration_ui_requires_exact_phrase_and_calls_only_dedicated_endpoint():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "modelark" / "web" / "static"
    html = (root / "index.html").read_text()
    script = (root / "disk.js").read_text()
    server = (Path(__file__).resolve().parents[1] / "modelark" / "web" / "server.py").read_text()

    assert 'id="driveOnboardingConfirm"' in html
    assert 'id="driveOnboardingApply"' in html
    assert 'id="driveOnboardingPlan"' in html
    assert 'post("/api/drive/register-new"' in script
    assert "archive namespace" in script
    assert "adds_to_active_plan" in script
    assert "prepare_archive_permissions" in script
    assert "onboardingPreview.confirmation" in script
    assert 'u.path == "/api/drive/register-new"' in server
    assert "Network outcome unknown" in script
    assert "reconciliation" in script.lower()
