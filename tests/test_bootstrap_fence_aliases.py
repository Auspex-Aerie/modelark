"""Serial-compatible bootstrap fences and complete persisted-facts commit checks.

All catalogs and lock files are disposable; physical observation is injected. The
expected lock identities come directly from the unchanged v1 hash contract, not
the production alias helper under test.
"""
from types import SimpleNamespace
from unittest import mock

import pytest

from modelark import capacity_evidence, drive_bootstrap as bs, drive_fence
from modelark import drive_mutation as dm
from test_drive_bootstrap import (
    _ANX, _FS, _SER, _anchor, _catalog, _clean_inv, _dirty_gen, _drive_row,
    _ev, _proven_drive,
)


def _fingerprint(capacity, serial):
    return capacity_evidence.identity_fingerprint_v1(
        fs_uuid=_FS, annex_uuid=_ANX, serial=serial,
        filesystem_capacity_bytes=capacity,
    )


_KINDS = ('bootstrap', 'generation_zero', 'sessionless_dirty', 'clean', 'transition')


@pytest.fixture(params=_KINDS)
def case(request, tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, '_LOCK_DIR', tmp_path / 'locks')
    kind = request.param
    with _catalog(tmp_path) as con:
        if kind == 'bootstrap':
            _drive_row(con)
        else:
            generation = 0 if kind == 'generation_zero' else 1
            _proven_drive(con, generation=generation)
            if generation:
                _dirty_gen(con)
            if kind in ('clean', 'transition'):
                _anchor(con)
        capacity = 2000 if kind == 'transition' else 1000
        observed = _ev(fp=_fingerprint(capacity, _SER), capacity=capacity,
                       free=capacity - 100)
        inventory = _clean_inv()
        inspect = mock.Mock(return_value=inventory)
        monkeypatch.setattr(bs, '_live_evidence', mock.Mock(return_value=observed))
        monkeypatch.setattr(bs, '_inventory', inspect)
        monkeypatch.setattr(bs.register, 'archive_path', lambda *args: tmp_path / 'archive')
        yield SimpleNamespace(con=con, kind=kind, capacity=capacity,
                              inventory=inventory, inspect=inspect)


def _run(case):
    return bs.reconcile_drive(case.con, 'drive-00', dedicated=True,
                              now='2026-09-09T18:00:00Z', blocking=False)


def _published_state(con):
    """Exclude the intentionally changed descriptive fields, retain all publication state."""
    return (
        con.execute('SELECT * FROM drive_clean_anchors ORDER BY anchor_id').fetchall(),
        con.execute('SELECT * FROM drive_dirty_generations '
                    'ORDER BY drive_label,identity_epoch,generation').fetchall(),
        con.execute('SELECT identity_epoch,write_generation,identity_fingerprint,'
                    'filesystem_capacity_bytes,write_authority FROM drives').fetchall(),
        con.execute('SELECT * FROM planner_state').fetchall(),
        con.execute('PRAGMA user_version').fetchone(),
    )


@pytest.mark.parametrize('case', ['transition'], indirect=True)
@pytest.mark.parametrize('capacity,epoch', [(1000, 1), (2000, 2)])
@pytest.mark.parametrize('serial', [None, _SER], ids=['null-serial', 'canonical-serial'])
def test_transition_refuses_each_old_and_new_epoch_alias(case, capacity, epoch, serial):
    before = _published_state(case.con)
    key = (_fingerprint(capacity, serial), epoch)
    with drive_fence.hold_drives_sorted([key], blocking=False):
        with pytest.raises(dm.DriveMutationRefused) as raised:
            _run(case)
    assert raised.value.code == 'DRIVE_FENCE_UNAVAILABLE'
    case.inspect.assert_not_called()
    assert _published_state(case.con) == before
    assert not case.con.in_transaction


@pytest.mark.parametrize('serial', [None, _SER], ids=['null-serial', 'canonical-serial'])
def test_all_reconcile_paths_refuse_a_held_current_epoch_alias(case, serial):
    before = _published_state(case.con)
    # During a resize this is the old epoch; every other path uses the current
    # capacity/epoch. Bootstrap has no saved fingerprint but still must fence it.
    with drive_fence.hold_drives_sorted([(_fingerprint(1000, serial), 1)], blocking=False):
        with pytest.raises(dm.DriveMutationRefused) as raised:
            _run(case)
    assert raised.value.code == 'DRIVE_FENCE_UNAVAILABLE'
    case.inspect.assert_not_called()
    assert _published_state(case.con) == before


@pytest.mark.parametrize('column', ['serial', 'fs_uuid', 'annex_uuid'])
@pytest.mark.parametrize('phase', ['inventory', 'before_begin'])
def test_persisted_identity_drift_cannot_publish_on_any_reconcile_path(
    case, monkeypatch, column, phase,
):
    before = _published_state(case.con)
    changed_value = 'changed-' + column
    changes = []

    def change():
        assert not case.con.in_transaction
        # column is exclusively one of the closed parametrized identifiers above.
        case.con.execute(f'UPDATE drives SET {column}=? WHERE drive_label=?',
                         [changed_value, 'drive-00'])
        changes.append(True)

    if phase == 'inventory':
        def inspect(*args, **kwargs):
            change()
            return case.inventory
        case.inspect.side_effect = inspect
    else:
        original = dm._immediate

        def begin(con, body):
            change()
            return original(con, body)
        monkeypatch.setattr(dm, '_immediate', begin)

    with pytest.raises(dm.DriveMutationRefused) as raised:
        _run(case)
    assert raised.value.code == 'DRIVE_RECOVERY_OWNER_CHANGED'
    assert changes == [True]
    case.inspect.assert_called_once()
    assert case.con.execute(f'SELECT {column} FROM drives').fetchone() == (changed_value,)
    assert _published_state(case.con) == before
    assert not case.con.in_transaction


def test_unchanged_reconcile_paths_hold_every_alias_and_publish(case):
    keys = {(_fingerprint(1000, serial), 1) for serial in (None, _SER)}
    if case.kind == 'transition':
        keys.update((_fingerprint(2000, serial), 2) for serial in (None, _SER))
    checked = []

    def inspect(*args, **kwargs):
        for key in sorted(keys):
            with pytest.raises(drive_fence.FenceUnavailable):
                with drive_fence.hold_drives_sorted([key], blocking=False):
                    pytest.fail(f'inventory did not retain required alias {key}')
            checked.append(key)
        return case.inventory

    case.inspect.side_effect = inspect
    result = _run(case)
    expected = {
        'bootstrap': ('bootstrapped', 1, 1),
        'generation_zero': ('refreshed', 1, 1),
        'sessionless_dirty': ('recovered', 1, 1),
        'clean': ('refreshed', 1, 2),
        'transition': ('epoch_advanced', 2, 1),
    }[case.kind]
    assert (result.outcome, result.identity_epoch, result.generation) == expected
    assert checked == sorted(keys)
    assert case.con.execute(
        'SELECT identity_fingerprint,filesystem_capacity_bytes FROM drive_clean_anchors '
        'WHERE identity_epoch=? AND generation=?', expected[1:],
    ).fetchone() == (_fingerprint(case.capacity, _SER), case.capacity)
    # All handles must also be released when a successful reconciliation returns.
    with drive_fence.hold_drives_sorted(keys, blocking=False) as handles:
        assert len(handles) == len(keys)
    assert not case.con.in_transaction
