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


def _successor_review(con, stored: dict) -> dict:
    """Explain a successor proposal against its exact approved baseline."""
    from modelark import proposal

    mutation = (
        stored.get("mutation_kind") or "adopt_current",
        tuple(stored.get("mutation_args") or ()),
    )
    baseline_id = mutation[1][2]
    baseline = proposal.load_proposal(con, baseline_id)
    before = {
        str(task.get("requirement_id")): str(
            task.get("target_drive") or task.get("satisfying_drive") or ""
        )
        for task in baseline.get("tasks") or ()
    }
    after = {
        str(task.get("requirement_id")): str(
            task.get("target_drive") or task.get("satisfying_drive") or ""
        )
        for task in stored.get("tasks") or ()
    }
    shared = sorted(set(before) & set(after))
    changed = [rid for rid in shared if before[rid] != after[rid]]
    predecessor_drive, successor_drive = mutation[1][:2]
    context_refusal = None
    try:
        preference = proposal.successor_preference(
            con,
            stored["plan_id"],
            mutation,
            require_active_baseline=False,
            validate_current_drives=False,
        )
        lane_bytes = preference.lane_bytes
    except proposal.Refusal as exc:
        # A historical approval must remain inspectable after its drive facts are
        # invalidated or removed. review_input_status reports it stale separately.
        lane_bytes = None
        context_refusal = exc.code
    moved = [rid for rid in changed if after[rid] == successor_drive]
    return {
        "predecessor_drive": predecessor_drive,
        "successor_drive": successor_drive,
        "baseline_proposal_id": baseline_id,
        "lane_bytes": lane_bytes,
        "context_refusal": context_refusal,
        "changed_requirements": len(changed),
        "moved_to_successor": len(moved),
        "unchanged_requirements": len(shared) - len(changed),
        "previous_targets": before,
    }


def _review_with_context(con, stored: dict, *, include_assignments: bool) -> dict:
    out = _review(stored, include_assignments=include_assignments)
    if stored.get("mutation_kind") != "successor_replan":
        return out
    successor = _successor_review(con, stored)
    previous = successor.pop("previous_targets", {})
    out["successor"] = successor
    if include_assignments:
        for assignment in out.get("assignments") or ():
            prior = previous.get(str(assignment.get("requirement_id") or ""))
            assignment["previous_target"] = prior
            assignment["target_changed"] = bool(
                prior is not None and prior != assignment.get("target_drive")
            )
    return out


def _status_on(con) -> dict:
    from modelark import plan, proposal

    state = _planner_state(con)
    active_plan = plan.active(con)
    active_plan_id = str(active_plan["plan_id"]) if active_plan else None
    active_id = state["active_approved_proposal_id"]
    if not active_id:
        out = {
            "ok": True,
            "state": "missing",
            "planner_revision": state["planner_revision"],
            "active_proposal": None,
        }
    else:
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
        out = {
            "ok": True,
            "state": "approved_current" if current else "approved_stale",
            "planner_revision": state["planner_revision"],
            "active_proposal": _review_with_context(
                con, stored, include_assignments=False
            ),
            "input_status": input_status,
        }

    pending_ids = proposal.current_draft_ids(con)
    if not pending_ids:
        return out
    if len(pending_ids) > 1:
        refused = _refused(
            "MULTIPLE_CURRENT_DRAFTS",
            {
                "proposal_ids": list(pending_ids),
                "planner_revision": state["planner_revision"],
            },
            ("inspect_pending_proposals",),
        )
        refused.update({
            "state": "review_ambiguous",
            "planner_revision": state["planner_revision"],
            "approval_state": out["state"],
            "active_proposal": out.get("active_proposal"),
        })
        return refused

    pending_id = pending_ids[0]
    try:
        pending = proposal.load_proposal(con, pending_id)
    except KeyError:
        refused = _refused(
            "PENDING_PROPOSAL_MISSING",
            {"proposal_id": pending_id},
            ("inspect_catalog",),
        )
        refused.update({
            "state": "review_ambiguous",
            "planner_revision": state["planner_revision"],
            "approval_state": out["state"],
            "active_proposal": out.get("active_proposal"),
        })
        return refused

    pending_input_status = proposal.review_input_status(con, pending)
    pending_review = _review_with_context(con, pending, include_assignments=True)
    pending_review["input_status"] = pending_input_status
    pending_plan_active = str(pending.get("plan_id") or "") == active_plan_id
    pending_review["plan_active"] = pending_plan_active
    pending_review["active_plan_id"] = active_plan_id
    pending_current = bool(pending_input_status.get("current")) and pending_plan_active
    if not pending_current:
        pending_review["approvable"] = False
        pending_review.pop("confirmation_phrase", None)
    out["approval_state"] = out["state"]
    if not pending_plan_active:
        out["state"] = "review_pending_inactive"
    else:
        out["state"] = "review_pending" if pending_current else "review_pending_stale"
    out["pending_proposal"] = pending_review
    out["pending_input_status"] = pending_input_status
    return out


def status() -> dict:
    with data._lock:
        return _status_on(data.conn())


def create_draft(_body: dict | None = None) -> dict:
    """Create a backend-bound current or operator-directed successor draft."""
    from modelark import plan, proposal

    body = _body or {}
    try:
        with data._lock:
            con = data.conn()
            active = plan.active(con)
            if active is None:
                return _refused("NO_ACTIVE_PLAN", None, ("select_plan",))
            mutation = ("adopt_current", ())
            if body.get("mode") == "successor":
                predecessor = str(body.get("predecessor_drive") or "").strip()
                successor = str(body.get("successor_drive") or "").strip()
                if not predecessor or not successor:
                    return _refused(
                        "SUCCESSOR_DRIVES_REQUIRED",
                        None,
                        ("choose_predecessor_and_successor",),
                    )
                state = _planner_state(con)
                baseline_id = state["active_approved_proposal_id"]
                if not baseline_id:
                    return _refused(
                        "SUCCESSOR_BASELINE_REQUIRED",
                        None,
                        ("approve_current_placement",),
                    )
                try:
                    baseline = proposal.load_proposal(con, baseline_id)
                except KeyError:
                    return _refused(
                        "SUCCESSOR_BASELINE_MISSING",
                        {"proposal_id": baseline_id},
                        ("review_current_placement",),
                    )
                baseline_status = proposal.review_input_status(con, baseline)
                if (baseline.get("lifecycle") != "approved"
                        or not baseline_status.get("current")):
                    return _refused(
                        "SUCCESSOR_BASELINE_STALE",
                        baseline_status,
                        ("review_current_placement",),
                    )
                mutation = (
                    "successor_replan",
                    (predecessor, successor, str(baseline_id)),
                )
            created = proposal.create_draft(
                con,
                plan_id=active["plan_id"],
                mutation=mutation,
            )
            stored = proposal.load_proposal(con, created["proposal_id"])
            return {
                "ok": True,
                "state": "draft",
                "review": _review_with_context(con, stored, include_assignments=True),
            }
    except proposal.Refusal as exc:
        return _domain_refusal(exc)


def successor_options() -> dict:
    """Backend-authored predecessor/successor choices for the active approved plan."""
    from modelark import plan, proposal

    baseline_id = None
    try:
        with data._lock:
            con = data.conn()
            active = plan.active(con)
            if active is None:
                return _refused("NO_ACTIVE_PLAN", None, ("select_plan",))
            state = _planner_state(con)
            baseline_id = state["active_approved_proposal_id"]
            if not baseline_id:
                return _refused(
                    "SUCCESSOR_BASELINE_REQUIRED",
                    None,
                    ("approve_current_placement",),
                )
            baseline = proposal.load_proposal(con, baseline_id)
            status = proposal.review_input_status(con, baseline)
            if not status.get("current"):
                return _refused(
                    "SUCCESSOR_BASELINE_STALE",
                    status,
                    ("review_current_placement",),
                )
            assigned = defaultdict(int)
            for task in baseline.get("tasks") or ():
                target = task.get("target_drive") or task.get("satisfying_drive")
                if target:
                    assigned[str(target)] += 1
            rows = con.execute(
                "SELECT d.drive_label,coalesce(d.role,'primary'),d.lifecycle,d.eligibility,"
                "coalesce(d.capacity_bytes,0),d.identity_epoch,d.identity_fingerprint "
                "FROM plan_drives pd JOIN drives d USING(drive_label) "
                "WHERE pd.plan_id=? ORDER BY d.drive_label",
                [active["plan_id"]],
            ).fetchall()
            predecessors = []
            successors = []
            for label, role, lifecycle, eligibility, capacity_bytes, epoch, fingerprint in rows:
                item = {
                    "drive_label": str(label),
                    "role": str(role),
                    "lifecycle": str(lifecycle),
                    "eligibility": str(eligibility),
                    "capacity_bytes": int(capacity_bytes or 0),
                    "identity_epoch": int(epoch or 0),
                    "identity_proven": bool(fingerprint),
                    "assigned_requirements": int(assigned[str(label)]),
                }
                if lifecycle != "active" or eligibility != "enabled":
                    predecessors.append(item)
                elif fingerprint and int(epoch or 0) >= 1:
                    successors.append(item)
            return {
                "ok": True,
                "plan_id": active["plan_id"],
                "baseline_proposal_id": baseline_id,
                "predecessors": predecessors,
                "successors": successors,
            }
    except (proposal.Refusal, KeyError) as exc:
        if isinstance(exc, proposal.Refusal):
            return _domain_refusal(exc)
        return _refused(
            "SUCCESSOR_BASELINE_MISSING",
            {"proposal_id": baseline_id},
            ("review_current_placement",),
        )


def discard(body: dict) -> dict:
    """Discard one exact draft while preserving the active approval and revision."""
    from modelark import proposal

    proposal_id = str(body.get("proposal_id") or "").strip()
    if not proposal_id:
        return _refused(
            "PROPOSAL_ID_REQUIRED",
            None,
            ("review_pending_proposal",),
        )
    try:
        with data._lock:
            con = data.conn()
            proposal.discard_draft(con, proposal_id)
            out = _status_on(con)
            out["discarded_proposal_id"] = proposal_id
            return out
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
