"""An explicit proposal selection never falls through to another active approval."""
from types import SimpleNamespace

import pytest

from modelark import drive_fence, execution_service, execution_session, proposal, register
from test_drive_bootstrap import _catalog
from test_serial_repair_public_fill import _preview_approve, _seed_catalog


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, '_LOCK_DIR', tmp_path / 'locks')
    monkeypatch.setattr(register, 'archive_path', lambda *_: None)
    with _catalog(tmp_path) as con:
        _seed_catalog(con, pending=True)
        services = execution_service.production_services(con)
        old = _preview_approve(con, proposal._DefaultServices())
        started = execution_service.start_fill(con=con, proposal_id=old['proposal_id'], services=services)
        assert isinstance(started, execution_session.SessionStart)
        execution_session.terminalize(
            con, session_id=started.session.session_id, fencing_token=started.session.fencing_token,
            state='paused', terminal_code='retained previous result')
        active = _preview_approve(con, proposal._DefaultServices())
        assert proposal.load_proposal(con, old['proposal_id'])['lifecycle'] == 'superseded'
        assert con.execute('SELECT active_approved_proposal_id FROM planner_state').fetchone() == (
            active['proposal_id'],)
        yield SimpleNamespace(con=con, services=services, old=old['proposal_id'],
                              active=active['proposal_id'], predecessor=started.session.session_id)


def _start(case, entry, requested, predecessor=None):
    if entry == 'session':
        return execution_session.start_session(case.con, requested, predecessor, case.services)
    return execution_service.start_fill(con=case.con, proposal_id=requested,
                                        predecessor_id=predecessor, services=case.services)


@pytest.mark.parametrize('entry', ['session', 'service'])
@pytest.mark.parametrize('selection', ['superseded', 'unknown', 'empty', 'draft'])
@pytest.mark.parametrize('resume', [False, True])
def test_explicit_nonapproved_id_refuses_without_switching_active_proposal(case, entry, selection, resume):
    requested = {'superseded': case.old, 'unknown': 'not-in-catalog', 'empty': ''}.get(selection)
    if selection == 'draft':
        requested = proposal.create_draft(case.con, 'ark')['proposal_id']
    before = tuple(case.con.iterdump())
    result = _start(case, entry, requested, case.predecessor if resume else None)
    assert isinstance(result, proposal.Refusal), result
    assert result.code == 'APPROVAL_MISSING'
    assert tuple(case.con.iterdump()) == before
    assert not case.con.in_transaction


@pytest.mark.parametrize('entry', ['session', 'service'])
@pytest.mark.parametrize('explicit', [False, True])
def test_omitted_selection_uses_active_and_explicit_valid_selection_still_starts(case, entry, explicit):
    old = case.con.execute('SELECT * FROM execution_sessions WHERE session_id=?',
                           [case.predecessor]).fetchone()
    result = _start(case, entry, case.active if explicit else None)
    assert isinstance(result, execution_session.SessionStart), result
    assert result.session.approved_proposal_id == case.active
    assert result.session.session_id != case.predecessor
    assert result.session.resumed_from_session_id is None
    assert case.con.execute('SELECT * FROM execution_sessions WHERE session_id=?',
                            [case.predecessor]).fetchone() == old


@pytest.mark.parametrize('entry', ['session', 'service'])
def test_omitted_selection_does_not_resume_old_predecessor_under_active_replacement(case, entry):
    before = tuple(case.con.iterdump())
    result = _start(case, entry, None, case.predecessor)
    assert isinstance(result, proposal.Refusal)
    assert result.code == 'RESUME_APPROVAL_MISMATCH'
    assert tuple(case.con.iterdump()) == before
