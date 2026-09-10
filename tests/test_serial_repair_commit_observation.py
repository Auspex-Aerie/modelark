"""Commit-time live identity must be sampled after actual SQLite lock acquisition."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap as bootstrap, drive_mutation as dm
from test_drive_bootstrap import _catalog, _FP
from test_serial_repair_adversarial import _paused_owner
from test_serial_repair_workflow import OLD, live_setup, seed


@contextmanager
def _scenario(tmp_path, monkeypatch, *, dirty):
    with _catalog(tmp_path) as con:
        seed(con, dirty=dirty)
        if dirty:
            _paused_owner(con)
        archive = live_setup(monkeypatch, tmp_path)
        binding = bootstrap.inspect_serial_identity(con, 'drive-00')['binding']
        current = bootstrap._live_evidence(con, 'drive-00')
        case = SimpleNamespace(con=con, archive=archive, binding=binding, current=current,
                               probes=[], actual_transactions=0, clone_transactions=0,
                               acquisition_changes=[])
        original_immediate = dm._immediate

        def observe(connection, label):
            # A rehearsal is a catalog-only replay. It must never reread host
            # hardware using the clone as though it were live source authority.
            assert connection is con, 'clone rehearsal must never probe live hardware'
            case.probes.append((con.in_transaction, case.actual_transactions))
            return case.current

        def immediate(connection, body):
            if connection is not con:
                case.clone_transactions += 1
                return original_immediate(connection, body)
            case.actual_transactions += 1

            def acquired():
                assert con.in_transaction
                if getattr(case, 'change_on', None) == case.actual_transactions:
                    case.current = replace(case.current, **case.change)
                    case.acquisition_changes.append(case.actual_transactions)
                return body()

            # Preserve the actual BEGIN IMMEDIATE/COMMIT/ROLLBACK implementation;
            # inject only after the real database write lock has been acquired.
            return original_immediate(connection, acquired)

        monkeypatch.setattr(bootstrap, '_live_evidence', observe)
        monkeypatch.setattr(dm, '_immediate', immediate)
        yield case


def _run(case):
    return bootstrap.repair_serial_identity(
        case.con, 'drive-00', expected_binding=case.binding,
        now='2026-09-09', writers_stopped=True)


_STAGES = [('clean_enrichment', False, 1), ('dirty_bridge', True, 1),
           ('after_bridge_enrichment', True, 2)]


@pytest.mark.parametrize('stage,dirty,change_on', _STAGES)
@pytest.mark.parametrize('field,value', [
    ('serial', 'replaced-serial'), ('fs_uuid', 'replaced-filesystem'),
    ('annex_uuid', 'replaced-annex'), ('capacity', 2000), ('proven', False),
])
def test_identity_change_after_begin_refuses_before_anchor_publication(
    tmp_path, monkeypatch, stage, dirty, change_on, field, value,
):
    with _scenario(tmp_path, monkeypatch, dirty=dirty) as case:
        before = tuple(case.con.iterdump())
        sessions = case.con.execute('SELECT * FROM execution_sessions').fetchall()
        generations = case.con.execute('SELECT * FROM drive_dirty_generations '
                                        'ORDER BY generation').fetchall()
        case.change_on, case.change = change_on, {field: value}
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_LIVE_MISMATCH') as raised:
            _run(case)
        assert case.acquisition_changes == [change_on]
        assert (True, change_on) in case.probes
        assert raised.value.evidence['legacy_recovered'] is (change_on == 2)
        assert case.con.execute('PRAGMA user_version').fetchone() == (7,)
        assert case.con.execute('SELECT * FROM execution_sessions').fetchall() == sessions
        assert case.con.execute('SELECT * FROM drive_dirty_generations '
                                'ORDER BY generation').fetchall() == generations
        if change_on == 2:
            assert case.con.execute('SELECT identity_epoch,write_generation,identity_fingerprint '
                                    'FROM drives').fetchone() == (1, 2, OLD)
            assert case.con.execute('SELECT generation,identity_fingerprint FROM drive_clean_anchors '
                                    'ORDER BY generation').fetchall() == [(1, OLD), (2, OLD)]
        else:
            assert tuple(case.con.iterdump()) == before
        assert not case.con.in_transaction
        assert (case.archive / 'untouched.txt').read_bytes() == b'unchanged archive sentinel'


@pytest.mark.parametrize('stage,dirty,change_on', _STAGES)
def test_free_space_changed_at_acquisition_is_the_published_anchor_value(
    tmp_path, monkeypatch, stage, dirty, change_on,
):
    with _scenario(tmp_path, monkeypatch, dirty=dirty) as case:
        case.change_on, case.change = change_on, {'free': 733}
        result = _run(case)
        assert result['status'] == 'repaired'
        assert case.acquisition_changes == [change_on]
        target_generation = 2 if stage in ('clean_enrichment', 'dirty_bridge') else 3
        assert case.con.execute('SELECT anchor_free_bytes FROM drive_clean_anchors '
                                'WHERE generation=?', [target_generation]).fetchone() == (733,)
        assert case.con.execute('SELECT anchor_free_bytes,identity_fingerprint FROM drive_clean_anchors '
                                'WHERE generation=?', [result['generation']]).fetchone() == (733, _FP)
        assert (True, change_on) in case.probes
        assert case.con.execute('PRAGMA user_version').fetchone() == (8,)
        assert not case.con.in_transaction


@pytest.mark.parametrize('dirty', [False, True])
def test_each_actual_publication_probes_inside_its_acquired_transaction(tmp_path, monkeypatch, dirty):
    with _scenario(tmp_path, monkeypatch, dirty=dirty) as case:
        assert _run(case)['status'] == 'repaired'
        expected = {1, 2} if dirty else {1}
        assert {number for inside, number in case.probes if inside} == expected
        assert case.actual_transactions == len(expected)
        assert case.clone_transactions >= len(expected)


@pytest.mark.parametrize('dirty', [False, True])
def test_catalog_rehearsal_never_calls_live_hardware_observer(tmp_path, monkeypatch, dirty):
    with _scenario(tmp_path, monkeypatch, dirty=dirty) as case:
        result = _run(case)
        assert result['status'] == 'repaired'
        assert case.clone_transactions == (2 if dirty else 1)
        assert case.probes  # observer asserts canonical connection on every call
