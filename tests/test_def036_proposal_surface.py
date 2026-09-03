"""DEF-036 operator-facing proposal review/approval adapter contracts."""

from __future__ import annotations

import sqlite3
from unittest import mock

from modelark import proposal
from modelark.web import proposal_api


def _connection(*, revision: int = 9, active: str | None = None):
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute(
        "CREATE TABLE planner_state("
        "singleton_id INTEGER PRIMARY KEY, planner_revision INTEGER NOT NULL, "
        "active_approved_proposal_id TEXT, next_fencing_token INTEGER NOT NULL)"
    )
    con.execute(
        "INSERT INTO planner_state VALUES(1,?,?,0)",
        [revision, active],
    )
    con.execute(
        "CREATE TABLE plans("
        "plan_id TEXT PRIMARY KEY, name TEXT, annex_root TEXT, "
        "capacity_mode TEXT NOT NULL, status TEXT NOT NULL, is_active INTEGER NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, notes TEXT)"
    )
    con.execute(
        "INSERT INTO plans(plan_id,name,annex_root,capacity_mode,status,is_active) "
        "VALUES('ark','Ark','/tmp/modelark','guaranteed','active',1)"
    )
    con.execute(
        "CREATE TABLE plan_drives("
        "plan_id TEXT NOT NULL, drive_label TEXT NOT NULL, position INTEGER, "
        "PRIMARY KEY(plan_id,drive_label))"
    )
    con.execute(
        "CREATE TABLE placement_proposals("
        "proposal_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, "
        "based_on_revision INTEGER NOT NULL, lifecycle TEXT NOT NULL, "
        "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, superseded_at TEXT)"
    )
    return con


def _stored(*, proposal_id: str = "proposal-123", lifecycle: str = "draft") -> dict:
    return {
        "proposal_id": proposal_id,
        "plan_id": "ark",
        "based_on_revision": 9,
        "lifecycle": lifecycle,
        "canonical_hash": "c" * 64,
        "mutation_kind": "adopt_current",
        "mutation_args": (),
        "serializer_version": "1",
        "requirement_set_hash": "r" * 64,
        "semantic_input_hash": "s" * 64,
        "selection_before_hash": "b" * 64,
        "selection_after_hash": "b" * 64,
        "capacity_mode": "guaranteed",
        "policy_version": "1",
        "solver_version": "1",
        "gate_b_code": "FEASIBLE",
        "derivation_mode": "optimized",
        "execution_config_hash": "e" * 64,
        "created_at": "2026-08-29 20:00:00",
        "approved_at": None,
        "superseded_at": None,
        "tasks": [
            {
                "requirement_id": "primary:org/base",
                "row_kind": "baseline_satisfied",
                "repo_id": "org/base",
                "target_drive": "drive-00",
                "source_drive": None,
                "satisfying_drive": "drive-00",
                "full_manifest_hash": "1" * 64,
                "order_key": 1,
                "guaranteed_durable": 100,
                "expected_durable": 80,
                "identity_epoch": 1,
                "baseline_certificate": "certificate",
            },
            {
                "requirement_id": "primary:org/new",
                "row_kind": "executable",
                "repo_id": "org/new",
                "target_drive": "drive-03",
                "source_drive": None,
                "satisfying_drive": None,
                "full_manifest_hash": "2" * 64,
                "order_key": 2,
                "guaranteed_durable": 200,
                "expected_durable": 150,
                "identity_epoch": 1,
                "baseline_certificate": None,
            },
            {
                "requirement_id": "replica:org/new",
                "row_kind": "executable",
                "repo_id": "org/new",
                "target_drive": "drive-04",
                "source_drive": "drive-03",
                "satisfying_drive": None,
                "full_manifest_hash": "2" * 64,
                "order_key": 3,
                "guaranteed_durable": 200,
                "expected_durable": 150,
                "identity_epoch": 1,
                "baseline_certificate": None,
            },
        ],
        "files": [
            {
                "requirement_id": "primary:org/new",
                "rfilename": "model.safetensors",
                "role": "missing",
                "size_bytes": 200,
                "orig_sha256": "a" * 64,
                "format": "safetensors",
                "quant": "bf16",
                "storage_action": "raw",
            },
            {
                "requirement_id": "replica:org/new",
                "rfilename": "model.safetensors",
                "role": "missing",
                "size_bytes": 200,
                "orig_sha256": "a" * 64,
                "format": "safetensors",
                "quant": "bf16",
                "storage_action": "raw",
            },
        ],
    }


def test_review_is_exact_bounded_operator_evidence():
    review = proposal_api._review(_stored(), include_assignments=True)
    assert review["confirmation_phrase"] == "APPROVE proposal-123"
    assert review["canonical_hash"] == "c" * 64
    assert review["selection_before_hash"] == review["selection_after_hash"]
    assert review["totals"] == {
        "requirements": 3,
        "executable": 2,
        "baseline_satisfied": 1,
        "repositories": 2,
        "files": 2,
        "guaranteed_bytes": 500,
        "expected_bytes": 380,
    }
    assert [row["drive_label"] for row in review["drives"]] == [
        "drive-00", "drive-03", "drive-04"
    ]
    assert review["drives"][2]["source_requirements"] == 1
    assert [row["requirement_id"] for row in review["assignments"]] == [
        "primary:org/base", "primary:org/new", "replica:org/new"
    ]
    assert review["assignments"][2]["source_drive"] == "drive-03"
    assert "baseline_certificate" not in review["assignments"][0]


def test_create_draft_uses_active_plan_and_ignores_client_authority():
    con = _connection()
    stored = _stored()
    body = {
        "plan_id": "client-plan",
        "mutation": ["finalize", ["attacker/repo"]],
        "canonical_hash": "0" * 64,
        "serialized_proposal": {"forged": True},
    }
    with mock.patch.object(proposal_api.data, "conn", return_value=con), \
         mock.patch("modelark.plan.active", return_value={"plan_id": "ark"}), \
         mock.patch("modelark.proposal.create_draft", return_value={
             "proposal_id": stored["proposal_id"]
         }) as create, \
         mock.patch("modelark.proposal.load_proposal", return_value=stored):
        result = proposal_api.create_draft(body)
    assert result["ok"] is True and result["state"] == "draft"
    assert result["review"]["proposal_id"] == "proposal-123"
    create.assert_called_once_with(
        con,
        plan_id="ark",
        mutation=("adopt_current", ()),
    )


def test_approval_requires_exact_backend_phrase_then_uses_stored_mutation():
    con = _connection()
    stored = _stored()
    with mock.patch.object(proposal_api.data, "conn", return_value=con), \
         mock.patch("modelark.proposal.load_proposal", return_value=stored), \
         mock.patch("modelark.proposal.approve") as approve:
        refused = proposal_api.approve({
            "proposal_id": "proposal-123",
            "confirmation": "APPROVE something-else",
            "mutation": ["finalize", ["attacker/repo"]],
        })
    assert refused["ok"] is False and refused["refused"] is True
    assert refused["code"] == "PROPOSAL_CONFIRMATION_MISMATCH"
    approve.assert_not_called()

    def apply_approval(connection, proposal_id, *, mutation):
        assert mutation == ("adopt_current", ())
        connection.execute(
            "UPDATE planner_state SET planner_revision=10, "
            "active_approved_proposal_id=? WHERE singleton_id=1",
            [proposal_id],
        )
        stored["lifecycle"] = "approved"
        stored["approved_at"] = "2026-08-29 20:01:00"
        return {"proposal_id": proposal_id, "lifecycle": "approved"}

    with mock.patch.object(proposal_api.data, "conn", return_value=con), \
         mock.patch("modelark.proposal.load_proposal", return_value=stored), \
         mock.patch("modelark.proposal.approve", side_effect=apply_approval) as approve, \
         mock.patch("modelark.proposal.review_input_status", return_value={
             "current": True,
             "semantic_input_matches": True,
             "execution_config_matches": True,
         }):
        accepted = proposal_api.approve({
            "proposal_id": "proposal-123",
            "confirmation": "APPROVE proposal-123",
            "mutation": ["finalize", ["attacker/repo"]],
        })
    assert accepted["ok"] is True
    assert accepted["state"] == "approved_current"
    assert accepted["planner_revision"] == 10
    approve.assert_called_once_with(
        con,
        "proposal-123",
        mutation=("adopt_current", ()),
    )


def test_status_distinguishes_missing_current_and_stale_approval():
    con = _connection()
    with mock.patch.object(proposal_api.data, "conn", return_value=con):
        missing = proposal_api.status()
    assert missing == {
        "ok": True,
        "state": "missing",
        "planner_revision": 9,
        "active_proposal": None,
    }

    con.execute(
        "UPDATE planner_state SET active_approved_proposal_id='proposal-123' "
        "WHERE singleton_id=1"
    )
    stored = _stored(lifecycle="approved")
    with mock.patch.object(proposal_api.data, "conn", return_value=con), \
         mock.patch("modelark.proposal.load_proposal", return_value=stored), \
         mock.patch("modelark.proposal.review_input_status", return_value={
             "current": False,
             "semantic_input_matches": False,
             "execution_config_matches": True,
         }):
        stale = proposal_api.status()
    assert stale["state"] == "approved_stale"
    assert stale["active_proposal"]["proposal_id"] == "proposal-123"
    assert stale["input_status"]["semantic_input_matches"] is False


def test_status_recovers_one_current_draft_and_preserves_active_approval_context():
    con = _connection(revision=10, active="approved-10")
    con.execute(
        "INSERT INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle) "
        "VALUES('pending-10','ark',10,'draft')"
    )
    active = _stored(proposal_id="approved-10", lifecycle="approved")
    pending = _stored(proposal_id="pending-10", lifecycle="draft")
    pending["based_on_revision"] = 10
    current = {
        "current": True,
        "semantic_input_matches": True,
        "execution_config_matches": True,
    }

    with mock.patch.object(proposal_api.data, "conn", return_value=con), \
         mock.patch("modelark.proposal.load_proposal", side_effect=lambda _con, pid: {
             "approved-10": active,
             "pending-10": pending,
         }[pid]), \
         mock.patch("modelark.proposal.review_input_status", return_value=current):
        status = proposal_api.status()

    assert status["ok"] is True
    assert status["state"] == "review_pending"
    assert status["approval_state"] == "approved_current"
    assert status["active_proposal"]["proposal_id"] == "approved-10"
    assert status["pending_proposal"]["proposal_id"] == "pending-10"
    assert status["pending_proposal"]["confirmation_phrase"] == "APPROVE pending-10"
    assert len(status["pending_proposal"]["assignments"]) == 3


def test_status_keeps_inactive_plan_draft_visible_but_not_approvable():
    con = _connection(revision=10, active="approved-10")
    con.execute(
        "INSERT INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle) "
        "VALUES('pending-other','other-plan',10,'draft')"
    )
    active = _stored(proposal_id="approved-10", lifecycle="approved")
    pending = _stored(proposal_id="pending-other", lifecycle="draft")
    pending["plan_id"] = "other-plan"
    pending["based_on_revision"] = 10
    current = {
        "current": True,
        "semantic_input_matches": True,
        "execution_config_matches": True,
    }

    with mock.patch.object(proposal_api.data, "conn", return_value=con), \
         mock.patch("modelark.proposal.load_proposal", side_effect=lambda _con, pid: {
             "approved-10": active,
             "pending-other": pending,
         }[pid]), \
         mock.patch("modelark.proposal.review_input_status", return_value=current):
        status = proposal_api.status()

    assert status["state"] == "review_pending_inactive"
    assert status["pending_proposal"]["plan_active"] is False
    assert status["pending_proposal"]["active_plan_id"] == "ark"
    assert status["pending_proposal"]["approvable"] is False
    assert "confirmation_phrase" not in status["pending_proposal"]


def test_status_refuses_multiple_current_drafts_without_choosing_by_timestamp():
    con = _connection(revision=10)
    con.executemany(
        "INSERT INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,created_at) "
        "VALUES(?,'ark',10,'draft',?)",
        [
            ("pending-a", "2026-09-03 00:00:00"),
            ("pending-b", "2026-09-03 00:00:01"),
        ],
    )

    with mock.patch.object(proposal_api.data, "conn", return_value=con):
        status = proposal_api.status()

    assert status["ok"] is False
    assert status["state"] == "review_ambiguous"
    assert status["code"] == "MULTIPLE_CURRENT_DRAFTS"
    assert status["evidence"]["proposal_ids"] == ["pending-a", "pending-b"]


def test_discard_supersedes_exact_draft_without_bumping_revision():
    con = _connection(revision=10)
    con.execute(
        "INSERT INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle) "
        "VALUES('pending-10','ark',10,'draft')"
    )

    with mock.patch.object(proposal_api.data, "conn", return_value=con):
        result = proposal_api.discard({"proposal_id": "pending-10"})

    assert result["ok"] is True
    assert result["state"] == "missing"
    assert result["discarded_proposal_id"] == "pending-10"
    assert con.execute(
        "SELECT lifecycle FROM placement_proposals WHERE proposal_id='pending-10'"
    ).fetchone()[0] == "superseded"
    assert con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()[0] == 10


def test_domain_refusal_is_preserved_as_typed_operator_result():
    con = _connection()
    refusal = proposal.Refusal(
        "PREVIEW_STALE",
        {"current": 10, "based_on": 9},
        ("preview_again",),
    )
    with mock.patch.object(proposal_api.data, "conn", return_value=con), \
         mock.patch("modelark.plan.active", return_value={"plan_id": "ark"}), \
         mock.patch("modelark.proposal.create_draft", side_effect=refusal):
        result = proposal_api.create_draft({})
    assert result == {
        "ok": False,
        "refused": True,
        "code": "PREVIEW_STALE",
        "error": "PREVIEW_STALE",
        "evidence": {"current": 10, "based_on": 9},
        "actions": ["preview_again"],
    }
