"""Exact, session-bound drive-card data for the guided Fill UI (INC-063).

The Library plan endpoint is an advisory reconciliation and may legitimately move work after an
approval.  This module instead summarizes the immutable proposal plus the execution projection
that passed Start admission, then applies small runtime labels to that frozen summary.  It is a
presentation contract only: no scheduler, placement, capacity, or archive fact is changed here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


_STATE_LABELS = {
    "approved_remaining": "Approved for later",
    "writing": "Writing now",
    "waiting_for_drive": "Waiting for drive",
    "access_followup": "Access follow-up",
    "paused_unwritable": "Drive needs attention",
    "capacity_stop": "Capacity stop",
    "waiting_dependency": "Waiting on dependency",
    "download_throttled": "Download cap reached",
    "stopped": "Stopped",
    "error": "Fill error",
    "complete": "Complete",
    "satisfied": "Already satisfied",
}
_TIER_PRIORITY = {"primary": 0, "replica": 1, "raid": 2}


def _get(value: Any, name: str, default=None):
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _task_dict(task: Any) -> dict:
    if isinstance(task, Mapping):
        return dict(task)
    if hasattr(task, "__dict__"):
        return dict(vars(task))
    return {}


def _target(task: Mapping) -> str:
    return str(task.get("target_drive") or task.get("satisfying_drive") or "")


def _is_replica(task: Mapping) -> bool:
    requirement = str(task.get("requirement_id") or "")
    return requirement.startswith(("replica:", "protected_replica:"))


def _is_protected_home(task: Mapping, protected_repos: set[str]) -> bool:
    requirement = str(task.get("requirement_id") or "")
    repo = str(task.get("repo_id") or "")
    return requirement.startswith("protected_home:") or (
        requirement.startswith("primary:") and repo in protected_repos
    )


def _copy_kind(task: Mapping, protected_repos: set[str]) -> str:
    if _is_replica(task) or task.get("source_drive"):
        return "2"
    return "1" if _is_protected_home(task, protected_repos) else "bulk"


def _tier(task: Mapping, protected_repos: set[str]) -> str:
    if _is_protected_home(task, protected_repos):
        return "raid"
    if _is_replica(task) or task.get("source_drive"):
        return "replica"
    return "primary"


def build(session_start: Any) -> dict:
    """Build the immutable drive-card contract from one admitted ``SessionStart``.

    Production ``SessionStart`` carries the proposal cached by ``start_session``.  The projection-
    only fallback keeps adapter/test starts observable without fabricating baseline satisfaction.
    """
    projection = _get(session_start, "projection")
    projected = [_task_dict(task) for task in (_get(projection, "tasks", ()) or ())]
    proposal = _get(session_start, "_proposal") or {}
    if not isinstance(proposal, Mapping):
        proposal = {}
    approved = [_task_dict(task) for task in (_get(proposal, "tasks", ()) or ())]
    if not approved:
        approved = list(projected)
    protected_repos = {
        str(task.get("repo_id") or "")
        for task in (*approved, *projected)
        if _is_replica(task) and task.get("repo_id")
    }

    remaining_ids = {
        str(task.get("requirement_id")) for task in projected if task.get("requirement_id") is not None
    }
    rows: dict[str, dict] = {}

    def drive_row(label: str) -> dict:
        return rows.setdefault(label, {
            "label": label,
            "tier": "primary",
            "approved_requirements": 0,
            "baseline_satisfied": 0,
            "satisfied_since_approval": 0,
            "approved_executable": 0,
            "remaining_at_start": 0,
            "approved_guaranteed_bytes": 0,
            "remaining_guaranteed_bytes": 0,
            "models": [],
        })

    for task in approved:
        label = _target(task)
        if not label:
            continue
        row = drive_row(label)
        task_tier = _tier(task, protected_repos)
        if _TIER_PRIORITY[task_tier] > _TIER_PRIORITY[row["tier"]]:
            row["tier"] = task_tier
        row["approved_requirements"] += 1
        row["approved_guaranteed_bytes"] += int(task.get("guaranteed_durable") or 0)
        if task.get("row_kind") == "baseline_satisfied":
            row["baseline_satisfied"] += 1
        elif task.get("row_kind") == "executable":
            row["approved_executable"] += 1

    links: set[tuple[str, str]] = set()
    batch_order: list[str] = []
    for task in projected:
        label = _target(task)
        if not label:
            continue
        row = drive_row(label)
        if label not in batch_order:
            batch_order.append(label)
        size = int(task.get("guaranteed_durable") or 0)
        row["remaining_at_start"] += 1
        row["remaining_guaranteed_bytes"] += size
        row["models"].append({
            "requirement_id": task.get("requirement_id"),
            "repo": task.get("repo_id"),
            "size": size,
            "copy": _copy_kind(task, protected_repos),
            "schedule_state": task.get("schedule_state") or "ready",
        })
        source = str(task.get("source_drive") or "")
        if source and source != label:
            links.add((source, label))

    # Approved tasks which disappeared from the admitted projection became satisfied after approval.
    approved_executable_ids = {
        str(task.get("requirement_id"))
        for task in approved
        if task.get("row_kind") == "executable" and task.get("requirement_id") is not None
    }
    satisfied_ids = approved_executable_ids - remaining_ids
    for task in approved:
        requirement = task.get("requirement_id")
        if requirement is None or str(requirement) not in satisfied_ids:
            continue
        label = _target(task)
        if label:
            drive_row(label)["satisfied_since_approval"] += 1
    satisfied_since_approval = len(satisfied_ids)
    session = _get(session_start, "session")
    totals = {
        "approved_requirements": sum(row["approved_requirements"] for row in rows.values()),
        "baseline_satisfied": sum(row["baseline_satisfied"] for row in rows.values()),
        "approved_executable": sum(row["approved_executable"] for row in rows.values()),
        "remaining_at_start": sum(row["remaining_at_start"] for row in rows.values()),
        "satisfied_since_approval": satisfied_since_approval,
    }
    return {
        "authority": "approved_execution",
        "proposal_id": str(_get(projection, "proposal_id", "") or _get(proposal, "proposal_id", "")),
        "session_id": str(_get(session, "session_id", "") or ""),
        "bound_revision": _get(session, "bound_planner_revision"),
        "projection_hash": str(_get(projection, "projection_hash", "") or ""),
        "batch_order": batch_order,
        "totals": totals,
        "drives": list(rows.values()),
        "links": [{"from": source, "to": target} for source, target in sorted(links)],
    }


def _followup_repositories(status: Mapping) -> set[str]:
    evidence = status.get("evidence") or {}
    repos = set(evidence.get("access_gated") or ()) if isinstance(evidence, Mapping) else set()
    if isinstance(evidence, Mapping):
        for refusal in evidence.get("content_refusals") or ():
            if isinstance(refusal, Mapping) and (refusal.get("repo_id") or refusal.get("repo")):
                repos.add(str(refusal.get("repo_id") or refusal.get("repo")))
    notice = status.get("notice") or {}
    notice_id = str(notice.get("id") or "") if isinstance(notice, Mapping) else ""
    if notice_id.endswith(":skip") or notice_id.endswith(":timeout"):
        if notice.get("repo"):
            repos.add(str(notice["repo"]))
    return repos


def with_runtime_state(status: Mapping) -> dict:
    """Return an API snapshot with backend-authored state on every exact drive card."""
    result = dict(status)
    plan = status.get("execution_plan")
    if not isinstance(plan, Mapping):
        return result
    view = deepcopy(plan)
    result.pop("execution_plan", None)

    awaiting = str(status.get("awaiting_drive") or "")
    current = str(status.get("drive") or "")
    completed = {str(item) for item in (status.get("execution_completed_requirements") or ())}
    result.pop("execution_completed_requirements", None)
    followups = _followup_repositories(status)
    followup_drives = {
        row.get("label")
        for row in view.get("drives", ())
        if any(model.get("repo") in followups for model in row.get("models", ()))
    }
    terminal = str(status.get("status") or "")
    code = str(status.get("code") or "")
    unavailable = set()
    evidence = status.get("evidence") or {}
    if code == "DRIVE_UNAVAILABLE" and isinstance(evidence, Mapping):
        unavailable.update(str(label) for label in (evidence.get("drives") or ()))
    waiting_requirements = set()
    if isinstance(evidence, Mapping):
        waiting_requirements.update(str(item) for item in (evidence.get("requirements") or ()))
        waiting_requirements.update(
            str(item) for item in (evidence.get("waiting_requirements") or ())
        )
    waiting_dependency_drives = {
        row.get("label")
        for row in view.get("drives", ())
        if any(
            str(model.get("requirement_id")) in waiting_requirements
            for model in row.get("models", ())
        )
    }
    unavailable_replica_drives = set()
    if code == "SOURCE_UNAVAILABLE" and isinstance(evidence, Mapping):
        unavailable_replica_drives.update(
            str(label) for label in (evidence.get("deferred_sources") or ())
        )
        unavailable_replica_drives.update(
            str(label) for label in (evidence.get("deferred_targets") or ())
        )
        if awaiting:
            unavailable_replica_drives.add(awaiting)
    typed_drive = str(evidence.get("drive") or "") if isinstance(evidence, Mapping) else ""
    completed_total = 0

    for row in view.get("drives", ()):
        label = str(row.get("label") or "")
        remaining = int(row.get("remaining_at_start") or 0)
        requirement_ids = {
            str(model.get("requirement_id"))
            for model in row.get("models", ())
            if model.get("requirement_id") is not None
        }
        completed_here = len(requirement_ids & completed)
        completed_total += completed_here
        if remaining == 0:
            state = "satisfied"
        elif requirement_ids and requirement_ids <= completed:
            state = "complete"
        else:
            state = "approved_remaining"

        if terminal == "running":
            if label == awaiting:
                state = "waiting_for_drive"
            elif not awaiting and label == current:
                state = "writing"
            elif label in followup_drives:
                state = "access_followup"
        elif terminal == "done":
            if label in followup_drives:
                state = "access_followup"
            elif label in waiting_dependency_drives:
                state = "waiting_dependency"
            elif remaining and requirement_ids <= completed:
                state = "complete"
        elif label in unavailable:
            state = "waiting_for_drive"
        elif label in unavailable_replica_drives:
            state = "waiting_for_drive"
        elif label in waiting_dependency_drives:
            state = "waiting_dependency"
        elif label == (typed_drive or current) and code == "DRIVE_UNWRITABLE":
            state = "paused_unwritable"
        elif label == (typed_drive or current) and code == "PLAN_CAPACITY_STOP":
            state = "capacity_stop"
        elif label == current and code == "DOWNLOAD_THROTTLED":
            state = "download_throttled"
        elif label == current and terminal == "stopped":
            state = "stopped"
        elif (
            label == current
            and terminal in {"error", "failed", "blocked", "paused"}
            and code not in {
                "DRIVE_UNAVAILABLE", "DRIVE_UNWRITABLE", "PLAN_CAPACITY_STOP",
                "SOURCE_UNAVAILABLE", "WAITING_DEPENDENCY",
            }
        ):
            state = "error"

        row["state"] = state
        row["state_label"] = _STATE_LABELS[state]
        row["completed_in_run"] = completed_here
        row["access_followups"] = sorted({
            str(model.get("repo")) for model in row.get("models", ())
            if model.get("repo") in followups
        })

    view["totals"]["completed_in_run"] = completed_total
    view["totals"]["unresolved"] = max(
        0, int(view["totals"].get("remaining_at_start") or 0) - completed_total,
    )
    result["execution"] = view
    return result
