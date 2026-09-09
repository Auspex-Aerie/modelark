"""Synthetic candidate admission; not kernel-vfat write qualification."""
import copy
import errno
import json
import os
from types import SimpleNamespace

import pytest

from modelark.slice import fat32_observation as module
from modelark.slice.host_observation import ProtectedTarget
from modelark.slice.linux import BoundTree
from modelark.slice.transaction import TransferRefusal


@pytest.fixture
def fat(tmp_path):
    volume = tmp_path / 'FatVolume'
    volume.mkdir(mode=0o700)
    parent = volume / 'Exports'
    parent.mkdir(mode=0o700)
    with module.Fat32Tree(parent) as tree:
        value = os.fstat(tree.fd)
        key = f'{os.major(value.st_dev)}:{os.minor(value.st_dev)}'
        mount_id = tree.mount_id
    leaf = {'path': '/dev/testusb1', 'type': 'part', 'maj:min': key,
            'fstype': 'vfat', 'fsver': 'FAT32', 'uuid': 'FAT-UUID', 'ro': False}
    disk = {'path': '/dev/testusb', 'type': 'disk', 'maj:min': '238:0',
            'serial': 'USB-SERIAL', 'wwn': 'USB-WWN', 'tran': 'usb', 'ro': False,
            'children': [leaf]}
    options = f'rw,uid={os.geteuid()},gid={os.getegid()},fmask=0022,dmask=0022,codepage=437,iocharset=iso8859-1,utf8,shortname=mixed,errors=remount-ro'
    state = {'payload': {'blockdevices': [disk]}, 'options': options, 'backing': (1, 2),
             'roles': {}, 'extra_mounts': '', 'version': 'vfat', 'volume': volume}
    def mounts():
        return (f'1 0 239:1 / / rw - ext4 /dev/host rw\n'
                f"{mount_id} 1 {key} / {volume} rw - {state['version']} /dev/testusb1 {state['options']}\n"
                + state['extra_mounts'])
    def resolver(path, **kwargs):
        path = str(path)
        target = state['roles'].get(path, path)
        if target is None:
            return None
        own = target == str(volume) or target.startswith(str(volume) + '/')
        return ProtectedTarget(path, target, key if own else '239:1', mount_id if own else 1, 10)
    observer = module.Fat32FolderObserver(inventory=lambda: copy.deepcopy(state['payload']),
        mounts=mounts, backing=lambda key: state['backing'], resolver=resolver)
    return observer, parent, state, disk, leaf


def test_read_only_fat32_intent_has_no_persistent_inode_identity(fat):
    observer, parent, *_ = fat
    before = tuple(parent.iterdir())
    evidence = observer.observe(parent / 'Demo', archives=())
    assert evidence.parent_parts == ('Exports',)
    assert evidence.child_name == 'Demo'
    assert evidence.filesystem_id == 'FAT-UUID'
    assert evidence.backing_ids == tuple(sorted(set(evidence.backing_ids)))
    assert {'serial:USB-SERIAL', 'wwn:USB-WWN', 'filesystem:FAT-UUID'} <= set(evidence.backing_ids)
    assert not hasattr(evidence, 'parent_identity')
    assert evidence.capacity.free_inodes is None
    assert tuple(parent.iterdir()) == before


def test_fat_tree_uses_live_inode_without_relaxing_native_birth_requirement(tmp_path, monkeypatch):
    from modelark.slice import linux
    actual = linux._statx
    calls = []
    def no_birth(fd, *, require_birth=True):
        calls.append(require_birth)
        if require_birth:
            raise TransferRefusal('FILESYSTEM_UNSUPPORTED', 'birth unavailable')
        value = actual(fd, require_birth=False)
        value.mask &= ~0x800
        return value
    monkeypatch.setattr(linux, '_statx', no_birth)
    monkeypatch.setattr(module, '_statx', no_birth)
    with module.Fat32Tree(tmp_path) as tree:
        tree.check()
        assert len(tree.identity(tree.fd)) == 2
        assert not any(calls)
    with pytest.raises(TransferRefusal, match='birth unavailable'):
        BoundTree(tmp_path)


def test_replaced_preview_parent_may_be_freshly_bound_at_start(fat):
    observer, parent, *_ = fat
    evidence = observer.observe(parent / 'Demo', archives=())
    parent.rename(parent.with_name('Previous'))
    parent.mkdir(mode=0o700)
    with module.Fat32Tree(parent) as tree:
        assert observer.recheck(tree, evidence).filesystem_scope == evidence.filesystem_scope


def test_replaced_live_parent_refuses(fat):
    observer, parent, *_ = fat
    evidence = observer.observe(parent / 'Demo', archives=())
    with module.Fat32Tree(parent) as tree:
        parent.rename(parent.with_name('Previous'))
        parent.mkdir(mode=0o700)
        with pytest.raises(TransferRefusal, match='changed'):
            observer.recheck(tree, evidence)


def test_closed_live_parent_cannot_be_recreated_from_evidence(fat):
    observer, parent, *_ = fat
    evidence = observer.observe(parent / 'Demo', archives=())
    tree = module.Fat32Tree(parent)
    tree.close()
    with pytest.raises(TransferRefusal, match='closed'):
        observer.recheck(tree, evidence)


def test_unrelated_sibling_and_owned_child_do_not_change_live_intent(fat):
    observer, parent, *_ = fat
    evidence = observer.observe(parent / 'Demo', archives=())
    with module.Fat32Tree(parent) as tree:
        (parent / 'Sibling').write_text('retain')
        (parent / 'Demo').mkdir(mode=0o700)
        assert observer.recheck(tree, evidence).filesystem_scope == evidence.filesystem_scope


@pytest.mark.parametrize('existing', ['Demo', 'demo', 'DEMO'])
def test_existing_case_equivalent_child_refuses(fat, existing):
    observer, parent, *_ = fat
    (parent / existing).mkdir()
    with pytest.raises(TransferRefusal, match='already exists'):
        observer.observe(parent / 'Demo', archives=())


@pytest.mark.parametrize('name', ['ALIAS~1', 'éxport', 'bad.', 'CON', 'bad:name', '.slice-owner'])
def test_unqualified_output_spelling_refuses(fat, name):
    observer, parent, *_ = fat
    with pytest.raises(TransferRefusal):
        observer.observe(parent / name, archives=())


def test_parent_canonical_case_ambiguity_refuses_even_on_synthetic_host(fat):
    observer, parent, *_ = fat
    parent.with_name('exports').mkdir()
    with pytest.raises(TransferRefusal, match='alias or ambiguous case'):
        observer.observe(parent / 'Demo', archives=())


def test_parent_short_alias_spelling_refuses(fat):
    observer, parent, *_ = fat
    alias = parent.with_name('EXPORT~1')
    alias.mkdir()
    with pytest.raises(TransferRefusal, match='component'):
        observer.observe(alias / 'Demo', archives=())


def test_parent_symlink_refuses(fat):
    observer, parent, *_ = fat
    alias = parent.with_name('Alias')
    alias.symlink_to(parent, target_is_directory=True)
    with pytest.raises(TransferRefusal):
        observer.observe(alias / 'Demo', archives=())


@pytest.mark.parametrize('option', ['nonumtail', 'codepage=850', 'shortname=lower', 'iocharset=koi8-r',
                                  'iocharset=ascii', 'iocharset=utf8',
                                  'showexec', 'check=r', 'utf8=0'])
def test_unqualified_name_options_refuse(fat, option):
    observer, parent, state, *_ = fat
    key = option.split('=', 1)[0]
    state['options'] = ','.join(value for value in state['options'].split(',') if value.split('=', 1)[0] != key)
    state['options'] += ',' + option
    with pytest.raises(TransferRefusal, match='qualified'):
        observer.observe(parent / 'Demo', archives=())


def test_utf8_mount_flag_is_required_by_qualified_codec_profile(fat):
    observer, parent, state, *_ = fat
    state['options'] = ','.join(option for option in state['options'].split(',') if option != 'utf8')
    with pytest.raises(TransferRefusal, match='utf8') as raised:
        observer.observe(parent / 'Demo', archives=())
    assert raised.value.code == 'FILESYSTEM_UNSUPPORTED'


@pytest.mark.parametrize('option', ['uid=99999', 'dmask=0000', 'fmask=0002', 'dmask=bad',
                                  'fmask=0477', 'fmask=0277', 'dmask=0477',
                                  'dmask=0277', 'dmask=0177'])
def test_mount_permissions_refuse_without_changing_anything(fat, option):
    observer, parent, state, *_ = fat
    key = option.split('=', 1)[0]
    state['options'] = ','.join(value for value in state['options'].split(',') if not value.startswith(key + '='))
    state['options'] += ',' + option
    with pytest.raises(TransferRefusal) as error:
        observer.observe(parent / 'Demo', archives=())
    assert error.value.code == 'DESTINATION_NOT_WRITABLE'
    assert not (parent / 'Demo').exists()


@pytest.mark.parametrize('fmask,dmask', [('0077', '0077'), ('0177', '0077')])
def test_masks_preserving_required_owner_permissions_are_admitted(fat, fmask, dmask):
    observer, parent, state, *_ = fat
    state['options'] = state['options'].replace('fmask=0022', 'fmask=' + fmask).replace(
        'dmask=0022', 'dmask=' + dmask)
    observer.observe(parent / 'Demo', archives=())
    assert not (parent / 'Demo').exists()


def test_effective_parent_nonowner_write_refuses_even_when_sticky(fat):
    observer, parent, *_ = fat
    parent.chmod(0o1777)
    with pytest.raises(TransferRefusal, match='nonowner write'):
        observer.observe(parent / 'Demo', archives=())


@pytest.mark.parametrize('version', [None, 'FAT12', 'FAT16', '32', 'exFAT'])
def test_explicit_fat32_version_required(fat, version):
    observer, parent, _, _, leaf = fat
    leaf['fsver'] = version
    with pytest.raises(TransferRefusal, match='FAT32 version'):
        observer.observe(parent / 'Demo', archives=())


def test_non_vfat_kernel_mount_refuses(fat):
    observer, parent, state, *_ = fat
    state['version'] = 'fuseblk'
    with pytest.raises(TransferRefusal, match='kernel vfat'):
        observer.observe(parent / 'Demo', archives=())


def test_non_usb_destination_refuses(fat):
    observer, parent, _, disk, _ = fat
    disk['tran'] = 'nvme'
    with pytest.raises(TransferRefusal, match='USB'):
        observer.observe(parent / 'Demo', archives=())


def test_mapped_fat_destination_refuses(fat):
    observer, parent, _, _, leaf = fat
    leaf['type'] = 'crypt'
    with pytest.raises(TransferRefusal, match='stacked'):
        observer.observe(parent / 'Demo', archives=())


def test_archive_backing_excluded(fat):
    observer, parent, *_ = fat
    with pytest.raises(TransferRefusal, match='archive'):
        observer.observe(parent / 'Demo', archives=(SimpleNamespace(fs_uuid='FAT-UUID'),))


def test_protected_role_retarget_blocks_live_tree(fat):
    observer, parent, state, *_ = fat
    evidence = observer.observe(parent / 'Demo', archives=())
    state['roles']['/var'] = str(parent)
    with module.Fat32Tree(parent) as tree:
        with pytest.raises(TransferRefusal, match='protected'):
            observer.recheck(tree, evidence)


def test_mount_alias_refuses(fat):
    observer, parent, state, _, leaf = fat
    state['extra_mounts'] = f"9898 1 {leaf['maj:min']} / /alias rw - vfat /dev/testusb1 rw\n"
    with pytest.raises(TransferRefusal, match='aliases'):
        observer.observe(parent / 'Demo', archives=())


def test_backing_loss_proves_wait_but_does_not_authorize_resume(fat):
    observer, parent, *_ = fat
    evidence = observer.observe(parent / 'Demo', archives=())
    def gone(key):
        raise FileNotFoundError(errno.ENOENT, 'gone')
    observer._backing = gone
    with module.Fat32Tree(parent) as tree:
        with pytest.raises(TransferRefusal) as raised:
            observer.recheck(tree, evidence)
    assert raised.value.code == 'WAITING_DESTINATION'


def test_inventory_order_cannot_change_stable_intent(fat):
    observer, parent, state, disk, _ = fat
    first = observer.observe(parent / 'Demo', archives=())
    state['payload']['blockdevices'][0] = dict(reversed(tuple(disk.items())))
    second = observer.observe(parent / 'Demo', archives=())
    assert first.filesystem_scope == second.filesystem_scope
    assert first.backing_ids == second.backing_ids


def test_inventory_cli_requests_kernel_aliases_for_unrelated_mapped_host(fat, monkeypatch):
    observer, parent, state, *_ = fat
    root = {'path': '/dev/mapper/host-root', 'kname': '/dev/dm-1', 'type': 'lvm',
            'maj:min': '239:1', 'pkname': '/dev/dm-0', 'fstype': 'ext4', 'uuid': 'HOST-FS', 'ro': False}
    crypt = {'path': '/dev/mapper/host-crypt', 'kname': '/dev/dm-0', 'type': 'crypt',
             'maj:min': '239:2', 'pkname': '/dev/host1', 'ro': False, 'children': [root]}
    partition = {'path': '/dev/host1', 'kname': '/dev/host1', 'type': 'part',
                 'maj:min': '239:3', 'pkname': '/dev/host', 'ro': False, 'children': [crypt]}
    state['payload']['blockdevices'].append({'path': '/dev/host', 'kname': '/dev/host',
        'type': 'disk', 'maj:min': '239:0', 'serial': 'HOST-SERIAL', 'tran': 'nvme',
        'ro': False, 'children': [partition]})
    calls = []
    def inventory_command(argv, **kwargs):
        assert argv[:5] == ['lsblk', '--json', '--bytes', '--paths', '--output']
        assert {'KNAME', 'FSVER'} <= set(argv[5].split(','))
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps(state['payload']))
    monkeypatch.setattr(module.subprocess, 'run', inventory_command)
    observer._inventory = module._inventory
    value = observer.observe(parent / 'Demo', archives=())
    assert value.filesystem_id == 'FAT-UUID'
    assert len(calls) == 2


def test_serialized_observation_does_not_create_in_process_proof(fat):
    observer, parent, *_ = fat
    value = observer.observe(parent / 'Demo', archives=())
    observer._observed.clear()
    with module.Fat32Tree(parent) as tree:
        with pytest.raises(TransferRefusal, match='no matching'):
            observer.recheck(tree, value)


def test_new_archive_role_is_checked_at_recheck(fat):
    observer, parent, *_ = fat
    value = observer.observe(parent / 'Demo', archives=())
    with module.Fat32Tree(parent) as tree:
        with pytest.raises(TransferRefusal, match='archive'):
            observer.recheck(tree, value, archives=(SimpleNamespace(serial='USB-SERIAL'),))


@pytest.mark.parametrize('key,mask,detail', [
    ('dmask', '0000', 'exclude group/other writes'), ('fmask', '0477', 'preserve owner')])
def test_permission_mask_change_during_live_attempt_is_revalidated(fat, key, mask, detail):
    observer, parent, state, *_ = fat
    value = observer.observe(parent / 'Demo', archives=())
    state['options'] = state['options'].replace(key + '=0022', key + '=' + mask)
    with module.Fat32Tree(parent) as tree:
        with pytest.raises(TransferRefusal, match=detail):
            observer.recheck(tree, value)


def test_effective_permission_probe_failure_preserves_specific_refusal(fat, monkeypatch):
    observer, parent, *_ = fat
    def denied(fd):
        raise PermissionError(errno.EACCES, 'denied')
    monkeypatch.setattr(module, 'require_create_access', denied)
    with pytest.raises(TransferRefusal) as raised:
        observer.observe(parent / 'Demo', archives=())
    assert raised.value.code == 'DESTINATION_NOT_WRITABLE'


def test_mount_disappearance_latches_live_tree_until_closed(fat):
    observer, parent, *_ = fat
    value = observer.observe(parent / 'Demo', archives=())
    with module.Fat32Tree(parent) as tree:
        original = tree._mount_ids
        tree._mount_ids = lambda: frozenset()
        with pytest.raises(TransferRefusal) as raised:
            observer.recheck(tree, value)
        assert raised.value.code == 'WAITING_DESTINATION'
        tree._mount_ids = original
        with pytest.raises(TransferRefusal, match='attachment was lost'):
            observer.recheck(tree, value)
