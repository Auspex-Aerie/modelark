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
