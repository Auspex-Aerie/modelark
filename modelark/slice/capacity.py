"""Conservative direct-USB ext4 admission, not a promise of fault-free media.

See docs/usable-slice-direct.md for the supported feature mask and allocation derivation.
Only read-only probes are used; there is no sudo, formatting, or probing by writing files.
"""
from dataclasses import asdict
import array
import fcntl
import os
from pathlib import Path, PurePosixPath
import stat
import struct
import uuid

from . import domain as d
from .transaction import DestinationBinding, TransferPlan, TransferRefusal


POLICY = 'modelark.ext4.direct.v1'
BLOCK = 4096
INDEX = 0x1000


def _refuse(detail, code='DESTINATION_CAPACITY_UNPROVEN'):
    raise TransferRefusal(code, detail)


def parse_superblock(data, expected_uuid):
    if len(data) != 1024:
        _refuse('short ext4 superblock')
    u32 = lambda offset: struct.unpack_from('<I', data, offset)[0]
    u16 = lambda offset: struct.unpack_from('<H', data, offset)[0]
    compat, incompat, ro = u32(0x5c), u32(0x60), u32(0x64)
    observed_uuid = str(uuid.UUID(bytes=bytes(data[0x68:0x78])))
    if (u16(0x38) != 0xef53 or u32(0x4c) != 1 or u32(0x18) != 2 or u32(0x1c) != 2
            or observed_uuid.casefold() != expected_uuid.casefold()
            or compat & ~0x023c or compat & 0x000c != 0x000c
            or incompat & ~0x22c6 or incompat & 0x0042 != 0x0042
            or ro & ~0x047b or ro & 0x000a != 0x000a or ro & 0x410 == 0x410
            or u32(0xe4) != 0 or u32(0xe0) == 0 or u16(0x58) not in {128, 256, 512, 1024}):
        _refuse('volume is outside the supported 4-KiB ext4 feature profile')
    return {'uuid': observed_uuid, 'block_size': BLOCK, 'inode_size': u16(0x58),
            'compat': compat, 'incompat': incompat & ~4, 'ro_compat': ro,
            'journal_inode': u32(0xe0)}


def _volume(tree, evidence):
    device = os.fstat(tree.fd).st_dev
    path = f'/dev/block/{os.major(device)}:{os.minor(device)}'
    try:
        # /dev/block is a kernel-managed alias. Inspect the opened handle BEFORE reading;
        # neither a regular file nor a different block device can supply this evidence.
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISBLK(info.st_mode) or info.st_rdev != device:
                _refuse('superblock descriptor differs from mounted device')
            result = parse_superblock(os.pread(fd, 1024, 1024), evidence.fs_uuid)
        finally:
            os.close(fd)
        matching = [line for line in Path('/proc/self/mountinfo').read_text().splitlines()
                    if line.split()[0] == str(tree.mount_id)]
        if len(matching) != 1:
            _refuse('mounted feature evidence unavailable')
        left, right = matching[0].split(' - ', 1)
        options = left.split()[5].split(',') + right.split()[2].split(',')
        if any('quota' in option or option.startswith('jqfmt=') for option in options):
            _refuse('quota-enabled mounts are outside the supported profile')
        if 'ro' in options:
            _refuse('read-only destination')
        return result
    except (OSError, ValueError, IndexError) as exc:
        _refuse('read-only volume feature proof unavailable (no privilege escalation): ' + str(exc))


def _root_metadata(tree):
    flags = array.array('l', [0])
    try:
        fcntl.ioctl(tree.fd, 0x80086601, flags, True)  # FS_IOC_GETFLAGS on supported 64-bit ABIs
    except OSError as exc:
        _refuse('root inode feature proof unavailable: ' + str(exc))
    return os.fstat(tree.fd).st_size, flags[0]


def _free(tree):
    value = os.fstatvfs(tree.fd)
    free, inodes = value.f_bavail * value.f_frsize, min(value.f_favail, value.f_ffree)
    if free < 0 or inodes < 0:
        _refuse('free block/inode counts unavailable')
    return free, inodes


def _record_size(name):
    try:
        size = len(name.encode('utf-8'))
    except UnicodeError:
        _refuse('non-UTF-8 directory name', 'DESTINATION_LAYOUT_UNSUPPORTED')
    if size > 255:
        _refuse('component exceeds ext4 byte limit', 'DESTINATION_LAYOUT_UNSUPPORTED')
    return (8 + size + 3) // 4 * 4


def layout(file_paths):
    """Bound every new directory's complete live namespace, including dual publication links."""
    directories, names = set(), {}
    for path in file_paths:
        if not d._path(path) or len(path.encode('utf-8')) >= 4096:
            _refuse('invalid or overlong path', 'DESTINATION_LAYOUT_UNSUPPORTED')
        value = PurePosixPath(path)
        for component in value.parts:
            _record_size(component)
        for parent in value.parents:
            if str(parent) != '.':
                directories.add(str(parent))
        names.setdefault(str(value.parent), []).extend([value.name, '.slice-' + '0' * 32])
    for directory in directories:
        value = PurePosixPath(directory)
        names.setdefault(str(value.parent), []).append(value.name)
    for directory in directories:
        if 24 + 12 + sum(_record_size(name) for name in names.get(directory, [])) > BLOCK:
            _refuse('consumer directory fanout exceeds one-block profile: ' + directory,
                    'DESTINATION_LAYOUT_UNSUPPORTED')
    return sorted(directories), names


def _entries(tree):
    result = {}
    for name in sorted(os.listdir(tree.fd)):
        _record_size(name)
        fd = tree.open(name, os.O_PATH | os.O_NOFOLLOW)
        try:
            result[name] = list(tree.identity(fd)[1:])
        finally:
            os.close(fd)
    return result


def _payload_bounds(proposal, evidence, store_root):
    # Use the largest possible reserve's decimal representation. The real reserve cannot
    # lengthen these serialized controls; plan/destination hashes have fixed-length encodings.
    binding = DestinationBinding(evidence.device_id, evidence.fs_uuid, 'direct-v1:' + '0' * 64,
                                 evidence.available_bytes)
    plan = TransferPlan(proposal, binding,
                        metadata_reserve_bytes=evidence.available_bytes - proposal.total_bytes)
    tx = '0' * 32
    files = [{'path': str(PurePosixPath(proposal.spec.destination_root) / a.repo_id / a.rfilename),
              'size': a.size_bytes, 'sha256': a.sha256,
              'source': asdict(max(a.sources, key=lambda source: len(d._json(asdict(source)))))}
             for a in proposal.closure]
    control = d._json({'transaction': tx, 'seal': plan.seal, 'store': str(store_root),
                      'destination': asdict(binding)})
    return len(control), len(d._json(plan.receipt(tx, files)))


def _attachment(tree, evidence):
    if (evidence.mount_id != tree.mount_id or str(evidence.mount_path) != str(tree.path)):
        _refuse('observation belongs to a different retained attachment', 'DESTINATION_CHANGED')


def capture(tree, evidence, proposal, store_root):
    tree.check()
    _attachment(tree, evidence)
    volume = _volume(tree, evidence)
    size, flags = _root_metadata(tree)
    if (evidence.block_size != BLOCK or evidence.name_max < 255 or size != BLOCK
            or flags & INDEX or flags & (0x10 | 0x20 | 0x800 | 0x10000000)):
        _refuse('initial root must be one ordinary nonindexed ext4 directory block')
    root_entries = _entries(tree)
    top = PurePosixPath(proposal.spec.destination_root).parts[0]
    if top in root_entries or '.modelark-slice-owner' in root_entries:
        _refuse('delivery root/control must not exist', 'OUTPUT_COLLISION')
    file_paths = [str(PurePosixPath(proposal.spec.destination_root) / a.repo_id / a.rfilename)
                  for a in proposal.closure]
    file_paths += ['.modelark-slice-owner', str(PurePosixPath(proposal.spec.destination_root)
                                             / '.modelark-slice-receipt.json')]
    directories, names = layout(file_paths)
    control, receipt = _payload_bounds(proposal, evidence, store_root)
    sizes = [a.size_bytes for a in proposal.closure] + [control, receipt]
    if any(size > (2**32 - 1) * BLOCK for size in sizes):
        _refuse('file exceeds conservative ext4 logical block limit', 'DESTINATION_LAYOUT_UNSUPPORTED')
    blocks = sum((size + BLOCK - 1) // BLOCK for size in sizes)
    root_blocks = 6 * (2 + 4 * len(names.get('.', [])))
    required = BLOCK * (6 * blocks + len(file_paths) + 2 * len(directories) + root_blocks)
    free, inodes = _free(tree)
    if free != evidence.available_bytes:
        _refuse('free space changed during preview', 'DESTINATION_CAPACITY_CHANGED')
    if required > free:
        _refuse(f'conservative profile requires {required} bytes; available {free}',
                'DESTINATION_CAPACITY_INSUFFICIENT')
    required_inodes = len(file_paths) + len(directories)
    if required_inodes > inodes:
        _refuse('insufficient free inodes', 'DESTINATION_INODES_INSUFFICIENT')
    return {'policy': POLICY, 'hardware_profile': evidence.profile, 'filesystem': volume,
            'root_identity': list(tree.identity(tree.fd)[1:]), 'root_entries': root_entries,
            'root_flags': flags, 'root_max_bytes': BLOCK * (2 + 4 * len(names.get('.', []))),
            'available_bytes': free, 'free_inodes': inodes, 'required_inodes': required_inodes,
            'reserve_bytes': required - proposal.total_bytes, 'file_paths': sorted(file_paths),
            'directory_paths': directories}


def metadata_reserve_bytes(proposal, binding, caps, store_root):
    if caps.get('policy') != POLICY or type(caps.get('reserve_bytes')) is not int:
        _refuse('unsupported sealed capacity policy')
    return caps['reserve_bytes']


def _require_unique_marker(fd, token):
    from .destination import OWNER_XATTR, owner_marker
    try:
        valid = os.getxattr(fd, OWNER_XATTR) == owner_marker(token)
    except OSError:
        valid = False
    if not valid:
        _refuse('external unique ownership marker required', 'DESTINATION_ALLOCATION_UNPROVEN')


def verify(tree, evidence, proposal, caps, adapter=None, *, refresh_volume=True):
    """Run from DestinationPort.check, never from BoundTree.verify (which would recurse)."""
    tree.check()
    _attachment(tree, evidence)
    if (caps.get('policy') != POLICY or evidence.profile != caps['hardware_profile']
            or list(tree.identity(tree.fd)[1:]) != caps['root_identity']
            or (refresh_volume and _volume(tree, evidence) != caps['filesystem'])):
        _refuse('sealed filesystem/capability evidence changed', 'DESTINATION_CHANGED')
    size, flags = _root_metadata(tree)
    if size > caps['root_max_bytes'] or flags & ~INDEX != caps['root_flags'] & ~INDEX:
        _refuse('root growth/flags outside sealed profile', 'DESTINATION_CHANGED')
    current = _entries(tree)
    for name, identity in caps['root_entries'].items():
        if current.get(name) != identity:
            _refuse('unrelated root entry changed: ' + name, 'OUTPUT_COLLISION')
    for name in current.keys() - caps['root_entries'].keys():
        info = adapter.inspect(name) if adapter else None
        if not info or info.token is None:
            _refuse('unowned root entry: ' + name, 'OUTPUT_COLLISION')
    owned = 0
    if adapter is not None:
        # Count unique inode identities, not hardlink names or creation history.
        identities = set()
        with adapter._connection() as con:
            paths = con.execute('SELECT DISTINCT path FROM certificates WHERE tx=? AND seal=? AND device=?'
                                ' AND filesystem=? AND mount=?', adapter._scope).fetchall()
        for (path,) in paths:
            info = adapter.inspect(path)
            if info is not None:
                if info.token is None:
                    _refuse('certificate no longer authenticates object', 'OUTPUT_COLLISION')
                fd = tree.open(path, os.O_PATH | os.O_NOFOLLOW)
                try:
                    identities.add(tree.identity(fd))
                    # fgetxattr does not accept O_PATH; inspect through a confined ordinary
                    # descriptor, retaining the same authenticated inode identity.
                    marker_fd = tree.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    try:
                        if tree.identity(marker_fd) != tree.identity(fd):
                            _refuse('object replaced during allocation proof', 'OUTPUT_COLLISION')
                        _require_unique_marker(marker_fd, info.token)
                    finally:
                        os.close(marker_fd)
                    if info.kind == 'directory' and os.fstat(fd).st_size > BLOCK:
                        _refuse('consumer directory outgrew sealed profile', 'DESTINATION_CHANGED')
                finally:
                    os.close(fd)
        owned = len(identities)
    free, inodes = _free(tree)
    if inodes + owned != caps['free_inodes']:
        _refuse('unexplained inode consumption', 'DESTINATION_CAPACITY_CHANGED')
    if owned > caps['required_inodes'] or inodes < caps['required_inodes'] - owned:
        _refuse('sealed inode budget exhausted', 'DESTINATION_INODES_INSUFFICIENT')
    if adapter is None and free != caps['available_bytes']:
        _refuse('free space changed before transfer', 'DESTINATION_CAPACITY_CHANGED')
