"""Adversarial serial repair on disposable catalogs and real isolated host fences."""
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap as bootstrap, execution_recovery, proposal
from modelark import drive_mutation as dm
from test_drive_bootstrap import _archived, _catalog, _catalogued, _dirty_gen, _FP, _SER
from test_serial_repair_workflow import OLD, PROOF, live_setup, seed


def _paused_owner(con):
    con.execute("INSERT INTO plans(plan_id) VALUES('ark')")
    con.execute(
        "INSERT INTO placement_proposals(proposal_id,plan_id,based_on_revision,lifecycle,"
        "canonical_hash,mutation_kind,serializer_version) "
        "VALUES('historical-proposal','ark',0,'superseded',?,'adopt_current','1')", ['a' * 64])
    con.execute(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
        "controller_identity,worker_identity,state,bound_planner_revision,fencing_token,"
        "expires_at,terminal_at,terminal_code,terminal_evidence) "
        "VALUES('paused-owner','ark','historical-proposal','controller','worker','paused',0,7,"
        "NULL,'2026-09-09','DRIVE_RECONCILIATION_REQUIRED','retained evidence')")
    con.execute("UPDATE drive_dirty_generations SET owner_session_id='paused-owner',"
                "owner_fencing_token=7 WHERE generation=2")


@pytest.fixture
def case(tmp_path, monkeypatch):
    with _catalog(tmp_path) as con:
        seed(con, dirty=True)
        _paused_owner(con)
        archive = live_setup(monkeypatch, tmp_path)
        yield SimpleNamespace(con=con, archive=archive, root=tmp_path)


def _intent(case):
    return bootstrap.inspect_serial_identity(case.con, 'drive-00')['binding']


def _run(case, binding=None, progress=None):
    return bootstrap.repair_serial_identity(
        case.con, 'drive-00', expected_binding=binding or _intent(case), now='2026-09-09',
        writers_stopped=True, progress=progress)


def _history(con):
    return (con.execute('SELECT * FROM execution_sessions').fetchall(),
            con.execute('SELECT * FROM drive_dirty_generations ORDER BY generation').fetchall())


def _append_anchor(con, *, epoch=1, generation=2):
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,"
        "fence_proof,observed_at) VALUES('drive-00',?,?,900,1000,?,'dedicated_local',?,?,"
        "'2026-09-09')", [epoch, generation, OLD, PROOF, PROOF])


def test_paused_no_expiry_owner_and_prior_generations_remain_history(case):
    sessions, generations = _history(case.con)
    old_anchor = case.con.execute('SELECT * FROM drive_clean_anchors').fetchall()
    result = _run(case)
    assert result['legacy_recovered'] is True
    assert result['generation'] == 3
    assert case.con.execute('SELECT * FROM execution_sessions').fetchall() == sessions
    assert case.con.execute('SELECT state,expires_at,fencing_token,terminal_code '
                            'FROM execution_sessions').fetchone() == (
        'paused', None, 7, 'DRIVE_RECONCILIATION_REQUIRED')
    assert case.con.execute('SELECT * FROM drive_dirty_generations '
                            'ORDER BY generation').fetchall()[:2] == generations
    assert case.con.execute('SELECT owner_session_id,owner_fencing_token '
                            'FROM drive_dirty_generations WHERE generation=3').fetchone() == (None, None)
    assert case.con.execute('SELECT * FROM drive_clean_anchors ORDER BY generation').fetchall()[:1] == old_anchor
    bridge = case.con.execute('SELECT identity_fingerprint,identity_proof,fence_proof '
                              'FROM drive_clean_anchors WHERE generation=2').fetchone()
    assert bridge[0] == OLD
    assert json.loads(bridge[1]) == json.loads(PROOF)
    assert json.loads(bridge[2])['serial'] == _SER
    assert case.con.execute('SELECT identity_fingerprint FROM drive_clean_anchors '
                            'WHERE generation=3').fetchone() == (_FP,)
    assert (case.archive / 'untouched.txt').read_bytes() == b'unchanged archive sentinel'


def test_real_child_marker_blocks_owned_dirty_bridge_without_changes(case):
    binding = _intent(case)
    before = tuple(case.con.iterdump())
    handles = execution_recovery.inherit_drive_fence_fds(
        session_id='paused-owner', drive_labels=[], marker_only=True)
    assert handles
    try:
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECOVERY_CHILD_UNPROVEN'):
            _run(case, binding)
    finally:
        execution_recovery.release_child_fences('paused-owner')
    assert tuple(case.con.iterdump()) == before
    assert not list(case.root.glob('.serial-repair-*'))


@pytest.mark.parametrize('state', ['starting', 'running', 'stopping'])
def test_live_owner_refuses_before_any_repair_or_backup(case, state):
    case.con.execute('UPDATE execution_sessions SET state=?', [state])
    binding = _intent(case)
    before = tuple(case.con.iterdump())
    with pytest.raises(proposal.Refusal, match='FILL_SESSION_ACTIVE'):
        _run(case, binding)
    assert tuple(case.con.iterdump()) == before
    assert not list(case.root.glob('.serial-repair-*'))


@pytest.mark.parametrize('change', ['missing', 'malformed', 'duplicate', 'serial_present', 'other_epoch'])
def test_dirty_bridge_cannot_guess_from_hash_without_valid_same_epoch_proof(tmp_path, monkeypatch, change):
    with _catalog(tmp_path) as con:
        proof = {
            'malformed': '{}',
            'duplicate': PROOF[:-1] + ', "serial": null}',
            'serial_present': json.dumps({**json.loads(PROOF), 'serial': _SER}),
        }.get(change, PROOF)
        # Seed the historical evidence directly; production append-only guards
        # correctly forbid corrupting or deleting an existing anchor afterward.
        seed(con, dirty=True, anchor=change not in ('missing', 'other_epoch'), proof=proof)
        if change == 'other_epoch':
            _dirty_gen(con, epoch=2, gen=1)
            _append_anchor(con, epoch=2, generation=1)
        _paused_owner(con)
        archive = live_setup(monkeypatch, tmp_path)
        case = SimpleNamespace(con=con, archive=archive, root=tmp_path)
        before = tuple(con.iterdump())
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_UNPROVEN'):
            bootstrap.inspect_serial_identity(con, 'drive-00')
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_UNPROVEN'):
            _run(case, '0' * 64)
        assert tuple(con.iterdump()) == before
        assert not list(case.root.glob('.serial-repair-*'))


@pytest.mark.parametrize('change', [
    'append_current_anchor',
    "UPDATE execution_sessions SET terminal_evidence='changed owner evidence'",
    "UPDATE execution_sessions SET fencing_token=8",
    "UPDATE drives SET eligibility='excluded'",
])
def test_operator_binding_rejects_changed_anchor_owner_or_drive_facts(case, change):
    binding = _intent(case)
    if change == 'append_current_anchor':
        _append_anchor(case.con)
    else:
        case.con.execute(change)
    before = tuple(case.con.iterdump())
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_STALE'):
        _run(case, binding)
    assert tuple(case.con.iterdump()) == before
    assert not list(case.root.glob('.serial-repair-*'))


@pytest.mark.parametrize('failure', [dm.DriveMutationRefused, RuntimeError])
def test_failure_after_actual_bridge_preserves_clean_old_identity_and_retry(case, failure):
    sessions, generations = _history(case.con)
    revision = case.con.execute('SELECT planner_revision FROM planner_state').fetchone()[0]
    binding = _intent(case)
    reached = []

    def fail_after_bridge(event):
        if event.phase == 'serial_legacy_recovered':
            assert not case.con.in_transaction
            assert case.con.execute('PRAGMA user_version').fetchone() == (7,)
            reached.append(event.phase)
            raise failure('INJECTED_AFTER_BRIDGE')

    with pytest.raises(dm.DriveMutationRefused) as raised:
        _run(case, binding, progress=fail_after_bridge)
    assert reached == ['serial_legacy_recovered']
    assert raised.value.evidence['legacy_recovered'] is True
    assert raised.value.evidence['backup']
    assert raised.value.evidence['rehearsal']
    assert _history(case.con) == (sessions, generations)
    assert case.con.execute('SELECT identity_epoch,write_generation,identity_fingerprint '
                            'FROM drives').fetchone() == (1, 2, OLD)
    assert case.con.execute('SELECT generation,identity_fingerprint FROM drive_clean_anchors '
                            'ORDER BY generation').fetchall() == [(1, OLD), (2, OLD)]
    assert case.con.execute('PRAGMA user_version').fetchone() == (7,)
    assert case.con.execute('SELECT planner_revision FROM planner_state').fetchone() == (revision + 1,)
    assert bootstrap.inspect_serial_identity(case.con, 'drive-00')['status'] == 'legacy_clean'
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_STALE'):
        _run(case, binding)
    result = _run(case)
    assert result['generation'] == 3
    assert result['legacy_recovered'] is False
    assert case.con.execute('PRAGMA user_version').fetchone() == (8,)
    assert case.con.execute('SELECT * FROM execution_sessions').fetchall() == sessions


@pytest.mark.parametrize('column,value', [('fencing_token', 8), ('terminal_evidence', 'raced history')])
def test_bridge_progress_callback_cannot_replace_the_captured_owner_binding(case, column, value):
    binding = _intent(case)
    generations = case.con.execute('SELECT * FROM drive_dirty_generations ORDER BY generation').fetchall()
    reached = []

    def change_owner(event):
        if event.phase == 'serial_legacy_recovered':
            assert not case.con.in_transaction
            case.con.execute(f'UPDATE execution_sessions SET {column}=?', [value])
            reached.append(True)

    with pytest.raises(dm.DriveMutationRefused) as raised:
        _run(case, binding, progress=change_owner)
    assert reached == [True]
    assert raised.value.code in ('DRIVE_SERIAL_REPAIR_STALE', 'DRIVE_RECOVERY_OWNER_CHANGED')
    assert raised.value.evidence['legacy_recovered'] is True
    assert raised.value.evidence['backup']
    assert raised.value.evidence['rehearsal']
    assert case.con.execute('PRAGMA user_version').fetchone() == (7,)
    assert case.con.execute('SELECT identity_epoch,write_generation,identity_fingerprint '
                            'FROM drives').fetchone() == (1, 2, OLD)
    assert case.con.execute('SELECT generation,identity_fingerprint FROM drive_clean_anchors '
                            'ORDER BY generation').fetchall() == [(1, OLD), (2, OLD)]
    assert case.con.execute('SELECT * FROM drive_dirty_generations ORDER BY generation').fetchall() == generations
    assert case.con.execute(f'SELECT {column} FROM execution_sessions').fetchone() == (value,)
    assert not case.con.in_transaction


@pytest.mark.parametrize('point', ['dirty', 'fingerprint', 'anchor', 'approvals', 'floor'])
def test_each_enrichment_write_point_rolls_back_entire_real_transaction(tmp_path, monkeypatch, point):
    with _catalog(tmp_path) as con:
        seed(con)
        archive = live_setup(monkeypatch, tmp_path)
        case = SimpleNamespace(con=con, archive=archive, root=tmp_path)
        binding = _intent(case)
        before = tuple(con.iterdump())
        hit = []
        if point == 'dirty':
            owner, name = dm, '_advance_one'
        elif point in ('fingerprint', 'anchor'):
            owner, name = dm, '_publish_anchor_locked'
        elif point == 'approvals':
            owner, name = proposal, 'supersede_serial_repair_approvals'
        else:
            owner, name = proposal, 'bump_revision'
        original = getattr(owner, name)

        def fail(connection, *args, **kwargs):
            if connection is con and point == 'fingerprint':
                assert con.execute('SELECT identity_fingerprint FROM drives').fetchone() == (_FP,)
                hit.append(point)
                raise RuntimeError('injected at ' + point)
            result = original(connection, *args, **kwargs)
            if connection is con:
                assert con.in_transaction
                if point == 'floor':
                    assert con.execute('PRAGMA user_version').fetchone() == (8,)
                hit.append(point)
                raise RuntimeError('injected at ' + point)
            return result

        monkeypatch.setattr(owner, name, fail)
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_FAILED') as raised:
            _run(case, binding)
        assert raised.value.evidence['reason'] == 'injected at ' + point
        assert raised.value.evidence['legacy_recovered'] is False
        assert raised.value.evidence['backup']
        assert raised.value.evidence['rehearsal']
        assert hit == [point]
        assert tuple(con.iterdump()) == before
        assert con.execute('PRAGMA user_version').fetchone() == (7,)
        assert not con.in_transaction
        assert (archive / 'untouched.txt').read_bytes() == b'unchanged archive sentinel'


def test_missing_real_inventory_claim_leaves_original_owned_dirty_state(case):
    _catalogued(case.con, 'org/missing', 'weights.bin')
    _archived(case.con, 'org/missing', 'weights.bin', 'weights.bin', 'drive-00')
    binding = _intent(case)
    before = tuple(case.con.iterdump())
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECONCILIATION_REQUIRED') as raised:
        _run(case, binding)
    assert raised.value.evidence['missing'] == 1
    assert raised.value.evidence['legacy_recovered'] is False
    assert tuple(case.con.iterdump()) == before
    assert not list(case.root.glob('.serial-repair-*'))
    assert not case.con.in_transaction


@pytest.mark.parametrize('failure', ['backup_open', 'rehearsal_write',
                                   'backup_fsync', 'rehearsal_fsync', 'directory_fsync', 'parent_fsync'])
def test_backup_rehearsal_or_durability_failure_cannot_change_canonical_catalog(case, monkeypatch, failure):
    binding = _intent(case)
    before = tuple(case.con.iterdump())
    hit = []
    if failure == 'backup_open':
        original = bootstrap.sqlite3.connect

        def connect(path, *args, **kwargs):
            if str(path).endswith('/before.sqlite'):
                hit.append(failure)
                raise sqlite3.OperationalError('injected backup open failure')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(bootstrap.sqlite3, 'connect', connect)
    elif failure == 'rehearsal_write':
        original = bootstrap._serial_enrich_locked

        def enrich(connection, *args, **kwargs):
            result = original(connection, *args, **kwargs)
            if connection is not case.con:
                hit.append(failure)
                raise RuntimeError('injected rehearsal write failure')
            return result
        monkeypatch.setattr(bootstrap, '_serial_enrich_locked', enrich)
    else:
        original = bootstrap.os.fsync
        target = {'backup_fsync': 1, 'rehearsal_fsync': 2,
                  'directory_fsync': 3, 'parent_fsync': 4}[failure]
        calls = []

        def fsync(fd):
            calls.append(fd)
            if len(calls) == target:
                hit.append(failure)
                raise OSError('injected ' + failure)
            return original(fd)
        monkeypatch.setattr(bootstrap.os, 'fsync', fsync)
    with pytest.raises(dm.DriveMutationRefused) as raised:
        _run(case, binding)
    assert hit == [failure]
    assert raised.value.evidence['legacy_recovered'] is False
    backup = Path(raised.value.evidence['backup'])
    rehearsal = Path(raised.value.evidence['rehearsal'])
    assert backup.name == 'before.sqlite'
    assert rehearsal.name == 'rehearsal.sqlite'
    assert backup.parent == rehearsal.parent
    assert backup.parent.parent == case.root
    assert backup.parent.is_dir()
    # These are attempted artifact paths, not claims of validity or durability.
    if failure == 'backup_open':
        assert not backup.exists()
    assert tuple(case.con.iterdump()) == before
    assert case.con.execute('PRAGMA user_version').fetchone() == (7,)
    assert not case.con.in_transaction
    assert (case.archive / 'untouched.txt').read_bytes() == b'unchanged archive sentinel'


@pytest.mark.parametrize('phase', ['inventory', 'before_begin'])
@pytest.mark.parametrize('change', [
    "UPDATE drives SET serial='different-serial'",
    "UPDATE drives SET fs_uuid='different-fs'",
    "UPDATE drives SET annex_uuid='different-annex'",
    "UPDATE drives SET eligibility='excluded'",
    "UPDATE execution_sessions SET terminal_evidence='changed during repair'",
])
def test_stale_facts_during_inventory_or_before_real_transaction_cannot_publish(case, monkeypatch, phase, change):
    binding = _intent(case)
    changed_dump = []

    def mutate():
        assert not case.con.in_transaction
        case.con.execute(change)
        changed_dump.append(tuple(case.con.iterdump()))

    def progress(event):
        if phase == 'inventory' and event.phase == 'filesystem_scan_started':
            mutate()

    if phase == 'before_begin':
        original = dm._immediate

        def immediate(connection, body):
            if connection is case.con:
                mutate()
            return original(connection, body)
        monkeypatch.setattr(dm, '_immediate', immediate)
    with pytest.raises(dm.DriveMutationRefused) as raised:
        _run(case, binding, progress=progress)
    assert len(changed_dump) == 1
    assert raised.value.code in ('DRIVE_SERIAL_REPAIR_STALE', 'DRIVE_SERIAL_REPAIR_UNPROVEN')
    assert raised.value.evidence['legacy_recovered'] is False
    assert tuple(case.con.iterdump()) == changed_dump[0]
    assert case.con.execute('PRAGMA user_version').fetchone() == (7,)
    assert not case.con.in_transaction


@pytest.mark.parametrize('call_number', [1, 2, 4], ids=['initial', 'after-inventory', 'after-bridge'])
@pytest.mark.parametrize('field,value', [('serial', 'replacement-serial'), ('fs_uuid', 'replacement-fs'),
                                       ('annex_uuid', 'replacement-annex'), ('capacity', 2000)])
def test_fresh_live_identity_mismatch_never_enriches(case, monkeypatch, call_number, field, value):
    binding = _intent(case)
    before = tuple(case.con.iterdump())
    sessions, generations = _history(case.con)
    original = bootstrap._live_evidence
    calls = []

    def observe(*args):
        observed = original(*args)
        calls.append(True)
        return replace(observed, **{field: value}) if len(calls) == call_number else observed

    monkeypatch.setattr(bootstrap, '_live_evidence', observe)
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_LIVE_MISMATCH') as raised:
        _run(case, binding)
    assert len(calls) == call_number
    assert raised.value.evidence['legacy_recovered'] is (call_number == 4)
    assert case.con.execute('PRAGMA user_version').fetchone() == (7,)
    assert case.con.execute('SELECT identity_epoch,write_generation,identity_fingerprint '
                            'FROM drives').fetchone() == (1, 2, OLD)
    assert _history(case.con) == (sessions, generations)
    if call_number == 4:
        assert case.con.execute('SELECT generation,identity_fingerprint FROM drive_clean_anchors '
                                'ORDER BY generation').fetchall() == [(1, OLD), (2, OLD)]
    else:
        assert tuple(case.con.iterdump()) == before
    assert not case.con.in_transaction
    assert (case.archive / 'untouched.txt').read_bytes() == b'unchanged archive sentinel'
