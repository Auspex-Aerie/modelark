"""Operator drive-loss workflow: observation stays advisory; exact transitions replan safely."""
from __future__ import annotations

from unittest import mock

import pytest

from modelark import drive_lifecycle, plan, proposal
from modelark.core import db


def _catalog(tmp_path):
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    from modelark.web import data
    if data._con is not None:
        data._con.close()
    data._con = None
    con = db.connect()
    con.execute(
        "INSERT INTO models(repo_id,numcopies) VALUES('org/model',1)"
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/model','model.safetensors',100,'safetensors','bf16',?)",
        ["1" * 64],
    )
    for label, serial, fingerprint in (
        ("drive-00", "KEEP-SERIAL", "a" * 64),
        ("drive-02", "FAILED-SERIAL", "b" * 64),
    ):
        con.execute(
            "INSERT INTO drives(drive_label,serial,hw_model,capacity_bytes,free_bytes,"
            "filesystem_capacity_bytes,role,lifecycle,eligibility,identity_epoch,"
            "identity_fingerprint,write_authority) "
            "VALUES(?,?,?,1000000000000,900000000000,1000000000000,'primary',"
            "'active','enabled',3,?,'dedicated_local')",
            [label, serial, "test disk", fingerprint],
        )
    plan.create(con, "ark", name="Ark")
    plan.add_drive(con, "ark", "drive-00")
    plan.add_drive(con, "ark", "drive-02")
    plan.set_active(con, "ark")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_bytes,compressed) "
        "VALUES('org/model','model.safetensors','drive-02',90,false)"
    )
    con.execute(
        "INSERT INTO replicas(repo_id,rfilename,drive_label,present) "
        "VALUES('org/model','model.safetensors','drive-02',true)"
    )
    con.execute("UPDATE planner_state SET planner_revision=7 WHERE singleton_id=1")
    return con


def _declare(con, preview, **overrides):
    values = {
        "expected_revision": preview["planner_revision"],
        "expected_identity_epoch": preview["identity_epoch"],
        "expected_identity_fingerprint": preview["identity_fingerprint"],
        "confirmation": preview["confirmation"],
    }
    values.update(overrides)
    return drive_lifecycle.declare_lost(con, preview["drive_label"], **values)


def test_passive_observation_never_promotes_missing_or_binds_replacement(tmp_path):
    con = _catalog(tmp_path)
    before = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0]
    mapped = drive_lifecycle.observe_registered(con, [{
        "dev": "/dev/sdz", "size": "7.3T", "model": "Seagate 8TB",
        "serial": "NEW-SEAGATE-SERIAL", "bus": "usb", "spinning": True,
    }])
    old = next(item for item in mapped["registered"] if item["drive_label"] == "drive-02")
    assert old["observation"] == "not_attached"
    assert old["lifecycle"] == "active" and old["eligibility"] == "enabled"
    assert mapped["unregistered"] == [{
        "dev": "/dev/sdz", "size": "7.3T", "model": "Seagate 8TB",
        "serial": "NEW-SEAGATE-SERIAL", "bus": "usb", "spinning": True,
        "observation": "unregistered", "action_taken": False,
    }]
    assert con.execute(
        "SELECT drive_label FROM drives WHERE serial='NEW-SEAGATE-SERIAL'"
    ).fetchone() is None
    assert con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == before
    con.close()


def test_serial_collision_is_ambiguous_not_an_identity_match(tmp_path):
    con = _catalog(tmp_path)
    con.execute(
        "INSERT INTO drives(drive_label,serial,capacity_bytes,role,lifecycle,eligibility) "
        "VALUES('drive-collision','FAILED-SERIAL',1000,'primary','active','enabled')"
    )
    mapped = drive_lifecycle.observe_registered(con, [{
        "dev": "/dev/sdz", "serial": "FAILED-SERIAL", "model": "some disk",
    }])
    collisions = [
        item for item in mapped["registered"] if item["serial"] == "FAILED-SERIAL"
    ]
    assert len(collisions) == 2
    assert {item["observation"] for item in collisions} == {"ambiguous_serial"}
    assert all(item["device"] is None for item in collisions)
    assert mapped["unregistered"] == []
    con.close()


def test_preview_is_read_only_and_exposes_preserved_residency(tmp_path):
    con = _catalog(tmp_path)
    before = con.total_changes
    preview = drive_lifecycle.loss_preview(con, "drive-02")
    assert preview["planner_revision"] == 7
    assert preview["identity_epoch"] == 3
    assert preview["identity_fingerprint"] == "b" * 64
    assert preview["plans"] == [{"plan_id": "ark", "is_active": True}]
    assert preview["archived_rows"] == 1
    assert preview["replica_rows"] == 1
    assert preview["confirmation"] == "DECLARE LOST drive-02"
    assert con.total_changes == before
    con.close()


def test_declare_lost_bumps_once_invalidates_approval_and_preserves_history(tmp_path):
    con = _catalog(tmp_path)
    con.execute(
        "UPDATE planner_state SET active_approved_proposal_id='reviewed-proposal' "
        "WHERE singleton_id=1"
    )
    preview = drive_lifecycle.loss_preview(con, "drive-02")
    result = _declare(con, preview)
    assert result == {
        "drive_label": "drive-02", "lifecycle": "lost", "eligibility": "excluded",
        "identity_epoch": 3, "identity_fingerprint": "b" * 64,
        "planner_revision": 8, "changed": True, "approval_invalidated": True,
    }
    assert con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] is None
    assert con.execute(
        "SELECT count(*) FROM plan_drives WHERE plan_id='ark' AND drive_label='drive-02'"
    ).fetchone()[0] == 1
    assert con.execute(
        "SELECT count(*) FROM archived WHERE drive_label='drive-02'"
    ).fetchone()[0] == 1
    assert con.execute(
        "SELECT count(*) FROM replicas WHERE drive_label='drive-02'"
    ).fetchone()[0] == 1
    assert plan.capacity(con, "ark") < preview["active_plan_capacity_bytes"]

    repeat_preview = drive_lifecycle.loss_preview(con, "drive-02")
    repeated = _declare(con, repeat_preview)
    assert repeated["changed"] is False
    assert repeated["planner_revision"] == 8
    con.close()


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"confirmation": "yes"}, "DRIVE_LOSS_CONFIRMATION_MISMATCH"),
        ({"expected_revision": 6}, "DRIVE_LOSS_PREVIEW_STALE"),
        ({"expected_identity_epoch": 2}, "DRIVE_LOSS_PREVIEW_STALE"),
        ({"expected_identity_fingerprint": "c" * 64}, "DRIVE_LOSS_PREVIEW_STALE"),
    ],
)
def test_declare_lost_refuses_wrong_confirmation_or_stale_binding(tmp_path, override, code):
    con = _catalog(tmp_path)
    preview = drive_lifecycle.loss_preview(con, "drive-02")
    with pytest.raises(proposal.Refusal) as caught:
        _declare(con, preview, **override)
    assert caught.value.code == code
    assert con.execute(
        "SELECT lifecycle,eligibility FROM drives WHERE drive_label='drive-02'"
    ).fetchone() == ("active", "enabled")
    assert con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == 7
    con.close()


def test_declare_lost_refuses_while_fill_is_live(tmp_path):
    con = _catalog(tmp_path)
    preview = drive_lifecycle.loss_preview(con, "drive-02")
    with mock.patch("modelark.execution_session.live_session_exists", return_value=True), \
         mock.patch("modelark.execution_session.live_owner", return_value={"session_id": "fill-1"}):
        with pytest.raises(proposal.Refusal) as caught:
            _declare(con, preview)
    assert caught.value.code == "FILL_SESSION_ACTIVE"
    assert con.execute(
        "SELECT lifecycle FROM drives WHERE drive_label='drive-02'"
    ).fetchone()[0] == "active"
    assert con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == 7
    con.close()


def test_portal_operation_returns_canonical_replan_without_targeting_lost_drive(tmp_path):
    con = _catalog(tmp_path)
    from modelark.web import data, drive_api
    data._con = con
    preview = drive_lifecycle.loss_preview(con, "drive-02")
    result = drive_api.declare_lost({
        "drive_label": "drive-02",
        "expected_revision": preview["planner_revision"],
        "expected_identity_epoch": preview["identity_epoch"],
        "expected_identity_fingerprint": preview["identity_fingerprint"],
        "confirmation": preview["confirmation"],
    }, observe=lambda _label: None)
    assert result["ok"] is True
    assert result["transition"]["lifecycle"] == "lost"
    assert result["after"]["replan"]["planner_revision"] == 8
    assert result["after"]["replan"]["target_counts"].get("drive-02", 0) == 0
    assert result["preserved"]["plan_membership"] == [
        {"plan_id": "ark", "is_active": True}
    ]
    data._con = None
    con.close()


def test_portal_overview_keeps_mock_attached_replacement_unregistered(tmp_path):
    con = _catalog(tmp_path)
    from modelark.web import data, drive_api
    data._con = con
    result = drive_api.overview(inventory={
        "available": True,
        "devices": [{
            "dev": "/dev/sdz", "size": "7.3T", "model": "Seagate 8TB",
            "serial": "NEW-SEAGATE-SERIAL", "bus": "usb", "spinning": True,
        }],
    })
    assert result["observation_authority"] == "advisory_only"
    assert result["unregistered"][0]["action_taken"] is False
    assert next(
        item for item in result["registered"] if item["drive_label"] == "drive-02"
    )["observation"] == "not_attached"
    assert con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == 7
    data._con = None
    con.close()


def test_drives_screen_defers_smart_until_explicit_button():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "modelark" / "web" / "static"
    html = (root / "index.html").read_text()
    script = (root / "disk.js").read_text()
    assert 'id="runHealthChecks"' in html
    assert "window.loadDisk = loadInventory" in script
    assert "async function runSmartChecks()" in script
    assert 'api("/api/drives")' in script
    assert 'api("/api/disk")' in script
