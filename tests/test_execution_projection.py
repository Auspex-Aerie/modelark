"""PR-09 Gate 1: project_pure — exact RFC codes, complete inputs, no false greens."""
from __future__ import annotations

import inspect
from copy import deepcopy
from types import SimpleNamespace

import _pr09_gate1_fixtures as f


def _approved(con, **seed_kw):
    f.seed_plan_selection(con, **seed_kw)
    _p, _pid, loaded = f.create_and_approve(con)
    return loaded


def test_project_pure_four_parameters():
    _mod, fn = f.project_pure_fn()
    params = list(inspect.signature(fn).parameters)
    assert len(params) >= 4, params
    assert params[0] == "proposal", params
    assert params[1] == "current_input", params
    assert params[2] == "current_graph", params
    assert "session" in params[3], params


def test_partial_file_shrink_preserves_map_and_hash():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved(con)
    before = deepcopy(proposal)
    archived = {
        ("org/a", "model.safetensors", "d0"): {
            "orig_sha256": "1" * 64, "orig_bytes": 100, "stored_bytes": 100},
    }
    inp, graph = f.complete_projection_inputs(proposal, archived=archived)
    out = f.require_success(project_pure(proposal, inp, graph, f.EMPTY_OVERLAY),
                            label="partial shrink")
    ph = f.get_field(out, "projection_hash")
    assert ph and len(str(ph)) == 64, ph
    assert proposal == before


def test_baseline_loss_is_approval_projection_violation():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/b",), with_archive_on=[("org/b", "d0")])
    _p, _pid, proposal = f.create_and_approve(con)
    drives = {
        "d0": SimpleNamespace(
            lifecycle="lost", eligibility="enabled",
            identity_epoch=1, identity_fingerprint=f.DRIVE_IDS["d0"]["fingerprint"],
            offline=False),
    }
    inp, graph = f.complete_projection_inputs(proposal, archived={}, drives=drives)
    f.assert_refuses(
        lambda: project_pure(proposal, inp, graph, f.EMPTY_OVERLAY),
        code="APPROVAL_PROJECTION_VIOLATION",
        label="baseline loss",
    )


def test_identity_epoch_drift_is_approved_target_identity_changed():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved(con)
    drives = {
        "d0": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=99, identity_fingerprint="9" * 64, offline=False),
        "d1": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=f.DRIVE_IDS["d1"]["epoch"],
            identity_fingerprint=f.DRIVE_IDS["d1"]["fingerprint"], offline=False),
    }
    inp, graph = f.complete_projection_inputs(proposal, drives=drives)
    f.assert_refuses(
        lambda: project_pure(proposal, inp, graph, f.EMPTY_OVERLAY),
        code="APPROVED_TARGET_IDENTITY_CHANGED",
        label="identity/epoch drift",
    )


def test_content_manifest_drift_is_approved_input_changed():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved(con)
    manifests = {
        t["repo_id"]: ("9" * 64)
        for t in proposal.get("tasks") or () if t.get("repo_id")
    }
    inp, graph = f.complete_projection_inputs(proposal, manifests=manifests)
    f.assert_refuses(
        lambda: project_pure(proposal, inp, graph, f.EMPTY_OVERLAY),
        code="APPROVED_INPUT_CHANGED",
        label="manifest content drift",
    )


def test_replica_source_not_ready_without_exact_source_fact():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/m",))
    con.execute("UPDATE models SET numcopies=2 WHERE repo_id='org/m'")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, _pid, proposal = f.create_and_approve(con)
    replica_tasks = [
        t for t in proposal.get("tasks") or ()
        if str(t.get("requirement_id", "")).startswith("replica")
    ]
    assert replica_tasks, f"fixture must produce replica task; tasks={proposal.get('tasks')}"
    # No archived source fact — must not report ready with exact source
    inp, graph = f.complete_projection_inputs(proposal, archived={})
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    if f.is_refusal(out):
        assert f.refusal_code(out) == "APPROVAL_PROJECTION_VIOLATION", f.refusal_code(out)
        return
    tasks = list(f.get_field(out, "tasks") or ())
    replica = [
        t for t in tasks
        if str(f.get_field(t, "requirement_id", "")).startswith("replica")
    ]
    assert replica, "projection must retain replica requirement row"
    for t in replica:
        state = f.get_field(t, "schedule_state")
        assert state == "waiting_dependency", (
            f"without exact source fact, replica must be waiting_dependency not {state!r}")


def test_compression_budget_overrun_is_no_longer_feasible():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved(con)
    # Fixed-point decimal string ratio, not binary float
    observed_ratio = {"org/a": "10.000"}
    archived = {
        ("org/a", "model.safetensors", "d0"): {
            "orig_sha256": "1" * 64, "orig_bytes": 100, "stored_bytes": 10**15},
    }
    inp, graph = f.complete_projection_inputs(
        proposal, archived=archived, observed_ratio=observed_ratio)
    f.assert_refuses(
        lambda: project_pure(proposal, inp, graph, f.EMPTY_OVERLAY),
        code="APPROVED_PLACEMENT_NO_LONGER_FEASIBLE",
        label="compression budget overrun",
    )


def test_offline_target_does_not_remap_and_uses_capacity_unknown_or_await():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved(con)
    # d0 offline; d1 online — must not remap approved d0 work to d1
    drives = {
        "d0": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=f.DRIVE_IDS["d0"]["epoch"],
            identity_fingerprint=f.DRIVE_IDS["d0"]["fingerprint"], offline=True),
        "d1": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=f.DRIVE_IDS["d1"]["epoch"],
            identity_fingerprint=f.DRIVE_IDS["d1"]["fingerprint"], offline=False),
    }
    evidence = {
        "d0": SimpleNamespace(kind="unknown", executable=False, admissible_free=None),
        "d1": SimpleNamespace(kind="offline", executable=True, admissible_free=10**12),
    }
    inp, graph = f.complete_projection_inputs(proposal, drives=drives, evidence=evidence)
    before_targets = {
        t["requirement_id"]: t.get("target_drive")
        for t in proposal.get("tasks") or () if t.get("target_drive")
    }
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    if f.is_refusal(out):
        assert f.refusal_code(out) == "CAPACITY_EVIDENCE_UNKNOWN", f.refusal_code(out)
        return
    tasks = list(f.get_field(out, "tasks") or ())
    for t in tasks:
        rid = f.get_field(t, "requirement_id")
        tgt = f.get_field(t, "target_drive")
        if rid in before_targets and before_targets[rid] == "d0":
            assert tgt == "d0", f"offline target must not remap d0→{tgt}"


def test_expanded_requirement_set_is_approved_input_changed():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved(con)
    inp, graph = f.complete_projection_inputs(
        proposal, extra_requirements=["primary:org/extra"])
    f.assert_refuses(
        lambda: project_pure(proposal, inp, graph, f.EMPTY_OVERLAY),
        code="APPROVED_INPUT_CHANGED",
        label="expanded requirements",
    )


def test_schedule_only_overlay_parks_without_changing_requirement_set():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved(con)
    before_ids = sorted(t["requirement_id"] for t in proposal.get("tasks") or ())
    inp, graph = f.complete_projection_inputs(proposal)
    overlay = SimpleNamespace(parked_gated_repos=frozenset({"org/a"}))
    out = f.require_success(
        project_pure(proposal, inp, graph, overlay), label="schedule overlay")
    tasks = list(f.get_field(out, "tasks") or ())
    after_ids = sorted(f.get_field(t, "requirement_id") for t in tasks)
    # Remaining ids ⊆ approved; schedule_state only
    assert set(after_ids) <= set(before_ids)
    parked = [
        t for t in tasks
        if "org/a" in str(f.get_field(t, "requirement_id", ""))
        and f.get_field(t, "schedule_state") == "parked_gated"
    ]
    assert parked, "gated overlay must set schedule_state=parked_gated for org/a work"


def test_deterministic_projection_hash():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved(con)
    inp, graph = f.complete_projection_inputs(proposal)
    a = f.require_success(project_pure(proposal, inp, graph, f.EMPTY_OVERLAY), label="hash a")
    b = f.require_success(project_pure(proposal, inp, graph, f.EMPTY_OVERLAY), label="hash b")
    assert f.get_field(a, "projection_hash") == f.get_field(b, "projection_hash")
    assert len(str(f.get_field(a, "projection_hash"))) == 64


def test_invalid_lost_work_refuses_and_does_not_mutate_map():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved(con)
    before = deepcopy(proposal)
    drives = {
        label: SimpleNamespace(
            lifecycle="lost", eligibility="enabled",
            identity_epoch=meta["epoch"], identity_fingerprint=meta["fingerprint"],
            offline=False)
        for label, meta in f.DRIVE_IDS.items()
    }
    inp, graph = f.complete_projection_inputs(proposal, drives=drives)
    f.assert_refuses(
        lambda: project_pure(proposal, inp, graph, f.EMPTY_OVERLAY),
        code="APPROVAL_PROJECTION_VIOLATION",
        label="lost work invalid",
    )
    assert proposal == before
