"""Reconciled guided-fill scheduler (DEC-045 Phase 3).

Durable catalog facts are the only completion truth.  Each batch rebuilds an unpersisted work graph,
admits it through the capacity ledger, pins one drive, executes exact missing manifests, and then
reconciles again.  A crash discards only ephemeral scheduler state; completed file rows self-heal the
next graph.  Both CLI and portal call :func:`execute`.
"""
from __future__ import annotations

import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from modelark import capacity, fetch, plan, reconcile, register

_MAX_TASK_ATTEMPTS = 2
_GATED_DECISION_TIMEOUT = 5 * 60


@dataclass(frozen=True)
class _Snapshot:
    graph: reconcile.ReconcileResult
    ledger: capacity.CapacityPlan


def _mounted(ctx, label: str) -> tuple[bool, bool]:
    """Return (block-registered, mounted) without treating special remotes as awaitable disks."""
    with ctx.lock:
        uuid = (ctx.con.execute(
            "SELECT fs_uuid FROM drives WHERE drive_label=?", [label]
        ).fetchone() or [None])[0]
        mounted = uuid is not None and register.archive_path(ctx.con, label) is not None
    return uuid is not None, mounted


def _writable(ctx, label: str) -> bool:
    """A cheap, NON-MUTATING readiness check: the drive resolves to a mounted archive directory that
    statvfs's and is readable. It performs no probe write/unlink and no identity subprocess — authoritative
    identity proof and real write access are established inside the mutation envelope (post-dirty)."""
    with ctx.lock:
        path = register.archive_path(ctx.con, label)
    if path is None:
        return False
    path = Path(path)
    try:
        os.statvfs(path)
    except OSError:
        return False
    return path.is_dir() and os.access(path, os.R_OK)


def _await_drive(ctx, label: str, poll_secs: float) -> bool:
    """Pin the scheduler until the requested drive is live/writable or Stop is requested."""
    if ctx.should_stop():
        return False
    registered, mounted = _mounted(ctx, label)
    if not registered:
        return True
    if mounted and _writable(ctx, label):
        return True
    reason = "insert it" if not mounted else "mounted but not writable (I/O error) — re-seat it"
    ctx.on_progress({
        "phase": "awaiting-drive", "awaiting_drive": label,
        "say": f"⏳ drive {label}: {reason} — the fill continues once it's writable.",
    })
    while not ctx.should_stop():
        time.sleep(poll_secs)
        _, mounted = _mounted(ctx, label)
        if mounted and _writable(ctx, label):
            ctx.on_progress({
                "phase": "running", "awaiting_drive": None,
                "say": f"✅ {label} writable — continuing.",
            })
            return True
    return False


def _live_free(ctx, plan_id: str) -> dict[str, int]:
    """Snapshot only currently mounted drives; offline drives retain catalog evidence for planning."""
    with ctx.lock:
        labels = [row[0] for row in ctx.con.execute(
            "SELECT drive_label FROM plan_drives WHERE plan_id=? ORDER BY drive_label", [plan_id]
        ).fetchall()]
        paths = {label: register.archive_path(ctx.con, label) for label in labels}
    live = {}
    for label, path in paths.items():
        if path is None:
            continue
        try:
            live[label] = shutil.disk_usage(path).free
        except OSError:
            pass
    return live


def _reconcile(ctx, plan_id: str, capacity_mode: str, repo_scope: list[str] | None) -> _Snapshot:
    """Bulk graph/ledger snapshot, using a dedicated read connection in real executions."""
    live_free = _live_free(ctx, plan_id)
    if ctx.read_connection_factory is None:  # isolated in-memory/unit harness
        with ctx.lock:
            graph = reconcile.reconcile_plan(ctx.con, plan_id, repo_scope)
            ledger = capacity.plan_capacity(
                ctx.con, graph, capacity_mode=capacity_mode, live_free_by_drive=live_free,
            )
        return _Snapshot(graph, ledger)
    con = ctx.read_connection_factory()
    try:
        graph = reconcile.reconcile_plan(con, plan_id, repo_scope)
        ledger = capacity.plan_capacity(
            con, graph, capacity_mode=capacity_mode, live_free_by_drive=live_free,
        )
        return _Snapshot(graph, ledger)
    finally:
        con.close()


def _failure_dict(failure: capacity.CapacityFailure) -> dict:
    return {
        "code": failure.code.value,
        "requirement_id": failure.requirement_id,
        "target_tier": failure.target_tier,
        "eligible_drives": list(failure.eligible_drives),
        "required_bytes": failure.required_bytes,
        "available_bytes": failure.available_bytes,
        "workspace_bytes": failure.workspace_bytes,
        "shortfall_bytes": failure.shortfall_bytes,
        "evidence": failure.evidence.value if failure.evidence else None,
        "actions": list(failure.actions),
    }


def _terminal(
    state: str,
    message: str,
    *,
    code: str,
    gate: str | None = None,
    evidence: dict | list | None = None,
    actions: list[str] | tuple[str, ...] = (),
    failed: list[dict] | None = None,
    stopped: bool = False,
) -> dict:
    return {
        "ok": state == "done",
        "stopped": stopped,
        "state": state,
        "message": message,
        "code": code,
        "gate": gate,
        "evidence": evidence or {},
        "actions": list(actions),
        "failed": failed or [],
    }


def _admission_terminal(snapshot: _Snapshot, plan_id: str, made_progress: bool) -> dict:
    ledger = snapshot.ledger
    state = "plan-capacity-stop" if made_progress else "blocked"
    if ledger.blocking_diagnostics:
        codes = list(ledger.blocking_diagnostics)
        diagnostics = [
            {
                "code": item.code,
                "severity": item.severity.value,
                "requirement_id": item.requirement_id,
                "detail": dict(item.detail),
            }
            for item in snapshot.graph.diagnostics
            if item.severity in {
                reconcile.DiagnosticSeverity.BLOCKING,
                reconcile.DiagnosticSeverity.ERROR,
            }
        ]
        message = (
            f"{len(diagnostics)} archive-policy/invariant blocker(s) prevent plan '{plan_id}' admission. "
            "Review the typed evidence, change policy or selection, then re-run. (No bytes written.)"
        )
        return _terminal(
            state, message, code=codes[0], gate="B",
            evidence={"blocking_diagnostics": diagnostics},
            actions=["review_manifest_policy", "trim_selection", "replan"],
        )
    failures = [_failure_dict(item) for item in ledger.failures]
    first = failures[0] if failures else {
        "code": "GRAPH_UNASSIGNED", "shortfall_bytes": 0, "actions": ["replan"]
    }
    short = sum(item.get("shortfall_bytes", 0) for item in failures)
    if made_progress:
        message = (
            f"live capacity changed and {len(failures) or len(ledger.unassigned_intents)} requirement(s) "
            f"cannot be admitted ({short / 1e12:.2f} TB short). Add eligible capacity, then re-run. "
            "Completed files remain safe."
        )
    else:
        message = (
            f"plan '{plan_id}' cannot safely admit its committed work: "
            f"{len(failures) or len(ledger.unassigned_intents)} requirement(s), "
            f"{short / 1e12:.2f} TB short. Add capacity or trim the selection. (No bytes written.)"
        )
    return _terminal(
        state, message, code=first["code"], gate="B",
        evidence={"capacity_failures": failures, "unassigned": [
            item.requirement_id for item in ledger.unassigned_intents
        ]},
        actions=first.get("actions", ["replan"]),
    )


def _stop_terminal() -> dict:
    return _terminal(
        "stopped", "stopped by request", code="OPERATOR_STOP", stopped=True,
        actions=["start_fill"],
    )


def _file_guard(ctx, plan_id: str, capacity_mode: str, task: capacity.AssignedTask):
    budgets = {item.rfilename: item for item in task.budget.file_budgets}
    mode = capacity.mode_from_value(capacity_mode)

    def before_file(repo_id, item):
        with ctx.lock:
            if ctx.con.execute(
                "SELECT 1 FROM archived WHERE repo_id=? AND rfilename=? AND drive_label=?",
                [repo_id, item.rfilename, task.target_drive],
            ).fetchone():
                return False
            path = register.archive_path(ctx.con, task.target_drive)
        if path is None:
            with ctx.lock:
                drive = next((
                    entry for entry in capacity.inspect_drives(ctx.con, plan_id)
                    if entry.drive_label == task.target_drive
                ), None)
        else:
            try:
                free = shutil.disk_usage(path).free
            except OSError:
                free = 0
            with ctx.lock:
                drive = next((
                    entry for entry in capacity.inspect_drives(
                        ctx.con, plan_id, live_free_by_drive={task.target_drive: free}
                    ) if entry.drive_label == task.target_drive
                ), None)
        if drive is None:
            raise fetch.CapacityPreflightError(
                capacity.target_drive_changed_failure(task, mode)
            )
        failure = capacity.preflight_file(
            drive, budgets[item.rfilename], mode,
            requirement_id=task.requirement_id,
            task_id=task.task_id,
        )
        if failure is not None:
            raise fetch.CapacityPreflightError(failure)
        return True

    return before_file


def _ready_tasks(
    snapshot: _Snapshot,
    deferred_gated: set[str] | None = None,
) -> list[capacity.AssignedTask]:
    satisfied = snapshot.graph.satisfied
    deferred_gated = deferred_gated or set()
    return [
        task for task in snapshot.ledger.tasks
        if task.depends_on_requirement is None or task.depends_on_requirement in satisfied
        if not (task.kind == reconcile.TaskKind.FETCH and task.repo_id in deferred_gated)
    ]


def _scope_without_deferred(ctx, repo_scope: list[str] | None, deferred_gated: set[str]):
    """Remove session-parked access work before graph derivation and capacity admission.

    Selection remains durable and unchanged. A later Fill starts with an empty deferred set and the
    repository naturally re-enters the graph; this scope exists only to prevent parked bytes from
    causing a false capacity failure during the current run.
    """
    if not deferred_gated:
        return repo_scope
    if repo_scope is None:
        with ctx.lock:
            candidates = [row[0] for row in ctx.con.execute(
                "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL ORDER BY repo_id"
            ).fetchall()]
    else:
        candidates = repo_scope
    return [repo for repo in candidates if repo not in deferred_gated]


def execute(
    ctx,
    *,
    plan_id: str | None = None,
    max_24h_gb: float = 1000,
    repo_scope: list[str] | None = None,
    guided: bool = False,
    poll_secs: float = 3.0,
) -> dict:
    """Execute the reconciled graph without persisting scheduler completion state."""
    ctx.stats["t0"] = time.monotonic()
    ctx.stats.setdefault("by_drive", {})
    with ctx.lock:
        prow = (plan.get(ctx.con, plan_id) if plan_id else plan.active(ctx.con)) or plan.bootstrap(ctx.con)
    pid, capacity_mode = prow["plan_id"], prow["capacity_mode"]
    ctx.on_progress({
        "phase": "plan", "plan_id": pid, "capacity_mode": capacity_mode,
        "provisioning": plan.legacy_capacity_mode(capacity_mode),
        "deprecated_fields": ["provisioning"],
        "say": f"plan '{pid}' · capacity mode={capacity_mode} · {len(prow['drives'])} drive(s)",
    })

    if ctx.check_hf_auth:
        auth_failure = fetch.hf_auth_preflight(ctx)
        if auth_failure is not None:
            ctx.on_progress({
                "phase": "auth-invalid", "code": auth_failure["code"],
                "evidence": auth_failure["evidence"], "actions": auth_failure["actions"],
                "say": f"🔴 {auth_failure['message']}",
            })
            return _terminal(
                "blocked", auth_failure["message"], code=auth_failure["code"],
                gate=auth_failure["gate"], evidence=auth_failure["evidence"],
                actions=auth_failure["actions"],
            )

    attempts: dict[str, int] = {}
    gated_hits: dict[str, int] = {}
    deferred_gated: set[str] = set()
    made_progress = False
    first = True
    pinned_drive: str | None = None
    while not ctx.should_stop():
        active_scope = _scope_without_deferred(ctx, repo_scope, deferred_gated)
        snapshot = _reconcile(ctx, pid, capacity_mode, active_scope)
        if not snapshot.ledger.feasible:
            terminal = _admission_terminal(snapshot, pid, made_progress)
            ctx.on_progress({
                "phase": terminal["state"], "gate": "B", "code": terminal["code"],
                "evidence": terminal["evidence"], "actions": terminal["actions"],
                "say": ("🟠 " if made_progress else "🔴 ") + terminal["message"],
            })
            return terminal

        ready = _ready_tasks(snapshot, deferred_gated)
        if not ready:
            remaining_repos = {task.repo_id for task in snapshot.ledger.tasks}
            if deferred_gated and (not remaining_repos or remaining_repos <= deferred_gated):
                repos = sorted(deferred_gated)
                message = (
                    f"fill complete with {len(repos)} gated-access follow-up(s); "
                    "all other feasible work is safe"
                )
                ctx.on_progress({
                    "phase": "done", "code": "PLAN_COMPLETE_WITH_FOLLOWUPS",
                    "followups": [{"repo": repo, "type": "access-gated"} for repo in repos],
                    "say": "⚠ " + message,
                })
                return _terminal(
                    "done", message, code="PLAN_COMPLETE_WITH_FOLLOWUPS",
                    evidence={"access_gated": repos}, actions=["review_followups", "start_fill"],
                )
            if snapshot.ledger.tasks:
                return _terminal(
                    "error", "work graph has tasks but none have a satisfied dependency",
                    code="GRAPH_DEPENDENCY_DEADLOCK", gate="C",
                    evidence={"requirements": [task.requirement_id for task in snapshot.ledger.tasks]},
                    actions=["inspect_plan_explain", "report_bug"],
                )
            n_must = sum(
                requirement.kind == reconcile.RequirementKind.PROTECTED_REPLICA
                for requirement in snapshot.graph.requirements
            )
            message = f"fill complete — all {n_must} finalized must-have(s) hold their copies"
            ctx.on_progress({"phase": "done", "code": "PLAN_SATISFIED", "say": "✅ " + message})
            return _terminal("done", message, code="PLAN_SATISFIED")

        if first and not guided:
            involved = {task.target_drive for task in ready}
            involved.update(task.source_drive for task in ready if task.source_drive)
            unmounted = [
                label for label in sorted(involved)
                if _mounted(ctx, label) == (True, False)
            ]
            if unmounted:
                message = (
                    f"required drive(s) not mounted: {', '.join(unmounted)}. Mount them, then re-run. "
                    "(No bytes fetched.)"
                )
                return _terminal(
                    "blocked", message, code="DRIVE_UNAVAILABLE", gate="A",
                    evidence={"drives": unmounted}, actions=["mount_drives", "replan"],
                )
        first = False

        labels = {task.target_drive for task in ready}
        if pinned_drive not in labels:
            pinned_drive = next(
                (label for label in snapshot.ledger.batch_order if label in labels),
                sorted(labels)[0],
            )
        batch = sorted(
            (task for task in ready if task.target_drive == pinned_drive),
            key=lambda task: capacity.execution_rank(task, snapshot.graph),
        )
        if guided and not _await_drive(ctx, pinned_drive, poll_secs):
            return _stop_terminal()
        if ctx.should_stop():
            return _stop_terminal()

        fetch_tasks = [task for task in batch if task.kind == reconcile.TaskKind.FETCH]
        replica_tasks = [task for task in batch if task.kind == reconcile.TaskKind.REPLICATE]
        if fetch_tasks:
            ctx.on_progress({
                "phase": "primary", "drive": pinned_drive, "n_repos": len(fetch_tasks),
                "say": f"== {pinned_drive} ({len(fetch_tasks)} exact fetch task(s)) ==",
            })
            manifests = {
                task.repo_id: tuple(
                    item for item in snapshot.graph.manifests[task.repo_id]
                    if item.rfilename in task.budget.missing_files
                )
                for task in fetch_tasks
            }
            guards = {
                task.repo_id: _file_guard(ctx, pid, capacity_mode, task) for task in fetch_tasks
            }

            def on_gated(repo_id: str) -> str:
                hit = gated_hits.get(repo_id, 0) + 1
                gated_hits[repo_id] = hit
                url = f"https://huggingface.co/{repo_id}"
                if hit == 1:
                    ctx.on_progress({
                        "notice": {
                            "id": f"access-gated:{repo_id}:1", "type": "access-gated",
                            "repo": repo_id, "url": url,
                            "message": f"Access is required for {repo_id}; continuing other work.",
                        },
                        "say": f"⚠ {repo_id} needs Hugging Face access; continuing other work.",
                    })
                    return "continue"
                prompt = {
                    "id": f"access-gated:{secrets.token_urlsafe(12)}", "type": "access-gated",
                    "repo": repo_id, "url": url,
                    "title": "Hugging Face access required",
                    "message": (
                        f"{repo_id} is still gated. Obtain access in Hugging Face, then retry, "
                        "or skip it for this run."
                    ),
                    "timeout_seconds": _GATED_DECISION_TIMEOUT,
                }
                action = ctx.request_action(prompt, _GATED_DECISION_TIMEOUT)
                if action in {"skip", "timeout"}:
                    word = "skipped" if action == "skip" else "timed out"
                    ctx.on_progress({
                        "notice": {
                            "id": f"access-gated:{repo_id}:{action}", "type": "access-gated",
                            "repo": repo_id, "url": url,
                            "message": f"{repo_id} {word}; added to Verify follow-ups.",
                        },
                        "say": f"⚠ {repo_id} {word}; parked as an access follow-up.",
                    })
                return action

            outcome = fetch.run(
                drive_label=pinned_drive,
                repos=[task.repo_id for task in fetch_tasks],
                max_24h_gb=max_24h_gb,
                ctx=ctx,
                task_manifests=manifests,
                before_file=lambda repo_id, item: guards[repo_id](repo_id, item),
                on_gated=on_gated,
            )
            if outcome["stored_repos"]:
                made_progress = True
            if outcome["stopped"] or ctx.should_stop():
                return _stop_terminal()
            if outcome.get("terminal_failure") is not None:
                failure = outcome["terminal_failure"]
                state = "paused" if made_progress else "blocked"
                return _terminal(
                    state, failure["message"], code=failure["code"],
                    gate=failure.get("gate", "C"), evidence=failure.get("evidence"),
                    actions=failure.get("actions", ()),
                    failed=([{"repo": outcome["terminal_repo"]}]
                            if outcome.get("terminal_repo") else []),
                )
            if outcome["throttled"]:
                return _terminal(
                    "paused", "24h download cap reached (resumable)", code="DOWNLOAD_THROTTLED",
                    evidence={"max_24h_gb": max_24h_gb}, actions=["wait_for_window", "start_fill"],
                )
            if outcome["capacity_failure"] is not None:
                # Reconcile immediately.  If no alternative target is feasible the next loop emits
                # plan-capacity-stop; otherwise deterministic placement re-homes the stale task.
                pinned_drive = None
                continue
            for item in outcome.get("gated_repos", []):
                deferred_gated.add(item["repo"])
            for repo_id in outcome["failed_repos"]:
                attempts[repo_id] = attempts.get(repo_id, 0) + 1
                if attempts[repo_id] >= _MAX_TASK_ATTEMPTS:
                    return _terminal(
                        "error", f"fetch task for {repo_id} failed {_MAX_TASK_ATTEMPTS} times",
                        code="FETCH_TASK_FAILED", gate="C",
                        evidence={"repo": repo_id, "attempts": attempts[repo_id]},
                        actions=["inspect_fetch_events", "retry_repo", "trim_selection"],
                        failed=[{"repo": repo_id, "attempts": attempts[repo_id]}],
                    )
            if outcome.get("gated_retry"):
                continue
            if outcome["drive_unwritable"]:
                pinned_drive = None
                continue

        if replica_tasks:
            ctx.on_progress({
                "phase": "replica", "drive": pinned_drive, "n_repos": len(replica_tasks),
                "say": f"== {pinned_drive} ({len(replica_tasks)} exact replica task(s)) ==",
            })
            outcome = fetch.run_replica_tasks(replica_tasks, ctx=ctx)
            if outcome["copied_files"]:
                made_progress = True
            if outcome["failed"]:
                return _terminal(
                    "error", f"{len(outcome['failed'])} replica key operation(s) failed verification",
                    code="REPLICA_KEY_FAILED", gate="C", evidence={"failures": outcome["failed"]},
                    actions=["inspect_annex_whereis", "verify_source", "retry_replica"],
                    failed=outcome["failed"][:12],
                )
            if outcome["deferred"]:
                # INV-13 / DEF-022: every ready replica has a safe source copy.  An unavailable
                # source/target is a resumable pause, never a red missing-copy error.
                return _terminal(
                    "paused", "copy #1 is safe; copy #2 is deferred until its drive is available",
                    code="SOURCE_UNAVAILABLE", gate="C",
                    evidence={"source_offline": outcome["source_offline"],
                              "deferred_targets": outcome["deferred_targets"]},
                    actions=["mount_or_reseat_drive", "start_fill"],
                )

        # The pinned snapshot batch is exhausted. Rebuild from durable facts before selecting the
        # next drive; this preserves drive affinity without persisting task claims.
        pinned_drive = None

    return _stop_terminal()
