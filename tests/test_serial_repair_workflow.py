"""Explicit serial evidence repair uses disposable catalogs, never archive writes."""
import json
import sqlite3

import pytest

from modelark import drive_bootstrap as bootstrap
from modelark import drive_fence
from modelark.capacity_evidence import identity_fingerprint_v1
from test_drive_bootstrap import _catalog, _proven_drive, _dirty_gen, _FS, _ANX, _SER, _FP


OLD = identity_fingerprint_v1(fs_uuid=_FS, annex_uuid=_ANX, serial=None, filesystem_capacity_bytes=1000)
PROOF = json.dumps({'v': 1, 'fs_uuid': _FS, 'annex_uuid': _ANX, 'serial': None})


def seed(con, *, dirty=False, anchor=True, proof=PROOF, anchor_fp=OLD):
    _proven_drive(con, generation=2 if dirty else 1, fp=OLD)
    _dirty_gen(con)
    if anchor:
        con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,"
                    "anchor_free_bytes,filesystem_capacity_bytes,identity_fingerprint,write_authority,"
                    "identity_proof,fence_proof,observed_at) "
                    "VALUES('drive-00',1,1,900,1000,?,'dedicated_local',?,?,'2026-09-09')",
                    [anchor_fp, proof, proof])
    if dirty:
        _dirty_gen(con, gen=2)


@pytest.mark.parametrize('dirty', [False, True])
def test_inspection_is_read_only_and_binds_current_or_historical_proof(tmp_path, dirty):
    with _catalog(tmp_path) as con:
        seed(con, dirty=dirty)
        before = tuple(con.iterdump())
        report = bootstrap.inspect_serial_identity(con, 'drive-00')
        assert report['status'] == ('legacy_dirty' if dirty else 'legacy_clean')
        assert report['old_fingerprint'] == OLD
        assert report['canonical_fingerprint'] == _FP
        assert report['requires_live_verification'] is True
        assert report['affected_approvals'] == ()
        assert len(report['binding']) == 64
        assert tuple(con.iterdump()) == before
        assert not con.in_transaction
        assert bootstrap.inspect_serial_identity(con, 'drive-00') == report


@pytest.mark.parametrize('options,mutation', [
    ({'proof': '{}'}, None),
    ({'anchor': False}, None),
    ({}, "UPDATE drives SET serial=''"),
    ({}, "UPDATE drives SET lifecycle='lost'"),
    ({'anchor_fp': 'a' * 64}, None),
])
def test_inspection_refuses_unproven_legacy_state_without_changes(tmp_path, options, mutation):
    with _catalog(tmp_path) as con:
        seed(con, **options)
        if mutation:
            con.execute(mutation)
        before = tuple(con.iterdump())
        with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_UNPROVEN'):
            bootstrap.inspect_serial_identity(con, 'drive-00')
        assert tuple(con.iterdump()) == before
        assert not con.in_transaction


def test_inspection_does_not_end_callers_transaction(tmp_path):
    with _catalog(tmp_path) as con:
        seed(con)
        con.execute('BEGIN IMMEDIATE')
        con.execute("UPDATE drives SET eligibility='excluded'")
        bootstrap.inspect_serial_identity(con, 'drive-00')
        assert con.in_transaction
        con.execute('ROLLBACK')
        assert con.execute('SELECT eligibility FROM drives').fetchone() == ('enabled',)


def test_inspection_binding_changes_with_anchor_or_generation_evidence(tmp_path):
    with _catalog(tmp_path) as con:
        seed(con)
        before = bootstrap.inspect_serial_identity(con, 'drive-00')['binding']
        _dirty_gen(con, gen=2)
        con.execute('UPDATE drives SET write_generation=2')
        assert bootstrap.inspect_serial_identity(con, 'drive-00')['binding'] != before


def live_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(drive_fence, '_LOCK_DIR', tmp_path / 'locks')
    archive = tmp_path / 'archive'
    archive.mkdir()
    (archive / 'untouched.txt').write_bytes(b'unchanged archive sentinel')
    ev = bootstrap._LiveEvidence(str(archive), _FS, _ANX, _SER, 1000, 900, 4096, _FP, True)
    monkeypatch.setattr(bootstrap, '_live_evidence', lambda *_: ev)
    return archive


@pytest.mark.parametrize('dirty', [False, True])
def test_repair_rehearses_backs_up_and_preserves_old_history(tmp_path, monkeypatch, dirty):
    with _catalog(tmp_path) as con:
        seed(con, dirty=dirty)
        archive = live_setup(monkeypatch, tmp_path)
        before = tuple(con.iterdump())
        old_anchors = con.execute('SELECT * FROM drive_clean_anchors').fetchall()
        old_generations = con.execute('SELECT * FROM drive_dirty_generations').fetchall()
        intent = bootstrap.inspect_serial_identity(con, 'drive-00')
        milestones = []
        report = bootstrap.repair_serial_identity(
            con, 'drive-00', expected_binding=intent['binding'], now='2026-09-09',
            writers_stopped=True, progress=lambda e: milestones.append(e.phase))
        assert report['status'] == 'repaired'
        assert report['identity_epoch'] == 1
        assert report['generation'] == (3 if dirty else 2)
        assert report['legacy_recovered'] is dirty
        assert ('serial_legacy_recovered' in milestones) is dirty
        assert con.execute('PRAGMA user_version').fetchone() == (8,)
        assert con.execute('SELECT serial,identity_fingerprint FROM drives').fetchone() == (_SER, _FP)
        assert con.execute('SELECT * FROM drive_clean_anchors ORDER BY anchor_id').fetchall()[:1] == old_anchors
        assert con.execute('SELECT * FROM drive_dirty_generations ORDER BY generation').fetchall()[:-1] == old_generations
        assert (archive / 'untouched.txt').read_bytes() == b'unchanged archive sentinel'
        assert report['inventory_extra'] == 1
        with sqlite3.connect(report['backup']) as backup:
            assert tuple(backup.iterdump()) == before
            assert backup.execute('PRAGMA user_version').fetchone() == (7,)
        with sqlite3.connect(report['rehearsal']) as rehearsal:
            assert rehearsal.execute('PRAGMA user_version').fetchone() == (8,)
            assert rehearsal.execute('SELECT identity_fingerprint FROM drives').fetchone() == (_FP,)
        fresh = bootstrap.inspect_serial_identity(con, 'drive-00')
        after = tuple(con.iterdump())
        assert bootstrap.repair_serial_identity(
            con, 'drive-00', expected_binding=fresh['binding'], now='2026-09-09',
            writers_stopped=True)['status'] == 'already_correct'
        assert tuple(con.iterdump()) == after


def test_failed_enrichment_rolls_back_floor_and_identity(tmp_path, monkeypatch):
    with _catalog(tmp_path) as con:
        seed(con)
        live_setup(monkeypatch, tmp_path)
        before = tuple(con.iterdump())
        intent = bootstrap.inspect_serial_identity(con, 'drive-00')
        original = bootstrap.dm._publish_anchor_locked

        def fail_actual(connection, *args):
            result = original(connection, *args)
            if connection is con:
                raise RuntimeError('injected after anchor')
            return result

        monkeypatch.setattr(bootstrap.dm, '_publish_anchor_locked', fail_actual)
        with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_SERIAL_REPAIR_FAILED') as refused:
            bootstrap.repair_serial_identity(con, 'drive-00', expected_binding=intent['binding'],
                                             now='2026-09-09', writers_stopped=True)
        assert refused.value.evidence['legacy_recovered'] is False
        assert refused.value.evidence['reason'] == 'injected after anchor'
        assert refused.value.evidence['backup'].endswith('before.sqlite')
        assert tuple(con.iterdump()) == before
        assert con.execute('PRAGMA user_version').fetchone() == (7,)
        assert not con.in_transaction


@pytest.mark.parametrize('fingerprint', [OLD, _FP])
def test_repair_contends_on_either_legacy_or_canonical_key(tmp_path, monkeypatch, fingerprint):
    with _catalog(tmp_path) as con:
        seed(con)
        live_setup(monkeypatch, tmp_path)
        binding = bootstrap.inspect_serial_identity(con, 'drive-00')['binding']
        before = tuple(con.iterdump())
        with drive_fence.hold_drives_sorted([(fingerprint, 1)], blocking=False):
            with pytest.raises(bootstrap.DriveMutationRefused, match='DRIVE_FENCE_UNAVAILABLE'):
                bootstrap.repair_serial_identity(
                    con, 'drive-00', expected_binding=binding, now='2026-09-09', writers_stopped=True)
        assert tuple(con.iterdump()) == before
        assert not list(tmp_path.glob('.serial-repair-*'))


def test_inspection_captures_one_snapshot_across_concurrent_catalog_change(tmp_path, monkeypatch):
    from modelark import proposal

    with _catalog(tmp_path) as con:
        seed(con)
        original = bootstrap.inspect_serial_identity(con, 'drive-00')
        other = sqlite3.connect(tmp_path / 'catalog.sqlite', isolation_level=None)
        bound = proposal.approved_proposals_bound_to_drive
        changed = []

        def concurrent_change(connection, label):
            if not changed:
                other.execute('BEGIN IMMEDIATE')
                other.execute("UPDATE drives SET eligibility='excluded'")
                other.execute('PRAGMA user_version=9')
                other.execute('COMMIT')
                changed.append(True)
            return bound(connection, label)

        monkeypatch.setattr(proposal, 'approved_proposals_bound_to_drive', concurrent_change)
        try:
            assert bootstrap.inspect_serial_identity(con, 'drive-00') == original
            assert changed == [True]
            assert not con.in_transaction
            with pytest.raises(bootstrap.DriveMutationRefused, match='CATALOG_VERSION_UNSUPPORTED'):
                bootstrap.inspect_serial_identity(con, 'drive-00')
        finally:
            other.close()
