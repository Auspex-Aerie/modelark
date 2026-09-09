"""Native ext4 folder admission; read-only evidence, never execution authority.

The writer must exclusively create a 0700 root, authenticate any resumed child,
and retain/recheck the observed parent. No probe creates files, reads raw block
devices, or changes permissions. Non-owner-writable parents need sticky protection;
root/current-user ownership prevents another directory owner removing the child.
Ordinary default ACLs are allowed only when their owner entry preserves rwx:
the required 0700 creation mode masks all inherited non-owner permissions.
"""
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess

from .folder_contract import CapacityObservation, FolderProfile, FolderTarget
from .hardware import _Inventory, _backing_identity, _inventory, _nonempty
from .host_observation import parse_mounts, read_fs_text, resolve_protected
from .io_errors import classify_io, probe_io
from .linux import BoundTree, require_create_access
from .paths import canonical_attachment
from .transaction import TransferRefusal


_OS_SUBTREES = ('/boot', '/usr', '/etc', '/var', '/bin', '/sbin', '/lib', '/lib64',
                '/dev', '/proc', '/sys', '/run', '/root')


def _refuse(detail, code='DESTINATION_UNPROVEN'):
    raise TransferRefusal(code, detail)


def _within(path, parent):
    return path == parent or path.startswith(parent.rstrip('/') + '/')


@dataclass(frozen=True)
class NativeFolderEvidence:
    target: FolderTarget
    parent_path: str
    destination_path: str
    filesystem_id: str
    backing_ids: tuple[str, ...]
    mount_id: int
    major_minor: str
    capacity: CapacityObservation
    name_max: int
    block_size: int


def _stable(value):
    return (value.target, value.parent_path, value.destination_path, value.filesystem_id,
            value.backing_ids, value.mount_id, value.major_minor, value.name_max, value.block_size)


@probe_io('FILESYSTEM_UNSUPPORTED', 'native directory flags unavailable')
def _directory_flags(fd):
    # FS_IOC_GETFLAGS uses long in its ioctl number; supported Linux ABIs are 64-bit.
    return struct.unpack('=L', fcntl.ioctl(fd, 0x80086601, bytes(8))[:4])[0]


@probe_io('DESTINATION_NOT_WRITABLE', 'native parent permissions or xattrs unavailable')
def _permissions(fd):
    require_create_access(fd)
    info = os.fstat(fd)
    if info.st_uid not in {0, os.geteuid()}:
        _refuse('parent must be owned by the current user or root', 'DESTINATION_NOT_WRITABLE')
    if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
        _refuse('non-owner-writable parent requires sticky protection', 'DESTINATION_NOT_WRITABLE')
    try:
        os.getxattr(fd, 'user.modelark.slice-capability-probe')
    except OSError as exc:
        if exc.errno != errno.ENODATA:
            raise
    try:
        acl = os.getxattr(fd, 'system.posix_acl_default')
    except OSError as exc:
        if exc.errno == errno.ENODATA:
            return
        raise
    if len(acl) < 4 or (len(acl) - 4) % 8 or struct.unpack('<I', acl[:4])[0] != 2:
        _refuse('unrecognized inherited ACL', 'DESTINATION_NOT_WRITABLE')
    entries = tuple(struct.iter_unpack('<HHI', acl[4:]))
    owners = [permissions for tag, permissions, _ in entries if tag == 1]
    if owners != [7] or any(permissions & ~7 for _, permissions, _ in entries):
        _refuse('inherited ACL must preserve owner rwx for a 0700 output root',
                'DESTINATION_NOT_WRITABLE')


@probe_io('DESTINATION_CAPACITY_UNPROVEN', 'shared-space observation unavailable')
def _capacity(fd):
    value = os.fstatvfs(fd)
    if (value.f_frsize <= 0 or value.f_bavail < 0 or value.f_bavail > value.f_blocks
            or min(value.f_favail, value.f_ffree) < 0):
        _refuse('invalid shared-space observation', 'DESTINATION_CAPACITY_UNPROVEN')
    return CapacityObservation(value.f_bavail * value.f_frsize,
                               min(value.f_favail, value.f_ffree)), value.f_frsize


def check_directory(fd, *, flags=None):
    """Read-only flag/permission admission for retained parent or owned directories."""
    # fscrypt, casefold, verity, immutable, append-only and inline-data directory
    # semantics are not qualified. Ordinary indexed/multiblock directories are.
    if (flags or _directory_flags)(fd) & (0x800 | 0x40000000 | 0x100000 | 0x10 | 0x20 | 0x10000000):
        _refuse('unqualified native directory flags', 'FILESYSTEM_UNSUPPORTED')
    _permissions(fd)


class NativeFolderObserver:
    """Strict ext4 folder observations with injectable read-only host providers.

    One unambiguous mount and a single-parent block chain are required. Bind mount
    aliases and multi-parent mappings fail closed; ordinary single-parent LVM/crypt
    chains do not require raw-device access. System backing is permitted, OS-managed
    subtrees are not. Callers MUST supply catalog/runtime/private paths in addition
    to the standard private state paths below. Missing paths remain lexically protected.
    """

    def __init__(self, *, inventory=None, mounts=None, tree_factory=BoundTree,
                 backing=None, resolver=None, flags=None):
        self._inventory = inventory or _inventory
        self._mounts = mounts or (lambda: read_fs_text('/proc/self/mountinfo'))
        self._tree_factory = tree_factory
        self._backing = backing or _backing_identity
        self._resolver = resolver or resolve_protected
        self._flags = flags or _directory_flags
        self._observed = {}

    def _protected(self, destination, mounts, extra):
        private = (Path.home() / '.local/state/modelark', Path.home() / '.local/share/modelark',
                   Path.home() / '.config/modelark', Path.home() / '.cache/modelark')
        result = []
        for role in (*_OS_SUBTREES, *private, *extra):
            requested = str(canonical_attachment(role))
            if requested == '/':
                _refuse('root cannot be supplied as an unrestricted protected role')
            # Protect exact file roles and whole directory roles, not the parent of
            # a catalog file: ~/catalog.sqlite must not ban every ~/exports child.
            try:
                directory = not Path(requested).is_file()
                target = self._resolver(requested, directory=directory, optional=True)
            except FileNotFoundError:
                # resolve_protected distinguishes genuinely missing from dangling links.
                raise
            paths = [requested]
            if target is not None:
                matching = [m for m in mounts if m.mount_id == target.mount_id]
                if (len(matching) != 1 or matching[0].major_minor != target.major_minor
                        or not _within(target.path, matching[0].path)):
                    _refuse('protected role differs from mount inventory')
                paths.append(target.path)
            if any(_within(destination, path) or _within(path, destination) for path in paths):
                _refuse('output intersects a protected OS, catalog, runtime or private subtree')
            result.append((directory, requested, target))
        return tuple(result)

    def _roles(self, inventory, key, archives):
        disk_key = inventory.disk(key)
        disk = inventory.nodes[disk_key]
        serial, wwn = _nonempty(disk.get('serial')), _nonempty(disk.get('wwn'))
        if not (serial or wwn):
            _refuse('stable backing disk identity required')
        # Refuse unknown mappings, cycles and read-only members. _Inventory already
        # refuses ambiguous duplicate identities and multiple-parent lsblk trees.
        current = key
        while True:
            node = inventory.nodes[current]
            if node.get('type') not in {'disk', 'part', 'crypt', 'lvm'}:
                _refuse('unqualified block mapping')
            if node.get('ro') not in (False, 0, '0'):
                _refuse('read-only or unknown backing member', 'DESTINATION_NOT_WRITABLE')
            if current == disk_key:
                break
            current = inventory.parents[current]
        for archive in archives:
            uuid = _nonempty(getattr(archive, 'fs_uuid', None))
            archive_serial = _nonempty(getattr(archive, 'serial', None))
            if not uuid and not archive_serial:
                _refuse('registered archive lacks comparable backing identity')
            if archive_serial and archive_serial in {serial, wwn}:
                _refuse('registered archive backing disk is excluded')
            for candidate, value in inventory.nodes.items():
                candidate_uuid = _nonempty(value.get('uuid'))
                if uuid and candidate_uuid and uuid.casefold() == candidate_uuid.casefold():
                    if inventory.disk(candidate) == disk_key:
                        _refuse('registered archive filesystem or sibling backing disk is excluded')
        return tuple(sorted(key for key in (('wwn:' + wwn) if wwn else None,
                                            ('serial:' + serial) if serial else None) if key))

    def _read(self, tree, destination, inventory, mounts, archives, protected_paths):
        tree.check()
        info = os.fstat(tree.fd)
        key = f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}'
        covering = [m for m in mounts if _within(str(tree.path), m.path)]
        if not covering:
            _refuse('parent has no covering mount')
        longest = max(len(m.path) for m in covering)
        matching = [m for m in covering if len(m.path) == longest]
        if len(matching) != 1:
            _refuse('parent has ambiguous covering mounts')
        mount = matching[0]
        if mount.mount_id != tree.mount_id or mount.major_minor != key:
            _refuse('parent descriptor differs from mount inventory', 'DESTINATION_CHANGED')
        if mount.root != '/' or sum(m.major_minor == key for m in mounts) != 1:
            _refuse('filesystem mount aliases are not qualified')
        if any(_within(m.path, destination) for m in mounts if m != mount):
            _refuse('nested output mount is not qualified')
        if key not in inventory.nodes:
            _refuse('mounted block backing unavailable', 'WAITING_DESTINATION')
        node = inventory.nodes[key]
        if mount.fs_type != 'ext4' or node.get('fstype') != 'ext4':
            _refuse('native folder profile requires ext4', 'FILESYSTEM_UNSUPPORTED')
        if 'rw' not in mount.options or {'ro', 'nouser_xattr'} & mount.options:
            _refuse('writable user-xattr mount required', 'DESTINATION_NOT_WRITABLE')
        uuid = _nonempty(node.get('uuid'))
        if not uuid:
            _refuse('stable filesystem identity required')
        backing_ids = self._roles(inventory, key, archives)
        protected = self._protected(destination, mounts, protected_paths)
        check_directory(tree.fd, flags=self._flags)
        capacity, block_size = _capacity(tree.fd)
        name_max = os.fpathconf(tree.fd, 'PC_NAME_MAX')
        if name_max <= 0 or len(os.fsencode(Path(destination).name)) > name_max:
            _refuse('output name exceeds filesystem capability', 'DESTINATION_LAYOUT_UNSUPPORTED')
        filesystem_scope = 'native-fs-v1:' + hashlib.sha256(json.dumps(
            {'uuid': uuid.casefold(), 'backing': backing_ids}, sort_keys=True).encode()).hexdigest()
        parts = Path(tree.path).relative_to(mount.path).parts
        identity = tree.identity(tree.fd)
        target = FolderTarget(FolderProfile.NATIVE_EXT4, filesystem_scope, parts,
                              identity[1:], Path(destination).name)
        # One canonical representation must cross observation/admission/state; do
        # not append aliases after sorting only the physical-disk subset.
        backing_ids = tuple(sorted(set((*backing_ids, 'filesystem:' + uuid, filesystem_scope))))
        evidence = NativeFolderEvidence(target, str(tree.path), destination, uuid,
                                        backing_ids, tree.mount_id, key,
                                        capacity, name_max, block_size)
        tree.check()
        return evidence, protected

    def observe(self, destination_path, *, archives, protected_paths=(), allow_existing=False):
        """Observe only. allow_existing is NOT ownership proof or resume permission."""
        destination = canonical_attachment(destination_path)
        archives, protected_paths = tuple(archives), tuple(protected_paths)
        if destination == Path('/'):
            _refuse('output must be a child of an existing parent', 'PATH_UNSAFE')
        try:
            payload = json.dumps(self._inventory(), sort_keys=True)
            inventory = _Inventory(json.loads(payload))
            mounts = parse_mounts(self._mounts())
            with self._tree_factory(destination.parent) as tree:
                value, protected = self._read(tree, str(destination), inventory, mounts,
                                              tuple(archives), tuple(protected_paths))
                if not allow_existing:
                    try:
                        os.stat(destination.name, dir_fd=tree.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        _refuse('output already exists; exclusive creation required', 'DESTINATION_COLLISION')
                backing = self._backing(value.major_minor)
                if (json.dumps(self._inventory(), sort_keys=True) != payload
                        or parse_mounts(self._mounts()) != mounts
                        or self._protected(str(destination), mounts, protected_paths) != protected
                        or self._backing(value.major_minor) != backing):
                    _refuse('folder observation changed during admission', 'DESTINATION_CHANGED')
                tree.check()
                self._observed[value.target.target_id] = (value, inventory, mounts, backing,
                                                          tuple(archives), tuple(protected_paths))
                return value
        except TransferRefusal:
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            if isinstance(exc, OSError):
                raise classify_io(exc, None, TransferRefusal('DESTINATION_UNPROVEN', str(exc))) from exc
            raise TransferRefusal('DESTINATION_UNPROVEN', str(exc)) from exc

    def recheck(self, tree, evidence, *, archives=None):
        """Revalidate retained parent, permitting an owned child; capacity is advisory."""
        proof = self._observed.get(evidence.target.target_id)
        if proof is None or _stable(proof[0]) != _stable(evidence) or str(tree.path) != evidence.parent_path:
            _refuse('no matching parent observation', 'DESTINATION_CHANGED')
        original, inventory, mounts, backing, prior_archives, protected_paths = proof
        try:
            tree.check()
            try:
                current_backing = self._backing(evidence.major_minor)
            except FileNotFoundError:
                _refuse('observed backing disappeared', 'WAITING_DESTINATION')
            if current_backing != backing:
                _refuse('observed backing changed', 'DESTINATION_CHANGED')
            roles = prior_archives if archives is None else tuple(archives)
            current_mounts = parse_mounts(self._mounts())
            current = evidence.major_minor
            mapped = False
            while current is not None:
                mapped |= inventory.nodes[current].get('type') in {'crypt', 'lvm'}
                current = inventory.parents[current]
            # A dm table reload need not change mountinfo or the dm sysfs inode.
            # Re-observe mapped ancestry until a cheaper retained topology proof is
            # qualified; a stale disk-role cache cannot authorize archive writes.
            if current_mounts != mounts or roles != prior_archives or mapped:
                value = self.observe(evidence.destination_path, archives=roles,
                                     protected_paths=protected_paths, allow_existing=True)
            else:
                value, _ = self._read(tree, evidence.destination_path, inventory, mounts, roles, protected_paths)
            if _stable(value) != _stable(original):
                _refuse('folder binding changed', 'DESTINATION_CHANGED')
            tree.check()
            return value
        except OSError as exc:
            raise classify_io(exc, tree.check, TransferRefusal('DESTINATION_UNPROVEN', str(exc))) from exc
