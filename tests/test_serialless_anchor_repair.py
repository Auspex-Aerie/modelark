"""Repair old observed-serial anchors without enriching serial-less registration."""
import json
import sqlite3
from dataclasses import replace

import pytest

from modelark import drive_bootstrap as bootstrap, drive_fence
from modelark.drive_identity import FenceIdentity
from test_drive_bootstrap import _catalog, _dirty_gen, _FS, _ANX, _SER, _FP
from test_serial_repair_workflow import OLD, seed, live_setup


def seed_observed(con, *, dirty=False, anchor_changes=None):
    proof = json.dumps({'v': 1, 'fs_uuid': _FS, 'annex_uuid': _ANX, 'serial': _SER})
    seed(con, dirty=dirty, anchor=False)
    con.execute('UPDATE drives SET serial=NULL,identity_fingerprint=?', [_FP])
    values = dict(generation=1, filesystem_capacity_bytes=1000, identity_fingerprint=_FP,
                  identity_proof=proof, fence_proof=proof)
    if anchor_changes is not None:
        values.update(anchor_changes)
    if values['generation'] != 1:
        _dirty_gen(con, gen=values['generation'])
    con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,"
                "anchor_free_bytes,filesystem_capacity_bytes,identity_fingerprint,write_authority,"
                "identity_proof,fence_proof,observed_at) "
                "VALUES('drive-00',1,:generation,900,:filesystem_capacity_bytes,:identity_fingerprint,"
                "'dedicated_local',:identity_proof,:fence_proof,'2026-09-11')", values)


def setup_live(monkeypatch, tmp_path):
    archive = live_setup(monkeypatch, tmp_path)
    ev = bootstrap._LiveEvidence(str(archive), _FS, _ANX, _SER, 1000, 900, 4096,
                                 OLD, True, serial_in_identity=False)
    monkeypatch.setattr(bootstrap, '_live_evidence', lambda *_: ev)
    return archive, ev


def run(con, binding):
    return bootstrap.repair_serial_identity(con, 'drive-00', expected_binding=binding,
                                           now='2026-09-11', writers_stopped=True)


def test_backup_rehearsal_repair_and_noop_keep_serial_optional(tmp_path, monkeypatch):
    with _catalog(tmp_path) as con:
        seed_observed(con)
        archive, _ = setup_live(monkeypatch, tmp_path)
        before = tuple(con.iterdump())
        anchors = con.execute('SELECT * FROM drive_clean_anchors').fetchall()
        intent = bootstrap.inspect_serial_identity(con, 'drive-00')
        assert intent['status'] == 'observed_serial_clean'
        assert intent['old_fingerprint'] == _FP
        assert intent['canonical_fingerprint'] == OLD
        assert intent['required_live_serial'] == _SER
        assert tuple(con.iterdump()) == before
        result = run(con, intent['binding'])
        assert result['status'] == 'repaired'
        assert result['legacy_recovered'] is False
        assert con.execute('SELECT serial,identity_epoch,write_generation,identity_fingerprint '
                           'FROM drives').fetchone() == (None, 1, 2, OLD)
        assert con.execute('SELECT * FROM drive_clean_anchors ORDER BY generation').fetchall()[:1] == anchors
        proof, fence = con.execute('SELECT identity_proof,fence_proof FROM drive_clean_anchors '
                                   'WHERE generation=2').fetchone()
        assert json.loads(proof)['serial'] is None
        assert json.loads(fence)['serial'] == _SER
        with sqlite3.connect(result['backup']) as backup:
            assert tuple(backup.iterdump()) == before
        with sqlite3.connect(result['rehearsal']) as rehearsal:
            assert rehearsal.execute('SELECT serial,identity_fingerprint FROM drives').fetchone() == (None, OLD)
        assert (archive / 'untouched.txt').read_bytes() == b'unchanged archive sentinel'
        fresh = bootstrap.inspect_serial_identity(con, 'drive-00')
        before_noop = tuple(con.iterdump())
        assert run(con, fresh['binding'])['status'] == 'already_correct'
        assert tuple(con.iterdump()) == before_noop


@pytest.mark.parametrize('changes,mutation', [
    ({}, "UPDATE drives SET serial=''"),
    ({}, "UPDATE drives SET lifecycle='lost'"),
    ({'identity_proof': '{}'}, None),
    ({'fence_proof': '{}'}, None),
    ({'generation': 2}, None),
    ({'filesystem_capacity_bytes': 2000}, None),
    ({'identity_fingerprint': 'f' * 64}, None),
])
def test_unproven_or_contradictory_saved_evidence_refuses(tmp_path, changes, mutation):
    with _catalog(tmp_path) as con:
        seed_observed(con, anchor_changes=changes)
        if mutation is not None:
            con.execute(mutation)
        before = tuple(con.iterdump())
        with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_UNPROVEN'):
            bootstrap.inspect_serial_identity(con, 'drive-00')
        assert tuple(con.iterdump()) == before


def test_dirty_inverse_case_is_not_implicitly_extended(tmp_path):
    with _catalog(tmp_path) as con:
        seed_observed(con, dirty=True)
        with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_UNPROVEN'):
            bootstrap.inspect_serial_identity(con, 'drive-00')


@pytest.mark.parametrize('field,value', [('serial', None), ('serial', 'other'),
                                       ('fs_uuid', 'other'), ('annex_uuid', 'other'),
                                       ('capacity', 2000), ('proven', False)])
@pytest.mark.parametrize('at_commit', [False, True])
def test_live_change_including_after_sqlite_acquisition_refuses(tmp_path, monkeypatch, field, value, at_commit):
    with _catalog(tmp_path) as con:
        seed_observed(con)
        _, ev = setup_live(monkeypatch, tmp_path)
        intent = bootstrap.inspect_serial_identity(con, 'drive-00')
        before = tuple(con.iterdump())
        monkeypatch.setattr(bootstrap, '_live_evidence', lambda *_:
                            replace(ev, **{field: value}) if not at_commit or con.in_transaction else ev)
        with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_LIVE_MISMATCH'):
            run(con, intent['binding'])
        assert tuple(con.iterdump()) == before


@pytest.mark.parametrize('fingerprint', [_FP, OLD])
def test_both_physical_aliases_exclude_repair(tmp_path, monkeypatch, fingerprint):
    with _catalog(tmp_path) as con:
        seed_observed(con)
        setup_live(monkeypatch, tmp_path)
        intent = bootstrap.inspect_serial_identity(con, 'drive-00')
        before = tuple(con.iterdump())
        with drive_fence.hold_drives_sorted([(fingerprint, 1)]):
            with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_FENCE_UNAVAILABLE'):
                run(con, intent['binding'])
        assert tuple(con.iterdump()) == before


def test_ordinary_identity_fences_still_refuse_inconsistent_serialless_row():
    with pytest.raises(ValueError, match='fingerprint does not bind'):
        FenceIdentity(_FS, _ANX, None, 1000, 1, _FP).lock_keys()


@pytest.mark.parametrize('failure', ['inventory', 'backup', 'publication'])
def test_failure_preserves_catalog_and_history(tmp_path, monkeypatch, failure):
    with _catalog(tmp_path) as con:
        seed_observed(con)
        setup_live(monkeypatch, tmp_path)
        intent = bootstrap.inspect_serial_identity(con, 'drive-00')
        before = tuple(con.iterdump())
        if failure == 'publication':
            original = bootstrap.dm._publish_anchor_locked

            def fail(connection, *args):
                original(connection, *args)
                if connection is con:
                    raise RuntimeError('injected publication failure')

            monkeypatch.setattr(bootstrap.dm, '_publish_anchor_locked', fail)
        else:
            def fail(*args, **kwargs):
                raise RuntimeError('injected ' + failure + ' failure')

            target = '_require_complete_inventory' if failure == 'inventory' else '_serial_backup_rehearsal'
            monkeypatch.setattr(bootstrap, target, fail)
        with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_FAILED'):
            run(con, intent['binding'])
        assert tuple(con.iterdump()) == before
        assert not con.in_transaction


def test_stale_binding_and_quiescence_required_before_backup(tmp_path, monkeypatch):
    with _catalog(tmp_path) as con:
        seed_observed(con)
        setup_live(monkeypatch, tmp_path)
        intent = bootstrap.inspect_serial_identity(con, 'drive-00')
        con.execute("UPDATE drives SET eligibility='excluded'")
        before = tuple(con.iterdump())
        with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_STALE'):
            run(con, intent['binding'])
        with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_QUIESCENCE_REQUIRED'):
            bootstrap.repair_serial_identity(con, 'drive-00', expected_binding=intent['binding'], now='now')
        assert tuple(con.iterdump()) == before
        assert not list(tmp_path.glob('.serial-repair-*'))


def test_operator_cli_inspect_and_repair(tmp_path, monkeypatch, capsys):
    from modelark import cli
    from test_serial_repair_cli import args
    with _catalog(tmp_path) as con:
        seed_observed(con)
        setup_live(monkeypatch, tmp_path)
        cli.cmd_drive_reconcile(args(inspect_serial_identity=True))
        intent = json.loads(capsys.readouterr().out)
        assert intent['status'] == 'observed_serial_clean'
        cli.cmd_drive_reconcile(args(repair_serial_identity=True, writers_stopped=True,
                                     expected_binding=intent['binding']))
        result = json.loads(capsys.readouterr().out)
        assert result['canonical_fingerprint'] == OLD
        assert result['observed_serial'] == _SER


@pytest.mark.parametrize('binding', ['source_drive', 'target_drive', 'satisfying_drive'])
def test_affected_approvals_superseded_without_rewriting_tasks(tmp_path, monkeypatch, binding):
    from test_serial_repair_approvals import seed_proposal
    with _catalog(tmp_path) as con:
        seed_observed(con)
        setup_live(monkeypatch, tmp_path)
        con.execute("INSERT INTO plans(plan_id) VALUES('ark')")
        con.execute("INSERT INTO models(repo_id) VALUES('org/a')")
        seed_proposal(con, 'affected', binding=binding, drive='drive-00')
        con.execute("UPDATE planner_state SET active_approved_proposal_id='affected'")
        immutable = con.execute('SELECT * FROM proposal_tasks').fetchall()
        intent = bootstrap.inspect_serial_identity(con, 'drive-00')
        assert intent['affected_approvals'] == ('affected',)
        result = run(con, intent['binding'])
        assert result['superseded_approvals'] == ('affected',)
        assert con.execute('SELECT lifecycle FROM placement_proposals').fetchone() == ('superseded',)
        assert con.execute('SELECT active_approved_proposal_id FROM planner_state').fetchone() == (None,)
        assert con.execute('SELECT * FROM proposal_tasks').fetchall() == immutable
