"""Reconciled guided-fill scheduler (DEC-045 Phase 3).

Durable catalog facts are the only completion truth.  Each batch rebuilds an unpersisted work graph,
admits it through the capacity ledger, pins one drive, executes exact missing manifests, and then
reconciles again.  A crash discards only ephemeral scheduler state; completed file rows self-heal the
next graph.  Both CLI and portal call :func:`execute`.
"""
from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from modelark import capacity, fetch, plan, reconcile, register

_PROBE_NAME = ".modelark-write-probe"
_MAX_TASK_ATTEMPTS = 2


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
    """A mount is usable only after a write/read/delete probe succeeds."""
    with ctx.lock:
        path = register.archive_path(ctx.con, label)
    if path is None:
        return False
    probe = Path(path) / _PROBE_NAME
    try:
        probe.write_bytes(b"modelark")
        ok = probe.read_bytes() == b"modelark"
        probe.unlink()
        return ok
    except OSError:
        try:
            probe.unlink()
        except OSError:
            pass
        return False


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


def _reconcile(ctx, plan_id: str, provisioning: str, repo_scope: list[str] | None) -> _Snapshot:
    """Bulk graph/ledger snapshot, using a dedicated read connection in real executions."""
    live_free = _live_free(ctx, plan_id)
    if ctx.read_connection_factory is None:  # isolated in-memory/unit harness
        with ctx.lock:
            graph = reconcile.reconcile_plan(ctx.con, plan_id, repo_scope)
            ledger = capacity.plan_capacity(
                ctx.con, graph, provisioning=provisioning, live_free_by_drive=live_free,
            )
        return _Snapshot(graph, ledger)
    con = ctx.read_connection_factory()
    try:
        graph = reconcile.reconcile_plan(con, plan_id, repo_scope)
        ledger = capacity.plan_capacity(
            con, graph, provisioning=provisioning, live_free_by_drive=live_free,
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


def _file_guard(ctx, plan_id: str, provisioning: str, task: capacity.AssignedTask):
    budgets = {item.rfilename: item for item in task.budget.file_budgets}
    mode = capacity.mode_from_legacy(provisioning)

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


def _ready_tasks(snapshot: _Snapshot) -> list[capacity.AssignedTask]:
    satisfied = snapshot.graph.satisfied
    return [
        task for task in snapshot.ledger.tasks
        if task.depends_on_requirement is None or task.depends_on_requirement in satisfied
    ]


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
    pid, provisioning = prow["plan_id"], prow["provisioning"]
    ctx.on_progress({
        "phase": "plan", "plan_id": pid, "provisioning": provisioning,
        "say": f"plan '{pid}' · capacity={provisioning} · {len(prow['drives'])} drive(s)",
    })

    attempts: dict[str, int] = {}
    made_progress = False
    first = True
    pinned_drive: str | None = None
    while not ctx.should_stop():
        snapshot = _reconcile(ctx, pid, provisioning, repo_scope)
        if not snapshot.ledger.feasible:
            terminal = _admission_terminal(snapshot, pid, made_progress)
            ctx.on_progress({
                "phase": terminal["state"], "gate": "B", "code": terminal["code"],
                "evidence": terminal["evidence"], "actions": terminal["actions"],
                "say": ("🟠 " if made_progress else "🔴 ") + terminal["message"],
            })
            return terminal

        ready = _ready_tasks(snapshot)
        if not ready:
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
                task.repo_id: _file_guard(ctx, pid, provisioning, task) for task in fetch_tasks
            }
            outcome = fetch.run(
                drive_label=pinned_drive,
                repos=[task.repo_id for task in fetch_tasks],
                max_24h_gb=max_24h_gb,
                ctx=ctx,
                task_manifests=manifests,
                before_file=lambda repo_id, item: guards[repo_id](repo_id, item),
            )
            if outcome["stored_repos"]:
                made_progress = True
            if outcome["stopped"] or ctx.should_stop():
                return _stop_terminal()
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
