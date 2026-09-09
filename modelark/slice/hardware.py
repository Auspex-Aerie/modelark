"""Explicit, read-only Linux device eligibility observations for direct Slice delivery.

No discovery is performed at import or construction. Production observation reads lsblk,
mountinfo and active swap inventory only when observe() is explicitly called. Tests inject
those inventories and bind disposable directories. Evidence is an observation, not a lease:
writers must retain BoundTree and re-observe identity under the shared execution authority.
"""
from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from .linux import BoundTree, require_create_access, require_no_default_acl
from .paths import canonical_attachment
from .io_errors import attachment_refusal, classify_io, probe_io
from .host_observation import (ProtectedTarget, decode_proc_path, parse_mounts, proc_fields,
                               proc_lines, read_fs_text, resolve_protected)
from .transaction import DestinationBinding, TransferRefusal


@dataclass(frozen=True)
class DeviceEvidence:
    device_id: str
    fs_uuid: str
    serial: str | None
    total_bytes: int
    mount_path: str
    mount_id: int
    fs_type: str
    available_bytes: int
    block_size: int
    name_max: int
    device_size: int
    profile: str

    def binding(self):
        """Legacy mount_id slot seals the stable capability profile, NOT an attachment ID.

        Linux mount IDs change on reattachment and are separately checked against retained
        descriptors. The stable profile includes capacity/filesystem/device capabilities,
        allowing a later explicit Start to re-observe the same device after remounting.
        """
        return DestinationBinding(self.device_id, self.fs_uuid, self.profile, self.available_bytes)


def _refuse(detail, code="DESTINATION_UNPROVEN"):
    raise TransferRefusal(code, detail)


def _decode(value):
    return decode_proc_path(value)


def _mounts(text):
    return parse_mounts(text)


_SYSTEM_PATHS = ('/', '/boot', '/home', '/var', '/usr', '/etc', '/bin', '/sbin', '/lib', '/lib64')


def _swap_records(swaps):
    rows = proc_lines(swaps)
    if not rows or not proc_fields(rows[0]) or proc_fields(rows[0])[0] != 'Filename':
        _refuse('active swap inventory unavailable')
    result = []
    for row in rows[1:]:
        fields = proc_fields(row)
        if len(fields) != 5 or fields[1] not in {'file', 'partition'}:
            _refuse('invalid active swap inventory')
        name = _decode(fields[0])
        if not name.startswith('/'):
            _refuse('nonabsolute active swap path')
        result.append((name, fields[1]))
    return tuple(result)


def _inventory():
    result = subprocess.run(
        ["lsblk", "--json", "--bytes", "--paths", "--output",
         "NAME,PATH,KNAME,TYPE,PKNAME,MAJ:MIN,SIZE,FSTYPE,UUID,SERIAL,WWN,TRAN,RO"],
        check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def _nonempty(value):
    return value if isinstance(value, str) and value.strip() == value and value else None


def _integer(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (ValueError, TypeError):
        return None
    return result if result > 0 and str(result) == str(value) else None


def _backing_identity(major_minor):
    info = Path('/sys/dev/block', major_minor).stat()
    return info.st_dev, info.st_ino


@probe_io('DESTINATION_NOT_WRITABLE', 'effective creation permission/user xattrs unavailable')
def _writable_root(tree, mount):
    if 'rw' not in mount.options or {'ro', 'nouser_xattr'} & mount.options:
        _refuse('writable mount and user xattrs required', 'DESTINATION_NOT_WRITABLE')
    if any('quota' in option or option.startswith('jqfmt=') for option in mount.options):
        _refuse('quota-enabled mounts are unsupported', 'FILESYSTEM_UNSUPPORTED')
    require_create_access(tree.fd)
    try:
        os.getxattr(tree.fd, 'user.modelark.slice-capability-probe')
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise
    require_no_default_acl(tree.fd)


def _stable_attachment(evidence):
    return (evidence.device_id, evidence.fs_uuid, evidence.profile, evidence.total_bytes,
            evidence.mount_id, evidence.mount_path)


def _archive_roles(archives):
    return tuple((getattr(a, 'fs_uuid', None), getattr(a, 'serial', None)) for a in archives)


class _Inventory:
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
            # lsblk may spell a mapper node as /dev/mapper/name while a child's
            # PKNAME uses /dev/dm-N. KNAME is explicit inventory evidence for that
            # alias; do not infer ancestry from names or ignore a disagreement.
            kernel_path = _nonempty(node.get("kname"))
            if kernel_path:
                if not kernel_path.startswith("/"):
                    kernel_path = "/dev/" + kernel_path
                if not kernel_path.startswith("/dev/") or self.paths.get(kernel_path, key) != key:
                    _refuse("ambiguous kernel block-device alias")
                self.paths[kernel_path] = key
            for child in node.get("children") or ():
                visit(child, key)
        if not isinstance(payload, dict) or not isinstance(payload.get("blockdevices"), list):
            _refuse("invalid lsblk JSON")
        for node in payload["blockdevices"]:
            visit(node)
        # --json trees are expected, but resolve explicit PKNAME on a flat inventory too.
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

    def disk(self, key, *, direct=False):
        """Follow ancestry for exclusion, requiring a direct disk/partition for delivery."""
        seen = set()
        while key in self.nodes and key not in seen:
            seen.add(key)
            node = self.nodes[key]
            kind, parent = node.get("type"), self.parents[key]
            if kind == "disk" and parent is None:
                return key
            if direct and (kind != "part" or parent is None
                           or self.nodes[parent].get("type") != "disk"):
                _refuse("stacked or unknown delivery device")
            if parent is None:
                break
            key = parent
        _refuse("cannot prove whole parent disk")


class LinuxObserver:
    """Read-only observer with injectable inventory providers; no implicit live discovery."""

    def __init__(self, *, inventory=None, mounts=None, swaps=None, tree_factory=BoundTree, backing=None, resolver=None):
        self._inventory = inventory or _inventory
        self._mounts = mounts or (lambda: read_fs_text('/proc/self/mountinfo'))
        self._swaps = swaps or (lambda: read_fs_text('/proc/swaps'))
        self._tree_factory = tree_factory
        self._backing = backing or _backing_identity
        self._resolver = resolver or resolve_protected
        self._observed = {}

    def _protected_snapshot(self, mounts, swaps):
        requests = [(path, True, path != '/') for path in _SYSTEM_PATHS]
        requests.extend((name, False, False) for name, kind in _swap_records(swaps) if kind == 'file')
        snapshot = []
        for requested, directory, optional in requests:
            target = self._resolver(requested, directory=directory, optional=optional)
            if target is None:
                if not optional:
                    _refuse('required protected role is unavailable')
            else:
                target = ProtectedTarget(str(requested), target.path, target.major_minor, target.mount_id, target.inode)
                matching = [mount for mount in mounts if mount.mount_id == target.mount_id]
                if (len(matching) != 1 or matching[0].major_minor != target.major_minor
                        or not (target.path == matching[0].path
                                or target.path.startswith(matching[0].path.rstrip('/') + '/'))):
                    _refuse('protected descriptor differs from mount inventory')
            snapshot.append((directory, requested, target))
        return tuple(snapshot)

    def check_attachment(self, tree, evidence, *, archives=None):
        """No subprocess on unchanged topology; cached proof belongs to this attachment only."""
        key = (str(tree.path), tree.mount_id)
        proof = self._observed.get(key)
        if proof is None or _stable_attachment(proof['evidence']) != _stable_attachment(evidence):
            _refuse('no full observation for retained attachment', 'DESTINATION_CHANGED')
        try:
            # Check backing before path/stat IO: a disconnected filesystem may return EIO
            # while its mount ID is still present. A lost tree never silently reacquires.
            self._check_backing(tree, proof)
            tree.check()
            roles = proof['archives'] if archives is None else tuple(archives)
            current_mounts, current_swaps = _mounts(self._mounts()), self._swaps()
            current_protected = self._protected_snapshot(current_mounts, current_swaps)
            if (current_mounts != proof['mounts'] or current_swaps != proof['swaps']
                    or current_protected != proof['protected']
                    or _archive_roles(roles) != _archive_roles(proof['archives'])):
                fresh = self.observe(tree.path, writable=proof['writable'], archives=roles)
                if _stable_attachment(fresh) != _stable_attachment(evidence):
                    _refuse('attachment changed during revalidation', 'DESTINATION_CHANGED')
            elif proof['writable']:
                _writable_root(tree, proof['mount'])
            tree.check()
            self._check_backing(tree, proof)
        except OSError as exc:
            raise classify_io(exc, lambda: self.recheck_attachment(tree, evidence), TransferRefusal(
                'DESTINATION_UNPROVEN', 'attachment check failed: ' + str(exc))) from exc

    def recheck_attachment(self, tree, evidence):
        """Raw failure-time proof; no discovery, policy admission or recursive classifier."""
        proof = self._observed.get((str(tree.path), tree.mount_id))
        if proof is None or _stable_attachment(proof['evidence']) != _stable_attachment(evidence):
            _refuse('no matching retained attachment evidence')
        self._check_backing(tree, proof)
        try:
            tree.check()
        except (OSError, TransferRefusal) as exc:
            refusal = attachment_refusal(lambda: self._check_backing(tree, proof))
            if refusal is not None:
                raise refusal from exc
            raise
        self._check_backing(tree, proof)

    def _check_backing(self, tree, proof):
        if tree._attachment_lost:
            _refuse('attachment lost; fresh Start required', 'WAITING_DESTINATION')
        try:
            backing = self._backing(proof['device'])
        except FileNotFoundError:
            tree._attachment_lost = True
            _refuse('mounted block backing disappeared', 'WAITING_DESTINATION')
        if backing != proof['backing']:
            _refuse('block backing was replaced', 'DESTINATION_CHANGED')

    def observe(self, path, writable=False, archives=()):
        path = canonical_attachment(path)
        try:
            return self._observe(path, writable, tuple(archives))
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            fallback = TransferRefusal("DESTINATION_UNPROVEN", "device observation failed: " + str(exc))
            if isinstance(exc, OSError):
                raise classify_io(exc, lambda: self._recheck_observed(path), fallback) from exc
            raise fallback from exc

    def _recheck_observed(self, path):
        # Only the latest full proof for this spelling is relevant. This callback
        # is raw evidence, never policy evaluation or another IO classifier.
        for (observed_path, _), proof in reversed(tuple(self._observed.items())):
            if observed_path == str(path):
                try:
                    backing = self._backing(proof['device'])
                except FileNotFoundError:
                    _refuse('previously observed block backing disappeared', 'WAITING_DESTINATION')
                if backing != proof['backing']:
                    _refuse('previously observed block backing was replaced', 'DESTINATION_CHANGED')
                return

    def _observe(self, path, writable, archives):
        inventory_snapshot = json.dumps(self._inventory(), sort_keys=True)
        inventory = _Inventory(json.loads(inventory_snapshot))
        mounts = _mounts(self._mounts())
        swaps = self._swaps()
        with self._tree_factory(path, writable=writable) as tree:
            target = str(tree.path)
            if target == "/":
                _refuse("host root is not a delivery device")
            matching = [m for m in mounts if m.path == target or (
                not writable and target.startswith(m.path.rstrip("/") + "/"))]
            if matching and not writable:
                longest = max(len(m.path) for m in matching)
                matching = [m for m in matching if len(m.path) == longest]
            if len(matching) != 1:
                _refuse("path must have a unique covering mount; writable paths require its exact root")
            mount = matching[0]
            actual = os.fstat(tree.fd)
            major_minor = f"{os.major(actual.st_dev)}:{os.minor(actual.st_dev)}"
            if mount.root != "/" or mount.mount_id != tree.mount_id or mount.major_minor != major_minor:
                _refuse("mount inventory differs from retained descriptor")
            if mount.major_minor not in inventory.nodes:
                _refuse("mounted block backing is absent", "WAITING_DESTINATION")
            try:
                backing = self._backing(major_minor)
            except FileNotFoundError:
                _refuse('mounted block backing disappeared', 'WAITING_DESTINATION')
            if sum(m.major_minor == mount.major_minor for m in mounts) != 1:
                _refuse("filesystem has multiple mount aliases")
            if any(m.path.startswith(mount.path.rstrip("/") + "/") and m != mount for m in mounts):
                _refuse("nested mounts within delivery filesystem")
            node = inventory.nodes[mount.major_minor]
            disk_key = inventory.disk(mount.major_minor, direct=True)
            disk = inventory.nodes[disk_key]
            uuid = _nonempty(node.get("uuid"))
            serial, wwn = _nonempty(disk.get("serial")), _nonempty(disk.get("wwn"))
            device_size = _integer(node.get("size"))
            disk_size = _integer(disk.get("size"))
            if not uuid or not (serial or wwn) or not device_size or not disk_size or device_size > disk_size:
                _refuse("stable filesystem and parent disk identity/capacity required")
            permitted = {"ext4"} if writable else {"ext4", "xfs"}
            if node.get("fstype") != mount.fs_type or mount.fs_type not in permitted:
                _refuse("unsupported or inconsistent filesystem", "FILESYSTEM_UNSUPPORTED")
            if writable and (disk.get("tran") != "usb" or "rw" not in mount.options or "ro" in mount.options
                             or node.get("ro") not in (False, 0, "0") or disk.get("ro") not in (False, 0, "0")):
                _refuse("destination must be a writable direct USB disk")
            if writable:
                _writable_root(tree, mount)

            protected_snapshot = self._protected_snapshot(mounts, swaps)
            protected = set()
            for directory, requested, protected_target in protected_snapshot:
                if protected_target is None:
                    continue
                protected.add(inventory.disk(protected_target.major_minor))
                if directory and requested != '/':
                    for other in mounts:
                        if (other.path.startswith(protected_target.path.rstrip('/') + '/')
                                or other.path.startswith(requested.rstrip('/') + '/')):
                            if other.major_minor in inventory.nodes:
                                protected.add(inventory.disk(other.major_minor))
                            elif other.source.startswith('/dev/'):
                                _refuse('nested protected block mount has unknown ancestry')
            for name, kind in _swap_records(swaps):
                if kind == 'file':
                    continue  # Descriptor-backed target was included in the snapshot.
                if name not in inventory.paths:
                    _refuse("cannot resolve active swap parent disk")
                protected.add(inventory.disk(inventory.paths[name]))
            if disk_key in protected:
                _refuse("system or swap disk and all siblings are excluded")
            if writable:
                for other in mounts:
                    if other.major_minor in inventory.nodes and other.major_minor != mount.major_minor:
                        # Loop mounts are not sibling partitions. Protected/system/swap
                        # ancestry above remains strict, as do unknown mapped devices here.
                        if inventory.nodes[other.major_minor].get('type') == 'loop':
                            continue
                        if inventory.disk(other.major_minor) == disk_key:
                            _refuse("destination disk has another mounted partition")
                for archive in archives:
                    archive_uuid = _nonempty(getattr(archive, "fs_uuid", None))
                    archive_serial = _nonempty(getattr(archive, "serial", None))
                    if not archive_uuid and not archive_serial:
                        _refuse("registered archive lacks comparable physical identity")
                    if archive_serial and archive_serial in {serial, wwn}:
                        _refuse("registered archive parent disk is excluded")
                    for key, candidate in inventory.nodes.items():
                        candidate_uuid = _nonempty(candidate.get("uuid"))
                        if (archive_uuid and candidate_uuid and candidate_uuid.casefold() == archive_uuid.casefold()
                                and inventory.disk(key) == disk_key):
                            _refuse("registered archive filesystem or sibling disk is excluded")

            capacity = os.fstatvfs(tree.fd)
            total = capacity.f_blocks * capacity.f_frsize
            available = capacity.f_bavail * capacity.f_frsize
            block_size = capacity.f_frsize
            name_max = os.fpathconf(tree.fd, "PC_NAME_MAX")
            if (total <= 0 or total > device_size or not 0 <= available <= total
                    or block_size <= 0 or name_max <= 0):
                _refuse("filesystem capacity/capability cannot be proven")
            device_id = ("wwn:" + wwn) if wwn else ("serial:" + serial)
            profile_fields = {"version": "modelark.linux.direct.v1", "device_id": device_id,
                              "disk_size": disk_size, "device_size": device_size, "filesystem_id": uuid,
                              "filesystem": mount.fs_type, "total_bytes": total,
                              "block_size": block_size, "name_max": name_max}
            profile = "linux-direct-v1:" + hashlib.sha256(json.dumps(
                profile_fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            tree.check()
            if (json.dumps(self._inventory(), sort_keys=True) != inventory_snapshot
                    or _mounts(self._mounts()) != mounts or self._swaps() != swaps
                    or self._protected_snapshot(mounts, swaps) != protected_snapshot):
                _refuse("hardware inventory changed during descriptor observation")
            try:
                final_backing = self._backing(major_minor)
            except FileNotFoundError:
                _refuse('mounted block backing disappeared', 'WAITING_DESTINATION')
            if final_backing != backing:
                _refuse('block backing changed during observation', 'DESTINATION_CHANGED')
            evidence = DeviceEvidence(device_id, uuid, serial, total, mount.path, mount.mount_id, mount.fs_type,
                                      available, block_size, name_max, device_size, profile)
            self._observed[(target, tree.mount_id)] = {
                'evidence': evidence, 'device': major_minor, 'backing': backing, 'mounts': mounts,
                'mount': mount, 'swaps': swaps, 'writable': writable, 'archives': archives,
                'protected': protected_snapshot}
            return evidence
