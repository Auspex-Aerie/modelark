"""Operator-facing adapter for canonical placement proposal review and approval.

The browser supplies no planning authority. Draft creation always uses the active plan and the
existing ``adopt_current`` proposal domain; approval accepts only a stored proposal id plus an exact
backend-authored confirmation phrase. The domain retains all CAS, fencing, evidence, and assignment
revalidation authority.
"""
from __future__ import annotations

from collections import defaultdict

from modelark.web import data


def _refused(code: str, evidence=None, actions=()) -> dict:
    return {
        "ok": False,
        "refused": True,
        "code": code,
        "error": code,
        "evidence": evidence,
        "actions": list(actions or ()),
    }


def _domain_refusal(exc) -> dict:
    return _refused(exc.code, exc.evidence, exc.actions)


def _planner_state(con) -> dict:
    row = con.execute(
        "SELECT planner_revision, active_approved_proposal_id, next_fencing_token "
        "FROM planner_state WHERE singleton_id=1"
    ).fetchone()
    if row is None:
        raise RuntimeError("planner_state singleton is missing")
    return {
        "planner_revision": int(row[0]),
        "active_approved_proposal_id": row[1],
        "next_fencing_token": int(row[2]),
    }


def _review(stored: dict, *, include_assignments: bool) -> dict:
    tasks = list(stored.get("tasks") or ())
    files = list(stored.get("files") or ())
    drive_rows = defaultdict(lambda: {
        "requirements": 0,
        "executable": 0,
        "baseline_satisfied": 0,
        "source_requirements": 0,
        "guaranteed_bytes": 0,
        "expected_bytes": 0,
    })
    assignments = []
    repos = set()
    guaranteed = 0
    expected = 0
    executable = 0
    baseline = 0
    for task in tasks:
        row_kind = task.get("row_kind") or ""
        executable += int(row_kind == "executable")
        baseline += int(row_kind == "baseline_satisfied")
        repo_id = str(task.get("repo_id") or "")
        if repo_id:
            repos.add(repo_id)
        target = task.get("target_drive") or task.get("satisfying_drive")
        guaranteed_bytes = int(task.get("guaranteed_durable") or 0)
        expected_bytes = int(task.get("expected_durable") or 0)
        guaranteed += guaranteed_bytes
        expected += expected_bytes
        if target:
            drive = drive_rows[str(target)]
            drive["requirements"] += 1
            drive["executable"] += int(row_kind == "executable")
            drive["baseline_satisfied"] += int(row_kind == "baseline_satisfied")
            drive["source_requirements"] += int(bool(task.get("source_drive")))
            drive["guaranteed_bytes"] += guaranteed_bytes
            drive["expected_bytes"] += expected_bytes
        if include_assignments:
            assignments.append({
                "requirement_id": task.get("requirement_id"),
                "row_kind": row_kind,
                "repo_id": repo_id,
                "target_drive": target,
                "source_drive": task.get("source_drive"),
                "full_manifest_hash": task.get("full_manifest_hash"),
                "order_key": int(task.get("order_key") or 0),
                "guaranteed_durable": guaranteed_bytes,
                "expected_durable": expected_bytes,
                "identity_epoch": task.get("identity_epoch"),
            })
    drives = [
        {"drive_label": label, **drive_rows[label]}
        for label in sorted(drive_rows)
    ]
    approvable = (
        stored.get("lifecycle") == "draft"
        and stored.get("gate_b_code") in (None, "FEASIBLE")
    )
    out = {
        "proposal_id": stored["proposal_id"],
        "lifecycle": stored.get("lifecycle"),
        "plan_id": stored.get("plan_id"),
        "based_on_revision": int(stored.get("based_on_revision") or 0),
        "canonical_hash": stored.get("canonical_hash"),
        "mutation_kind": stored.get("mutation_kind"),
        "mutation_args": list(stored.get("mutation_args") or ()),
        "serializer_version": stored.get("serializer_version"),
        "requirement_set_hash": stored.get("requirement_set_hash"),
        "semantic_input_hash": stored.get("semantic_input_hash"),
        "selection_before_hash": stored.get("selection_before_hash"),
        "selection_after_hash": stored.get("selection_after_hash"),
        "capacity_mode": stored.get("capacity_mode"),
        "policy_version": stored.get("policy_version"),
        "solver_version": stored.get("solver_version"),
        "gate_b_code": stored.get("gate_b_code"),
        "derivation_mode": stored.get("derivation_mode"),
        "execution_config_hash": stored.get("execution_config_hash"),
        "created_at": stored.get("created_at"),
        "approved_at": stored.get("approved_at"),
        "approvable": approvable,
        "totals": {
            "requirements": len(tasks),
            "executable": executable,
            "baseline_satisfied": baseline,
            "repositories": len(repos),
            "files": len(files),
            "guaranteed_bytes": guaranteed,
            "expected_bytes": expected,
        },
        "drives": drives,
    }
    if include_assignments:
        out["assignments"] = assignments
    if approvable:
        out["confirmation_phrase"] = f"APPROVE {stored['proposal_id']}"
    return out


def _status_on(con) -> dict:
    from modelark import proposal

    state = _planner_state(con)
    active_id = state["active_approved_proposal_id"]
    if not active_id:
        return {
            "ok": True,
            "state": "missing",
            "planner_revision": state["planner_revision"],
            "active_proposal": None,
        }
    try:
        stored = proposal.load_proposal(con, active_id)
    except KeyError:
        return _refused(
            "ACTIVE_PROPOSAL_MISSING",
            {"proposal_id": active_id},
            ("inspect_catalog", "review_again"),
        )
    input_status = proposal.review_input_status(con, stored)
    current = bool(input_status.get("current"))
    return {
        "ok": True,
        "state": "approved_current" if current else "approved_stale",
        "planner_revision": state["planner_revision"],
        "active_proposal": _review(stored, include_assignments=False),
        "input_status": input_status,
    }


def status() -> dict:
    with data._lock:
        return _status_on(data.conn())


def create_draft(_body: dict | None = None) -> dict:
    """Create one fresh ``adopt_current`` draft for the server-selected active plan."""
    from modelark import plan, proposal

    try:
        with data._lock:
            con = data.conn()
            active = plan.active(con)
            if active is None:
                return _refused("NO_ACTIVE_PLAN", None, ("select_plan",))
            created = proposal.create_draft(
                con,
                plan_id=active["plan_id"],
                mutation=("adopt_current", ()),
            )
            stored = proposal.load_proposal(con, created["proposal_id"])
            return {
                "ok": True,
                "state": "draft",
                "review": _review(stored, include_assignments=True),
            }
    except proposal.Refusal as exc:
        return _domain_refusal(exc)


def approve(body: dict) -> dict:
    """Approve only the named stored draft after its exact backend phrase is supplied."""
    from modelark import proposal

    proposal_id = str(body.get("proposal_id") or "")
    if not proposal_id:
        return _refused("PROPOSAL_ID_REQUIRED", None, ("review_again",))
    try:
        with data._lock:
            con = data.conn()
            try:
                stored = proposal.load_proposal(con, proposal_id)
            except KeyError:
                return _refused(
                    "PROPOSAL_NOT_FOUND",
                    {"proposal_id": proposal_id},
                    ("review_again",),
                )
            expected = f"APPROVE {proposal_id}"
            if body.get("confirmation") != expected:
                return _refused(
                    "PROPOSAL_CONFIRMATION_MISMATCH",
                    {"expected": expected, "proposal_id": proposal_id},
                    ("type_exact_confirmation",),
                )
            mutation = (
                stored.get("mutation_kind") or "adopt_current",
                tuple(stored.get("mutation_args") or ()),
            )
            proposal.approve(con, proposal_id, mutation=mutation)
            return _status_on(con)
    except proposal.Refusal as exc:
        return _domain_refusal(exc)
