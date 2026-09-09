"""Synthetic native admission only: these tests do not qualify real ext4 writes."""
import copy
import errno
import os
import struct
from types import SimpleNamespace

import pytest

from modelark.slice import folder_observation as module
from modelark.slice.linux import BoundTree
from modelark.slice.transaction import TransferRefusal


@pytest.fixture
def native(tmp_path, monkeypatch):
    parent = tmp_path / 'exports'
    parent.mkdir(mode=0o700)
    with BoundTree(parent) as tree:
        info = os.fstat(tree.fd)
        key = f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}'
        mount_id = tree.mount_id
    leaf = {'path': '/dev/testdisk1', 'type': 'part', 'maj:min': key,
            'fstype': 'ext4', 'uuid': 'NATIVE-FS', 'ro': False}
    disk = {'path': '/dev/testdisk', 'type': 'disk', 'maj:min': '239:0',
            'serial': 'HOST-SERIAL', 'wwn': 'HOST-WWN', 'ro': False,
            'children': [leaf]}
    state = {'payload': {'blockdevices': [disk]},
             'mounts': f'{mount_id} 0 {key} / / rw - ext4 /dev/testdisk1 rw\n',
             'backing': (17, 24), 'flags': 0, 'roles': {}, 'acl': None}
    def resolve(path, **kwargs):
        target = state['roles'].get(str(path), str(path))
        if target is None:
            return None
        return module.resolve_protected_result(str(path), target, key, mount_id, 1)
    # Keep role resolution descriptor metadata synthetic too; no actual host paths
    # or user's state files need be opened by this fixture.
    from modelark.slice.host_observation import ProtectedTarget
    monkeypatch.setattr(module, 'resolve_protected_result', ProtectedTarget, raising=False)
    def xattr(fd, name):
        if name == 'system.posix_acl_default' and state['acl'] is not None:
            return state['acl']
        raise OSError(errno.ENODATA, 'absent')
    monkeypatch.setattr(module.os, 'getxattr', xattr)
    observer = module.NativeFolderObserver(inventory=lambda: copy.deepcopy(state['payload']),
        mounts=lambda: state['mounts'], backing=lambda key: state['backing'],
        resolver=resolve, flags=lambda fd: state['flags'])
    return observer, parent, state, disk, leaf


def test_system_backed_ext4_parent_observation_is_read_only(native):
    observer, parent, _, _, _ = native
    (parent / 'unrelated').write_text('keep')
    before = set(parent.iterdir())
    value = observer.observe(parent / 'demo', archives=())
    assert value.parent_path == str(parent)
    assert value.destination_path == str(parent / 'demo')
    assert value.target.child_name == 'demo'
    assert value.target.parent_parts == parent.parts[1:]
    assert value.filesystem_id == 'NATIVE-FS'
    assert 'wwn:HOST-WWN' in value.backing_ids
    assert 'serial:HOST-SERIAL' in value.backing_ids
    assert 'filesystem:NATIVE-FS' in value.backing_ids
    assert value.capacity.available_bytes >= 0
    assert set(parent.iterdir()) == before


def test_shared_sibling_write_does_not_change_binding(native):
    observer, parent, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    (parent / 'unrelated').write_bytes(bytes(4096))
    with BoundTree(parent) as tree:
        assert observer.recheck(tree, value).target == value.target


def test_real_observer_evidence_enters_native_plan_binding_canonically(native):
    from modelark.slice.folder_plan import binding_for
    observer, parent, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    assert value.backing_ids == tuple(sorted(set(value.backing_ids)))
    binding = binding_for(value.target, str(parent.parent / 'catalog.sqlite'),
                          value.parent_path, value.backing_ids)
    with BoundTree(parent) as tree:
        refreshed = observer.recheck(tree, value)
    assert refreshed.backing_ids == value.backing_ids
    assert binding == binding_for(refreshed.target, str(parent.parent / 'catalog.sqlite'),
                                  refreshed.parent_path, refreshed.backing_ids)


def test_inventory_dictionary_order_does_not_change_admission_binding(native):
    from modelark.slice.folder_plan import binding_for
    observer, parent, state, disk, _ = native
    first = observer.observe(parent / 'demo', archives=())
    state['payload']['blockdevices'][0] = dict(reversed(tuple(disk.items())))
    second = observer.observe(parent / 'demo', archives=())
    assert first.target == second.target
    assert first.backing_ids == second.backing_ids
    assert binding_for(first.target, str(parent / 'catalog'), first.parent_path, first.backing_ids) == (
        binding_for(second.target, str(parent / 'catalog'), second.parent_path, second.backing_ids))


def test_owned_child_presence_does_not_prevent_recheck(native):
    observer, parent, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    (parent / 'demo').mkdir()
    with BoundTree(parent) as tree:
        assert observer.recheck(tree, value).target == value.target
    with pytest.raises(TransferRefusal, match='already exists'):
        observer.observe(parent / 'demo', archives=())
    assert observer.observe(parent / 'demo', archives=(), allow_existing=True).target == value.target


@pytest.mark.parametrize('kind', ['file', 'directory', 'symlink'])
def test_existing_child_is_never_adopted(native, kind):
    observer, parent, *_ = native
    child = parent / 'demo'
    if kind == 'file':
        child.write_text('untouched')
    elif kind == 'directory':
        child.mkdir()
    else:
        child.symlink_to(parent / 'missing')
    with pytest.raises(TransferRefusal, match='already exists'):
        observer.observe(child, archives=())


def test_parent_symlink_refused(native):
    observer, parent, *_ = native
    link = parent.parent / 'link'
    link.symlink_to(parent, target_is_directory=True)
    with pytest.raises(TransferRefusal):
        observer.observe(link / 'demo', archives=())


def test_parent_replacement_refused(native):
    observer, parent, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    with BoundTree(parent) as tree:
        parent.rename(parent.with_name('old'))
        parent.mkdir()
        with pytest.raises(TransferRefusal, match='changed'):
            observer.recheck(tree, value)


def test_backing_replacement_refused(native):
    observer, parent, state, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    state['backing'] = (18, 25)
    with BoundTree(parent) as tree:
        with pytest.raises(TransferRefusal, match='backing changed'):
            observer.recheck(tree, value)


@pytest.mark.parametrize('flag', [0x800, 0x40000000, 0x100000, 0x10, 0x20, 0x10000000])
def test_unqualified_directory_flags_refuse(native, flag):
    observer, parent, state, *_ = native
    state['flags'] = flag
    with pytest.raises(TransferRefusal, match='flags'):
        observer.observe(parent / 'demo', archives=())


def test_indexed_parent_is_allowed(native):
    observer, parent, state, *_ = native
    state['flags'] = 0x1000
    observer.observe(parent / 'demo', archives=())


@pytest.mark.parametrize('permissions', [7, 5])
def test_default_acl_requires_owner_rwx_but_accepts_nonowner_entries(native, permissions):
    observer, parent, state, *_ = native
    state['acl'] = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, perm, 0xffffffff)
        for tag, perm in [(1, permissions), (4, 7), (16, 7), (32, 7)])
    if permissions == 7:
        observer.observe(parent / 'demo', archives=())
    else:
        with pytest.raises(TransferRefusal, match='owner rwx'):
            observer.observe(parent / 'demo', archives=())


@pytest.mark.parametrize('mode,allowed', [(0o777, False), (0o1777, True), (0o755, True)])
def test_shared_parent_requires_sticky_protection(native, mode, allowed):
    observer, parent, *_ = native
    parent.chmod(mode)
    if allowed:
        observer.observe(parent / 'demo', archives=())
    else:
        with pytest.raises(TransferRefusal, match='sticky'):
            observer.observe(parent / 'demo', archives=())


def test_descriptor_acl_access_error_preserves_permission_meaning(native, monkeypatch):
    observer, parent, *_ = native
    def denied(fd):
        raise PermissionError(errno.EACCES, 'denied')
    monkeypatch.setattr(module, 'require_create_access', denied)
    with pytest.raises(TransferRefusal) as raised:
        observer.observe(parent / 'demo', archives=())
    assert raised.value.code == 'DESTINATION_NOT_WRITABLE'


def test_large_inherited_acl_refuses_before_creating_output(native):
    observer, parent, state, *_ = native
    entries = [(1, 7, 0xffffffff)] + [(2, 7, uid) for uid in range(100000, 100200)]
    entries += [(4, 7, 0xffffffff), (16, 7, 0xffffffff), (32, 7, 0xffffffff)]
    state['acl'] = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)
    with pytest.raises(TransferRefusal, match='xattr') as raised:
        observer.observe(parent / 'demo', archives=())
    assert raised.value.code == 'DESTINATION_NOT_WRITABLE'
    assert not (parent / 'demo').exists()


@pytest.mark.parametrize('block_size,named_users,allowed', [
    (1024, 0, False), (2048, 1, True), (2048, 100, False), (4096, 100, True)])
def test_marker_and_acl_budget_uses_filesystem_block_size(native, monkeypatch,
                                                        block_size, named_users, allowed):
    observer, parent, state, *_ = native
    if named_users:
        entries = [(1, 7, 0xffffffff)] + [(2, 7, uid) for uid in range(100000, 100000 + named_users)]
        entries += [(4, 7, 0xffffffff), (16, 7, 0xffffffff), (32, 7, 0xffffffff)]
        state['acl'] = struct.pack('<I', 2) + b''.join(struct.pack('<HHI', *entry) for entry in entries)
    original = os.fstatvfs
    def observed(fd):
        value = original(fd)
        return SimpleNamespace(f_bsize=block_size, f_frsize=block_size, f_blocks=value.f_blocks,
                               f_bavail=value.f_bavail, f_favail=value.f_favail, f_ffree=value.f_ffree)
    monkeypatch.setattr(os, 'fstatvfs', observed)
    if allowed:
        observer.observe(parent / 'demo', archives=())
    else:
        with pytest.raises(TransferRefusal, match='xattr'):
            observer.observe(parent / 'demo', archives=())
    assert not (parent / 'demo').exists()


def test_mapped_single_parent_storage_allowed(native):
    observer, parent, state, disk, leaf = native
    leaf['type'] = 'lvm'
    leaf['path'] = '/dev/mapper/test'
    disk['children'] = [{'path': '/dev/testdisk2', 'type': 'crypt', 'maj:min': '238:2',
                         'uuid': 'CRYPT-FS', 'ro': False, 'children': [leaf]}]
    value = observer.observe(parent / 'demo', archives=())
    assert value.filesystem_id == 'NATIVE-FS'


def test_ambiguous_mapped_parent_refused(native):
    observer, parent, state, disk, leaf = native
    state['payload']['blockdevices'].append({'path': '/dev/other', 'type': 'disk',
        'maj:min': '237:0', 'serial': 'OTHER', 'ro': False, 'children': [copy.deepcopy(leaf)]})
    with pytest.raises(TransferRefusal, match='ambiguous'):
        observer.observe(parent / 'demo', archives=())


@pytest.mark.parametrize('archive', [SimpleNamespace(fs_uuid='NATIVE-FS'),
                                   SimpleNamespace(serial='HOST-SERIAL'), SimpleNamespace()])
def test_registered_archive_is_excluded(native, archive):
    observer, parent, *_ = native
    with pytest.raises(TransferRefusal, match='archive'):
        observer.observe(parent / 'demo', archives=(archive,))


def test_archive_sibling_partition_is_excluded(native):
    observer, parent, _, disk, _ = native
    disk['children'].append({'path': '/dev/testdisk2', 'type': 'part', 'maj:min': '236:2',
                             'uuid': 'ARCHIVE', 'ro': False})
    with pytest.raises(TransferRefusal, match='archive'):
        observer.observe(parent / 'demo', archives=(SimpleNamespace(fs_uuid='ARCHIVE'),))


def test_protected_role_retarget_is_detected_without_mount_change(native):
    observer, parent, state, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    state['roles']['/var'] = str(parent)
    with BoundTree(parent) as tree:
        with pytest.raises(TransferRefusal, match='protected'):
            observer.recheck(tree, value)


def test_supplied_private_subtree_is_protected(native):
    observer, parent, *_ = native
    with pytest.raises(TransferRefusal, match='protected'):
        observer.observe(parent / 'demo', archives=(), protected_paths=(parent,))


def test_catalog_file_does_not_protect_its_whole_parent(native):
    observer, parent, *_ = native
    catalog = parent / 'catalog.sqlite'
    catalog.write_text('fixture')
    observer.observe(parent / 'demo', archives=(), protected_paths=(catalog,))
    with pytest.raises(TransferRefusal, match='protected'):
        observer.observe(catalog, archives=(), protected_paths=(catalog,))


@pytest.mark.parametrize('fs', ['tmpfs', 'vfat', 'xfs', 'overlay', 'fuse'])
def test_other_filesystem_refused_before_flags_probe(native, fs):
    observer, parent, state, _, leaf = native
    state['mounts'] = state['mounts'].replace(' - ext4 ', f' - {fs} ')
    leaf['fstype'] = fs
    with pytest.raises(TransferRefusal, match='ext4'):
        observer.observe(parent / 'demo', archives=())


def test_mount_alias_refused(native):
    observer, parent, state, _, leaf = native
    state['mounts'] += f"9292 1 {leaf['maj:min']} / /alias rw - ext4 /dev/testdisk1 rw\n"
    with pytest.raises(TransferRefusal, match='aliases'):
        observer.observe(parent / 'demo', archives=())


def test_scope_shared_by_distinct_children(native):
    observer, parent, state, *_ = native
    first = observer.observe(parent / 'first', archives=())
    second = observer.observe(parent / 'second', archives=())
    assert first.target.filesystem_scope == second.target.filesystem_scope
    assert first.target.target_id != second.target.target_id


def test_serialized_evidence_does_not_recreate_observer_authority(native):
    observer, parent, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    observer._observed.clear()
    with BoundTree(parent) as tree:
        with pytest.raises(TransferRefusal, match='no matching'):
            observer.recheck(tree, value)


def test_mapped_recheck_refreshes_backing_ancestry(native):
    observer, parent, state, disk, leaf = native
    leaf['type'] = 'lvm'
    value = observer.observe(parent / 'demo', archives=())
    disk['serial'] = 'REPLACEMENT'
    disk['wwn'] = 'REPLACEMENT-WWN'
    with BoundTree(parent) as tree:
        with pytest.raises(TransferRefusal, match='binding changed'):
            observer.recheck(tree, value)


def test_changed_registered_archive_roles_are_revalidated(native):
    observer, parent, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    with BoundTree(parent) as tree:
        with pytest.raises(TransferRefusal, match='archive'):
            observer.recheck(tree, value, archives=(SimpleNamespace(fs_uuid='NATIVE-FS'),))


def test_initial_observation_inventory_race_refused(native):
    observer, parent, state, *_ = native
    calls = []
    def changing():
        payload = copy.deepcopy(state['payload'])
        if calls:
            payload['blockdevices'][0]['serial'] = 'CHANGED'
        calls.append(1)
        return payload
    observer._inventory = changing
    with pytest.raises(TransferRefusal, match='changed during admission'):
        observer.observe(parent / 'demo', archives=())
    assert not (parent / 'demo').exists()


def test_full_observe_keeps_semantic_refusal_code(native):
    observer, parent, state, *_ = native
    state['flags'] = 0x40000000
    with pytest.raises(TransferRefusal) as raised:
        observer.observe(parent / 'demo', archives=())
    assert raised.value.code == 'FILESYSTEM_UNSUPPORTED'


def test_recheck_flags_change_refused(native):
    observer, parent, state, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    state['flags'] = 0x40000000
    with BoundTree(parent) as tree:
        with pytest.raises(TransferRefusal, match='flags'):
            observer.recheck(tree, value)


def test_missing_backing_during_recheck_proves_wait(native):
    observer, parent, *_ = native
    value = observer.observe(parent / 'demo', archives=())
    def missing(key):
        raise FileNotFoundError(errno.ENOENT, 'gone')
    observer._backing = missing
    with BoundTree(parent) as tree:
        with pytest.raises(TransferRefusal) as raised:
            observer.recheck(tree, value)
    assert raised.value.code == 'WAITING_DESTINATION'
