"""Conservative admission math and synthetic superblocks; no actual block device access."""
import struct
import errno
import os
from pathlib import PurePosixPath
from types import SimpleNamespace
import uuid

import pytest

from modelark.slice import domain as d
from modelark.slice.capacity import _volume as PRODUCTION_VOLUME
from modelark.slice.linux import BoundTree
from modelark.slice.transaction import TransferRefusal
from test_slice_domain import facts, spec


def superblock():
    data = bytearray(1024)
    for offset, value in [(0x18, 2), (0x1c, 2), (0x4c, 1), (0x5c, 0x3c),
                          (0x60, 0x2c6), (0x64, 0x46b), (0xe0, 8)]:
        struct.pack_into('<I', data, offset, value)
    struct.pack_into('<H', data, 0x38, 0xef53)
    struct.pack_into('<H', data, 0x58, 256)
    data[0x68:0x78] = uuid.UUID('12345678-1234-1234-1234-123456789abc').bytes
    return data


def test_superblock_accepts_only_documented_profile():
    from modelark.slice import capacity as c
    result = c.parse_superblock(superblock(), '12345678-1234-1234-1234-123456789abc')
    assert result['block_size'] == 4096
    assert result['incompat'] & 4 == 0


@pytest.mark.parametrize('offset,bit', [(0x5c, 0x1000), (0x60, 0x8000), (0x60, 0x4000),
                                      (0x64, 0x100), (0x64, 0x200), (0x64, 0x10)])
def test_unknown_quota_bigalloc_inline_eainode_and_conflicting_checksums_refuse(offset, bit):
    from modelark.slice import capacity as c
    data = superblock()
    struct.pack_into('<I', data, offset, struct.unpack_from('<I', data, offset)[0] | bit)
    with pytest.raises(TransferRefusal, match='DESTINATION_CAPACITY_UNPROVEN'):
        c.parse_superblock(data, '12345678-1234-1234-1234-123456789abc')


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    from modelark.slice import capacity as c
    root = tmp_path / 'destination'
    root.mkdir()
    tree = BoundTree(root)
    evidence = SimpleNamespace(profile='hardware-v1', fs_uuid='fs-a', device_id='usb-destination',
                               available_bytes=1 << 30, block_size=4096, name_max=255,
                               mount_id=tree.mount_id, mount_path=str(tree.path))
    proposal = d.preview(spec(d), facts(d))
    monkeypatch.setattr(c, '_volume', lambda *a: {'block_size': 4096, 'uuid': 'fs-a'})
    monkeypatch.setattr(c, '_root_metadata', lambda *a: (4096, 0))
    monkeypatch.setattr(c, '_free', lambda *a: (evidence.available_bytes, 100000))
    try:
        caps = c.capture(tree, evidence, proposal, tmp_path / 'private')
        yield c, tree, evidence, proposal, caps
    finally:
        tree.close()


def test_capture_seals_capacity_and_inode_budget(admitted):
    c, tree, evidence, proposal, caps = admitted
    assert caps['reserve_bytes'] > proposal.total_bytes
    assert caps['required_inodes'] == len(caps['directory_paths']) + len(caps['file_paths'])
    c.verify(tree, evidence, proposal, caps)


@pytest.mark.parametrize('field,value', [('mount_id', -999), ('mount_path', '/different/mount')])
def test_observation_must_describe_the_retained_attachment(admitted, field, value):
    c, tree, evidence, proposal, caps = admitted
    setattr(evidence, field, value)
    with pytest.raises(TransferRefusal, match='DESTINATION_CHANGED'):
        c.capture(tree, evidence, proposal, '/private')
    with pytest.raises(TransferRefusal, match='DESTINATION_CHANGED'):
        c.verify(tree, evidence, proposal, caps)


def test_durable_root_identity_does_not_seal_transient_device_number(admitted, monkeypatch):
    c, tree, evidence, proposal, caps = admitted
    assert len(caps['root_identity']) == 2
    identity = tree.identity
    monkeypatch.setattr(tree, 'check', lambda: None)
    monkeypatch.setattr(tree, 'identity', lambda fd: (999999, *identity(fd)[1:]))
    c.verify(tree, evidence, proposal, caps)


def test_changed_hardware_or_unowned_root_name_refuses(admitted):
    c, tree, evidence, proposal, caps = admitted
    evidence.profile = 'replacement'
    with pytest.raises(TransferRefusal, match='DESTINATION_CHANGED'):
        c.verify(tree, evidence, proposal, caps)
    evidence.profile = 'hardware-v1'
    (tree.path / 'unknown').touch()
    with pytest.raises(TransferRefusal, match='OUTPUT_COLLISION'):
        c.verify(tree, evidence, proposal, caps)


def test_unknown_root_control_is_not_adopted(admitted):
    c, tree, evidence, proposal, _ = admitted
    (tree.path / '.modelark-slice-owner').touch()
    with pytest.raises(TransferRefusal, match='OUTPUT_COLLISION'):
        c.capture(tree, evidence, proposal, '/private')


def test_inode_exhaustion_refuses_before_approval(admitted, monkeypatch):
    c, tree, evidence, proposal, _ = admitted
    monkeypatch.setattr(c, '_free', lambda *a: (1 << 30, 0))
    with pytest.raises(TransferRefusal, match='DESTINATION_INODES_INSUFFICIENT'):
        c.capture(tree, evidence, proposal, '/private')


def test_unknown_or_indexed_initial_root_refuses(admitted, monkeypatch):
    c, tree, evidence, proposal, _ = admitted
    monkeypatch.setattr(c, '_root_metadata', lambda *a: (4096, 0x1000))
    with pytest.raises(TransferRefusal, match='DESTINATION_CAPACITY_UNPROVEN'):
        c.capture(tree, evidence, proposal, '/private')


def test_large_directory_fanout_is_non_executable():
    from modelark.slice import capacity as c
    files = ['delivery/' + 'a' * 230 + str(i) for i in range(30)]
    with pytest.raises(TransferRefusal, match='DESTINATION_LAYOUT_UNSUPPORTED'):
        c.layout(files)


@pytest.mark.parametrize('change', ['short', 'uuid', 'magic', 'block', 'cluster', 'external-journal', 'no-journal'])
def test_superblock_identity_and_required_layout_rejections(change):
    from modelark.slice import capacity as c
    data = superblock()
    if change == 'short':
        data = data[:-1]
    elif change == 'uuid':
        data[0x68] ^= 1
    elif change == 'magic':
        data[0x38] = 0
    elif change in {'block', 'cluster', 'external-journal', 'no-journal'}:
        offset, value = {'block': (0x18, 1), 'cluster': (0x1c, 3),
                         'external-journal': (0xe4, 1), 'no-journal': (0xe0, 0)}[change]
        struct.pack_into('<I', data, offset, value)
    with pytest.raises(TransferRefusal, match='DESTINATION_CAPACITY_UNPROVEN'):
        c.parse_superblock(data, '12345678-1234-1234-1234-123456789abc')


def test_path_byte_limits_are_not_character_limits():
    from modelark.slice import capacity as c
    with pytest.raises(TransferRefusal, match='DESTINATION_LAYOUT_UNSUPPORTED'):
        c.layout(['delivery/' + '\u00e9' * 128])


@pytest.mark.parametrize('path', ['delivery-\udcff/file', 'delivery/\udcff', 'delivery/\ud800'])
def test_non_utf8_full_paths_refuse_before_encoding(path):
    from modelark.slice import capacity as c
    with pytest.raises(TransferRefusal, match='DESTINATION_LAYOUT_UNSUPPORTED'):
        c.layout([path])


def _parent_path_bytes(length):
    """Produce an exact-length ASCII parent with individually valid ext4 components."""
    parts = []
    while length > 255:
        parts.append('a' * 255)
        length -= 256
    parts.append('b' * length)
    return '/'.join(parts)


@pytest.mark.parametrize('name', ['x', '.modelark-slice-receipt.json'])
@pytest.mark.parametrize('unicode_parent', [False, True])
def test_short_final_basename_does_not_hide_overlong_generated_temporary_path(name, unicode_parent):
    from modelark.slice import capacity as c
    parent = _parent_path_bytes(4056)
    if unicode_parent:
        parent = '\u00e9' * 127 + 'a' + parent[255:]
    final = parent + '/' + name
    temporary = str(PurePosixPath(final).parent / ('.slice-' + 'a' * 32))
    assert len(final.encode('utf-8')) < 4096
    assert len(temporary.encode('utf-8')) == 4096
    with pytest.raises(TransferRefusal, match='DESTINATION_LAYOUT_UNSUPPORTED'):
        c.layout([final])


def test_generated_temporary_path_accepts_last_valid_byte_length():
    from modelark.slice import capacity as c
    parent = _parent_path_bytes(4055)
    final = parent + '/x'
    temporary = str(PurePosixPath(final).parent / ('.slice-' + 'a' * 32))
    assert len(temporary.encode('utf-8')) == 4095
    directories, _ = c.layout([final])
    assert parent in directories


def test_capture_checks_control_and_receipt_temporary_names_too(admitted, monkeypatch):
    c, tree, evidence, proposal, _ = admitted
    checked = []
    layout = c.layout
    def observe(paths):
        checked.extend(paths)
        return layout(paths)
    monkeypatch.setattr(c, 'layout', observe)
    c.capture(tree, evidence, proposal, '/private')
    assert '.modelark-slice-owner' in checked
    assert str(PurePosixPath(proposal.spec.destination_root) / '.modelark-slice-receipt.json') in checked
    assert all(len(str(PurePosixPath(path).parent / ('.slice-' + 'a' * 32)).encode('utf-8')) < 4096
               for path in checked)


def test_inherited_default_acl_on_legacy_owned_directory_refuses_even_after_root_acl_removed(
        admitted, tmp_path, monkeypatch):
    from modelark.slice.destination import OWNER_XATTR, UsbDestination, owner_marker
    from modelark.slice.transaction import DestinationBinding
    c, tree, evidence, proposal, caps = admitted
    private = tmp_path / 'private-state'
    private.mkdir(mode=0o700)
    binding = DestinationBinding(evidence.device_id, evidence.fs_uuid, evidence.profile, evidence.available_bytes)
    store = SimpleNamespace(root=private, load=lambda tx: SimpleNamespace(destination=binding, seal='a' * 64))
    adapter = UsbDestination(tree, binding, store, 'b' * 32)
    # Linux POSIX ACL xattr v2: owner, owning group and other default entries.
    acl = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, permissions, 0xffffffff)
                                         for tag, permissions in [(1, 7), (4, 5), (32, 5)])
    os.setxattr(tree.fd, 'system.posix_acl_default', acl)
    # Reproduce a trusted pre-fix directory/certificate; do not run today's create
    # preconditions because they now reject the root default ACL before creation.
    os.mkdir('models', dir_fd=tree.fd)
    child = tree.open('models', os.O_RDONLY | os.O_DIRECTORY)
    try:
        token = '1' * 32
        os.setxattr(child, OWNER_XATTR, owner_marker(token))
        os.fsync(child)
        adapter._certify(child, 'models', token, 'directory')
        inherited = os.getxattr(child, 'system.posix_acl_default')
        assert inherited == acl
        os.removexattr(tree.fd, 'system.posix_acl_default')
        with pytest.raises(OSError) as missing:
            os.getxattr(tree.fd, 'system.posix_acl_default')
        assert missing.value.errno == errno.ENODATA
        monkeypatch.setattr(c, '_free', lambda current: (evidence.available_bytes, caps['free_inodes'] - 1))
        before = adapter._database.read_bytes()
        with pytest.raises(TransferRefusal, match='DESTINATION_CAPACITY_UNPROVEN'):
            c.verify(tree, evidence, proposal, caps, adapter)
        assert os.getxattr(child, 'system.posix_acl_default') == inherited
        assert adapter._database.read_bytes() == before
        assert os.listdir(child) == []
    finally:
        os.close(child)


def test_block_descriptor_is_verified_before_any_read(admitted, monkeypatch):
    import os
    _, tree, evidence, _, _ = admitted
    monkeypatch.setattr(os, 'pread', lambda *a: pytest.fail('must not read a regular file as a device'))
    # Production probe uses a separate descriptor, represented by this ordinary disposable file.
    path = tree.path / 'not-device'
    path.touch()
    fd = os.open(path, os.O_RDONLY)
    monkeypatch.setattr(os, 'open', lambda *a, **k: os.dup(fd))
    try:
        with pytest.raises(TransferRefusal, match='superblock descriptor differs'):
            PRODUCTION_VOLUME(tree, evidence)
    finally:
        os.close(fd)


@pytest.mark.parametrize('bit', [2, 8])
def test_large_file_features_must_already_be_enabled(bit):
    from modelark.slice import capacity as c
    data = superblock()
    struct.pack_into('<I', data, 0x64, struct.unpack_from('<I', data, 0x64)[0] & ~bit)
    with pytest.raises(TransferRefusal, match='DESTINATION_CAPACITY_UNPROVEN'):
        c.parse_superblock(data, '12345678-1234-1234-1234-123456789abc')


def test_legacy_inline_marker_cannot_supply_external_allocation_proof(tmp_path):
    import os
    from modelark.slice import capacity as c
    from modelark.slice.destination import OWNER_XATTR, owner_marker
    path = tmp_path / 'file'
    path.touch()
    fd = os.open(path, os.O_RDONLY)
    try:
        os.setxattr(fd, OWNER_XATTR, b'a' * 32)
        with pytest.raises(TransferRefusal, match='DESTINATION_ALLOCATION_UNPROVEN'):
            c._require_unique_marker(fd, 'a' * 32)
        os.setxattr(fd, OWNER_XATTR, owner_marker('a' * 32))
        c._require_unique_marker(fd, 'a' * 32)
    finally:
        os.close(fd)
