"""Destructive drive formatting fails closed without touching real block devices."""
from __future__ import annotations

import json
import stat
import subprocess
from types import SimpleNamespace
from unittest import mock

from modelark import register


def _cp(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _topology(children=None, *, kind="disk", mounts=None):
    return json.dumps({"blockdevices": [{
        "path": "/dev/sdz", "type": kind, "mountpoints": mounts or [None],
        "fstype": None, "children": children or [],
    }]})


def _probe(topology, *, swap="", root="/dev/nvme0n1p2\n"):
    def run(*args, check=True):
        if args[0] == "lsblk":
            return _cp(topology)
        if args[0] == "swapon":
            return _cp(swap)
        if args[0] == "findmnt":
            return _cp(root)
        raise AssertionError(args)
    return run


def _validate(topology, **kwargs):
    block = SimpleNamespace(st_mode=stat.S_IFBLK)
    with mock.patch.object(register.os, "stat", return_value=block), \
         mock.patch.object(register, "_run", side_effect=_probe(topology, **kwargs)):
        register._validate_format_target("/dev/sdz")


def _must_refuse(topology, message, **kwargs):
    try:
        _validate(topology, **kwargs)
        raise AssertionError("unsafe target must be refused")
    except RuntimeError as exc:
        assert message in str(exc), exc


def test_unmounted_plain_disk_is_accepted():
    _validate(_topology())


def test_non_block_path_is_refused_before_lsblk():
    regular = SimpleNamespace(st_mode=stat.S_IFREG)
    with mock.patch.object(register.os, "stat", return_value=regular), \
         mock.patch.object(register, "_run") as run:
        try:
            register._validate_format_target("/tmp/not-a-device")
            raise AssertionError("regular file must be refused")
        except RuntimeError as exc:
            assert "not a block device" in str(exc)
        run.assert_not_called()


def test_mounted_descendant_is_never_auto_unmounted():
    part = {"path": "/dev/sdz1", "type": "part", "mountpoints": ["/media/user/archive"],
            "fstype": "ext4"}
    _must_refuse(_topology([part]), "mounted device(s) detected")


def test_swap_and_active_storage_stacks_are_refused():
    part = {"path": "/dev/sdz1", "type": "part", "mountpoints": [None], "fstype": "swap"}
    _must_refuse(_topology([part]), "active swap", swap="/dev/sdz1\n")
    crypt = {"path": "/dev/mapper/secret", "type": "crypt", "mountpoints": [None], "fstype": None}
    _must_refuse(_topology([crypt]), "active storage stack")


def test_root_backing_device_and_unsupported_type_are_refused():
    _must_refuse(_topology(), "system root filesystem", root="/dev/sdz\n")
    _must_refuse(_topology(kind="loop"), "unsupported block-device type")


def test_safety_probe_failures_are_refused():
    block = SimpleNamespace(st_mode=stat.S_IFBLK)
    cases = [
        ([_cp(returncode=1, stderr="lsblk failed")], "topology inspection failed"),
        ([_cp("not json")], "invalid lsblk topology output"),
        ([_cp(_topology()), _cp(returncode=1)], "could not inspect active swap"),
        ([_cp(_topology()), _cp(), _cp(returncode=1)], "could not identify the system root"),
    ]
    for results, message in cases:
        with mock.patch.object(register.os, "stat", return_value=block), \
             mock.patch.object(register, "_run", side_effect=results):
            try:
                register._validate_format_target("/dev/sdz")
                raise AssertionError(message)
            except RuntimeError as exc:
                assert message in str(exc), exc


def test_format_requires_exact_typed_device_confirmation():
    for value in (None, "/dev/sdy", "sdz"):
        try:
            register._require_format_confirmation("/dev/sdz", value)
            raise AssertionError(value)
        except RuntimeError as exc:
            assert "--confirm-format /dev/sdz" in str(exc)
    register._require_format_confirmation("/dev/sdz", "/dev/sdz")


def test_failed_wipe_stops_before_mkfs():
    calls = []

    def run(*args, check=True):
        calls.append(args)
        if args[0] == "findmnt":
            return _cp("/dev/nvme0n1p2\n")
        if "wipefs" in args:
            raise RuntimeError("wipe failed")
        return _cp()

    with mock.patch.object(register, "_validate_format_target"), \
         mock.patch.object(register, "_run", side_effect=run):
        try:
            register._mkfs("/dev/sdz", "ext4", "drive-09")
            raise AssertionError("wipe failure must abort")
        except RuntimeError as exc:
            assert "wipe failed" in str(exc)
    assert not any("mkfs.ext4" in call for call in calls), calls


def test_failed_root_reconfirmation_stops_before_wipe():
    calls = []

    def run(*args, check=True):
        calls.append(args)
        if args[0] == "findmnt":
            return _cp(returncode=1, stderr="transient findmnt failure")
        return _cp()

    with mock.patch.object(register, "_validate_format_target"), \
         mock.patch.object(register, "_run", side_effect=run):
        try:
            register._mkfs("/dev/sdz", "ext4", "drive-09")
            raise AssertionError("root-device reconfirmation failure must abort")
        except RuntimeError as exc:
            assert "could not re-confirm system root device" in str(exc)
    assert not any("wipefs" in call or "mkfs.ext4" in call for call in calls), calls


def test_format_dry_run_preflights_without_confirmation_or_writes():
    baseline = {"model": "Test Disk", "serial": "SERIAL", "smart_passed": True,
                "reallocated": 0, "pending": 0, "offline_uncorrectable": 0,
                "power_on_hours": 1, "verdict": "ok"}
    with mock.patch.object(register, "_transport", return_value="usb"), \
         mock.patch.object(register, "smart_baseline", return_value=baseline), \
         mock.patch.object(register, "_validate_format_target") as validate, \
         mock.patch.object(register, "_mkfs") as mkfs:
        result = register.register_drive("/dev/sdz", "drive-09", format_fs="ext4", dry_run=True)
    validate.assert_called_once_with("/dev/sdz")
    mkfs.assert_not_called()
    assert result["format"] == "ext4"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
