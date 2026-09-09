"""Physical serial observation below the command boundary; no host discovery."""
import copy
import json
from types import SimpleNamespace
import subprocess

import pytest

from modelark import block_identity as bi


@pytest.fixture
def observed(monkeypatch):
    part = {"path": "/dev/sda1", "type": "part", "maj:min": "8:1",
            "pkname": "/dev/sda", "serial": None, "uuid": "archive-fs"}
    disk = {"path": "/dev/sda", "type": "disk", "maj:min": "8:0",
            "serial": "ZR16L100", "wwn": "disk-wwn", "children": [part]}
    state = {"payload": {"blockdevices": [disk]}, "mount": "8:1", "calls": []}
    def run(argv, **kwargs):
        assert kwargs == {"check": True, "capture_output": True, "text": True}
        state["calls"].append(argv)
        if argv[0] == "findmnt":
            assert argv == ["findmnt", "--json", "--first-only", "--output", "MAJ:MIN",
                            "--target", "/archive/modelark"]
            payload = {"filesystems": [{"maj:min": state["mount"], "source": "UUID=archive-fs"}]}
        else:
            assert argv == ["lsblk", "--json", "--bytes", "--paths", "--output",
                            "NAME,PATH,KNAME,TYPE,PKNAME,MAJ:MIN,SIZE,FSTYPE,UUID,SERIAL,WWN,TRAN,RO"]
            payload = state["payload"]
        return SimpleNamespace(stdout=json.dumps(payload))
    monkeypatch.setattr(bi.subprocess, "run", run)
    return state, disk, part


def observe():
    return bi.observe_mounted_disk("/archive/modelark")


def test_partition_serial_absent_parent_present(observed):
    state, _, part = observed
    assert part["serial"] is None
    assert observe() == bi.MountedDisk("8:1", "8:0", "ZR16L100")
    assert [call[0] for call in state["calls"]] == ["findmnt", "lsblk", "lsblk", "findmnt"]


def test_whole_disk_mount_needs_no_parent(observed):
    state, _, _ = observed
    state["mount"] = "8:0"
    assert observe() == bi.MountedDisk("8:0", "8:0", "ZR16L100")


@pytest.mark.parametrize("serial, expected", [(None, None), ("", None), ("  ", None),
                                              ("  MiXeD serial  ", "MiXeD serial")])
def test_absent_serial_is_not_wwn_and_spelling_is_preserved(observed, serial, expected):
    _, disk, _ = observed
    disk["serial"] = serial
    assert observe().serial == expected


@pytest.mark.parametrize("serial", [False, 1, [], {}])
def test_malformed_serial_is_not_absence(observed, serial):
    _, disk, _ = observed
    disk["serial"] = serial
    with pytest.raises(bi.BlockObservationError, match="serial observation"):
        observe()


def test_missing_serial_column_is_not_absence(observed):
    _, disk, _ = observed
    del disk["serial"]
    with pytest.raises(bi.BlockObservationError, match="serial observation"):
        observe()


@pytest.mark.parametrize("field,value", [("type", []), ("type", None), ("children", 1),
                                         ("children", {}), ("children", "")])
def test_malformed_graph_fields_refuse(observed, field, value):
    _, disk, _ = observed
    disk[field] = value
    with pytest.raises(bi.BlockObservationError):
        observe()


def test_flat_mapper_kernel_alias_and_lvm_chain(observed):
    state, disk, part = observed
    disk.pop("children")
    crypt = {"path": "/dev/mapper/crypt", "kname": "/dev/dm-0", "type": "crypt",
             "maj:min": "253:0", "pkname": "sda1"}
    lvm = {"path": "/dev/mapper/vg-lv", "type": "lvm", "maj:min": "253:1", "pkname": "dm-0"}
    state["payload"]["blockdevices"] += [part, crypt, lvm]
    state["mount"] = "253:1"
    assert observe() == bi.MountedDisk("253:1", "8:0", "ZR16L100")


@pytest.mark.parametrize("problem", ["duplicate", "multiple-parent", "missing-parent", "cycle",
                                     "unknown-type", "absent-mount", "duplicate-serial"])
def test_unproven_ancestry_never_returns_null(observed, problem):
    state, disk, part = observed
    if problem == "duplicate":
        state["payload"]["blockdevices"].append(copy.deepcopy(part))
    elif problem == "multiple-parent":
        part["pkname"] = "/dev/sdb"
        state["payload"]["blockdevices"].append(
            {"path": "/dev/sdb", "type": "disk", "maj:min": "8:16", "serial": "OTHER"})
    elif problem == "missing-parent":
        part["pkname"] = "/dev/missing"
    elif problem == "cycle":
        disk["pkname"] = "/dev/sda1"
        disk["type"] = "crypt"
    elif problem == "unknown-type":
        part["type"] = "raid1"
    elif problem == "absent-mount":
        state["mount"] = "9:9"
    elif problem == "duplicate-serial":
        state["payload"]["blockdevices"].append(
            {"path": "/dev/sdb", "type": "disk", "maj:min": "8:16", "serial": disk["serial"]})
    with pytest.raises(bi.BlockObservationError):
        observe()


@pytest.mark.parametrize("command", ["findmnt", "lsblk"])
@pytest.mark.parametrize("failure", [PermissionError("denied"), subprocess.CalledProcessError(1, "probe"),
                                     json.JSONDecodeError("bad", "{", 1)])
def test_probe_errors_never_return_null(observed, monkeypatch, command, failure):
    run = bi.subprocess.run
    def broken(argv, **kwargs):
        if argv[0] == command:
            raise failure
        return run(argv, **kwargs)
    monkeypatch.setattr(bi.subprocess, "run", broken)
    with pytest.raises(bi.BlockObservationError, match="observation failed") as caught:
        observe()
    assert caught.value.__cause__ is failure


@pytest.mark.parametrize("payload", [[], {}, {"filesystems": []}, {"filesystems": [None]},
                                     {"filesystems": [{"maj:min": "8:1"}, {"maj:min": "8:2"}]},
                                     {"filesystems": [{"maj:min": None}]}])
def test_malformed_mount_json_refuses(monkeypatch, payload):
    monkeypatch.setattr(bi.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload)))
    with pytest.raises(bi.BlockObservationError):
        observe()


def test_mount_change_during_inventory_refuses(observed, monkeypatch):
    state, *_ = observed
    run = bi.subprocess.run
    def changing(argv, **kwargs):
        result = run(argv, **kwargs)
        if argv[0] == "lsblk":
            state["mount"] = "8:0"
        return result
    monkeypatch.setattr(bi.subprocess, "run", changing)
    with pytest.raises(bi.BlockObservationError, match="mount changed"):
        observe()


@pytest.mark.parametrize("field", ["pkname", "kname", "path", "name"])
@pytest.mark.parametrize("value", [False, 1, [], {}, " /dev/sda ", " "])
def test_malformed_ancestry_string_fields_never_mean_absent(observed, field, value):
    state, disk, _ = observed
    state["mount"] = "8:0"
    disk[field] = value
    with pytest.raises(bi.BlockObservationError):
        observe()


@pytest.mark.parametrize("field", ["pkname", "kname"])
@pytest.mark.parametrize("value", [None, ""])
def test_explicit_absent_optional_ancestry_fields_remain_valid(observed, field, value):
    _, disk, _ = observed
    disk[field] = value
    assert observe().serial == "ZR16L100"


@pytest.mark.parametrize("first,second", [(" X ", "X"), ("X", " X "), (" X ", " X ")])
def test_serial_uniqueness_uses_same_normalization_as_observation(observed, first, second):
    state, disk, _ = observed
    disk["serial"] = first
    state["payload"]["blockdevices"].append(
        {"path": "/dev/sdb", "type": "disk", "maj:min": "8:16", "serial": second})
    with pytest.raises(bi.BlockObservationError, match="duplicate whole-disk identity"):
        observe()


@pytest.mark.parametrize("kind", [None, False, 1, [], {}, "", " ", " disk "])
def test_off_ancestry_malformed_type_cannot_hide_duplicate_serial(observed, kind):
    state, disk, _ = observed
    state["payload"]["blockdevices"].append(
        {"path": "/dev/sdb", "type": kind, "maj:min": "8:16", "serial": disk["serial"]})
    with pytest.raises(bi.BlockObservationError, match="type"):
        observe()


def test_off_ancestry_missing_type_refuses_even_without_serial(observed):
    state, _, _ = observed
    state["payload"]["blockdevices"].append({"path": "/dev/sdb", "maj:min": "8:16"})
    with pytest.raises(bi.BlockObservationError, match="type"):
        observe()


@pytest.mark.parametrize("value", [False, 1, [], {}, " /dev/sda "])
def test_nested_malformed_parent_is_not_overridden_by_tree(observed, value):
    _, _, part = observed
    part["pkname"] = value
    with pytest.raises(bi.BlockObservationError, match="pkname"):
        observe()


@pytest.mark.parametrize("change", ["parent", "serial", "missing", "malformed"])
def test_stable_mount_with_changed_backing_snapshot_refuses(observed, monkeypatch, change):
    state, disk, part = observed
    # A mounted mapper node retains its major:minor when its table changes.
    disk.pop("children")
    mapper = {"path": "/dev/mapper/crypt", "kname": "/dev/dm-0", "type": "crypt",
              "maj:min": "253:0", "pkname": "sda1"}
    other = {"path": "/dev/sdb", "type": "disk", "maj:min": "8:16", "serial": "OTHER"}
    state["payload"]["blockdevices"] += [part, mapper, other]
    state["mount"] = "253:0"
    run = bi.subprocess.run
    def changing(argv, **kwargs):
        result = run(argv, **kwargs)
        if argv[0] == "lsblk":
            if change == "parent":
                mapper["pkname"] = "sdb"
            elif change == "serial":
                disk["serial"] = "REPLACEMENT"
            elif change == "missing":
                state["payload"]["blockdevices"] = [other]
            else:
                mapper["pkname"] = []
        return result
    monkeypatch.setattr(bi.subprocess, "run", changing)
    with pytest.raises(bi.BlockObservationError, match="inventory changed"):
        observe()


@pytest.mark.parametrize("failure", [PermissionError("denied"), subprocess.CalledProcessError(1, "lsblk"),
                                     json.JSONDecodeError("bad", "{", 1)])
def test_final_inventory_failure_is_not_successful_observation(observed, monkeypatch, failure):
    run = bi.subprocess.run
    calls = []
    def failing(argv, **kwargs):
        if argv[0] == "lsblk":
            calls.append(argv)
            if len(calls) == 2:
                raise failure
        return run(argv, **kwargs)
    monkeypatch.setattr(bi.subprocess, "run", failing)
    with pytest.raises(bi.BlockObservationError, match="observation failed"):
        observe()


def test_slice_adapter_preserves_direct_policy_and_error_code(observed):
    from modelark.slice.hardware import _Inventory
    from modelark.slice.transaction import TransferRefusal
    state, _, part = observed
    part["type"] = "crypt"
    inventory = _Inventory(state["payload"])
    assert inventory.disk("8:1") == "8:0"
    with pytest.raises(TransferRefusal) as caught:
        inventory.disk("8:1", direct=True)
    assert caught.value.code == "DESTINATION_UNPROVEN"


def test_neutral_module_does_not_depend_on_workflows():
    import ast
    from pathlib import Path
    tree = ast.parse(Path(bi.__file__).read_text())
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert all(not isinstance(node, ast.ImportFrom) or not node.level for node in imports)
    assert all(not name.name.startswith("modelark") for node in imports if isinstance(node, ast.Import)
               for name in node.names)
