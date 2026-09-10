"""Ended Fill recovery: isolated catalogs, mocked media, real host locks/transactions."""
import fcntl
from types import SimpleNamespace

import pytest

import _pr09_gate1_fixtures as f
from test_drive_bootstrap import _anchor, _catalog, _dirty_gen, _ev, _FP, _proven_drive
from modelark import cli, drive_bootstrap as bs, drive_fence, execution_recovery as recovery
from modelark import drive_mutation as dm, execution_authority as authority
from modelark.proposal import Refusal


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, '_LOCK_DIR', tmp_path / 'locks')
    with _catalog(tmp_path) as con:
        f.seed_plan_selection(con, repos=('org/a',))
        _, proposal_id, _ = f.create_and_approve(con)
        _proven_drive(con, generation=2)
        _dirty_gen(con, gen=1)
        _anchor(con, gen=1)
        _dirty_gen(con, gen=2, owner='ended-fill', token=7)
        con.execute(
            "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
            "controller_identity,worker_identity,state,bound_planner_revision,fencing_token,"
            "terminal_at,terminal_code) VALUES('ended-fill','ark',?,'controller','worker',"
            "'paused',0,7,'2026-09-07 14:15:53','DRIVE_RECONCILIATION_REQUIRED')", [proposal_id])
        inventory = bs.Inventory(present=[('org/a', 'model.safetensors')], missing=[],
                                 debris=['partial.tmp'], extra=['keep.txt'])
        observed = _ev(fp=_FP, capacity=1000, free=870)
        monkeypatch.setattr(bs, '_live_evidence', lambda *args: observed)
        monkeypatch.setattr(bs.register, 'archive_path', lambda *args: tmp_path / 'archive')
        calls = []
        def inspect(*args, **kwargs):
            calls.append(True)
            return inventory
        monkeypatch.setattr(bs, '_inventory', inspect)
        yield SimpleNamespace(con=con, inventory=inventory, observed=observed,
                              calls=calls, tmp=tmp_path, proposal_id=proposal_id)


def run(case):
    return bs.reconcile_drive(case.con, 'drive-00', dedicated=True,
                              now='2026-09-09T18:00:00Z', blocking=False)


def no_anchor(case):
    assert case.con.execute("SELECT count(*) FROM drive_clean_anchors WHERE drive_label='drive-00' "
                            "AND generation=2").fetchone()[0] == 0
    assert not case.con.in_transaction


@pytest.mark.parametrize('state', ['paused', 'blocked', 'stopped', 'failed', 'done'])
def test_ended_session_recovers_without_rewriting_its_history(case, state):
    con = case.con
    con.execute('UPDATE execution_sessions SET state=?', [state])
    session = con.execute('SELECT * FROM execution_sessions').fetchall()
    dirty = con.execute('SELECT * FROM drive_dirty_generations').fetchall()
    drives = con.execute('SELECT * FROM drives').fetchall()
    revision = con.execute('SELECT planner_revision FROM planner_state').fetchone()[0]
    result = run(case)
    assert result.outcome == 'recovered'
    assert (result.identity_epoch, result.generation, result.anchor_free_bytes) == (1, 2, 870)
    assert result.inventory is case.inventory
    assert con.execute('SELECT * FROM execution_sessions').fetchall() == session
    assert con.execute('SELECT * FROM drive_dirty_generations').fetchall() == dirty
    assert con.execute('SELECT * FROM drives').fetchall() == drives
    assert con.execute('SELECT planner_revision FROM planner_state').fetchone()[0] == revision + 1
    assert con.execute("SELECT generation,anchor_free_bytes FROM drive_clean_anchors "
                       "WHERE drive_label='drive-00' ORDER BY generation").fetchall() == [(1, 900), (2, 870)]
    assert not (drive_fence._LOCK_DIR / 'session-child-ended-fill.lock').exists()


def test_second_reconcile_uses_existing_clean_refresh(case):
    assert run(case).outcome == 'recovered'
    assert run(case).outcome == 'refreshed'
    assert case.con.execute("SELECT write_generation FROM drives WHERE drive_label='drive-00'").fetchone() == (3,)


@pytest.mark.parametrize('change,code', [
    ("DELETE FROM execution_sessions", 'DRIVE_RECOVERY_OWNER_UNPROVEN'),
    ("UPDATE execution_sessions SET fencing_token=8", 'DRIVE_RECOVERY_OWNER_UNPROVEN'),
    ("UPDATE execution_sessions SET state='running'", 'FILL_SESSION_ACTIVE'),
])
def test_invalid_or_live_owner_refuses_before_inventory(case, change, code):
    case.con.execute(change)
    with pytest.raises((dm.DriveMutationRefused, Refusal)) as raised:
        run(case)
    assert raised.value.code == code
    assert not case.calls
    no_anchor(case)


def test_unknown_owner_state_fails_closed(case):
    case.con.execute('PRAGMA ignore_check_constraints=ON')
    case.con.execute("UPDATE execution_sessions SET state='unknown'")
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECOVERY_OWNER_UNPROVEN'):
        run(case)
    assert not case.calls
    no_anchor(case)


def test_real_held_child_marker_refuses_before_inventory(case):
    fds = recovery.inherit_drive_fence_fds(
        session_id='ended-fill', drive_labels=[], marker_only=True)
    try:
        assert fds
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECOVERY_CHILD_UNPROVEN'):
            run(case)
        assert not case.calls
        no_anchor(case)
    finally:
        recovery.release_child_fences('ended-fill')


def test_unlocked_existing_marker_does_not_prevent_recovery(case):
    drive_fence._LOCK_DIR.mkdir(exist_ok=True)
    (drive_fence._LOCK_DIR / 'session-child-ended-fill.lock').touch()
    assert run(case).outcome == 'recovered'


def test_child_probe_error_refuses(case, monkeypatch):
    def denied(**kwargs):
        assert kwargs == {'session_id': 'ended-fill'}
        raise PermissionError('cannot inspect child marker')
    monkeypatch.setattr(recovery, 'child_fence_still_held', denied)
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECOVERY_CHILD_UNPROVEN'):
        run(case)
    assert not case.calls
    no_anchor(case)


def test_physical_drive_lock_blocks_even_without_child_marker(case):
    with drive_fence.hold_drives_sorted([(_FP, 1)], blocking=False):
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_FENCE_UNAVAILABLE'):
            run(case)
    assert not case.calls
    no_anchor(case)


def test_owned_dirty_capacity_transition_cannot_skip_recovery_guards(case):
    case.observed.capacity = 2000
    case.observed.fingerprint = 'b' * 64
    before = case.con.execute('SELECT * FROM drives').fetchall()
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECOVERY_IDENTITY_CHANGED'):
        run(case)
    assert not case.calls
    assert case.con.execute('SELECT * FROM drives').fetchall() == before
    no_anchor(case)


@pytest.mark.parametrize('change', [
    "UPDATE drives SET identity_fingerprint=NULL WHERE drive_label='drive-00'",
    "UPDATE drives SET write_authority='unknown' WHERE drive_label='drive-00'",
])
def test_owned_dirty_bootstrap_or_authority_change_refuses(case, change):
    case.con.execute(change)
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECOVERY_IDENTITY_CHANGED'):
        run(case)
    assert not case.calls
    no_anchor(case)


@pytest.mark.parametrize('phase', ['before', 'inventory'])
def test_other_live_session_also_blocks_recovery(case, monkeypatch, phase):
    def add_live():
        case.con.execute(
            "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
            "controller_identity,worker_identity,state,bound_planner_revision,fencing_token) "
            "VALUES('other-fill','ark',?,'c','w','running',0,8)", [case.proposal_id])
    if phase == 'before':
        add_live()
    else:
        def inspect(*args, **kwargs):
            add_live()
            return case.inventory
        monkeypatch.setattr(bs, '_inventory', inspect)
    with pytest.raises(Refusal, match='FILL_SESSION_ACTIVE'):
        run(case)
    no_anchor(case)


@pytest.mark.parametrize('pair', [(None, 7), ('ended-fill', None), ('', 7)])
def test_corrupt_half_or_empty_owner_pair_is_not_sessionless(case, pair):
    # Deliberately corrupt this isolated fixture; production schema forbids owner rewrites.
    case.con.execute('DROP TRIGGER drive_dirty_generations_no_update')
    case.con.execute('PRAGMA ignore_check_constraints=ON')
    case.con.execute("UPDATE drive_dirty_generations SET owner_session_id=?,owner_fencing_token=? "
                     "WHERE drive_label='drive-00' AND generation=2", pair)
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECOVERY_OWNER_UNPROVEN'):
        run(case)
    assert not case.calls
    no_anchor(case)


def test_dirty_pair_changed_during_inventory_refuses(case, monkeypatch):
    case.con.execute('DROP TRIGGER drive_dirty_generations_no_update')
    def inspect(*args, **kwargs):
        case.con.execute("UPDATE drive_dirty_generations SET owner_session_id=NULL,"
                         "owner_fencing_token=NULL WHERE drive_label='drive-00' AND generation=2")
        return case.inventory
    monkeypatch.setattr(bs, '_inventory', inspect)
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECOVERY_OWNER_CHANGED'):
        run(case)
    no_anchor(case)


def test_child_marker_becomes_held_during_inventory_refuses(case, monkeypatch):
    def inspect(*args, **kwargs):
        recovery.inherit_drive_fence_fds(
            session_id='ended-fill', drive_labels=[], marker_only=True)
        return case.inventory
    monkeypatch.setattr(bs, '_inventory', inspect)
    try:
        with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECOVERY_CHILD_UNPROVEN'):
            run(case)
        no_anchor(case)
    finally:
        recovery.release_child_fences('ended-fill')


@pytest.mark.parametrize('phase', ['inventory', 'begin'])
@pytest.mark.parametrize('change,code', [
    ("UPDATE execution_sessions SET fencing_token=8", 'DRIVE_RECOVERY_OWNER_CHANGED'),
    ("UPDATE execution_sessions SET state='stopped'", 'DRIVE_RECOVERY_OWNER_CHANGED'),
    ("UPDATE execution_sessions SET state='running'", 'FILL_SESSION_ACTIVE'),
    ("UPDATE drives SET write_generation=3 WHERE drive_label='drive-00'", 'DRIVE_RECOVERY_OWNER_CHANGED'),
    ("UPDATE drives SET identity_epoch=2 WHERE drive_label='drive-00'", 'DRIVE_RECOVERY_OWNER_CHANGED'),
    ("UPDATE drives SET filesystem_capacity_bytes=2000 WHERE drive_label='drive-00'",
     'DRIVE_RECOVERY_OWNER_CHANGED'),
    ("UPDATE drives SET write_authority='unknown' WHERE drive_label='drive-00'",
     'DRIVE_RECOVERY_OWNER_CHANGED'),
    ("UPDATE drives SET identity_fingerprint='" + 'b' * 64 + "' WHERE drive_label='drive-00'",
     'DRIVE_RECOVERY_OWNER_CHANGED'),
])
def test_changes_during_inventory_or_before_commit_publish_nothing(case, monkeypatch, phase, change, code):
    if phase == 'inventory':
        def inspect(*args, **kwargs):
            case.con.execute(change)
            return case.inventory
        monkeypatch.setattr(bs, '_inventory', inspect)
    else:
        original = dm._immediate
        def begin(con, body):
            con.execute(change)
            return original(con, body)
        monkeypatch.setattr(dm, '_immediate', begin)
    with pytest.raises((dm.DriveMutationRefused, Refusal)) as raised:
        run(case)
    assert raised.value.code == code
    no_anchor(case)


def test_owner_check_and_anchor_publication_hold_both_fences_and_transaction(case, monkeypatch):
    original = authority.require_current
    seen = []
    def require(expected, actual, state, allowed):
        if case.con.in_transaction:
            assert expected == actual == authority.Attempt('ended-fill', 7)
            assert allowed == {'paused'}
            for path in (drive_fence.controller_lock_path(bs.db.DB_PATH),
                         drive_fence.drive_lock_path(_FP, 1)):
                with open(path) as handle:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            seen.append(True)
        original(expected, actual, state, allowed)
    monkeypatch.setattr(authority, 'require_current', require)
    run(case)
    assert seen


def test_incomplete_inventory_and_disappearing_media_leave_generation_dirty(case, monkeypatch):
    case.inventory.missing.append(('org/a', 'missing'))
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_RECONCILIATION_REQUIRED'):
        run(case)
    no_anchor(case)
    case.inventory.missing.clear()
    def lost(*args):
        raise dm.DriveMutationRefused('DRIVE_IDENTITY_UNPROVEN')
    monkeypatch.setattr(bs, '_final_observation', lost)
    with pytest.raises(dm.DriveMutationRefused, match='DRIVE_IDENTITY_UNPROVEN'):
        run(case)
    no_anchor(case)


def test_failed_publication_rolls_back_anchor_and_revision(case, monkeypatch):
    before = case.con.execute('SELECT planner_revision FROM planner_state').fetchone()
    original = dm._publish_anchor_locked
    def fail(*args):
        original(*args)
        raise OSError('publication failure')
    monkeypatch.setattr(dm, '_publish_anchor_locked', fail)
    with pytest.raises(OSError, match='publication failure'):
        run(case)
    no_anchor(case)
    assert case.con.execute('SELECT planner_revision FROM planner_state').fetchone() == before


def test_cli_runs_ended_recovery_and_reports_existing_inventory_counts(case, monkeypatch, capsys):
    monkeypatch.setattr(cli.db, 'connect', lambda: case.con)
    cli.main(['drive', 'reconcile', 'drive-00', '--dedicated'])
    output = capsys.readouterr().out
    assert 'recovered' in output
    assert 'present=1 missing=0 debris=1 extra=1' in output
    assert 'never deleted automatically' in output


@pytest.mark.parametrize('code', ['FILL_SESSION_ACTIVE', 'CHILD_FENCE_HELD', 'SESSION_TOKEN_MISMATCH'])
def test_cli_translates_session_refusals(case, monkeypatch, code):
    def refuse(*args, **kwargs):
        raise Refusal(code, {}, ())
    monkeypatch.setattr(cli.db, 'connect', lambda: case.con)
    monkeypatch.setattr(bs, 'reconcile_drive', refuse)
    with pytest.raises(SystemExit, match='drive reconcile failed: ' + code):
        cli.main(['drive', 'reconcile', 'drive-00', '--dedicated'])
