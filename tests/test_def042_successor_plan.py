"""DEF-042 operator-directed successor placement contracts."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

from modelark import archive_manifest, budgets, candidates, placement
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
         mock.patch("modelark.proposal.load_proposal", return_value=stored), \
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
    assert "fillStart" not in js[js.index('mode: "successor"'):][:400]
