"""Fresh public Fill admission after repair reuses completed archive work.

Only low-level UUID/serial probes and filesystem geometry are injected. Retained
directory binding, volume observation, fingerprint/proof derivation, Preview,
approval, repair, and session projection use their real implementations. Start
stops at durable session admission; no transport worker is launched.
"""
import hashlib
import json
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap as bootstrap, drive_fence, execution_service, execution_session
from modelark import fetch, plan, proposal, register
from modelark.capacity_evidence import identity_fingerprint_v1
from test_drive_bootstrap import _ANX, _FS, _SER, _catalog, _dirty_gen, _proven_drive


CAPACITY = 10**12
FREE = CAPACITY - 1000
COMPLETE = 'qualification/completed'
PENDING = 'qualification/pending'
PAYLOAD = b'completed archive bytes must not be fetched again'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def _fingerprint(serial):
    return identity_fingerprint_v1(
        fs_uuid=_FS, annex_uuid=_ANX, serial=serial, filesystem_capacity_bytes=CAPACITY)


def _seed_catalog(con, *, pending):
    old = _fingerprint(None)
    _proven_drive(con, generation=1, fp=old, fscap=CAPACITY, free=FREE)
    _dirty_gen(con)
    proof = json.dumps({'v': 1, 'fs_uuid': _FS, 'annex_uuid': _ANX, 'serial': None})
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,"
        "fence_proof,observed_at) VALUES('drive-00',1,1,?, ?,?,'dedicated_local',?,?,"
        "'2026-09-09')", [FREE, CAPACITY, old, proof, proof])
    plan.create(con, 'ark', name='Qualification')
    plan.add_drive(con, 'ark', 'drive-00')
    plan.set_active(con, 'ark')
    for repo in ([COMPLETE, PENDING] if pending else [COMPLETE]):
        con.execute('INSERT INTO models(repo_id,numcopies) VALUES(?,1)', [repo])
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES(?,'model.safetensors',?,?,'safetensors')", [repo, len(PAYLOAD), DIGEST])
        con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-09-09')", [repo])


def _preview_approve(con, services):
    before = tuple(con.iterdump())
    preview = proposal.preview_pure(con, 'ark')
    assert tuple(con.iterdump()) == before
    assert preview['header']['gate_b_code'] == 'FEASIBLE', preview
    draft = proposal.publish_draft(con, preview)
    proposal_id = draft['proposal_id']
    proposal.approve(con, proposal_id, services=services)
    approved = proposal.load_proposal(con, proposal_id)
    assert approved['lifecycle'] == 'approved'
    assert proposal.hash_stored_proposal(con, proposal_id) == approved['canonical_hash']
    return approved


def _completed_archive(con, archive):
    path = archive / COMPLETE / 'model.safetensors'
    path.parent.mkdir(parents=True)
    path.write_bytes(PAYLOAD)
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_relpath,compressed,orig_bytes,"
        "stored_bytes,orig_sha256,orig_sha256_provenance) "
        "VALUES(?,'model.safetensors','drive-00','model.safetensors',0,?,?,?,'ingestion_computed')",
        [COMPLETE, len(PAYLOAD), len(PAYLOAD), DIGEST])
    con.execute(
        "INSERT INTO fetch_events(repo_id,event_at,outcome,bytes,detail) "
        "VALUES(?,'2026-09-09','archived',?,'completed by previous Fill attempt')",
        [COMPLETE, len(PAYLOAD)])
    return path


@pytest.mark.parametrize('pending', [False, True], ids=['all-complete', 'work-remains'])
def test_repair_then_fresh_public_preview_approve_start_preserves_completed_work(tmp_path, monkeypatch, pending):
    with _catalog(tmp_path) as con:
        _seed_catalog(con, pending=pending)
        monkeypatch.setattr(drive_fence, '_LOCK_DIR', tmp_path / 'locks')
        archive = tmp_path / 'archive'
        archive.mkdir()
        (archive / 'untouched.txt').write_bytes(b'unchanged archive sentinel')
        # The historical approval uses persisted clean-anchor evidence. No host
        # attachment is consulted or fabricated while creating that old history.
        monkeypatch.setattr(register, 'archive_path', lambda *_: None)
        services = execution_service.production_services(con)
        old = _preview_approve(con, proposal._DefaultServices())
        old_start = execution_service.start_fill(con=con, proposal_id=old['proposal_id'], services=services)
        assert isinstance(old_start, execution_session.SessionStart), old_start
        old_session = old_start.session
        assert {task['repo_id'] for task in old_start.projection.tasks} == (
            {COMPLETE, PENDING} if pending else {COMPLETE})
        assert all(task['row_kind'] == 'executable' for task in old['tasks'])

        # Materialize the historical completed result and owned dirty generation;
        # no real transport is dispatched by this session-admission qualification.
        path = _completed_archive(con, archive)
        _dirty_gen(con, gen=2, owner=old_session.session_id, token=old_session.fencing_token)
        con.execute('UPDATE drives SET write_generation=2')
        execution_session.terminalize(
            con, session_id=old_session.session_id, fencing_token=old_session.fencing_token,
            state='paused', terminal_code='DRIVE_RECONCILIATION_REQUIRED')
        old_row = con.execute('SELECT * FROM execution_sessions WHERE session_id=?',
                              [old_session.session_id]).fetchone()
        assert con.execute('SELECT state,expires_at,fencing_token FROM execution_sessions '
                           'WHERE session_id=?', [old_session.session_id]).fetchone() == (
            'paused', None, old_session.fencing_token)
        protected = {table: tuple(con.execute(f'SELECT * FROM {table}').fetchall())
                     for table in ('archived', 'files', 'fetch_events', 'replicas', 'models',
                                   'plans', 'plan_drives', 'selection')}
        old_tasks = tuple(con.execute('SELECT * FROM proposal_tasks WHERE proposal_id=?',
                                     [old['proposal_id']]).fetchall())
        old_files = tuple(con.execute('SELECT * FROM proposal_files WHERE proposal_id=?',
                                     [old['proposal_id']]).fetchall())
        generations = tuple(con.execute('SELECT * FROM drive_dirty_generations '
                                        'ORDER BY generation').fetchall())

        # Coarse low-level host probes only: BoundDirectory still opens/checks the
        # actual disposable directory, and both shared observation consumers
        # calculate their own identity hashes, proofs, and admission evidence.
        probes = dict(fs_uuid=0, annex_uuid=0, serial=0, geometry=0)

        def fs_uuid(observed_path):
            assert observed_path == archive
            probes['fs_uuid'] += 1
            return _FS

        def annex_uuid(pinned_path):
            assert str(pinned_path).startswith('/proc/') and '/fd/' in str(pinned_path)
            assert pinned_path.stat().st_ino == archive.stat().st_ino
            probes['annex_uuid'] += 1
            return _ANX

        def serial(observed_path):
            assert observed_path == archive
            probes['serial'] += 1
            return _SER

        def geometry(fd):
            actual = register.os.fstat(fd)
            expected = archive.stat()
            assert (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
            probes['geometry'] += 1
            return SimpleNamespace(f_blocks=CAPACITY, f_frsize=1, f_bavail=FREE)

        monkeypatch.setattr(register, 'archive_path', lambda *_: archive)
        monkeypatch.setattr(register, 'probe_fs_uuid', fs_uuid)
        monkeypatch.setattr(register, 'probe_annex_uuid', annex_uuid)
        monkeypatch.setattr(register, 'probe_serial', serial)
        monkeypatch.setattr(register.os, 'fstatvfs', geometry)
        computed = fetch.observe_for_admission(con, 'drive-00')
        assert computed.fingerprint == _fingerprint(_SER)
        assert computed.refusal_code == 'DRIVE_SERIAL_REPAIR_REQUIRED'
        assert json.loads(computed.identity_proof) == {
            'v': 1, 'fs_uuid': _FS, 'annex_uuid': _ANX, 'serial': _SER}
        live = proposal._DefaultServices(observe_live=lambda label: fetch.observe_for_admission(con, label))
        services.observe_exact_capacity = live.observe_exact_capacity
        binding = bootstrap.inspect_serial_identity(con, 'drive-00')['binding']
        repaired = bootstrap.repair_serial_identity(
            con, 'drive-00', expected_binding=binding, now='2026-09-09', writers_stopped=True)
        assert repaired['status'] == 'repaired'
        assert repaired['legacy_recovered'] is True
        assert repaired['superseded_approvals'] == (old['proposal_id'],)
        assert repaired['inventory_present'] == 1
        assert con.execute('PRAGMA user_version').fetchone() == (8,)

        for predecessor in (None, old_session.session_id):
            refused = execution_service.start_fill(
                con=con, proposal_id=old['proposal_id'], predecessor_id=predecessor, services=services)
            assert isinstance(refused, proposal.Refusal) and refused.code == 'APPROVAL_MISSING'

        fresh = _preview_approve(con, live)
        assert fresh['proposal_id'] != old['proposal_id']
        complete_rows = [task for task in fresh['tasks'] if task['repo_id'] == COMPLETE]
        assert len(complete_rows) == 1
        assert complete_rows[0]['row_kind'] == 'baseline_satisfied'
        assert complete_rows[0]['satisfying_drive'] == 'drive-00'
        assert complete_rows[0]['baseline_certificate']
        executable = [task for task in fresh['tasks'] if task['row_kind'] == 'executable']
        assert {task['repo_id'] for task in executable} == ({PENDING} if pending else set())

        # A newly active approval must not turn an explicit stale request into
        # permission to start that different proposal.
        before_refusals = tuple(con.iterdump())
        for requested in (old['proposal_id'], 'unknown-explicit-proposal'):
            for predecessor in (None, old_session.session_id):
                refused = execution_service.start_fill(
                    con=con, proposal_id=requested, predecessor_id=predecessor, services=services)
                assert isinstance(refused, proposal.Refusal), refused
                assert refused.code == 'APPROVAL_MISSING'
        assert tuple(con.iterdump()) == before_refusals

        # A different proposal is a new attempt, never an implicit successor Resume.
        refused_resume = execution_service.start_fill(
            con=con, proposal_id=fresh['proposal_id'], predecessor_id=old_session.session_id,
            services=services)
        assert isinstance(refused_resume, proposal.Refusal)
        assert refused_resume.code == 'RESUME_APPROVAL_MISMATCH'
        started = execution_service.start_fill(con=con, proposal_id=fresh['proposal_id'], services=services)
        assert isinstance(started, execution_session.SessionStart), started
        assert started.session.session_id != old_session.session_id
        assert started.session.fencing_token > old_session.fencing_token
        assert started.session.resumed_from_session_id is None
        assert started.session.approved_proposal_id == fresh['proposal_id']
        assert started.session.state == 'starting'
        assert {task['repo_id'] for task in started.projection.tasks} == ({PENDING} if pending else set())
        assert COMPLETE not in {task['repo_id'] for task in started.projection.tasks}
        assert len(con.execute('SELECT session_id FROM execution_sessions').fetchall()) == 2
        assert con.execute('SELECT * FROM execution_sessions WHERE session_id=?',
                           [old_session.session_id]).fetchone() == old_row
        assert tuple(con.execute('SELECT * FROM proposal_tasks WHERE proposal_id=?',
                                 [old['proposal_id']]).fetchall()) == old_tasks
        assert tuple(con.execute('SELECT * FROM proposal_files WHERE proposal_id=?',
                                 [old['proposal_id']]).fetchall()) == old_files
        assert tuple(con.execute('SELECT * FROM drive_dirty_generations '
                                 'ORDER BY generation').fetchall())[:2] == generations
        assert {table: tuple(con.execute(f'SELECT * FROM {table}').fetchall())
                for table in protected} == protected
        assert path.read_bytes() == PAYLOAD
        assert (archive / 'untouched.txt').read_bytes() == b'unchanged archive sentinel'
        assert probes['serial'] > 0
        assert probes['fs_uuid'] == probes['annex_uuid'] == probes['geometry'] == 2 * probes['serial']
        assert not con.in_transaction
