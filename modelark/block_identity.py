"""Read-only physical ancestry, independent of archive and destination policy.

Observations are not leases. Callers own confinement, device eligibility, identity
admission, exclusion and the final re-observation before committing evidence.
"""
from dataclasses import dataclass
import json
import os
import re
import subprocess


class BlockObservationError(ValueError):
    """The physical backing could not be proven; NOT evidence of no serial."""


def _refuse(detail):
    raise BlockObservationError(detail)


def _nonempty(value):
    return value if isinstance(value, str) and value.strip() == value and value else None


def read_inventory():
    """Collect the same explicit columns for all physical-ancestry consumers."""
    result = subprocess.run(
        ["lsblk", "--json", "--bytes", "--paths", "--output",
         "NAME,PATH,KNAME,TYPE,PKNAME,MAJ:MIN,SIZE,FSTYPE,UUID,SERIAL,WWN,TRAN,RO"],
        check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


class BlockInventory:
    """Validated lsblk graph. Physical ancestry alone grants no workflow authority."""

    def __init__(self, payload):
        self.nodes, self.parents, self.paths = {}, {}, {}
        def visit(node, parent=None):
            if not isinstance(node, dict):
                _refuse("invalid block-device inventory")
            key = node.get("maj:min")
            path = _nonempty(node.get("path")) or _nonempty(node.get("name"))
            if (not isinstance(key, str) or not re.fullmatch(r"\d+:\d+", key)
                    or not path or not path.startswith("/dev/") or key in self.nodes or path in self.paths):
                _refuse("ambiguous block-device inventory")
            self.nodes[key], self.parents[key], self.paths[path] = node, parent, key
            # Mapper PATH and PKNAME can differ. Only explicit KNAME evidence
            # establishes the alias; conflicting paths remain an error.
            kernel_path = _nonempty(node.get("kname"))
            if kernel_path:
                if not kernel_path.startswith("/"):
                    kernel_path = "/dev/" + kernel_path
                if not kernel_path.startswith("/dev/") or self.paths.get(kernel_path, key) != key:
                    _refuse("ambiguous kernel block-device alias")
                self.paths[kernel_path] = key
            children = node.get("children")
            if children is not None and not isinstance(children, list):
                _refuse("invalid block-device children")
            for child in children or ():
                visit(child, key)
        if not isinstance(payload, dict) or not isinstance(payload.get("blockdevices"), list):
            _refuse("invalid lsblk JSON")
        for node in payload["blockdevices"]:
            visit(node)
        # Resolve explicit PKNAME on flat inventories as well as JSON trees.
        for key, node in self.nodes.items():
            parent_path = _nonempty(node.get("pkname"))
            if parent_path and not parent_path.startswith("/"):
                parent_path = "/dev/" + parent_path
            if parent_path:
                parent = self.paths.get(parent_path)
                if parent is None or self.parents[key] not in {None, parent}:
                    _refuse("ambiguous parent disk")
                self.parents[key] = parent
        uuids, disk_ids = set(), set()
        for node in self.nodes.values():
            uuid = _nonempty(node.get("uuid"))
            if uuid:
                if uuid.casefold() in uuids:
                    _refuse("duplicate filesystem UUID")
                uuids.add(uuid.casefold())
            if node.get("type") == "disk":
                for field in ("serial", "wwn"):
                    value = _nonempty(node.get(field))
                    if value:
                        key = (field, value)
                        if key in disk_ids:
                            _refuse("duplicate whole-disk identity")
                        disk_ids.add(key)

    def disk(self, key, *, direct=False, allowed_intermediates=None):
        """Find one disk; optional policy restricts the intervening node types.

        The default retains the pre-extraction ancestry used for protected-role
        exclusion. Direct delivery and archive observation impose their own limits.
        """
        seen = set()
        while key in self.nodes and key not in seen:
            seen.add(key)
            node = self.nodes[key]
            kind, parent = node.get("type"), self.parents[key]
            if not isinstance(kind, str):
                _refuse("invalid block-device type")
            if kind == "disk" and parent is None:
                return key
            if direct and (kind != "part" or parent is None
                           or self.nodes[parent].get("type") != "disk"):
                _refuse("stacked or unknown delivery device")
            if allowed_intermediates is not None and kind not in allowed_intermediates:
                _refuse("unsupported physical ancestry")
            if parent is None:
                break
            key = parent
        _refuse("cannot prove whole parent disk")


@dataclass(frozen=True)
class MountedDisk:
    mount_major_minor: str
    disk_major_minor: str
    serial: str | None


def _mounted_device(path):
    result = subprocess.run(
        ["findmnt", "--json", "--first-only", "--output", "MAJ:MIN", "--target", os.fspath(path)],
        check=True, capture_output=True, text=True)
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        _refuse("invalid covering mount inventory")
    mounts = payload.get("filesystems")
    if not isinstance(mounts, list) or len(mounts) != 1 or not isinstance(mounts[0], dict):
        _refuse("ambiguous covering mount")
    key = mounts[0].get("maj:min")
    if not isinstance(key, str) or not re.fullmatch(r"\d+:\d+", key):
        _refuse("invalid covering mount device")
    return key


def observe_mounted_disk(path):
    """Resolve mount major:minor to disk, never querying SERIAL on a partition.

    Null means a successful, unambiguous observation with an absent disk serial.
    Command, permission, JSON and topology failures raise instead. UUID=/mapper
    spellings in findmnt SOURCE cannot affect ancestry. No cached topology is used.
    """
    try:
        key = _mounted_device(path)
        inventory = BlockInventory(read_inventory())
        disk_key = inventory.disk(key, allowed_intermediates={"part", "crypt", "lvm"})
        disk = inventory.nodes[disk_key]
        if "serial" not in disk or (disk["serial"] is not None and not isinstance(disk["serial"], str)):
            _refuse("invalid physical disk serial observation")
        serial = disk["serial"]
        if serial is not None:
            serial = serial.strip() or None
        if _mounted_device(path) != key:
            _refuse("covering mount changed during observation")
        return MountedDisk(key, disk_key, serial)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise BlockObservationError("physical disk observation failed: " + str(exc)) from exc
