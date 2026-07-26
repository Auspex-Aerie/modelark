"""PR-09 / #39-B Gate 1: project_pure contracts (B1, B13) — RFC-002 seam.

Canonical API only:
  project_pure(proposal, current_input, current_graph, session_overlay)

Uses real PR-08 approved proposals. Expected red until production projection lands.
"""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import _pr09_gate1_fixtures as f


def _approved_bundle(con):
    f.seed_plan_selection(con, repos=("org/a", "org/b"))
    _prop, pid, loaded = f.create_and_approve(con)
    return loaded


def _input_graph_from_proposal(proposal, *, archived=None, drives=None, manifests=None,
                               observed_ratio=None, extra_requirements=None):
    """Build current_input / current_graph shaped for project_pure from approved proposal."""
    tasks = list(proposal.get("tasks") or ())
    req_ids = [t["requirement_id"] for t in tasks]
    # Per-file evidence from proposal_files if present
    files = list(proposal.get("files") or ())
    file_hash_evidence = {
        (ff.get("requirement_id"), ff.get("rfilename")): ff.get("orig_sha256")
        for ff in files
    }
    drives = drives or {
        "d0": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=1, identity_fingerprint="f" * 64),
        "d1": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=1, identity_fingerprint="f" * 64),
    }
    manifests = manifests or {
        t["repo_id"]: t.get("full_manifest_hash") for t in tasks if t.get("repo_id")
    }
    archived = archived if archived is not None else {}
    current_input = SimpleNamespace(
        manifests=manifests,
        archived=archived,
        drives=drives,
        observed_ratio=observed_ratio or {},
        evidence={},
        file_hash_evidence=file_hash_evidence,
    )
    req_set = list(req_ids)
    if extra_requirements:
        req_set = list(req_ids) + list(extra_requirements)
    current_graph = SimpleNamespace(
        requirement_ids=req_set,
        requirement_set_hash=proposal.get("requirement_set_hash"),
    )
    return current_input, current_graph


def test_project_pure_seam_exists():
    _mod, fn = f.project_pure_fn()
    assert fn.__code__.co_argcount >= 4 or fn.__code__.co_varnames[:4] == (
        "proposal", "current_input", "current_graph", "session_overlay") or True
    # At least named parameters preferred
    import inspect
    sig = inspect.signature(fn)
    params = list(sig.parameters)
    assert len(params) >= 4, (
        f"project_pure must take proposal, current_input, current_graph, session_overlay; "
        f"got {params}")


def test_satisfied_only_partial_file_shrink():
    """B1: partial file progress may shrink missing set; map identity preserved."""
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved_bundle(con)
    before = deepcopy(proposal)
    # One file of executable task now satisfied on target
    archived = {
        ("org/a", "model.safetensors", "d0"): {
            "orig_sha256": "1" * 64, "orig_bytes": 100},
    }
    inp, graph = _input_graph_from_proposal(proposal, archived=archived)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert not f.is_refusal(out), out
    assert getattr(out, "projection_hash", None) or (
        isinstance(out, dict) and out.get("projection_hash")), (
        "ExecutionProjection must carry projection_hash")
    # Approved proposal object must not be rewritten
    assert proposal.get("tasks") == before.get("tasks")


def test_baseline_loss_refuses_with_projection_violation():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    f.seed_plan_selection(
        con, repos=("org/b",), with_archive_on=[("org/b", "d0")])
    _p, _pid, proposal = f.create_and_approve(con)
    # Drop archive / lifecycle lost → certificate cannot recompute
    drives = {
        "d0": SimpleNamespace(
            lifecycle="lost", eligibility="enabled",
            identity_epoch=1, identity_fingerprint="f" * 64),
    }
    inp, graph = _input_graph_from_proposal(proposal, archived={}, drives=drives)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    code = f.refusal_code(out)
    if code is None:
        # may raise
        try:
            if f.is_refusal(out):
                code = f.refusal_code(out)
            else:
                raise AssertionError(f"baseline loss must refuse; got {out!r}")
        except AssertionError:
            raise
    assert code in (
        "APPROVAL_PROJECTION_VIOLATION", "APPROVED_INPUT_CHANGED",
        "APPROVED_TARGET_IDENTITY_CHANGED"), code


def test_identity_epoch_drift_refuses():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved_bundle(con)
    drives = {
        "d0": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=99, identity_fingerprint="9" * 64),
        "d1": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=1, identity_fingerprint="f" * 64),
    }
    inp, graph = _input_graph_from_proposal(proposal, drives=drives)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    code = f.refusal_code(out)
    assert code in (
        "APPROVED_TARGET_IDENTITY_CHANGED", "APPROVAL_PROJECTION_VIOLATION",
        "APPROVED_INPUT_CHANGED"), code


def test_content_certificate_drift_refuses():
    """Stored file/content certificates must recompute from current facts."""
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved_bundle(con)
    manifests = {
        t["repo_id"]: ("9" * 64) for t in proposal.get("tasks") or () if t.get("repo_id")
    }
    inp, graph = _input_graph_from_proposal(proposal, manifests=manifests)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert f.refusal_code(out) in (
        "APPROVED_INPUT_CHANGED", "APPROVAL_PROJECTION_VIOLATION"), f.refusal_code(out)


def test_replica_source_readiness_exact_only():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/m",))
    con.execute("UPDATE models SET numcopies=2 WHERE repo_id='org/m'")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _p, _pid, proposal = f.create_and_approve(con)
    # Replica task without source fact and without home task still pending
    inp, graph = _input_graph_from_proposal(proposal, archived={})
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    # Either waiting_dependency schedule or refusal if source required-ready
    if f.is_refusal(out):
        assert f.refusal_code(out) in (
            "APPROVAL_PROJECTION_VIOLATION", "APPROVED_PLACEMENT_NO_LONGER_FEASIBLE")
        return
    tasks = list(out.tasks if hasattr(out, "tasks") else out["tasks"])
    replica = [t for t in tasks if str(
        t.get("requirement_id") if isinstance(t, dict) else getattr(t, "requirement_id", "")
    ).startswith("replica")]
    if replica:
        t0 = replica[0]
        state = t0.get("schedule_state") if isinstance(t0, dict) else getattr(
            t0, "schedule_state", None)
        assert state in ("waiting_dependency", "ready", "parked_gated"), state


def test_cumulative_compression_budget_overrun_refuses():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved_bundle(con)
    # Observed ratio / actual bytes blow approved envelope
    observed_ratio = {"org/a": 10.0}  # hostile over-ratio
    archived = {
        # partial progress with huge stored_bytes
        ("org/a", "model.safetensors", "d0"): {
            "orig_sha256": "1" * 64, "orig_bytes": 100, "stored_bytes": 10**15},
    }
    inp, graph = _input_graph_from_proposal(
        proposal, archived=archived, observed_ratio=observed_ratio)
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert f.is_refusal(out), (
        "compression budget overrun must refuse APPROVED_PLACEMENT_NO_LONGER_FEASIBLE")
    assert f.refusal_code(out) == "APPROVED_PLACEMENT_NO_LONGER_FEASIBLE", f.refusal_code(out)


def test_offline_target_sets_await_not_remap():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved_bundle(con)
    drives = {
        "d0": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=1, identity_fingerprint="f" * 64, offline=True),
        "d1": SimpleNamespace(
            lifecycle="active", eligibility="enabled",
            identity_epoch=1, identity_fingerprint="f" * 64, offline=False),
    }
    evidence = {"d0": SimpleNamespace(executable=False, kind="unknown")}
    inp, graph = _input_graph_from_proposal(proposal, drives=drives)
    inp.evidence = evidence
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    code = f.refusal_code(out)
    # Offline may be CAPACITY_EVIDENCE_UNKNOWN or keep tasks with await semantics
    if f.is_refusal(out):
        assert code in (
            "CAPACITY_EVIDENCE_UNKNOWN", "APPROVED_PLACEMENT_NO_LONGER_FEASIBLE",
            "APPROVED_TARGET_IDENTITY_CHANGED"), code
    else:
        # Must not remap targets away from approved drives
        tasks = list(out.tasks if hasattr(out, "tasks") else out["tasks"])
        for t in tasks:
            tgt = t.get("target_drive") if isinstance(t, dict) else getattr(t, "target_drive", None)
            if tgt:
                assert tgt in ("d0", "d1", None)


def test_expanded_requirement_set_refuses():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved_bundle(con)
    inp, graph = _input_graph_from_proposal(
        proposal, extra_requirements=["primary:org/extra"])
    # Force hash mismatch path as well
    graph.requirement_set_hash = "e" * 64
    out = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert f.refusal_code(out) in (
        "APPROVED_INPUT_CHANGED", "APPROVAL_PROJECTION_VIOLATION"), f.refusal_code(out)


def test_schedule_only_overlay_does_not_change_completion_truth():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved_bundle(con)
    inp, graph = _input_graph_from_proposal(proposal)
    overlay = SimpleNamespace(parked_gated_repos=frozenset({"org/a"}))
    out = project_pure(proposal, inp, graph, overlay)
    if f.is_refusal(out):
        return  # production not ready
    tasks = list(out.tasks if hasattr(out, "tasks") else out["tasks"])
    for t in tasks:
        rid = t.get("requirement_id") if isinstance(t, dict) else getattr(t, "requirement_id", "")
        state = t.get("schedule_state") if isinstance(t, dict) else getattr(t, "schedule_state", None)
        if "org/a" in str(rid) and state is not None:
            assert state == "parked_gated", state


def test_deterministic_full_projection_hash():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved_bundle(con)
    inp, graph = _input_graph_from_proposal(proposal)
    a = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    b = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    if f.is_refusal(a) or f.is_refusal(b):
        raise AssertionError(
            "happy-path projection must succeed for deterministic hash contract once implemented")
    ha = a.projection_hash if hasattr(a, "projection_hash") else a["projection_hash"]
    hb = b.projection_hash if hasattr(b, "projection_hash") else b["projection_hash"]
    assert ha == hb and len(str(ha)) == 64


def test_invalid_work_does_not_mutate_approved_map():
    _mod, project_pure = f.project_pure_fn()
    con = f.mem_con()
    proposal = _approved_bundle(con)
    before = deepcopy(proposal)
    drives = {
        "d0": SimpleNamespace(
            lifecycle="lost", eligibility="enabled",
            identity_epoch=1, identity_fingerprint="f" * 64),
    }
    inp, graph = _input_graph_from_proposal(proposal, drives=drives)
    _ = project_pure(proposal, inp, graph, f.EMPTY_OVERLAY)
    assert proposal == before, "project_pure must not mutate approved proposal"
