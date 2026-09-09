"""Kernel-name evidence resolves mapper PKNAME without relaxing ancestry checks."""
from copy import deepcopy

import pytest

from modelark.slice.hardware import _Inventory
from modelark.slice.transaction import TransferRefusal


def mapped():
    logical = {"path": "/dev/mapper/data-root", "kname": "/dev/dm-1", "maj:min": "253:1",
               "type": "lvm", "pkname": "/dev/dm-0", "uuid": "ext4-root"}
    encrypted = {"path": "/dev/mapper/cryptdata", "kname": "/dev/dm-0", "maj:min": "253:0",
                 "type": "crypt", "pkname": "/dev/nvme0n1p3", "children": [logical]}
    partition = {"path": "/dev/nvme0n1p3", "maj:min": "259:3", "type": "part",
                 "pkname": "/dev/nvme0n1", "children": [encrypted]}
    disk = {"path": "/dev/nvme0n1", "maj:min": "259:0", "type": "disk", "children": [partition]}
    return {"blockdevices": [disk]}, encrypted, logical


def test_explicit_kernel_alias_resolves_luks_lvm_backing():
    payload, _, _ = mapped()
    inventory = _Inventory(payload)
    assert inventory.disk("253:1") == "259:0"
    assert inventory.paths["/dev/dm-0"] == inventory.paths["/dev/mapper/cryptdata"]
    with pytest.raises(TransferRefusal, match="stacked"):
        inventory.disk("253:1", direct=True)  # Legacy USB policy still rejects mapping.


@pytest.mark.parametrize("change", ["missing", "disagreement", "collision"])
def test_missing_or_ambiguous_kernel_evidence_still_refuses(change):
    payload, encrypted, logical = mapped()
    if change == "missing":
        encrypted.pop("kname")
    elif change == "disagreement":
        logical["pkname"] = "/dev/nvme0n1p3"
    else:
        logical["kname"] = encrypted["kname"]
    with pytest.raises(TransferRefusal, match="ambiguous"):
        _Inventory(payload)


def test_multi_parent_tree_remains_unqualified():
    payload, _, logical = mapped()
    payload["blockdevices"].append(deepcopy(logical))
    with pytest.raises(TransferRefusal, match="ambiguous"):
        _Inventory(payload)
