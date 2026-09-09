"""Narrow approval invalidation inside an explicit serial-evidence repair."""
from contextlib import contextmanager

import pytest

import _pr09_gate1_fixtures as f
from modelark import drive_fence, execution_service, execution_session, proposal


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    connection = f.mem_con()
    f.seed_plan_selection(connection, repos=("org/a",), with_archive_on=(("org/a", "d0"),))
    yield connection
    connection.close()


def seed_proposal(con, proposal_id, *, binding="target_drive", drive="d0", lifecycle="approved"):
    con.execute(
        "INSERT INTO placement_proposals(proposal_id,plan_id,based_on_revision,lifecycle,"
        "canonical_hash,mutation_kind,serializer_version,approved_at) "
        "VALUES(?,'ark',0,?,?,'adopt_current','1','2026-01-01')",
        [proposal_id, lifecycle, "a" * 64])
    row_kind = "baseline_satisfied" if binding == "satisfying_drive" else "executable"
    values = {"target_drive": None, "source_drive": None, "satisfying_drive": None}
    values[binding] = drive
    con.execute(
        "INSERT INTO proposal_tasks(proposal_id,requirement_id,row_kind,repo_id,"
        "target_drive,source_drive,satisfying_drive,full_manifest_hash,identity_epoch) "
        "VALUES(?,'requirement',?,'org/a',?,?,?,?,1)",
        [proposal_id, row_kind, values["target_drive"], values["source_drive"],
         values["satisfying_drive"], "b" * 64])
    con.execute(
        "INSERT INTO proposal_files(proposal_id,requirement_id,rfilename,size_bytes,orig_sha256) "
        "VALUES(?,'requirement','model.safetensors',100,?)", [proposal_id, "1" * 64])


@contextmanager
def repair_transaction(con):
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def rows(con, table):
    return tuple(con.execute(f"SELECT * FROM {table}").fetchall())


@pytest.mark.parametrize("binding", ["target_drive", "source_drive", "satisfying_drive"])
def test_each_binding_is_inspected_and_superseded_without_immutable_edits(con, binding):
    seed_proposal(con, "affected", binding=binding)
    con.execute("UPDATE planner_state SET active_approved_proposal_id='affected'")
    tasks, files, archived = (rows(con, table) for table in ("proposal_tasks", "proposal_files", "archived"))
    original = proposal.load_proposal(con, "affected")
    before = tuple(con.iterdump())
    assert proposal.approved_proposals_bound_to_drive(con, "d0") == ("affected",)
    assert tuple(con.iterdump()) == before
    with repair_transaction(con):
        assert proposal.supersede_serial_repair_approvals(con, "d0") == ("affected",)
        assert con.in_transaction
    after = proposal.load_proposal(con, "affected")
    assert after["lifecycle"] == "superseded"
    assert after["superseded_at"] is not None
    for key in original.keys() - {"lifecycle", "superseded_at"}:
        assert after[key] == original[key]
    assert (rows(con, "proposal_tasks"), rows(con, "proposal_files"), rows(con, "archived")) == (
        tasks, files, archived)
    assert con.execute(
        "SELECT active_approved_proposal_id,planner_revision FROM planner_state").fetchone() == (None, 0)


@pytest.mark.parametrize("active", [None, "unrelated", "affected"])
def test_active_pointer_cleared_only_if_affected(con, active):
    seed_proposal(con, "affected")
    seed_proposal(con, "unrelated", drive="d1")
    before_unrelated = proposal.load_proposal(con, "unrelated")
    con.execute("UPDATE planner_state SET active_approved_proposal_id=?", [active])
    with repair_transaction(con):
        assert proposal.supersede_serial_repair_approvals(con, "d0") == ("affected",)
    expected = None if active == "affected" else active
    assert con.execute("SELECT active_approved_proposal_id FROM planner_state").fetchone() == (expected,)
    assert proposal.load_proposal(con, "unrelated") == before_unrelated


def test_affected_ids_sorted_deduplicated_and_approved_only(con):
    for name in ("z", "a"):
        seed_proposal(con, name)
    seed_proposal(con, "draft", lifecycle="draft")
    seed_proposal(con, "historical", lifecycle="superseded")
    seed_proposal(con, "unrelated", drive="d1")
    con.execute(
        "INSERT INTO proposal_tasks(proposal_id,requirement_id,row_kind,repo_id,"
        "target_drive,source_drive,satisfying_drive,full_manifest_hash) "
        "VALUES('z','duplicate','executable','org/a','d0','d0','d0',?)", ["b" * 64])
    unaffected = {name: proposal.load_proposal(con, name) for name in ("draft", "historical", "unrelated")}
    assert proposal.approved_proposals_bound_to_drive(con, "d0") == ("a", "z")
    with repair_transaction(con):
        assert proposal.supersede_serial_repair_approvals(con, "d0") == ("a", "z")
    for name, original in unaffected.items():
        assert proposal.load_proposal(con, name) == original


def test_reselection_happens_inside_transaction_and_second_repair_is_noop(con):
    seed_proposal(con, "first")
    assert proposal.approved_proposals_bound_to_drive(con, "d0") == ("first",)
    seed_proposal(con, "newly-approved")
    with repair_transaction(con):
        assert proposal.supersede_serial_repair_approvals(con, "d0") == ("first", "newly-approved")
    before = tuple(con.iterdump())
    with repair_transaction(con):
        assert proposal.supersede_serial_repair_approvals(con, "d0") == ()
    assert tuple(con.iterdump()) == before


def test_mutation_requires_caller_transaction(con):
    seed_proposal(con, "affected")
    before = tuple(con.iterdump())
    with pytest.raises(proposal.Refusal, match="SERIAL_REPAIR_TRANSACTION_REQUIRED"):
        proposal.supersede_serial_repair_approvals(con, "d0")
    assert tuple(con.iterdump()) == before
    assert not con.in_transaction


def test_rollback_restores_approval_pointer_and_preserves_history(con):
    seed_proposal(con, "affected")
    con.execute("UPDATE planner_state SET active_approved_proposal_id='affected'")
    con.execute(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,controller_identity,"
        "worker_identity,state,bound_planner_revision,fencing_token,terminal_code) "
        "VALUES('paused','ark','affected','controller','worker','paused',0,9,"
        "'DRIVE_RECONCILIATION_REQUIRED')")
    before = tuple(con.iterdump())
    with pytest.raises(RuntimeError, match="repair publication failed"):
        with repair_transaction(con):
            proposal.supersede_serial_repair_approvals(con, "d0")
            raise RuntimeError("repair publication failed")
    assert tuple(con.iterdump()) == before
    history = rows(con, "execution_sessions")
    with repair_transaction(con):
        proposal.supersede_serial_repair_approvals(con, "d0")
    assert rows(con, "execution_sessions") == history


@pytest.mark.parametrize("state", ["starting", "running", "stopping"])
def test_live_fill_refuses_before_approval_changes(con, state):
    seed_proposal(con, "affected")
    con.execute(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,controller_identity,"
        "worker_identity,state,bound_planner_revision,fencing_token) "
        "VALUES('live','ark','affected','controller','worker',?,0,9)", [state])
    before = tuple(con.iterdump())
    with pytest.raises(proposal.Refusal, match="FILL_SESSION_ACTIVE"):
        with repair_transaction(con):
            proposal.supersede_serial_repair_approvals(con, "d0")
    assert tuple(con.iterdump()) == before


def test_old_resume_and_auto_resume_refuse_without_rewriting_paused_session(con):
    seed_proposal(con, "affected")
    con.execute("UPDATE planner_state SET active_approved_proposal_id='affected'")
    con.execute(
        "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,controller_identity,"
        "worker_identity,state,bound_planner_revision,fencing_token,terminal_code) "
        "VALUES('paused','ark','affected','controller','worker','paused',0,9,"
        "'DRIVE_RECONCILIATION_REQUIRED')")
    with repair_transaction(con):
        proposal.supersede_serial_repair_approvals(con, "d0")
    history = rows(con, "execution_sessions")
    services = f.default_services()
    resumed = execution_session.start_session(con, "affected", "paused", services)
    assert isinstance(resumed, proposal.Refusal) and resumed.code == "APPROVAL_MISSING"
    automatic = execution_service.start_fill(con=con, services=services, predecessor_id="paused")
    assert isinstance(automatic, proposal.Refusal) and automatic.code == "APPROVAL_MISSING"
    assert rows(con, "execution_sessions") == history
