"""Attachment races use disposable directories, never mounts or real archives."""
import os
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap as bs, fetch, register
from modelark import attachment_observation as ao
from modelark.block_identity import BlockObservationError
from modelark.core import db


@pytest.fixture
def attachment(tmp_path, monkeypatch):
    path = tmp_path / "archive"
    path.mkdir()
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.executescript(db.SCHEMA_PATH.read_text())
    con.execute("INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial) "
                "VALUES('drive-a','fs-a','annex-a','serial-a')")
    state = {"fs_uuid": "fs-a", "calls": [], "swap_at": None}

    def component(name, value):
        state["calls"].append(name)
        occurrence = state["calls"].count(name)
        if state["swap_at"] == (name, occurrence):
            path.rename(tmp_path / "detached")
            path.mkdir()
            if state.get("change_uuid"):
                state["fs_uuid"] = "fs-replacement"
        return value

    monkeypatch.setattr(register, "archive_path", lambda *a: path)
    monkeypatch.setattr(register, "probe_fs_uuid", lambda _: component("fs", state["fs_uuid"]))
    monkeypatch.setattr(register, "probe_annex_uuid", lambda _: component("annex", "annex-a"))
    monkeypatch.setattr(register, "probe_serial", lambda _: component("serial", "serial-a"))
    space = lambda _: component("space", SimpleNamespace(f_blocks=1000, f_frsize=1, f_bavail=800))
    monkeypatch.setattr(os, "statvfs", space)
    monkeypatch.setattr(os, "fstatvfs", space)
    try:
        yield con, path, state
    finally:
        con.close()


def assert_unproven(con, consumer):
    if consumer == "bootstrap":
        assert not bs._live_evidence(con, "drive-a").proven
    else:
        assert fetch._live_drive_evidence(con, "drive-a") is None


@pytest.mark.parametrize("consumer", ["bootstrap", "fetch"])
def test_final_uuid_read_cannot_return_old_uuid_over_new_attachment(attachment, consumer):
    con, _, state = attachment
    state.update(swap_at=("fs", 2), change_uuid=True)
    # Final UUID read returns the old UUID, then the pathname is replaced before
    # annex/capacity reads. Serial, annex UUID and geometry stay identical.
    assert_unproven(con, consumer)


@pytest.mark.parametrize("consumer", ["bootstrap", "fetch"])
@pytest.mark.parametrize("component", [
    ("fs", 1), ("annex", 1), ("space", 1), ("serial", 1),
    ("fs", 2), ("annex", 2), ("space", 2),
])
def test_identical_facts_cannot_hide_attachment_change_at_any_component(
    attachment, consumer, component,
):
    con, _, state = attachment
    state["swap_at"] = component
    assert_unproven(con, consumer)


@pytest.mark.parametrize("component", [
    ("fs", 1), ("annex", 1), ("space", 1), ("serial", 1),
    ("fs", 2), ("annex", 2), ("space", 2),
])
def test_mount_id_change_alone_is_detected_at_every_component(attachment, monkeypatch, component):
    con, _, state = attachment
    original = ao._fd_identity
    retained = []

    def identity(fd):
        if not retained:
            retained.append(fd)
        mount_id, dev, ino = original(fd)
        name, occurrence = component
        # Simulate a new mount instance of the same device/inode only on the
        # reopened pathname, while the retained descriptor stays on its mount.
        if fd != retained[0] and state["calls"].count(name) >= occurrence:
            mount_id += 1
        return mount_id, dev, ino

    monkeypatch.setattr(ao, "_fd_identity", identity)
    assert_unproven(con, "bootstrap")


@pytest.mark.parametrize("fdinfo", ["", "mnt_id: 0\n", "mnt_id: -1\n", "mnt_id: no\n",
                                      "mnt_id: 1\nmnt_id: 2\n", "mnt_id: ١\n"])
def test_missing_or_malformed_kernel_mount_identity_fails_closed(attachment, monkeypatch, fdinfo):
    con, _, _ = attachment
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path).startswith("/proc/self/fdinfo/"):
            return fdinfo
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert_unproven(con, "bootstrap")


@pytest.mark.parametrize("component", ["root", "parent"])
def test_symlink_attachment_is_not_followed(attachment, tmp_path, monkeypatch, component):
    con, path, _ = attachment
    link = tmp_path / "link"
    link.symlink_to(path if component == "root" else tmp_path, target_is_directory=True)
    monkeypatch.setattr(register, "archive_path", lambda *a: link if component == "root" else link / path.name)
    assert_unproven(con, "bootstrap")


@pytest.mark.parametrize("failure", [None, "probe", "identity", "reopened_identity", "final"])
def test_all_owned_descriptors_close_on_success_and_failure(attachment, monkeypatch, failure):
    con, _, _ = attachment
    opened = set()
    real_open, real_close = os.open, os.close

    def open_fd(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.add(fd)
        return fd

    def close_fd(fd):
        real_close(fd)
        opened.remove(fd)

    monkeypatch.setattr(ao.os, "open", open_fd)
    monkeypatch.setattr(ao.os, "close", close_fd)
    if failure:
        def fail(*args):
            raise OSError("synthetic read failure")
        if failure == "probe":
            monkeypatch.setattr(register, "probe_serial", fail)
        elif failure == "identity":
            monkeypatch.setattr(ao, "_fd_identity", fail)
        elif failure == "reopened_identity":
            original = ao._fd_identity
            calls = 0

            def fail_reopened(fd):
                nonlocal calls
                calls += 1
                if calls == 3:  # first reopened pathname, with both descriptors owned
                    raise OSError("reopened descriptor identity unavailable")
                return original(fd)

            monkeypatch.setattr(ao, "_fd_identity", fail_reopened)
        else:
            original = ao.BoundDirectory.check
            calls = 0

            def fail_final(bound):
                nonlocal calls
                calls += 1
                if calls == 16:  # initial check + two checks for seven components
                    raise BlockObservationError("final attachment check failed")
                return original(bound)

            monkeypatch.setattr(ao.BoundDirectory, "check", fail_final)
        assert_unproven(con, "bootstrap")
    else:
        assert bs._live_evidence(con, "drive-a").proven
    assert not opened


def test_annex_probe_reads_pinned_path_without_inherited_descriptor(tmp_path):
    path = tmp_path / "archive"

    def initialize(uuid):
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(path), "config", "--local", "annex.uuid", uuid],
                       capture_output=True, check=True)

    initialize("original-annex")
    with ao.BoundDirectory(path) as bound:
        saved_fd = bound.fd
        assert not os.get_inheritable(saved_fd)
        path.rename(tmp_path / "detached")
        initialize("replacement-annex")
        assert register.probe_annex_uuid(bound.pinned_path) == "original-annex"
        assert register.probe_annex_uuid(path) == "replacement-annex"
        with pytest.raises(BlockObservationError, match="attachment changed"):
            bound.check()
    with pytest.raises(OSError):
        os.fstat(saved_fd)


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError, UnicodeError])
def test_unreadable_fdinfo_is_unproven(attachment, monkeypatch, error):
    con, _, _ = attachment
    original = Path.read_text

    def read(path, *args, **kwargs):
        if str(path).startswith("/proc/self/fdinfo/"):
            raise error("descriptor mount information unavailable")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    assert_unproven(con, "bootstrap")


def test_composite_pins_annex_and_capacity_reads(attachment, monkeypatch):
    con, path, _ = attachment
    expected_inode = path.stat().st_ino
    original_annex, original_space = register.probe_annex_uuid, os.fstatvfs
    annex_calls, space_calls = [], []

    def annex(pinned):
        assert str(pinned).startswith(f"/proc/{os.getpid()}/fd/")
        assert pinned.stat().st_ino == expected_inode
        annex_calls.append(pinned)
        return original_annex(pinned)

    def space(fd):
        assert isinstance(fd, int) and os.fstat(fd).st_ino == expected_inode
        space_calls.append(fd)
        return original_space(fd)

    monkeypatch.setattr(register, "probe_annex_uuid", annex)
    monkeypatch.setattr(os, "fstatvfs", space)
    monkeypatch.setattr(os, "statvfs", lambda _: pytest.fail("must use the retained descriptor"))
    assert bs._live_evidence(con, "drive-a").proven
    assert len(annex_calls) == len(space_calls) == 2
