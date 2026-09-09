"""Read-only FAT32 admission; observations alone confer no writer authority.

FSVER is lsblk/udev evidence corroborated by the kernel's vfat mount, not raw
on-media forensic proof. The disposable kernel-driver test is not USB admission.
Durable intent contains path/backing only. Only a retained Fat32Tree holds live
parent identity; an inspection inode or saved observation cannot authorize resume.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .fat32_layout import _name
from .folder_contract import CapacityObservation
from .folder_observation import NativeFolderObserver, _within
from .hardware import _Inventory, _nonempty
from .host_observation import parse_mounts
from .io_errors import classify_io, probe_io
from .linux import BoundTree, _openat2, _statx, require_create_access
from .paths import canonical_attachment
from .transaction import TransferRefusal


def _refuse(detail, code='DESTINATION_UNPROVEN'):
    raise TransferRefusal(code, detail)


class Fat32Tree(BoundTree):
    """Confinement for one retained live descriptor, never durable FAT ownership."""

    @staticmethod
    def _statx(fd):
        return _statx(fd, require_birth=False)

    @staticmethod
    def identity(fd):
        value = _statx(fd, require_birth=False)
        return os.makedev(value.dev_major, value.dev_minor), value.ino


def _inventory():
    result = subprocess.run(
        ['lsblk', '--json', '--bytes', '--paths', '--output',
         'NAME,KNAME,PATH,TYPE,PKNAME,MAJ:MIN,SIZE,FSTYPE,FSVER,UUID,SERIAL,WWN,TRAN,RO'],
        check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


@dataclass(frozen=True)
class Fat32FolderEvidence:
    parent_path: str
    destination_path: str
    filesystem_id: str
    filesystem_scope: str
    parent_parts: tuple[str, ...]
    child_name: str
    backing_ids: tuple[str, ...]
    mount_id: int
    major_minor: str
    capacity: CapacityObservation
    name_max: int
    block_size: int


def _stable(evidence):
    return (evidence.parent_path, evidence.destination_path, evidence.filesystem_id,
            evidence.filesystem_scope, evidence.parent_parts, evidence.child_name,
            evidence.backing_ids, evidence.mount_id, evidence.major_minor,
            evidence.name_max, evidence.block_size)


def _mount_policy(mount):
    """Closed ASCII/numbered-shortname candidate, no mount-option modification."""
    plain = {'rw', 'nosuid', 'nodev', 'noexec', 'relatime', 'noatime', 'nodiratime',
             'sync', 'dirsync', 'flush', 'utf8'}
    values = {}
    for option in mount.options:
        if option in plain:
            continue
        key, separator, value = option.partition('=')
        if not separator or key in values:
            _refuse('unqualified FAT mount option: ' + option, 'FILESYSTEM_UNSUPPORTED')
        values[key] = value
    allowed = {'uid', 'gid', 'fmask', 'dmask', 'codepage', 'iocharset', 'shortname', 'errors'}
    if set(values) - allowed:
        _refuse('unqualified FAT name or permission options', 'FILESYSTEM_UNSUPPORTED')
    # Admit only the name-codec combination exercised by the kernel-vfat
    # qualification; ASCII-only output does not qualify other mount codecs.
    if (values.get('codepage') != '437' or values.get('iocharset') != 'iso8859-1'
            or 'utf8' not in mount.options
            or values.get('shortname') != 'mixed' or values.get('errors') not in {None, 'remount-ro'}):
        _refuse('qualified codepage=437, iocharset=iso8859-1, utf8 and shortname=mixed required',
                'FILESYSTEM_UNSUPPORTED')
    if values.get('uid') != str(os.geteuid()):
        _refuse('FAT mount must assign ownership to the current user', 'DESTINATION_NOT_WRITABLE')
    for key in ('dmask', 'fmask'):
        value = values.get(key, '')
        if not value or len(value) > 4 or any(character not in '01234567' for character in value):
            _refuse('explicit FAT directory/file permission masks required', 'DESTINATION_NOT_WRITABLE')
        if int(value, 8) & 0o022 != 0o022:
            _refuse('FAT masks must exclude group/other writes', 'DESTINATION_NOT_WRITABLE')
        required = 0o700 if key == 'dmask' else 0o600
        if int(value, 8) & required:
            _refuse('FAT masks must preserve owner directory rwx and file rw',
                    'DESTINATION_NOT_WRITABLE')
    if 'rw' not in mount.options:
        _refuse('writable FAT mount required', 'DESTINATION_NOT_WRITABLE')


@probe_io('DESTINATION_NOT_WRITABLE', 'FAT effective parent permissions unavailable')
def check_directory(fd):
    """FAT mode arguments do not enforce per-root protection; check mount semantics."""
    require_create_access(fd)
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        _refuse('FAT parent must be current-user-owned without nonowner write',
                'DESTINATION_NOT_WRITABLE')


@probe_io('DESTINATION_CAPACITY_UNPROVEN', 'FAT shared capacity unavailable')
def _capacity(fd):
    value = os.fstatvfs(fd)
    if value.f_frsize <= 0 or value.f_bavail < 0 or value.f_bavail > value.f_blocks:
        _refuse('invalid FAT shared-space observation', 'DESTINATION_CAPACITY_UNPROVEN')
    # vfat does not expose a meaningful reservable inode count.
    return CapacityObservation(value.f_bavail * value.f_frsize, None), value.f_frsize


class Fat32FolderObserver(NativeFolderObserver):
    """Read-only, exact spelling/direct USB candidate; does not adopt output roots."""

    def __init__(self, *, inventory=None, mounts=None, tree_factory=Fat32Tree, backing=None, resolver=None):
        super().__init__(inventory=inventory or _inventory, mounts=mounts, tree_factory=tree_factory,
                         backing=backing, resolver=resolver)

    def _canonical_parent(self, tree, mount):
        parts = Path(tree.path).relative_to(mount.path).parts
        with self._tree_factory(mount.path) as root:
            fd = os.dup(root.fd)
            try:
                for part in parts:
                    _name(part)
                    names = os.listdir(fd)
                    if [name for name in names if name.lower() == part.lower()] != [part]:
                        _refuse('FAT parent spelling is an alias or ambiguous case', 'PATH_UNSAFE')
                    child = _openat2(fd, part, os.O_RDONLY | os.O_DIRECTORY)
                    os.close(fd)
                    fd = child
                if (Fat32Tree.identity(fd) != Fat32Tree.identity(tree.fd)
                        or Fat32Tree._statx(fd).mount_id != tree.mount_id):
                    _refuse('FAT canonical parent changed', 'DESTINATION_CHANGED')
                root.check()
            finally:
                os.close(fd)
        return parts

    def _read_fat(self, tree, destination, inventory, mounts, archives, protected_paths):
        tree.check()
        info = os.fstat(tree.fd)
        key = f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}'
        covering = [mount for mount in mounts if _within(str(tree.path), mount.path)]
        if not covering:
            _refuse('FAT parent has no covering mount')
        longest = max(len(mount.path) for mount in covering)
        matching = [mount for mount in covering if len(mount.path) == longest]
        if len(matching) != 1:
            _refuse('FAT parent has ambiguous covering mounts')
        mount = matching[0]
        if mount.mount_id != tree.mount_id or mount.major_minor != key:
            _refuse('FAT parent descriptor differs from mount inventory', 'DESTINATION_CHANGED')
        if mount.root != '/' or sum(m.major_minor == key for m in mounts) != 1:
            _refuse('FAT mount aliases are not qualified')
        if any(_within(m.path, destination) for m in mounts if m != mount):
            _refuse('nested output mount is not qualified')
        if key not in inventory.nodes:
            _refuse('FAT backing is unavailable', 'WAITING_DESTINATION')
        node = inventory.nodes[key]
        if mount.fs_type != 'vfat' or node.get('fstype') != 'vfat' or node.get('fsver') != 'FAT32':
            _refuse('kernel vfat plus explicit FAT32 version evidence required', 'FILESYSTEM_UNSUPPORTED')
        disk = inventory.nodes[inventory.disk(key, direct=True)]
        if disk.get('tran') != 'usb':
            _refuse('FAT candidate requires a direct USB disk', 'FILESYSTEM_UNSUPPORTED')
        backing_ids = self._roles(inventory, key, archives)
        uuid = _nonempty(node.get('uuid'))
        if not uuid:
            _refuse('stable FAT filesystem identity required')
        _mount_policy(mount)
        check_directory(tree.fd)
        protected = self._protected(destination, mounts, protected_paths)
        parts = self._canonical_parent(tree, mount)
        capacity, block_size = _capacity(tree.fd)
        name_max = os.fpathconf(tree.fd, 'PC_NAME_MAX')
        if name_max < 1 or len(Path(destination).name) > name_max:
            _refuse('FAT child exceeds mounted name limit', 'DESTINATION_LAYOUT_UNSUPPORTED')
        scope = 'fat32-fs-v1:' + hashlib.sha256(json.dumps(
            {'uuid': uuid.casefold(), 'backing': backing_ids}, sort_keys=True).encode()).hexdigest()
        backing_ids = tuple(sorted(set((*backing_ids, 'filesystem:' + uuid, scope))))
        value = Fat32FolderEvidence(str(tree.path), destination, uuid, scope, parts,
                                     Path(destination).name, backing_ids, tree.mount_id, key,
                                     capacity, name_max, block_size)
        tree.check()
        return value, protected

    def observe(self, destination_path, *, archives, protected_paths=(), allow_existing=False):
        """Observe intent only; an existing child never becomes authorized by this API."""
        destination = canonical_attachment(destination_path)
        if destination == Path('/'):
            _refuse('FAT output must name a new child', 'PATH_UNSAFE')
        for component in destination.parts[1:]:
            _name(component)
        if len(os.fsencode(destination)) >= 4096:
            _refuse('FAT output path exceeds qualified bound', 'DESTINATION_LAYOUT_UNSUPPORTED')
        archives, protected_paths = tuple(archives), tuple(protected_paths)
        try:
            payload = json.dumps(self._inventory(), sort_keys=True)
            inventory = _Inventory(json.loads(payload))
            mounts = parse_mounts(self._mounts())
            with self._tree_factory(destination.parent) as tree:
                value, protected = self._read_fat(tree, str(destination), inventory, mounts,
                                                  archives, protected_paths)
                if not allow_existing:
                    if any(name.lower() == destination.name.lower() for name in os.listdir(tree.fd)):
                        _refuse('FAT output already exists or is case-equivalent', 'DESTINATION_COLLISION')
                    try:
                        os.stat(destination.name, dir_fd=tree.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        _refuse('FAT output already exists or is an alias', 'DESTINATION_COLLISION')
                backing = self._backing(value.major_minor)
                if (json.dumps(self._inventory(), sort_keys=True) != payload
                        or parse_mounts(self._mounts()) != mounts
                        or self._protected(str(destination), mounts, protected_paths) != protected
                        or self._backing(value.major_minor) != backing):
                    _refuse('FAT observation changed during admission', 'DESTINATION_CHANGED')
                tree.check()
                self._observed[value.destination_path] = (value, inventory, mounts, backing,
                                                          archives, protected_paths)
                return value
        except TransferRefusal:
            raise
        except OSError as exc:
            raise classify_io(exc, None, TransferRefusal('DESTINATION_UNPROVEN', str(exc))) from exc
        except (subprocess.SubprocessError, ValueError) as exc:
            raise TransferRefusal('DESTINATION_UNPROVEN', str(exc)) from exc

    def recheck(self, tree, evidence, *, archives=None):
        """Check one live parent and stable intent; not persistent inode authentication."""
        proof = self._observed.get(evidence.destination_path)
        if (not isinstance(tree, Fat32Tree) or proof is None or _stable(proof[0]) != _stable(evidence)
                or str(tree.path) != evidence.parent_path):
            _refuse('no matching FAT candidate observation/live tree', 'DESTINATION_CHANGED')
        original, inventory, mounts, backing, prior_archives, protected_paths = proof
        try:
            tree.check()
            try:
                current_backing = self._backing(evidence.major_minor)
            except FileNotFoundError:
                _refuse('FAT backing disappeared', 'WAITING_DESTINATION')
            if current_backing != backing:
                _refuse('FAT backing changed', 'DESTINATION_CHANGED')
            roles = prior_archives if archives is None else tuple(archives)
            current_mounts = parse_mounts(self._mounts())
            if current_mounts != mounts or roles != prior_archives:
                value = self.observe(evidence.destination_path, archives=roles,
                                     protected_paths=protected_paths, allow_existing=True)
            else:
                value, _ = self._read_fat(tree, evidence.destination_path, inventory, mounts,
                                         roles, protected_paths)
            if _stable(value) != _stable(original):
                _refuse('FAT path or backing intent changed', 'DESTINATION_CHANGED')
            tree.check()
            return value
        except OSError as exc:
            raise classify_io(exc, tree.check, TransferRefusal('DESTINATION_UNPROVEN', str(exc))) from exc
