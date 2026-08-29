"""INC-048/049 contracts: stable attached identity and bounded reconciliation progress.

These tests use only synthetic inventory, in-memory catalogs, temporary trees, and mocked
git-annex output.  They never inspect or mutate operator drives.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

from modelark import drive_bootstrap, drive_lifecycle
from modelark.core import db
from modelark.web import disk_api


def _catalog() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    return con


def _registered(
    con: sqlite3.Connection,
    label: str,
    *,
    fs_uuid: str,
    annex_uuid: str,
    serial: str | None,
) -> None:
    con.execute(
        "INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial,capacity_bytes) "
        "VALUES(?,?,?,?,1000)",
        [label, fs_uuid, annex_uuid, serial],
    )


def _observed(
    *,
    dev: str,
    fs_uuid: str,
    annex_uuid: str,
    serial: str | None,
) -> dict:
    return {
        "dev": dev,
        "size": "1T",
        "model": "observed disk",
        "serial": serial,
        "bus": "usb",
        "spinning": True,
        "storage_identities": [{
            "dev": f"{dev}1",
            "fs_uuid": fs_uuid,
            "annex_uuid": annex_uuid,
            "mountpoints": [f"/media/test/{fs_uuid}"],
            "archive_state": "annex",
        }],
    }


def test_stable_pair_maps_registered_drive_without_stored_serial():
    con = _catalog()
    _registered(con, "drive-00", fs_uuid="FS-00", annex_uuid="ANNEX-00", serial=None)
    device = _observed(
        dev="/dev/sdc", fs_uuid="FS-00", annex_uuid="ANNEX-00", serial="ISCSI-LUN-ID"
    )

    mapped = drive_lifecycle.observe_registered(con, [device])

    assert mapped["unregistered"] == []
    assert mapped["registered"] == [{
        "drive_label": "drive-00",
        "lifecycle": "active",
        "eligibility": "enabled",
        "identity_epoch": 1,
        "identity_fingerprint": None,
        "serial": None,
        "hw_model": None,
        "capacity_bytes": 1000,
        "last_seen": None,
        "fs_uuid": "FS-00",
        "annex_uuid": "ANNEX-00",
        "observation": "attached_exact_storage_identity",
        "serial_observation": "observed_without_registered_serial",
        "device": device,
    }]
    con.close()


def test_stable_pair_wins_over_usb_bridge_serial_without_rebinding_catalog():
    con = _catalog()
    _registered(con, "drive-01", fs_uuid="FS-01", annex_uuid="ANNEX-01", serial="DISK-SERIAL")
    device = _observed(
        dev="/dev/sdb", fs_uuid="FS-01", annex_uuid="ANNEX-01", serial="BRIDGE-SERIAL"
    )
    before = con.total_changes

    mapped = drive_lifecycle.observe_registered(con, [device])

    drive = mapped["registered"][0]
    assert drive["observation"] == "attached_exact_storage_identity"
    assert drive["serial_observation"] == "mismatch_supporting_only"
    assert drive["serial"] == "DISK-SERIAL"
    assert drive["device"]["serial"] == "BRIDGE-SERIAL"
    assert mapped["unregistered"] == []
    assert con.total_changes == before
    assert con.execute(
        "SELECT serial FROM drives WHERE drive_label='drive-01'"
    ).fetchone() == ("DISK-SERIAL",)
    con.close()


def test_complete_but_wrong_storage_pair_cannot_fall_back_to_matching_serial():
    con = _catalog()
    _registered(con, "drive-01", fs_uuid="FS-01", annex_uuid="ANNEX-01", serial="DISK-SERIAL")
    device = _observed(
        dev="/dev/sdb", fs_uuid="OTHER-FS", annex_uuid="OTHER-ANNEX", serial="DISK-SERIAL"
    )

    mapped = drive_lifecycle.observe_registered(con, [device])

    drive = mapped["registered"][0]
    assert drive["observation"] == "stable_identity_conflict"
    assert drive["device"] is None
    assert mapped["unregistered"][0]["observation"] == "identity_conflict"
    assert mapped["unregistered"][0]["registered_labels"] == ["drive-01"]
    con.close()


def test_attached_inventory_enriches_disk_with_read_only_storage_identity():
    topology = {
        "available": True,
        "requested_dev": "/dev/sdb",
        "system_backing": False,
        "nodes": [{
            "dev": "/dev/sdb1",
            "type": "part",
            "fstype": "ext4",
            "fs_uuid": "FS-01",
            "annex_uuid": "ANNEX-01",
            "mountpoints": ["/media/test/drive-01"],
            "archive_state": "annex",
        }],
    }
    with mock.patch.object(
        disk_api,
        "_lsblk_result",
        return_value=(True, [{
            "NAME": "sdb", "SIZE": "1T", "MODEL": "disk", "SERIAL": "BRIDGE",
            "TRAN": "usb", "ROTA": "1",
        }]),
    ), mock.patch.object(disk_api, "registration_topology", return_value=topology) as probe:
        inventory = disk_api.attached_inventory()

    assert inventory["devices"][0]["storage_identities"] == [{
        "dev": "/dev/sdb1",
        "fs_uuid": "FS-01",
        "annex_uuid": "ANNEX-01",
        "mountpoints": ["/media/test/drive-01"],
        "archive_state": "annex",
    }]
    probe.assert_called_once_with("/dev/sdb")


def _catalogued(con: sqlite3.Connection, rfilename: str, key: str) -> None:
    con.execute("INSERT OR IGNORE INTO models(repo_id) VALUES('repo')")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format) VALUES('repo',?,5,'gguf')",
        [rfilename],
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_bytes,stored_bytes,compressed,annex_key) "
        "VALUES('repo',?,?,?,'drive-00',5,5,0,?)",
        [rfilename, rfilename, rfilename, key],
    )


def test_inventory_queries_target_annex_uuid_once_and_fails_missing_key_closed(tmp_path):
    con = _catalog()
    _registered(con, "drive-00", fs_uuid="FS-00", annex_uuid="ANNEX-00", serial=None)
    _catalogued(con, "present.gguf", "KEY-PRESENT")
    _catalogued(con, "missing.gguf", "KEY-MISSING")
    dest = tmp_path / "modelark"
    dest.mkdir()
    result = subprocess.CompletedProcess(
        ["git"], 0, stdout="KEY-PRESENT\nUNRELATED-KEY\n", stderr=""
    )

    with mock.patch.object(drive_bootstrap.subprocess, "run", return_value=result) as run:
        inventory = drive_bootstrap._inventory(con, "drive-00", dest)

    assert inventory.present == [("repo", "present.gguf")]
    assert inventory.missing == [("repo", "missing.gguf")]
    assert run.call_count == 1
    command = run.call_args.args[0]
    assert command[:5] == ["git", "-C", str(dest), "annex", "whereis"]
    assert "--all" in command and "--in" in command and "ANNEX-00" in command
    assert all("--key" not in str(part) for part in command)
    con.close()


def test_inventory_progress_is_phase_bounded_and_git_tree_is_pruned(tmp_path):
    con = _catalog()
    _registered(con, "drive-00", fs_uuid="FS-00", annex_uuid="ANNEX-00", serial=None)
    _catalogued(con, "present.gguf", "KEY-PRESENT")
    dest = tmp_path / "modelark"
    (dest / "repo").mkdir(parents=True)
    (dest / "repo" / "extra.bin").write_bytes(b"extra")
    (dest / ".git" / "annex" / "objects").mkdir(parents=True)
    (dest / ".git" / "annex" / "objects" / "must-not-scan").write_bytes(b"object")
    events = []
    result = subprocess.CompletedProcess(["git"], 0, stdout="KEY-PRESENT\n", stderr="")

    with mock.patch.object(drive_bootstrap.subprocess, "run", return_value=result):
        inventory = drive_bootstrap._inventory(
            con, "drive-00", dest, progress=events.append
        )

    assert [event.phase for event in events] == [
        "inventory_started",
        "annex_membership_started",
        "annex_membership_completed",
        "filesystem_scan_started",
        "filesystem_scan_completed",
    ]
    assert events[0].total == 1
    assert events[-1].completed == 1
    assert "repo/extra.bin" in inventory.extra
    assert all("must-not-scan" not in item for item in inventory.extra)
    con.close()


def test_drive_ui_explains_storage_identity_and_serial_discrepancy():
    script = (
        Path(__file__).resolve().parents[1] / "modelark" / "web" / "static" / "disk.js"
    ).read_text()
    assert "attached_exact_storage_identity" in script
    assert "mismatch_supporting_only" in script
    assert "identity_conflict" in script
