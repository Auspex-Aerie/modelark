"""Reject volume/disk observations assembled across a remount; no host IO."""
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap as bs, fetch, register
from modelark.block_identity import BlockObservationError
from modelark.core import db


@pytest.fixture
def volume(monkeypatch):
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.executescript(db.SCHEMA_PATH.read_text())
    con.execute("INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial) "
                "VALUES('drive-a','fs-a','annex-a','serial-a')")
    state = dict(fs_uuid="fs-a", annex_uuid="annex-a", blocks=1000, allocation=1, free=800)
    monkeypatch.setattr(register, "archive_path", lambda *a: Path("/synthetic/modelark"))
    monkeypatch.setattr(register, "probe_fs_uuid", lambda _: state["fs_uuid"])
    monkeypatch.setattr(register, "probe_annex_uuid", lambda _: state["annex_uuid"])
    monkeypatch.setattr(register, "probe_serial", lambda _: "serial-a")
    monkeypatch.setattr(register.os, "statvfs", lambda _: SimpleNamespace(
        f_blocks=state["blocks"], f_frsize=state["allocation"], f_bavail=state["free"]))
    try:
        yield con, state
    finally:
        con.close()


@pytest.mark.parametrize("consumer", ["bootstrap", "fetch"])
@pytest.mark.parametrize("change", ["fs_uuid", "annex_uuid", "capacity", "allocation"])
def test_volume_change_during_disk_probe_is_unproven(volume, monkeypatch, consumer, change):
    con, state = volume

    def change_volume(_):
        if change == "capacity":
            state["blocks"] = 2000
        elif change == "allocation":
            state.update(blocks=500, allocation=2)  # same total, changed allocation geometry
        else:
            state[change] = "replacement-" + change
        return "serial-a"  # same physical disk, so serial alone cannot expose remount

    monkeypatch.setattr(register, "probe_serial", change_volume)
    if consumer == "bootstrap":
        assert not bs._live_evidence(con, "drive-a").proven
    else:
        assert fetch._live_drive_evidence(con, "drive-a") is None


@pytest.mark.parametrize("consumer", ["bootstrap", "fetch"])
def test_success_uses_final_free_space_not_the_pre_disk_probe_read(volume, monkeypatch, consumer):
    con, state = volume

    def observed(_):
        state["free"] = 750
        return "serial-a"

    monkeypatch.setattr(register, "probe_serial", observed)
    if consumer == "bootstrap":
        result = bs._live_evidence(con, "drive-a")
        assert result.proven and result.free == 750
    else:
        result = fetch._live_drive_evidence(con, "drive-a")
        assert result["free_bytes"] == 750


@pytest.mark.parametrize("consumer", ["bootstrap", "fetch"])
@pytest.mark.parametrize("probe", ["probe_fs_uuid", "probe_annex_uuid", "statvfs"])
@pytest.mark.parametrize("error", [OSError, RuntimeError, subprocess.SubprocessError])
def test_second_volume_read_error_returns_no_partial_evidence(
    volume, monkeypatch, consumer, probe, error,
):
    con, _ = volume
    owner = register.os if probe == "statvfs" else register
    original = getattr(owner, probe)
    calls = 0

    def fail_second(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise error("volume disappeared during recheck")
        return original(path)

    monkeypatch.setattr(owner, probe, fail_second)
    if consumer == "bootstrap":
        result = bs._live_evidence(con, "drive-a")
        assert not result.proven
        assert (result.fs_uuid, result.annex_uuid, result.serial, result.capacity,
                result.free, result.fingerprint) == (None,) * 6
    else:
        assert fetch._live_drive_evidence(con, "drive-a") is None
    assert calls == 2


def test_shared_boundary_raises_typed_refusal_for_changed_volume(volume, monkeypatch):
    _, state = volume

    def change_volume(_):
        state["fs_uuid"] = "replacement"
        return "serial-a"

    monkeypatch.setattr(register, "probe_serial", change_volume)
    with pytest.raises(BlockObservationError, match="volume changed"):
        register.observe_archive_volume(Path("/synthetic/modelark"))


@pytest.mark.parametrize("consumer", ["bootstrap", "fetch"])
def test_successful_absent_serial_is_not_an_observation_error(volume, monkeypatch, consumer):
    con, _ = volume
    con.execute("UPDATE drives SET serial=NULL")
    monkeypatch.setattr(register, "probe_serial", lambda _: None)
    if consumer == "bootstrap":
        result = bs._live_evidence(con, "drive-a")
        assert result.proven and result.serial is None
        assert result.capacity == 1000 and result.free == 800
    else:
        result = fetch._live_drive_evidence(con, "drive-a")
        assert result["serial"] is None
        assert result["filesystem_capacity_bytes"] == 1000 and result["free_bytes"] == 800


@pytest.mark.parametrize("consumer", ["bootstrap", "fetch"])
def test_failed_disk_observation_still_refuses_serialless_registration(
    volume, monkeypatch, consumer,
):
    con, _ = volume
    con.execute("UPDATE drives SET serial=NULL")

    def fail(_):
        raise BlockObservationError("ambiguous physical attachment")

    monkeypatch.setattr(register, "probe_serial", fail)
    if consumer == "bootstrap":
        assert not bs._live_evidence(con, "drive-a").proven
    else:
        assert fetch._live_drive_evidence(con, "drive-a") is None
