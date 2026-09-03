"""DEF-042 operator-directed successor placement contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from modelark import (
    archive_manifest,
    budgets,
    candidates,
    execution_session,
    execution_service,
    placement,
    proposal,
)
from modelark.execution_config import hash_config
from modelark.web import proposal_api


def _candidate(requirement_id: str, target: str, size: int = 60) -> candidates.Candidate:
    manifest = archive_manifest.ManifestFile(
        "model.safetensors", size, "a" * 64, "safetensors", "bf16", "raw"
    )
    file_budget = budgets.FileBudget(
        manifest.rfilename,
        size,
        size,
        0,
        0,
        "estimate",
    )
    return candidates.Candidate(
        requirement_id=requirement_id,
        task_kind=candidates.TaskKind.FETCH,
        target_drive=target,
        source=None,
        depends_on_requirement=None,
        reused_files=(),
        missing_files=(manifest,),
        budget=budgets.CandidateBudget(size, size, 0, 0, (file_budget,)),
        movement_cost=candidates.MovementCost(size),
    )


def _successor_solver_input() -> placement.SolverInput:
    requirement_ids = ("primary:org/a", "primary:org/b")
    graph = candidates.RequirementGraph(
        tuple(
            candidates.CopyRequirement(
                rid,
                rid.split(":", 1)[1],
                candidates.RequirementKind.PRIMARY,
                ("drive-new", "drive-other"),
            )
            for rid in requirement_ids
        ),
        "requirements",
    )
    cset = candidates.CandidateSet(
        satisfied=(),
        by_requirement=tuple(
            (
                rid,
                (
                    _candidate(rid, "drive-other"),
                    _candidate(rid, "drive-new"),
                ),
            )
            for rid in requirement_ids
        ),
        drift=(),
        blocked=(),
    )
    drives = tuple(
        candidates.DriveFact(label, "primary", False, 1_000, 1_000, 1)
        for label in ("drive-new", "drive-other")
    )
    evidence = tuple(
        (
            label,
            placement.DriveEvidenceFact(label, True, "live", None),
        )
        for label in ("drive-new", "drive-other")
    )
    return placement.SolverInput(
        graph=graph,
        candidates=cset,
        drives=drives,
        # The successor's executable budget is its predecessor lane. It can accept one
        # 60-byte task, but not both; overflow must remain on the prior target.
        executable_budget=(("drive-new", 100), ("drive-other", 120)),
        max_usable_for_epoch=(("drive-new", 100), ("drive-other", 120)),
        drive_evidence=evidence,
        capacity_mode="guaranteed",
        policy_version="tiered_v2",
        bounds=placement.SolverBounds(100, 100),
        preference=placement.SuccessorPreference(
            predecessor_drive="drive-old",
            successor_drive="drive-new",
            lane_bytes=100,
            baseline_targets=tuple((rid, "drive-other") for rid in requirement_ids),
        ),
    )


def test_successor_lane_is_preferred_but_bounded_and_stable():
    inp = _successor_solver_input()

    gate = placement.gate_b(inp)
    assert gate.code == "FEASIBLE"
    improved = placement.improve(inp, gate.assignment)

    targets = {task.requirement_id: task.target_drive for task in improved.assignment.tasks}
    assert targets == {
        "primary:org/a": "drive-new",
        "primary:org/b": "drive-other",
    }
    assert sum(
        task.durable
        for task in improved.assignment.tasks
        if task.target_drive == "drive-new"
    ) <= inp.preference.lane_bytes


def test_successor_draft_binds_backend_selected_plan_and_approved_baseline():
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute(
        "CREATE TABLE planner_state(singleton_id INTEGER PRIMARY KEY, "
        "planner_revision INTEGER NOT NULL, active_approved_proposal_id TEXT, "
        "next_fencing_token INTEGER NOT NULL)"
    )
    con.execute("INSERT INTO planner_state VALUES(1,10,'approved-10',0)")
    stored = {
        "proposal_id": "successor-11",
        "plan_id": "ark",
        "based_on_revision": 10,
        "lifecycle": "draft",
        "canonical_hash": "c" * 64,
        "mutation_kind": "successor_replan",
        "mutation_args": ("drive-02", "drive-07", "approved-10"),
        "gate_b_code": "FEASIBLE",
        "tasks": [],
        "files": [],
    }
    baseline = {
        "proposal_id": "approved-10",
        "plan_id": "ark",
        "lifecycle": "approved",
    }
    body = {
        "mode": "successor",
        "predecessor_drive": "drive-02",
        "successor_drive": "drive-07",
        # These fields are deliberately hostile client-authored planning authority.
        "plan_id": "other",
        "baseline_proposal_id": "forged",
        "lane_bytes": 1,
    }

    with mock.patch.object(proposal_api.data, "conn", return_value=con), \
         mock.patch("modelark.plan.active", return_value={"plan_id": "ark"}), \
         mock.patch("modelark.proposal.create_draft", return_value={
             "proposal_id": "successor-11"
         }) as create, \
         mock.patch("modelark.proposal.load_proposal", side_effect=[baseline, stored]), \
         mock.patch("modelark.proposal.review_input_status", return_value={
             "current": True,
         }), \
         mock.patch.object(proposal_api, "_successor_review", return_value={
             "predecessor_drive": "drive-02",
             "successor_drive": "drive-07",
             "baseline_proposal_id": "approved-10",
             "lane_bytes": 1_000,
             "changed_requirements": 0,
             "moved_to_successor": 0,
             "unchanged_requirements": 0,
         }):
        result = proposal_api.create_draft(body)

    assert result["ok"] is True
    assert result["review"]["successor"]["baseline_proposal_id"] == "approved-10"
    create.assert_called_once_with(
        con,
        plan_id="ark",
        mutation=(
            "successor_replan",
            ("drive-02", "drive-07", "approved-10"),
        ),
    )


def test_successor_draft_refuses_a_stale_active_baseline():
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute(
        "CREATE TABLE planner_state(singleton_id INTEGER PRIMARY KEY, "
        "planner_revision INTEGER NOT NULL, active_approved_proposal_id TEXT, "
        "next_fencing_token INTEGER NOT NULL)"
    )
    con.execute("INSERT INTO planner_state VALUES(1,10,'approved-10',0)")
    baseline = {"proposal_id": "approved-10", "lifecycle": "approved"}

    with mock.patch.object(proposal_api.data, "conn", return_value=con), \
         mock.patch("modelark.plan.active", return_value={"plan_id": "ark"}), \
         mock.patch("modelark.proposal.load_proposal", return_value=baseline), \
         mock.patch("modelark.proposal.review_input_status", return_value={
             "current": False,
             "semantic_input_matches": False,
             "execution_config_matches": True,
         }), \
         mock.patch("modelark.proposal.create_draft") as create:
        result = proposal_api.create_draft({
            "mode": "successor",
            "predecessor_drive": "drive-02",
            "successor_drive": "drive-07",
        })

    assert result["ok"] is False
    assert result["code"] == "SUCCESSOR_BASELINE_STALE"
    create.assert_not_called()


def _successor_fact_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.executescript(
        "CREATE TABLE planner_state(singleton_id INTEGER PRIMARY KEY, "
        "planner_revision INTEGER NOT NULL, active_approved_proposal_id TEXT, "
        "next_fencing_token INTEGER NOT NULL);"
        "INSERT INTO planner_state VALUES(1,10,'approved-10',0);"
        "CREATE TABLE plan_drives(plan_id TEXT, drive_label TEXT);"
        "INSERT INTO plan_drives VALUES('ark','drive-02'),('ark','drive-07');"
        "CREATE TABLE drives(drive_label TEXT PRIMARY KEY, role TEXT, raid_backed INTEGER, "
        "capacity_bytes INTEGER, filesystem_capacity_bytes INTEGER, lifecycle TEXT, "
        "eligibility TEXT, identity_epoch INTEGER, identity_fingerprint TEXT);"
        "INSERT INTO drives VALUES"
        "('drive-02','primary',0,4000,4000,'lost','excluded',1,'old'),"
        "('drive-07','primary',0,8000,8000,'active','enabled',1,'new');"
        "CREATE TABLE selection(repo_id TEXT, finalized_at TEXT);"
        "CREATE TABLE models(repo_id TEXT, numcopies INTEGER);"
        "INSERT INTO models VALUES('org/a',1);"
        "CREATE TABLE archived(repo_id TEXT, rfilename TEXT, drive_label TEXT, "
        "orig_sha256 TEXT, stored_bytes INTEGER, orig_bytes INTEGER, "
        "compressed INTEGER, annex_key TEXT);"
    )
    return con


def test_invalidated_successor_facts_report_stale_instead_of_raising():
    con = _successor_fact_db()
    mutation = ("successor_replan", ("drive-02", "drive-07", "approved-10"))
    baseline = {
        "proposal_id": "approved-10",
        "plan_id": "ark",
        "lifecycle": "approved",
        "tasks": [{
            "requirement_id": "primary:org/a",
            "repo_id": "org/a",
            "row_kind": "executable",
            "target_drive": "drive-03",
        }],
    }
    successor = {
        "proposal_id": "successor-11",
        "plan_id": "ark",
        "lifecycle": "approved",
        "mutation_kind": mutation[0],
        "mutation_args": mutation[1],
        "tasks": [{
            "requirement_id": "primary:org/a",
            "repo_id": "org/a",
            "row_kind": "executable",
            "target_drive": "drive-07",
        }],
    }
    with mock.patch("modelark.proposal.load_proposal", return_value=baseline):
        successor["semantic_input_hash"] = proposal._semantic_input_hash(
            con, "ark", mutation
        )
        con.execute(
            "UPDATE drives SET lifecycle='lost',eligibility='excluded' "
            "WHERE drive_label='drive-07'"
        )
        with mock.patch.object(
            proposal, "_current_execution_config_hash", return_value="cfg"
        ):
            successor["execution_config_hash"] = "cfg"
            status = proposal.review_input_status(con, successor)
        review = proposal_api._successor_review(con, successor)

    assert status["current"] is False
    assert status["semantic_input_matches"] is False
    assert review["successor_drive"] == "drive-07"
    assert review["moved_to_successor"] == 1


def test_fill_projection_refuses_when_successor_predecessor_leaves_plan():
    con = _successor_fact_db()
    mutation = ("successor_replan", ("drive-02", "drive-07", "approved-10"))
    baseline = {
        "proposal_id": "approved-10",
        "plan_id": "ark",
        "lifecycle": "approved",
        "tasks": [],
    }
    successor = {
        "proposal_id": "successor-11",
        "plan_id": "ark",
        "lifecycle": "approved",
        "mutation_kind": mutation[0],
        "mutation_args": mutation[1],
        "tasks": [],
        "files": [],
    }
    services = SimpleNamespace(
        observe_exact_capacity=lambda *_args, **_kwargs: {
            "drive-07": SimpleNamespace(
                kind="offline", executable=True, admissible_free=8_000
            )
        }
    )

    with mock.patch("modelark.proposal.load_proposal", return_value=baseline):
        successor["semantic_input_hash"] = proposal._semantic_input_hash(
            con, "ark", mutation
        )
        con.execute(
            "DELETE FROM plan_drives WHERE plan_id='ark' AND drive_label='drive-02'"
        )
        with pytest.raises(proposal.Refusal) as caught:
            execution_session._catalog_projection_bundle(
                con,
                successor,
                ["drive-07"],
                services,
                {"capacity_mode": "guaranteed"},
            )

    assert caught.value.code == "APPROVED_INPUT_CHANGED"
    assert caught.value.evidence["reason"] == "semantic_input_unavailable"
    assert caught.value.evidence["cause_code"] == "SUCCESSOR_DRIVE_NOT_IN_PLAN"


def test_successor_versions_are_bound_into_preview_and_fill_config():
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.executescript(
        "CREATE TABLE planner_state(singleton_id INTEGER PRIMARY KEY, "
        "planner_revision INTEGER NOT NULL, active_approved_proposal_id TEXT, "
        "next_fencing_token INTEGER NOT NULL);"
        "INSERT INTO planner_state VALUES(1,11,'successor-11',0);"
        "CREATE TABLE placement_proposals(proposal_id TEXT PRIMARY KEY, "
        "policy_version TEXT, solver_version TEXT);"
        "INSERT INTO placement_proposals VALUES"
        "('successor-11','successor_v1','successor_lane_v1');"
    )
    mutation = ("successor_replan", ("drive-02", "drive-07", "approved-10"))
    compression = {"enabled": True, "codec": "streamznn", "level": 3}
    expected = hash_config({
        "capacity_mode": "guaranteed",
        "policy_version": "successor_v1",
        "solver_version": "successor_lane_v1",
        "compression": compression,
        "numcopies_default": 1,
    })
    with mock.patch("modelark.plan.get", return_value={
        "capacity_mode": "guaranteed",
    }), mock.patch("modelark.wishlist.compression", return_value=compression):
        assert proposal._current_execution_config_hash(con, "ark", mutation) == expected
    with mock.patch("modelark.plan.active", return_value={
        "capacity_mode": "guaranteed",
    }), mock.patch("modelark.wishlist.compression", return_value=compression):
        live = execution_service.production_services(
            con
        ).config.read_graph_affecting_config()
    assert hash_config(live) == expected


def test_successor_workflow_is_a_named_review_action_not_a_fill_side_effect():
    root = Path(__file__).parents[1]
    html = (root / "modelark/web/static/index.html").read_text()
    js = (root / "modelark/web/static/proposal.js").read_text()
    server = (root / "modelark/web/server.py").read_text()

    assert 'id="proposalSuccessor"' in html
    assert 'id="successorPredecessor"' in html
    assert 'id="successorDrive"' in html
    assert "/api/proposal/successor-options" in js
    assert 'mode: "successor"' in js
    assert "/api/proposal/successor-options" in server
    assert 'id="proposalDiscard"' in html
    assert 'state: "review_pending"' in js
    assert "/api/proposal/discard" in js
    assert "/api/proposal/discard" in server
    assert "fillStart" not in js[js.index('mode: "successor"'):][:400]
