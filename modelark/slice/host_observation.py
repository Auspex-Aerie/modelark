"""Lossless procfs records and descriptor-backed host-role observations.

Protected roles may deliberately follow host symlinks (e.g. /home -> /data/users).
This is NOT attachment confinement: destination/archive BoundTree roots remain no-symlink.
IO failures stay OSError for the caller's attachment-aware classification boundary.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from .transaction import TransferRefusal


def read_fs_text(path):
    return os.fsdecode(Path(path).read_bytes())


def proc_lines(text):
    """Kernel records use LF, not Unicode's broader notion of a line boundary."""
    return tuple(line for line in text.split('\n') if line)


def proc_fields(line):
    """Mountinfo/swaps fields use ASCII space/tab; filename characters are not separators."""
    return tuple(re.split('[ \t]+', line.strip(' \t'))) if line.strip(' \t') else ()


def decode_proc_path(value):
    raw = os.fsencode(value)
    if re.search(rb'\\(?![0-7]{3})', raw):
        raise TransferRefusal('DESTINATION_UNPROVEN', 'invalid procfs path encoding')
    def decode(match):
        value = int(match[1], 8)
        if value > 255 or value == 0:
            raise TransferRefusal('DESTINATION_UNPROVEN', 'invalid procfs path byte')
        return bytes([value])
    return os.fsdecode(re.sub(rb'\\([0-7]{3})', decode, raw))


@dataclass(frozen=True)
class Mount:
    mount_id: int
    major_minor: str
    root: str
    path: str
    options: frozenset[str]
    fs_type: str
    source: str


def parse_mounts(text):
    result = []
    try:
        for line in proc_lines(text):
            left, right = line.split(' - ', 1)
            fields, filesystem = proc_fields(left), proc_fields(right)
            if len(fields) < 6 or len(filesystem) < 3:
                raise ValueError('short mountinfo row')
            path, root = decode_proc_path(fields[4]), decode_proc_path(fields[3])
            mount_id = int(fields[0])
            # Persistent namespace handles have kernel-generated opaque roots,
            # unlike filesystem directories. Preserve them in the inventory;
            # attachment admission still requires the selected root to be '/'.
            namespace_root = (filesystem[0] == 'nsfs' and re.fullmatch(
                r'(?:mnt|net|uts|ipc|pid|pid_for_children|user|cgroup|time|time_for_children):\[[0-9]+\]', root))
            if (not path.startswith('/') or not (root.startswith('/') or namespace_root) or mount_id <= 0
                    or not re.fullmatch('[0-9]+:[0-9]+', fields[2])):
                raise ValueError('invalid mount identity/path')
            result.append(Mount(mount_id, fields[2], root, path,
                                frozenset(fields[5].split(',') + filesystem[2].split(',')),
                                filesystem[0], decode_proc_path(filesystem[1])))
    except (ValueError, TypeError) as exc:
        raise TransferRefusal('DESTINATION_UNPROVEN', 'invalid mount inventory: ' + str(exc)) from exc
    if not result or len({mount.mount_id for mount in result}) != len(result):
        raise TransferRefusal('DESTINATION_UNPROVEN', 'missing or ambiguous mount inventory')
    return tuple(result)


@dataclass(frozen=True)
class ProtectedTarget:
    requested: str
    path: str
    major_minor: str
    mount_id: int
    inode: int


def _optional_missing(path):
    # A genuinely absent optional /home or /boot is normal. A symlink dangling
    # anywhere in that path is not absence evidence and must still fail closed.
    current = Path('/')
    for component in Path(path).parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError:
            return True
        if stat.S_ISLNK(info.st_mode):
            current = Path(os.path.realpath(current, strict=True))
    return False


def resolve_protected(path, *, directory=True, optional=False):
    """Resolve one host role to its actual filesystem using retained descriptors.

    Return None only for a genuinely missing optional role. Bind the resolved name
    and original request to the same current mount/inode; races cannot silently
    turn lexical prefix classification into physical-storage evidence.
    """
    from .linux import _statx  # Lazy: linux imports this module's procfs helpers.
    requested = os.path.abspath(path)
    try:
        fd = os.open(requested, os.O_PATH | os.O_CLOEXEC)
    except FileNotFoundError:
        if optional and _optional_missing(requested):
            return None
        raise
    try:
        info = os.fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise TransferRefusal('DESTINATION_UNPROVEN', 'protected role has unexpected file type')
        mount_id = _statx(fd).mount_id
        identity = info.st_dev, info.st_ino, mount_id
        resolved = os.path.realpath(requested, strict=True)
        for current in (resolved, requested):
            other = os.open(current, os.O_PATH | os.O_CLOEXEC)
            try:
                observed = os.fstat(other)
                if (observed.st_dev, observed.st_ino, _statx(other).mount_id) != identity:
                    raise TransferRefusal('DESTINATION_UNPROVEN', 'protected role changed while resolving')
            finally:
                os.close(other)
        if os.path.realpath(requested, strict=True) != resolved:
            raise TransferRefusal('DESTINATION_UNPROVEN', 'protected role target changed while resolving')
        return ProtectedTarget(requested, resolved, f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}',
                               mount_id, info.st_ino)
    finally:
        os.close(fd)
