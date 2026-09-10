"""Archive serial cutover below the host command boundary; disposable catalogs only."""
import sqlite3
from types import SimpleNamespace

import pytest

from modelark import admission, block_identity, capacity_evidence, drive_fence, drive_mutation, fetch, register
from modelark.core import db
from modelark import drive_bootstrap
from test_block_identity import observed as _observed_fixture

observed = _observed_fixture  # Reuse the synthetic subprocess inventory fixture.


def _fingerprint(serial):
    return capacity_evidence.identity_fingerprint_v1(
        fs_uuid="archive-fs", annex_uuid="archive-annex", serial=serial, filesystem_capacity_bytes=1000)


@pytest.fixture
def archive(observed, monkeypatch, tmp_path):
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.executescript(db.SCHEMA_PATH.read_text())
    con.execute("INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial,identity_epoch,"
                "identity_fingerprint,filesystem_capacity_bytes,write_authority) "
                "VALUES('drive-a','archive-fs','archive-annex','ZR16L100',1,?,1000,'dedicated_local')",
                [_fingerprint(None)])
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "isolated-catalog.sqlite")
    archive_path = tmp_path / "archive"
    archive_path.mkdir()
    observed[0]["path"] = str(archive_path)
    monkeypatch.setattr(register, "archive_path", lambda *args: archive_path)
    monkeypatch.setattr(register, "probe_fs_uuid", lambda _: "archive-fs")
    monkeypatch.setattr(register, "probe_annex_uuid", lambda _: "archive-annex")
    monkeypatch.setattr(fetch.os, "fstatvfs", lambda _: SimpleNamespace(f_blocks=1000, f_frsize=1, f_bavail=850))
    try:
        yield con, observed
    finally:
        con.close()


def test_register_serial_uses_parent_inventory_not_partition_serial(observed):
    state, _, part = observed
    assert part["serial"] is None
    assert register.probe_serial("/archive/modelark") == "ZR16L100"
    assert [call[0] for call in state["calls"]] == ["findmnt", "lsblk", "lsblk", "findmnt"]


def test_successful_absent_serial_and_observation_failure_remain_distinct(observed, monkeypatch):
    _, disk, _ = observed
    disk["serial"] = None
    assert register.probe_serial("/archive/modelark") is None

    def fail(*args, **kwargs):
        raise PermissionError("cannot inspect devices")

    monkeypatch.setattr(block_identity.subprocess, "run", fail)
    with pytest.raises(block_identity.BlockObservationError):
        register.probe_serial("/archive/modelark")


def test_real_fetch_observation_marks_legacy_repair_before_dirtying_or_work(archive):
    con, _ = archive
    before = tuple(con.iterdump())
    observation = fetch._observe_drive(con, "drive-a")
    assert observation.fingerprint == _fingerprint("ZR16L100")
    assert observation.refusal_code == "DRIVE_SERIAL_REPAIR_REQUIRED"
    assert not observation.identity_proven
    assert observation.free_bytes == 850
    with pytest.raises(drive_mutation.DriveMutationRefused, match="DRIVE_SERIAL_REPAIR_REQUIRED"):
        with drive_mutation.drive_mutation(
                con, ["drive-a"], "test", now="2026-09-09",
                observe=lambda label: fetch._observe_drive(con, label),
                reconcile=lambda *args: pytest.fail("must not inventory or reconcile")):
            pytest.fail("must not begin archive work")
    assert tuple(con.iterdump()) == before
    # Shared preview/reporting callers receive typed non-executable evidence,
    # not an exception escaping their HTTP boundary.
    evidence = admission.preview_by_drive(
        con, ["drive-a"], observe=lambda label: fetch.observe_for_admission(con, label), now="test")["drive-a"]
    assert not evidence.executable and evidence.admissible_free == 0


@pytest.mark.parametrize("serial", [None, "", "DIFFERENT-DISK"])
def test_known_serial_must_be_confirmed_even_when_saved_hash_has_null_serial(archive, serial):
    con, (_, disk, _) = archive
    disk["serial"] = serial
    observation = fetch._observe_drive(con, "drive-a")
    assert not observation.identity_proven
    assert observation.refusal_code == "DRIVE_IDENTITY_MISMATCH"
    with pytest.raises(drive_mutation.DriveMutationRefused, match="DRIVE_IDENTITY_MISMATCH"):
        drive_mutation._require_identity(observation, _fingerprint(None), 1000, "drive-a")


def test_observation_failure_does_not_mint_trusted_null_fingerprint(archive, monkeypatch):
    con, _ = archive

    def fail(*args, **kwargs):
        raise PermissionError("cannot inspect devices")

    monkeypatch.setattr(block_identity.subprocess, "run", fail)
    observation = fetch._observe_drive(con, "drive-a")
    assert not observation.identity_proven and observation.fingerprint is None


@pytest.mark.parametrize("serial", [None, ""])
def test_no_saved_serial_and_genuine_absence_remains_proven_not_repair(archive, serial):
    con, (_, disk, _) = archive
    disk["serial"] = None
    con.execute("UPDATE drives SET serial=?", [serial])
    observation = fetch._observe_drive(con, "drive-a")
    assert observation.identity_proven and observation.refusal_code is None
    assert observation.fingerprint == _fingerprint(None)


def test_corrected_identity_matches_without_repair_diagnostic(archive):
    con, _ = archive
    con.execute("UPDATE drives SET identity_fingerprint=?", [_fingerprint("ZR16L100")])
    observation = fetch._observe_drive(con, "drive-a")
    assert observation.identity_proven and observation.refusal_code is None
    drive_mutation._require_identity(observation, _fingerprint("ZR16L100"), 1000, "drive-a")


def test_bootstrap_and_fetch_observe_same_parent_disk_at_command_boundary(archive):
    con, (state, _, part) = archive
    con.execute("UPDATE drives SET identity_fingerprint=?", [_fingerprint("ZR16L100")])
    assert part["serial"] is None
    bootstrap = drive_bootstrap._live_evidence(con, "drive-a")
    transport = fetch._observe_drive(con, "drive-a")
    assert bootstrap.proven and transport.identity_proven
    assert bootstrap.serial == "ZR16L100"
    assert bootstrap.observation() == transport
    assert bootstrap.capacity == 1000 and bootstrap.free == 850
    assert [call[0] for call in state["calls"]] == ["findmnt", "lsblk", "lsblk", "findmnt"] * 2


@pytest.mark.parametrize("serial", [None, "", "DIFFERENT-DISK"])
def test_bootstrap_known_serial_absence_or_mismatch_is_unproven(archive, serial, monkeypatch):
    con, (_, disk, _) = archive
    disk["serial"] = serial
    before = tuple(con.iterdump())
    observation = drive_bootstrap._live_evidence(con, "drive-a")
    assert not observation.proven
    assert observation.fingerprint == _fingerprint(serial or None)
    monkeypatch.setattr(drive_bootstrap, "_require_complete_inventory",
                        lambda *a, **k: pytest.fail("unproven serial must not inventory"))
    with pytest.raises(drive_mutation.DriveMutationRefused, match="DRIVE_IDENTITY_UNPROVEN"):
        drive_bootstrap.reconcile_drive(con, "drive-a", now="test", dedicated=True)
    assert tuple(con.iterdump()) == before


def test_bootstrap_probe_error_is_unproven_not_a_null_identity(archive, monkeypatch):
    con, _ = archive

    def fail(*args, **kwargs):
        raise PermissionError("cannot inspect devices")

    monkeypatch.setattr(block_identity.subprocess, "run", fail)
    observation = drive_bootstrap._live_evidence(con, "drive-a")
    assert not observation.proven and observation.fingerprint is None
    assert observation.capacity is None
    with pytest.raises(drive_mutation.DriveMutationRefused, match="DRIVE_IDENTITY_UNPROVEN"):
        drive_bootstrap.reconcile_drive(con, "drive-a", now="test", dedicated=True)


@pytest.mark.parametrize("saved_serial", [None, ""])
def test_bootstrap_successfully_absent_serial_preserves_no_serial_workflow(archive, saved_serial):
    con, (_, disk, _) = archive
    con.execute("UPDATE drives SET serial=?", [saved_serial])
    disk["serial"] = None
    bootstrap = drive_bootstrap._live_evidence(con, "drive-a")
    assert bootstrap.proven and bootstrap.serial is None
    assert bootstrap.observation() == fetch._observe_drive(con, "drive-a")


@pytest.mark.parametrize("dedicated", [False, True])
def test_normal_reconcile_marks_recognized_legacy_before_inventory_or_any_mutation(archive, monkeypatch, dedicated):
    con, _ = archive
    before = tuple(con.iterdump())
    monkeypatch.setattr(drive_bootstrap, "_require_complete_inventory",
                        lambda *a, **k: pytest.fail("legacy mismatch must not inventory"))
    monkeypatch.setattr(drive_mutation, "_advance_one",
                        lambda *a, **k: pytest.fail("legacy mismatch must not advance generation"))
    with pytest.raises(drive_mutation.DriveMutationRefused, match="DRIVE_SERIAL_REPAIR_REQUIRED"):
        drive_bootstrap.reconcile_drive(con, "drive-a", now="test", dedicated=dedicated)
    assert tuple(con.iterdump()) == before
