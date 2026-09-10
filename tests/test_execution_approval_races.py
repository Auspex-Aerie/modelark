"""Start's approval is live authority, not a pre-controller cached object."""
from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace

import pytest

from modelark import drive_bootstrap, drive_fence, execution_service, execution_session
from modelark import proposal, register
from modelark.core import db
from test_drive_bootstrap import _ANX, _FS, _SER, _catalog
from test_serial_repair_public_fill import CAPACITY, FREE, _preview_approve, _seed_catalog


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, '_LOCK_DIR', tmp_path / 'locks')
    monkeypatch.setattr(register, 'archive_path', lambda *_: None)
    with _catalog(tmp_path) as con:
        _seed_catalog(con, pending=True)
        approved = _preview_approve(con, proposal._DefaultServices())
        services = execution_service.production_services(con)
        started = execution_service.start_fill(con=con, proposal_id=approved['proposal_id'], services=services)
        assert isinstance(started, execution_session.SessionStart)
        execution_session.terminalize(con, session_id=started.session.session_id,
                                     fencing_token=started.session.fencing_token,
                                     state='paused', terminal_code='preserve history')
        other = sqlite3.connect(db.DB_PATH, isolation_level=None)
        other.execute('PRAGMA foreign_keys=ON')
        try:
            yield SimpleNamespace(con=con, other=other, services=services,
                                  pid=approved['proposal_id'], predecessor=started.session.session_id,
                                  tmp=tmp_path)
        finally:
            other.close()


def _start(case, entry, selected, predecessor=None, connection=None):
    connection = case.con if connection is None else connection
    try:
        if entry == 'session':
            return execution_session.start_session(connection, selected, predecessor, case.services)
        return execution_service.start_fill(con=connection, proposal_id=selected,
                                            predecessor_id=predecessor, services=case.services)
    except proposal.Refusal as refused:
        return refused


def _inject(case, monkeypatch, phase, change):
    if phase == 'controller':
        original = case.services.controller_flock.hold

        @contextmanager
        def hold():
            change()
            with original():
                yield
        monkeypatch.setattr(case.services.controller_flock, 'hold', hold)
    elif phase == 'physical_fence':
        original = case.services.drive_fences.hold_all_sorted

        @contextmanager
        def hold(labels):
            change()
            with original(labels) as binding:
                yield binding
        monkeypatch.setattr(case.services.drive_fences, 'hold_all_sorted', hold)
    else:
        class BeforeBegin:
            def __getattr__(self, name):
                return getattr(case.con, name)

            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE':
                    change()
                return case.con.execute(sql, *args)
        return BeforeBegin()
    return case.con


@pytest.mark.parametrize('entry', ['session', 'service'])
@pytest.mark.parametrize('omitted', [False, True])
@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('phase', ['controller', 'physical_fence', 'before_begin'])
@pytest.mark.parametrize('change_kind', ['lifecycle', 'active_pointer'])
def test_approval_revocation_cannot_allocate_token_or_session(
    case, monkeypatch, entry, omitted, resume, phase, change_kind,
):
    changed = []

    def change():
        assert not case.con.in_transaction
        # Deliberate independent durable write: exercise publication CAS even
        # when a writer omits the controller protocol. No authority fixture mocks.
        if change_kind == 'lifecycle':
            case.other.execute("UPDATE placement_proposals SET lifecycle='superseded' WHERE proposal_id=?",
                               [case.pid])
        else:
            case.other.execute('UPDATE planner_state SET active_approved_proposal_id=NULL WHERE singleton_id=1')
        changed.append(tuple(case.other.iterdump()))

    connection = _inject(case, monkeypatch, phase, change)
    result = _start(case, entry, None if omitted else case.pid,
                    case.predecessor if resume else None, connection)
    assert isinstance(result, proposal.Refusal), result
    assert result.code == 'APPROVAL_MISSING'
    assert len(changed) == 1
    assert tuple(case.con.iterdump()) == changed[0]
    assert not case.con.in_transaction


@pytest.mark.parametrize('entry', ['session', 'service'])
@pytest.mark.parametrize('omitted', [False, True])
def test_public_approval_replacement_before_controller_never_starts_old_selection(case, monkeypatch, entry, omitted):
    changed = []

    def replace():
        # Actual public Preview/Approve completes before Start acquires controller.
        fresh = _preview_approve(case.other, proposal._DefaultServices())
        changed.append((fresh['proposal_id'], tuple(case.other.iterdump())))

    _inject(case, monkeypatch, 'controller', replace)
    result = _start(case, entry, None if omitted else case.pid)
    assert len(changed) == 1
    if omitted:
        assert isinstance(result, execution_session.SessionStart), result
        assert result.session.approved_proposal_id == changed[0][0]
        assert result._proposal['proposal_id'] == changed[0][0]
    else:
        assert isinstance(result, proposal.Refusal), result
        assert result.code == 'APPROVAL_MISSING'
        assert tuple(case.con.iterdump()) == changed[0][1]


@pytest.mark.parametrize('entry', ['session', 'service'])
@pytest.mark.parametrize('omitted', [False, True])
def test_actual_serial_repair_before_controller_revokes_executable_only_proposal(case, monkeypatch, entry, omitted):
    # A configured caller can keep exactly the same valid execution settings
    # after repair. Do not let the default reader's cleared-pointer fallback
    # incidentally hide stale proposal authority behind a config mismatch.
    config = case.services.config.read_graph_affecting_config()
    monkeypatch.setattr(case.services.config, 'read_graph_affecting_config', lambda: dict(config))
    archive = case.tmp / 'archive'
    archive.mkdir()
    sentinel = archive / 'untouched.txt'
    sentinel.write_bytes(b'not changed by repair or stale Start')
    changed = []

    def repair():
        monkeypatch.setattr(register, 'archive_path', lambda *_: archive)
        monkeypatch.setattr(register, 'probe_fs_uuid', lambda *_: _FS)
        monkeypatch.setattr(register, 'probe_annex_uuid', lambda *_: _ANX)
        monkeypatch.setattr(register, 'probe_serial', lambda *_: _SER)
        monkeypatch.setattr(register.os, 'fstatvfs', lambda fd: SimpleNamespace(
            f_blocks=CAPACITY, f_frsize=1, f_bavail=FREE))
        binding = drive_bootstrap.inspect_serial_identity(case.other, 'drive-00')['binding']
        result = drive_bootstrap.repair_serial_identity(case.other, 'drive-00',
            expected_binding=binding, writers_stopped=True, now='qualification', blocking=False)
        assert result['status'] == 'repaired'
        assert result['superseded_approvals'] == (case.pid,)
        assert case.other.execute('SELECT identity_epoch FROM drives').fetchone() == (1,)
        assert case.other.execute('PRAGMA user_version').fetchone() == (8,)
        changed.append(tuple(case.other.iterdump()))

    assert {row['row_kind'] for row in proposal.load_proposal(case.con, case.pid)['tasks']} == {'executable'}
    _inject(case, monkeypatch, 'controller', repair)
    result = _start(case, entry, None if omitted else case.pid)
    assert isinstance(result, proposal.Refusal), result
    assert result.code == 'APPROVAL_MISSING'
    assert len(changed) == 1 and tuple(case.con.iterdump()) == changed[0]
    assert sentinel.read_bytes() == b'not changed by repair or stale Start'


@pytest.mark.parametrize('omitted', [False, True])
def test_committing_changed_proposal_definition_refuses_instead_of_using_cached_projection(case, monkeypatch, omitted):
    changed = []

    def change():
        case.other.execute("UPDATE placement_proposals SET execution_config_hash=? WHERE proposal_id=?",
                           ['0' * 64, case.pid])
        changed.append(tuple(case.other.iterdump()))

    connection = _inject(case, monkeypatch, 'before_begin', change)
    result = _start(case, 'service', None if omitted else case.pid, connection=connection)
    assert isinstance(result, proposal.Refusal), result
    assert result.code == 'APPROVED_INPUT_CHANGED'
    assert tuple(case.con.iterdump()) == changed[0]
