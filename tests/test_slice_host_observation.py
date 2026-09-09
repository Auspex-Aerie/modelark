"""Lossless procfs and descriptor-backed protected roles on disposable paths only."""
import importlib
import os

import pytest


def test_filesystem_text_preserves_non_utf8_bytes_without_text_decoding(tmp_path, monkeypatch):
    host = importlib.import_module('modelark.slice.host_observation')
    path = tmp_path / 'proc-fixture'
    content = b'/mnt/nonutf-\xff\n'
    path.write_bytes(content)
    monkeypatch.setattr(type(path), 'read_text', lambda *args, **kwargs: pytest.fail('lossy text IO'))
    assert os.fsencode(host.read_fs_text(path)) == content


def test_proc_fields_and_records_preserve_unicode_whitespace_and_encoded_newlines():
    host = importlib.import_module('modelark.slice.host_observation')
    pathname = '/mnt/a\u00a0b\u2028c\u0085d\\012e'
    text = f'12 1 8:1 / {pathname} rw - ext4 /dev/test rw\n'
    mounts = host.parse_mounts(text)
    assert len(mounts) == 1
    assert mounts[0].path == '/mnt/a\u00a0b\u2028c\u0085d\ne'
    assert host.proc_fields(pathname + '\tfile 4096 0 -2')[0] == pathname


def test_mount_and_octal_decoding_round_trip_arbitrary_filename_bytes():
    host = importlib.import_module('modelark.slice.host_observation')
    raw = b'12 1 8:1 / /mnt/raw-\xff\\040space\\012line rw - ext4 /dev/test rw\n'
    mount = host.parse_mounts(os.fsdecode(raw))[0]
    assert os.fsencode(mount.path) == b'/mnt/raw-\xff space\nline'
    assert os.fsencode(host.decode_proc_path('/mnt/\\377')) == b'/mnt/\xff'


def test_protected_directory_resolves_symlink_and_binds_actual_descriptor(tmp_path):
    host = importlib.import_module('modelark.slice.host_observation')
    actual = tmp_path / 'actual-home'
    actual.mkdir()
    link = tmp_path / 'home'
    link.symlink_to(actual, target_is_directory=True)
    target = host.resolve_protected(link)
    info = actual.stat()
    assert target.path == str(actual)
    assert target.major_minor == f'{os.major(info.st_dev)}:{os.minor(info.st_dev)}'
    assert target.inode == info.st_ino
    assert target.mount_id > 0


def test_protected_file_resolves_parent_symlinks(tmp_path):
    host = importlib.import_module('modelark.slice.host_observation')
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'swap').write_bytes(b'disposable')
    link = tmp_path / 'alias'
    link.symlink_to(actual, target_is_directory=True)
    assert host.resolve_protected(link / 'swap', directory=False).path == str(actual / 'swap')


def test_absent_optional_role_is_distinct_from_dangling_symlink(tmp_path):
    host = importlib.import_module('modelark.slice.host_observation')
    missing = tmp_path / 'absent'
    assert host.resolve_protected(missing, optional=True) is None
    link = tmp_path / 'home'
    link.symlink_to(missing, target_is_directory=True)
    with pytest.raises(OSError):
        host.resolve_protected(link, optional=True)
    with pytest.raises(OSError):
        host.resolve_protected(link / 'nested', optional=True)


def test_unresolvable_protected_role_is_not_treated_as_absent(tmp_path):
    host = importlib.import_module('modelark.slice.host_observation')
    link = tmp_path / 'loop'
    link.symlink_to(link)
    with pytest.raises(OSError):
        host.resolve_protected(link, optional=True)


def test_protected_target_change_during_descriptor_observation_refuses(tmp_path, monkeypatch):
    host = importlib.import_module('modelark.slice.host_observation')
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    link = tmp_path / 'home'
    link.symlink_to(first, target_is_directory=True)
    realpath = host.os.path.realpath
    changed = False
    def change(path, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            link.unlink()
            link.symlink_to(second, target_is_directory=True)
        return realpath(path, **kwargs)
    monkeypatch.setattr(host.os.path, 'realpath', change)
    with pytest.raises(host.TransferRefusal, match='DESTINATION_UNPROVEN'):
        host.resolve_protected(link)


def test_namespace_handle_roots_are_preserved_but_not_generalized():
    from modelark.slice.host_observation import parse_mounts
    from modelark.slice.transaction import TransferRefusal
    import pytest
    row = '51 1 0:4 net:[4026533226] /run/netns/example rw - nsfs nsfs rw\n'
    assert parse_mounts(row)[0].root == 'net:[4026533226]'
    with pytest.raises(TransferRefusal):
        parse_mounts(row.replace(' - nsfs ', ' - ext4 '))
    with pytest.raises(TransferRefusal):
        parse_mounts(row.replace('net:[4026533226]', 'arbitrary'))


@pytest.mark.parametrize('consumer', ['hardware', 'linux', 'capacity'])
def test_all_mount_consumers_preserve_filesystem_bytes_and_unicode_records(monkeypatch, consumer):
    from pathlib import Path
    from types import SimpleNamespace
    import stat
    host = importlib.import_module('modelark.slice.host_observation')
    module = importlib.import_module('modelark.slice.' + consumer)
    pathname = b'/mnt/raw-\xff-' + '\u00a0\u2028\u0085'.encode() + b'\\012line'
    raw = (b'12 1 8:1 / ' + pathname + b' rw - ext4 /dev/test rw\n'
           b'13 1 0:4 net:[4026533226] /run/netns/example rw - nsfs nsfs rw\n')
    original = Path.read_bytes
    def read_bytes(path):
        return raw if str(path) == '/proc/self/mountinfo' else original(path)
    monkeypatch.setattr(Path, 'read_bytes', read_bytes)
    seen = []
    def parse(text):
        mounts = host.parse_mounts(text)
        seen.extend(mounts)
        return mounts
    monkeypatch.setattr(module, 'parse_mounts', parse)
    if consumer == 'hardware':
        observer = module.LinuxObserver(inventory=lambda: {}, swaps=lambda: '')
        assert len(module._mounts(observer._mounts())) == 2
    elif consumer == 'linux':
        assert module._mount_ids() == frozenset({12, 13})
    else:
        # Only the mount feature-parser path is exercised: the block descriptor
        # and superblock here are explicit synthetic evidence, not live devices.
        fake_os = SimpleNamespace(
            O_RDONLY=os.O_RDONLY, O_CLOEXEC=os.O_CLOEXEC, O_NONBLOCK=os.O_NONBLOCK,
            major=lambda dev: 8, minor=lambda dev: 1,
            open=lambda *args: 102, close=lambda fd: None, pread=lambda *args: b'synthetic',
            fstat=lambda fd: SimpleNamespace(st_dev=7, st_rdev=7, st_mode=stat.S_IFBLK))
        monkeypatch.setattr(module, 'os', fake_os)
        monkeypatch.setattr(module, 'parse_superblock', lambda data, uuid: {'uuid': uuid})
        assert module._volume(SimpleNamespace(fd=101, mount_id=12),
                              SimpleNamespace(fs_uuid='TEST')) == {'uuid': 'TEST'}
    assert os.fsencode(seen[0].path) == pathname.replace(b'\\012', b'\n')
    assert seen[1].root == 'net:[4026533226]'
