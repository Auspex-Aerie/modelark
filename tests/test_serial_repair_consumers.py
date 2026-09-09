"""Actual serial repair preserves history and revokes only affected consumers."""
import hashlib
from unittest import mock

import pytest

import _pr09_gate1_fixtures as f
from modelark import execution_service, execution_session, hash_repair, proposal
from modelark import drive_bootstrap as bootstrap
from test_drive_bootstrap import _catalog, _FP
from test_serial_repair_approvals import seed_proposal
from test_serial_repair_workflow import OLD, live_setup, seed


def _rows(con, table):
    return tuple(con.execute(f"SELECT * FROM {table}").fetchall())


def _repair(con):
    intent = bootstrap.inspect_serial_identity(con, "drive-00")
    return bootstrap.repair_serial_identity(
        con, "drive-00", expected_binding=intent["binding"],
        now="2026-09-09T23:00:00Z", writers_stopped=True)


def _archived_source(con, archive):
    """One actual disposable byte claim makes inventory/history assertions nonempty."""
    payload = b"synthetic consumer qualification bytes"
    digest = hashlib.sha256(payload).hexdigest()
    path = archive / "consumer/tiny/model.safetensors"
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    con.execute("INSERT INTO models(repo_id) VALUES('consumer/tiny')")
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) "
        "VALUES('consumer/tiny','model.safetensors',?,?,'safetensors')", [len(payload), digest])
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_relpath,compressed,"
        "orig_bytes,stored_bytes,orig_sha256,orig_sha256_provenance) "
        "VALUES('consumer/tiny','model.safetensors','drive-00','model.safetensors',0,?,?,?,"
        "'ingestion_computed')", [len(payload), len(payload), digest])
    return path, payload


def _consumer_fixtures(con, *, active):
    f.seed_plan_selection(con, repos=("org/a",))
    for binding in ("target_drive", "source_drive", "satisfying_drive"):
        seed_proposal(con, binding, binding=binding, drive="drive-00")
    seed_proposal(con, "unrelated", drive="d1")
    seed_proposal(con, "draft", drive="drive-00", lifecycle="draft")
    seed_proposal(con, "historical", drive="drive-00", lifecycle="superseded")
    con.execute("UPDATE planner_state SET active_approved_proposal_id=?", [active])
    for token, proposal_id in enumerate(("target_drive", "source_drive", "satisfying_drive"), 1):
        con.execute(
            "INSERT INTO execution_sessions(session_id,plan_id,approved_proposal_id,"
            "controller_identity,worker_identity,state,bound_planner_revision,fencing_token,"
            "terminal_at,terminal_code,terminal_evidence) "
            "VALUES(?,'ark',?,'controller','worker','paused',0,?,"
            "'2026-09-09','DRIVE_RECONCILIATION_REQUIRED','immutable previous result')",
            [f"paused-{proposal_id}", proposal_id, token])
    con.execute(
        "INSERT INTO fetch_events(repo_id,event_at,outcome,bytes,detail) "
        "VALUES('org/a','2026-09-08','archived',100,'immutable completed result')")


@pytest.mark.parametrize("dirty", [False, True])
def test_actual_repair_supersedes_all_bound_consumers_and_preserves_history(tmp_path, monkeypatch, dirty):
    with _catalog(tmp_path) as con:
        seed(con, dirty=dirty)
        archive = live_setup(monkeypatch, tmp_path)
        archived_path, payload = _archived_source(con, archive)
        _consumer_fixtures(con, active="source_drive")
        immutable_tables = ("proposal_tasks", "proposal_files", "execution_sessions", "fetch_events",
                            "files", "archived", "replicas", "models", "plans", "plan_drives")
        before = {table: _rows(con, table) for table in immutable_tables}
        proposals = {row[0]: proposal.load_proposal(con, row[0])
                     for row in con.execute("SELECT proposal_id FROM placement_proposals")}
        old_generations = _rows(con, "drive_dirty_generations")
        old_anchors = _rows(con, "drive_clean_anchors")
        before_revision = con.execute("SELECT planner_revision FROM planner_state").fetchone()[0]
        expected = ("satisfying_drive", "source_drive", "target_drive")
        assert bootstrap.inspect_serial_identity(con, "drive-00")["affected_approvals"] == expected

        result = _repair(con)

        assert result["status"] == "repaired"
        assert result["superseded_approvals"] == expected
        assert result["legacy_recovered"] is dirty
        assert result["inventory_present"] == 1
        assert con.execute("PRAGMA user_version").fetchone() == (8,)
        assert con.execute("SELECT active_approved_proposal_id,planner_revision FROM planner_state").fetchone() == (
            None, before_revision + (2 if dirty else 1))
        assert {table: _rows(con, table) for table in immutable_tables} == before
        assert set(old_generations).issubset(_rows(con, "drive_dirty_generations"))
        assert set(old_anchors).issubset(_rows(con, "drive_clean_anchors"))
        for proposal_id, original in proposals.items():
            after = proposal.load_proposal(con, proposal_id)
            if proposal_id in expected:
                assert after["lifecycle"] == "superseded"
                assert after["superseded_at"] is not None
                for key in original.keys() - {"lifecycle", "superseded_at"}:
                    assert after[key] == original[key]
            else:
                assert after == original
        assert (archive / "untouched.txt").read_bytes() == b"unchanged archive sentinel"
        assert archived_path.read_bytes() == payload

        # No fresh execution approval is silently manufactured or rebound. These
        # are real Start/Resume entry points, with explicitly injected test services.
        services = f.default_services()
        for proposal_id in expected:
            for predecessor in (None, f"paused-{proposal_id}"):
                refused = execution_session.start_session(con, proposal_id, predecessor, services)
                assert isinstance(refused, proposal.Refusal)
                assert refused.code == "APPROVAL_MISSING"
        automatic = execution_service.start_fill(con=con, services=services)
        assert isinstance(automatic, proposal.Refusal) and automatic.code == "APPROVAL_MISSING"
        assert {table: _rows(con, table) for table in immutable_tables} == before


def test_actual_repair_preserves_unrelated_active_approval_pointer(tmp_path, monkeypatch):
    with _catalog(tmp_path) as con:
        seed(con)
        live_setup(monkeypatch, tmp_path)
        _consumer_fixtures(con, active="unrelated")
        unrelated = proposal.load_proposal(con, "unrelated")
        result = _repair(con)
        assert result["superseded_approvals"] == ("satisfying_drive", "source_drive", "target_drive")
        assert con.execute("SELECT active_approved_proposal_id FROM planner_state").fetchone() == ("unrelated",)
        assert proposal.load_proposal(con, "unrelated") == unrelated


@pytest.mark.parametrize("status", ["complete", "halted"])
@pytest.mark.parametrize("dirty", [False, True])
def test_actual_repair_keeps_old_hash_evidence_and_old_identity_halts_before_resolver(
        tmp_path, monkeypatch, status, dirty):
    with _catalog(tmp_path) as con:
        seed(con, dirty=dirty)
        archive = live_setup(monkeypatch, tmp_path)
        archived_path, payload = _archived_source(con, archive)
        con.execute(
            "INSERT INTO drive_hash_repair_state(drive_label,identity_epoch,identity_fingerprint,"
            "status,updated_at,detail) VALUES('drive-00',1,?,?,'2026-09-08','historical hash evidence')",
            [OLD, status])
        original = _rows(con, "drive_hash_repair_state")

        assert _repair(con)["status"] == "repaired"

        assert _rows(con, "drive_hash_repair_state") == original
        assert con.execute(
            "SELECT identity_epoch,identity_fingerprint FROM drives WHERE drive_label='drive-00'").fetchone() == (1, _FP)
        before_archived = _rows(con, "archived")
        before_anchors = _rows(con, "drive_clean_anchors")
        before_generations = _rows(con, "drive_dirty_generations")
        resolver = mock.Mock(side_effect=AssertionError("old identity must halt before archive resolution"))

        refused = hash_repair.run_explicit_drive_repair(
            con, "drive-00", identity_epoch=1, identity_fingerprint=OLD, archive_resolver=resolver)

        resolver.assert_not_called()
        assert refused["status"] == "halted"
        assert refused["applied"] == 0
        assert refused["detail"] == "identity_fingerprint mismatch"
        assert con.execute(
            "SELECT identity_fingerprint,status FROM drive_hash_repair_state WHERE drive_label='drive-00'").fetchone() == (OLD, "halted")
        assert _rows(con, "archived") == before_archived
        assert _rows(con, "drive_clean_anchors") == before_anchors
        assert _rows(con, "drive_dirty_generations") == before_generations
        assert (archive / "untouched.txt").read_bytes() == b"unchanged archive sentinel"
        assert archived_path.read_bytes() == payload
        assert con.execute("PRAGMA user_version").fetchone() == (8,)
