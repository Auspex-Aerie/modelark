"""Pure syntax reuse must not turn a fresh attachment check into a cached one."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from modelark.slice import host_observation as host, linux
from modelark.slice.io_errors import ProbeFailure
from modelark.slice.transaction import TransferRefusal

ROW = '12 1 8:1 / /mnt/test rw - ext4 /dev/test rw\n'


@pytest.fixture(autouse=True)
def isolated_cache():
    host._cached_mounts.cache_clear()
    yield
    host._cached_mounts.cache_clear()


def test_identical_fresh_reads_reuse_only_parse(monkeypatch):
    reads, parses = [], []
    original = host._parse_mounts

    def read(path):
        assert str(path) == '/proc/self/mountinfo'
        reads.append(path)
        return ROW.encode()

    def parse(text):
        parses.append(text)
        return original(text)

    monkeypatch.setattr(Path, 'read_bytes', read)
    monkeypatch.setattr(host, '_parse_mounts', parse)
    for _ in range(5):
        assert linux._mount_ids() == frozenset({12})
    assert len(reads) == 5 and parses == [ROW]


@pytest.mark.parametrize('changed', [
    ROW.replace('12 1', '13 1'), ROW.replace('8:1', '8:2'),
    ROW.replace('/mnt/test', '/mnt/other'), ROW.replace('/dev/test', '/dev/other'),
    ROW.replace(' rw -', ' ro -'), ROW.replace(' rw\n', ' ro\n'),
    ROW + '51 1 0:4 net:[4026533226] /run/netns/example rw - nsfs nsfs rw\n',
])
def test_every_changed_byte_is_a_new_parse(changed):
    first = host.parse_mounts(ROW)
    assert host.parse_mounts(changed) != first
    assert host._cached_mounts.cache_info().misses == 2
    assert host.parse_mounts(ROW) is first


@pytest.mark.parametrize('malformed', ['', 'garbage', ROW + ROW,
                                      ROW.replace('/mnt/test', '/mnt/\\000')])
def test_invalid_inventory_never_reuses_warm_success(malformed):
    valid = host.parse_mounts(ROW)
    for _ in range(2):
        with pytest.raises(TransferRefusal):
            host.parse_mounts(malformed)
    assert host._cached_mounts.cache_info().currsize == 1
    assert host._cached_mounts.cache_info().misses == 3
    assert host.parse_mounts(ROW) is valid


def test_read_error_after_warm_cache_still_refuses(monkeypatch):
    host.parse_mounts(ROW)
    error = OSError('procfs read denied')

    def fail(path):
        raise error

    monkeypatch.setattr(Path, 'read_bytes', fail)
    with pytest.raises(ProbeFailure) as caught:
        linux._mount_ids()
    assert caught.value.__cause__ is error
    assert host._cached_mounts.cache_info().hits == 0


def test_detached_inventory_latches_even_when_old_text_returns(tmp_path, monkeypatch):
    # Real descriptor identities; only procfs text is a synthetic attachment view.
    with linux.BoundTree(tmp_path) as probe:
        retained = probe.mount_id
    attached = ROW.replace('12 1', f'{retained} 1')
    detached = ROW.replace('12 1', f'{retained + 1} 1')
    current = attached
    monkeypatch.setattr(linux, 'read_fs_text', lambda path: current)
    with linux.BoundTree(tmp_path) as tree:
        current = detached
        with pytest.raises(TransferRefusal, match='WAITING_DESTINATION'):
            tree.check()
        current = attached
        assert host.parse_mounts(current)[0].mount_id == retained
        with pytest.raises(TransferRefusal, match='WAITING_DESTINATION'):
            tree.check()


def test_cached_result_is_immutable():
    mounts = host.parse_mounts(ROW)
    with pytest.raises(TypeError):
        mounts[0] = mounts[0]
    with pytest.raises(FrozenInstanceError):
        mounts[0].path = '/changed'
    with pytest.raises(AttributeError):
        mounts[0].options.add('ro')
    assert host.parse_mounts(ROW) is mounts


def test_entry_count_and_input_retention_are_bounded():
    first = host.parse_mounts(ROW)
    host.parse_mounts(ROW.replace('12 1', '13 1'))
    host.parse_mounts(ROW.replace('12 1', '14 1'))
    assert host._cached_mounts.cache_info().currsize == 2
    assert host.parse_mounts(ROW) is not first  # LRU eviction, same semantics.
    limit = host._MOUNT_CACHE_MAX_CHARS
    exact = ROW.replace('/mnt/test', '/' + 'x' * (limit - len(ROW) + 8))
    assert len(exact) == limit
    assert host.parse_mounts(exact) is host.parse_mounts(exact)
    oversized = exact + '\n'
    before = host._cached_mounts.cache_info()
    one, two = host.parse_mounts(oversized), host.parse_mounts(oversized)
    assert one == two and one is not two
    assert host._cached_mounts.cache_info() == before
    with pytest.raises(TransferRefusal):
        host.parse_mounts(oversized + 'malformed')
    assert host._cached_mounts.cache_info() == before


def test_string_subclass_never_uses_custom_cache_equality():
    class StrangeText(str):
        def __hash__(self):
            pytest.fail('subclass used as cache key')

    one, two = host.parse_mounts(StrangeText(ROW)), host.parse_mounts(StrangeText(ROW))
    assert one == two and one is not two
    assert host._cached_mounts.cache_info().currsize == 0


def test_concurrent_different_texts_cannot_cross_contaminate():
    rows = [ROW.replace('12 1', f'{number} 1') for number in range(12, 32)] * 5
    with ThreadPoolExecutor(max_workers=4) as workers:
        values = list(workers.map(host.parse_mounts, rows))
    assert [value[0].mount_id for value in values] == [int(row.split()[0]) for row in rows]
    assert host._cached_mounts.cache_info().currsize <= 2
