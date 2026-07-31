"""Reconciled guided-fill scheduler (DEC-045 Phase 3).

Durable catalog facts are the only completion truth.  Each batch rebuilds an unpersisted work graph,
admits it through the capacity ledger, pins one drive, executes exact missing manifests, and then
reconciles again.  A crash discards only ephemeral scheduler state; completed file rows self-heal the
next graph.  Both CLI and portal call :func:`execute`.
"""
from __future__ import annotations

from types import SimpleNamespace

import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from modelark import admission, capacity, fetch, plan, reconcile, register

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
    reason = "insert it" if not mounted else "mounted but not ready/readable (I/O error) — re-seat it"
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


def _evidence(con, plan_id: str) -> dict:
    """Admission evidence for the plan's drives via the shared snapshot seam (#35-C): a non-blocking
    fenced live/anchor read per drive. Offline drives derive anchor/unknown; a mounted drive is live only
    while identity-proven and drive-fenced. Never a legacy free scalar or capacity-minus-stored guess."""
    labels = [row[0] for row in con.execute(
        "SELECT drive_label FROM plan_drives WHERE plan_id=? ORDER BY drive_label", [plan_id]
    ).fetchall()]
    now = datetime.now(timezone.utc).isoformat(sep=" ")
    return admission.preview_by_drive(
        con, labels, observe=lambda label: fetch.observe_for_admission(con, label), now=now)


def _snapshot(con, plan_id: str, capacity_mode: str, repo_scope: list[str] | None) -> _Snapshot:
    graph = reconcile.reconcile_plan(con, plan_id, repo_scope)
    ledger = capacity.plan_capacity(
        con, graph, capacity_mode=capacity_mode, evidence_by_drive=_evidence(con, plan_id),
    )
    return _Snapshot(graph, ledger)


def _reconcile(ctx, plan_id: str, capacity_mode: str, repo_scope: list[str] | None) -> _Snapshot:
    """Bulk graph/ledger snapshot, using a dedicated read connection in real executions."""
    if ctx.read_connection_factory is None:  # isolated in-memory/unit harness
        with ctx.lock:
            return _snapshot(ctx.con, plan_id, capacity_mode, repo_scope)
    con = ctx.read_connection_factory()
    try:
        return _snapshot(con, plan_id, capacity_mode, repo_scope)
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
            # Admit from a FRESH observation taken while the transport already holds the drive fence
            # (this runs inside the drive_mutation envelope): never reacquire a fence, never a legacy read.
            observation = fetch._observe_drive(ctx.con, task.target_drive)
            evidence = admission.execution_evidence(
                ctx.con, task.target_drive, observation,
                now=datetime.now(timezone.utc).isoformat(sep=" "))
            drive = next((
                entry for entry in capacity.inspect_drives(
                    ctx.con, plan_id, evidence_by_drive={task.target_drive: evidence}
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
    session_start=None,
) -> dict:
    """Execute **only** the approved fixed projection (PR-09 / B8 hard cut).

    Never calls ``_reconcile``, ``reconcile_plan``, or ``plan_capacity``. Worker claim
    failures are fail-closed. Capacity exhaustion does not re-home. Every outcome
    terminalizes the session and releases session child fences.
    """
    from modelark import execution_service, execution_session as esess, execution_recovery as erec
    from modelark.proposal import Refusal

    session = None
    result = None
    try:
        if session_start is None:
            svc_out = execution_service.start_fill(
                plan_id=plan_id or "ark", con=getattr(ctx, "con", None))
            if isinstance(svc_out, Refusal):
                return {
                    "ok": False, "stopped": False, "state": "blocked",
                    "message": str(svc_out), "code": svc_out.code,
                    "evidence": getattr(svc_out, "evidence", None),
                    "actions": list(getattr(svc_out, "actions", ()) or ()),
                    "failed": [],
                }
            session_start = svc_out

        session = getattr(session_start, "session", None)
        projection = getattr(session_start, "projection", None)
        if projection is None or not hasattr(projection, "tasks"):
            result = _terminal(
                "blocked", "session projection missing; cannot execute without approved map",
                code="APPROVAL_PROJECTION_MISSING",
                actions=["preview_again", "start_fill"],
            )
            return result

        # Fail closed if worker claim fails while session is still starting.
        if session is not None and getattr(session, "state", None) == "starting":
            try:
                esess.claim_worker(
                    ctx.con,
                    session_id=session.session_id,
                    fencing_token=session.fencing_token,
                    worker_identity=f"worker-fill-{os.getpid()}",
                    controller_identity=getattr(session, "controller_identity", "controller"),
                )
                session = esess.load_session(ctx.con, session.session_id) or session
            except Exception as exc:
                code = getattr(exc, "code", None) or "SESSION_CLAIM_FAILED"
                result = _terminal(
                    "blocked", f"worker claim failed: {exc}",
                    code=str(code),
                    evidence={"session_id": getattr(session, "session_id", None)},
                    actions=["retry_fill", "inspect_session"],
                )
                return result

        # Bind session authority onto the RunCtx so fetch writes use session_write.
        if session is not None:
            ctx.session_id = session.session_id
            ctx.fencing_token = int(session.fencing_token)
            # Do NOT take a second set of session-level drive locks — the physical
            # mutation envelope already holds drive fences and exposes child FDs.
            ctx.stats["child_fence_fds"] = ()
        # Finding 35: carry frozen ExecutionConfig into transport (no global reread).
        frozen_cfg = getattr(session_start, "execution_config", None) if session_start else None
        if frozen_cfg is not None:
            ctx.execution_config = frozen_cfg

        result = _drain_projection(
            ctx, session_start,
            plan_id=plan_id,
            max_24h_gb=max_24h_gb,
            repo_scope=repo_scope,
            guided=guided,
            poll_secs=poll_secs,
            child_fds=(),
        )
        return result
    finally:
        # Terminalize live session + release any session child markers (finding 32/36).
        if session is not None and getattr(ctx, "con", None) is not None:
            try:
                _terminalize_session_outcome(ctx.con, session, result)
            except Exception as exc:
                # Finding 36: terminalization failure must force an unsuccessful outcome
                # even when drain already produced ok=True (never leave session running
                # while reporting success).
                code = str(getattr(exc, "code", None) or "SESSION_TERMINALIZE_FAILED")
                if not isinstance(result, dict):
                    result = _terminal(
                        "failed", f"session terminalize refused: {code}",
                        code=code,
                        evidence=getattr(exc, "evidence", None),
                        actions=["inspect_session"],
                    )
                else:
                    result["ok"] = False
                    result["stopped"] = False
                    result["state"] = "failed"
                    result["code"] = code
                    result["terminalize_error"] = code[:200]
                    result["message"] = (
                        f"session terminalize refused: {code}; "
                        f"prior outcome overwritten"
                    )
                    if getattr(exc, "evidence", None) is not None:
                        result["evidence"] = getattr(exc, "evidence", None)
            try:
                erec.release_child_fences(session.session_id)
            except Exception:
                pass


def _terminalize_session_outcome(con, session, result) -> None:
    """Map execute terminal dict → session state and mark_terminal with fencing token.

    Finding 36: never replace a failed token CAS with a tokenless UPDATE. Surface the
    failure so the outer path can record a recoverable terminalization defect.
    """
    from modelark import execution_session as esess
    from modelark.proposal import Refusal
    if session is None:
        return
    # Reload — claim may have updated state.
    row = esess.load_session(con, session.session_id)
    if row is None:
        return
    if getattr(row, "state", None) not in esess.LIVE_STATES:
        return
    token = int(getattr(row, "fencing_token", None) or getattr(session, "fencing_token", 0))
    if result is None:
        state, code = "failed", "UNHANDLED_FILL_ERROR"
    else:
        rstate = result.get("state") or ("done" if result.get("ok") else "failed")
        code = result.get("code") or rstate.upper()
        if rstate in ("done",) or result.get("ok"):
            state = "done"
        elif rstate in ("stopped",) or result.get("stopped"):
            state = "stopped"
        elif rstate in ("paused", "plan-capacity-stop"):
            state = "paused"
        elif rstate in ("blocked",):
            state = "blocked"
        elif rstate in ("error", "failed"):
            state = "failed"
        else:
            state = "failed"
    try:
        esess.terminalize(
            con, session_id=session.session_id, fencing_token=token,
            state=state, terminal_code=str(code)[:64] if code else None)
    except Refusal:
        # Preserve typed CAS failure; do not tokenless-update.
        raise
    except Exception as exc:
        # Integrity/unexpected path — re-raise so caller can fail closed.
        raise Refusal(
            "SESSION_TERMINALIZE_FAILED",
            {"session_id": session.session_id, "error": str(exc)[:200]},
            ("inspect_session",)) from exc


# Instrumented batch/event refresh counters for B12 acceptance (finding 38).
_PROJECTION_REFRESH_CALLS = 0
_PROJECTION_REFRESH_BY_REASON: dict[str, int] = {}


def projection_refresh_call_count() -> int:
    return int(_PROJECTION_REFRESH_CALLS)


def projection_refresh_breakdown() -> dict[str, int]:
    return dict(_PROJECTION_REFRESH_BY_REASON)


def reset_projection_refresh_call_count() -> None:
    global _PROJECTION_REFRESH_CALLS, _PROJECTION_REFRESH_BY_REASON
    _PROJECTION_REFRESH_CALLS = 0
    _PROJECTION_REFRESH_BY_REASON = {}


def _refresh_projection(ctx, session_start, *, plan_id=None, reason: str = "batch_boundary"):
    """Re-run project_pure against current catalog facts (constrained refresh).

    Finding 37: propagate typed projection/config/evidence refusals — never convert
    them to None and continue on a stale projection.
    ``reason`` tags the production seam (batch_boundary | typed_event:<name>).
    """
    global _PROJECTION_REFRESH_CALLS, _PROJECTION_REFRESH_BY_REASON
    from modelark import execution_projection as eproj, execution_session as esess
    from modelark.proposal import load_proposal, Refusal
    _PROJECTION_REFRESH_CALLS += 1
    key = str(reason or "batch_boundary")
    _PROJECTION_REFRESH_BY_REASON[key] = int(_PROJECTION_REFRESH_BY_REASON.get(key) or 0) + 1
    session = getattr(session_start, "session", None)
    if session is None:
        raise Refusal(
            "APPROVAL_PROJECTION_MISSING",
            {"reason": "session_missing_on_refresh"},
            ("start_fill",))
    try:
        proposal = getattr(session_start, "_proposal", None) or load_proposal(
            ctx.con, session.approved_proposal_id)
    except Exception as exc:
        raise Refusal(
            "APPROVED_INPUT_CHANGED",
            {"reason": "proposal_load_failed", "error": str(exc)[:200]},
            ("preview_again",)) from exc
    if not proposal:
        raise Refusal(
            "APPROVAL_MISSING",
            {"proposal_id": getattr(session, "approved_proposal_id", None)},
            ("preview_again",))
    relevant = esess._proposal_drive_ids(proposal)
    # Finding 35: compare frozen config against authoritative *current* global reader
    # at projection boundaries only — never against a self-echo of the freeze.
    frozen = getattr(session_start, "execution_config", None)
    if frozen is not None:
        from modelark.execution_config import assert_frozen_unchanged
        global_reader = getattr(session_start, "_config_reader", None)
        if global_reader is None:
            # Production default: re-read live graph-affecting config at the boundary.
            from modelark.execution_service import production_services
            global_reader = production_services(ctx.con).config
        assert_frozen_unchanged(frozen, global_reader)
    # Authoritative capacity evidence (finding 37) — never None fabricate.
    observe = getattr(session_start, "_observe_exact_capacity", None)
    if observe is None:
        from modelark.proposal import _DefaultServices
        observe = _DefaultServices().observe_exact_capacity
    services = SimpleNamespace(
        observe_exact_capacity=observe,
        config=SimpleNamespace(
            read_graph_affecting_config=lambda: dict(getattr(frozen, "values", None) or {})),
    )
    cfg = getattr(frozen, "values", None) or {}
    current_input, current_graph = esess._catalog_projection_bundle(
        ctx.con, proposal, relevant, services, cfg)
    parked = set()
    out = eproj.project_pure(
        proposal, current_input, current_graph,
        SimpleNamespace(parked_gated_repos=frozenset(parked)))
    if isinstance(out, Refusal):
        raise out
    return out


def _proj_field(t, name, default=None):
    if isinstance(t, dict):
        return t.get(name, default)
    return getattr(t, name, default)


def _archive_content_satisfies(
    approved_sha,
    archived_sha=None,
    *,
    orig_sha256=None,
    compressed: bool = False,
    annex_key=None,
) -> bool:
    """Durable content satisfaction when an archive row exists (DEC-055).

    Resolves the stored copy's original-byte digest through
    ``archive_hash.expected_sha256`` (shared with restore, verification, repair)
    with ``catalog_sha=None`` so Fill never reopens catalog ``files`` authority.
    ``proposal_files`` remains the approved file-list authority (RFC-002).

    * Approved hash present → resolved digest must equal it (case-insensitive).
    * Approved hash absent → a digest must be resolvable (ingestion ``orig_sha256``
      or raw ``SHA256``/``SHA256E`` annex key). Compressed annex keys do not resolve.
    * Nothing resolvable → fails closed. Archive-row presence alone never proves
      durability.

    ``archived_sha`` is accepted as a legacy positional alias for ``orig_sha256``
    when keyword archive fields are omitted (pure two-arg matrix tests).
    """
    from modelark import archive_hash

    if orig_sha256 is None and annex_key is None and archived_sha is not None:
        orig_sha256 = archived_sha
    resolved = archive_hash.expected_sha256(
        catalog_sha=None,
        orig_sha256=orig_sha256,
        compressed=bool(compressed),
        annex_key=annex_key,
    )
    if approved_sha:
        if resolved is None:
            return False
        return str(resolved).lower() == str(approved_sha).lower()
    return resolved is not None and str(resolved) != ""


def _source_files_content_ready(con, repo_id, source_drive, proposal_files_for_req) -> bool:
    """True only when every approved file is content-satisfied on the source drive.

    DEC-055: same resolution rule as target evaluation via ``expected_sha256``.
    """
    if not proposal_files_for_req or not source_drive:
        return False
    for pf in proposal_files_for_req:
        rfilename = pf.get("rfilename") if isinstance(pf, dict) else getattr(pf, "rfilename", None)
        want = pf.get("orig_sha256") if isinstance(pf, dict) else getattr(pf, "orig_sha256", None)
        if not rfilename:
            return False
        arch = con.execute(
            "SELECT orig_sha256, compressed, annex_key FROM archived "
            "WHERE repo_id=? AND rfilename=? AND drive_label=?",
            [repo_id, rfilename, source_drive]).fetchone()
        if arch is None:
            return False
        if not _archive_content_satisfies(
                want, orig_sha256=arch[0], compressed=bool(arch[1]), annex_key=arch[2]):
            return False
    return True


def _projection_work_units(con, projection, repo_scope=None, proposal_files=None,
                           *, require_proposal_files=True):
    """Convert frozen projection tasks into drain units.

    Finding 37: when an approved proposal is bound (``require_proposal_files``),
    ``proposal_files`` are the sole file authority — never fall back to mutable
    catalog ``files``. Characterization paths without an approval may read catalog
    files. Never invent paths. Targets only from the projection.
    """
    from modelark.reconcile import TaskKind
    from modelark.proposal import Refusal
    scope = set(repo_scope) if repo_scope else None
    # requirement_id -> list of proposal file dicts (storage_action preserved, never invented).
    by_req = {}
    for ff in proposal_files or ():
        rid = ff.get("requirement_id") if isinstance(ff, dict) else getattr(ff, "requirement_id", None)
        if rid:
            if isinstance(ff, dict):
                by_req.setdefault(rid, []).append({
                    "rfilename": ff.get("rfilename"),
                    "size_bytes": ff.get("size_bytes"),
                    "orig_sha256": ff.get("orig_sha256"),
                    "format": ff.get("format"),
                    "quant": ff.get("quant"),
                    # Preserve frozen approval value, including absent → None. Do not default.
                    "storage_action": ff.get("storage_action"),
                })
            else:
                by_req.setdefault(rid, []).append({
                    "rfilename": getattr(ff, "rfilename", None),
                    "size_bytes": getattr(ff, "size_bytes", None),
                    "orig_sha256": getattr(ff, "orig_sha256", None),
                    "format": getattr(ff, "format", None),
                    "quant": getattr(ff, "quant", None),
                    "storage_action": getattr(ff, "storage_action", None),
                })
    units = []
    for t in projection.tasks or ():
        if _proj_field(t, "row_kind") == "baseline_satisfied":
            continue
        repo = _proj_field(t, "repo_id")
        if scope is not None and repo not in scope:
            continue
        target = _proj_field(t, "target_drive")
        source = _proj_field(t, "source_drive")
        rid = _proj_field(t, "requirement_id") or f"primary:{repo}"
        schedule = _proj_field(t, "schedule_state") or "ready"
        # Re-evaluate waiting_dependency: source must prove content identity against
        # approved proposal_files (finding 37) — mere archive row presence is not enough.
        if schedule == "waiting_dependency" and source:
            prop_for_src = by_req.get(rid) or []
            if require_proposal_files and not prop_for_src:
                # Stay waiting / later refuse on file authority — do not promote.
                pass
            elif prop_for_src:
                if _source_files_content_ready(con, repo, source, prop_for_src):
                    schedule = "ready"
            else:
                # Characterization path without approval: presence only.
                src_ok = con.execute(
                    "SELECT 1 FROM archived WHERE repo_id=? AND drive_label=? LIMIT 1",
                    [repo, source]).fetchone()
                if src_ok:
                    schedule = "ready"
        if schedule == "parked_gated":
            units.append(SimpleNamespace(
                requirement_id=rid, repo_id=repo, target_drive=target,
                source_drive=source, kind=None, schedule_state=schedule,
                order_key=int(_proj_field(t, "order_key") or 0),
                missing_files=(), file_rows=(),
            ))
            continue
        if schedule == "waiting_dependency":
            units.append(SimpleNamespace(
                requirement_id=rid, repo_id=repo, target_drive=target,
                source_drive=source, kind=None, schedule_state=schedule,
                order_key=int(_proj_field(t, "order_key") or 0),
                missing_files=(), file_rows=(),
            ))
            continue
        # Finding 37: approved proposal_files only when approval is bound.
        prop_files = by_req.get(rid) or []
        if prop_files:
            file_specs = [
                (pf.get("rfilename"), pf.get("size_bytes"), pf.get("orig_sha256"),
                 pf.get("format"), pf.get("quant"), pf.get("storage_action"))
                for pf in prop_files if pf.get("rfilename")
            ]
        elif require_proposal_files:
            raise Refusal(
                "APPROVED_INPUT_CHANGED",
                {"reason": "missing_proposal_file_authority",
                 "requirement_id": rid, "repo_id": repo},
                ("preview_again",))
        else:
            # Pre-approval / characterization only: canonical live acquisition
            # policy (not approved authority). Approved proposal_files branch
            # above must never call this — frozen authority only.
            from modelark import archive_manifest as _am
            file_specs = [
                (mf.rfilename, mf.size_bytes, mf.sha256, mf.format, mf.quant,
                 mf.storage_action)
                for mf in _am.manifest_for_repo(con, repo)
            ]
        if not file_specs:
            if require_proposal_files:
                raise Refusal(
                    "APPROVED_INPUT_CHANGED",
                    {"reason": "empty_proposal_file_authority",
                     "requirement_id": rid, "repo_id": repo},
                    ("preview_again",))
            continue
        missing = []
        file_rows = []
        for rfilename, size_bytes, sha256, fmt, quant, storage_action in file_specs:
            arch = con.execute(
                "SELECT orig_sha256, compressed, annex_key FROM archived "
                "WHERE repo_id=? AND rfilename=? AND drive_label=?",
                [repo, rfilename, target]).fetchone() if target else None
            row = SimpleNamespace(
                rfilename=rfilename, size_bytes=int(size_bytes or 0),
                sha256=sha256, format=fmt, quant=quant,
                storage_action=storage_action)
            file_rows.append(row)
            # Same content-satisfaction rule as source readiness (DEC-055).
            if arch is None or not _archive_content_satisfies(
                    sha256, orig_sha256=arch[0], compressed=bool(arch[1]),
                    annex_key=arch[2]):
                missing.append(rfilename)
        if not missing and file_rows:
            continue  # fully satisfied on approved target — shrink out
        kind = TaskKind.REPLICATE if source else TaskKind.FETCH
        # Finding 37: replica execution requires source content identity vs proposal_files.
        if (
            kind == TaskKind.REPLICATE
            and require_proposal_files
            and source
            and not _source_files_content_ready(con, repo, source, by_req.get(rid) or [])
        ):
            units.append(SimpleNamespace(
                requirement_id=rid, repo_id=repo, target_drive=target,
                source_drive=source, kind=None,
                schedule_state="waiting_dependency",
                order_key=int(_proj_field(t, "order_key") or 0),
                missing_files=(), file_rows=(),
            ))
            continue
        from modelark.budgets import FileBudget
        file_budgets = []
        for fr in file_rows:
            if fr.rfilename not in missing and missing:
                continue
            sz = int(fr.size_bytes or 0)
            file_budgets.append(FileBudget(
                rfilename=fr.rfilename,
                guaranteed_durable=sz,
                expected_durable=sz,
                workspace_peak_guaranteed=0,
                workspace_peak_expected=0,
                evidence="projection",
            ))
        gdur = int(_proj_field(t, "guaranteed_durable") or sum(fb.guaranteed_durable for fb in file_budgets))
        edur = int(_proj_field(t, "expected_durable") or gdur)
        units.append(SimpleNamespace(
            requirement_id=rid, repo_id=repo, target_drive=target,
            source_drive=source, kind=kind, schedule_state=schedule,
            order_key=int(_proj_field(t, "order_key") or 0),
            missing_files=tuple(missing), file_rows=tuple(file_rows),
            task_id=rid,
            depends_on_requirement=(f"primary:{repo}" if source else None),
            budget=SimpleNamespace(
                task_id=rid, requirement_id=rid, repo_id=repo, kind=kind,
                target_drive=target, source_drive=source,
                missing_files=tuple(missing),
                file_budgets=tuple(file_budgets),
                guaranteed_durable=gdur,
                expected_durable=edur,
                workspace_peak_guaranteed=0, workspace_peak_expected=0,
                evidence="projection",
            ),
        ))
    units.sort(key=lambda u: (u.order_key, u.requirement_id or ""))
    return units


def _fetch_task_manifests(fetch_tasks):
    """Build typed FETCH manifests from frozen approved missing rows (INC-025).

    Converts each unit's missing_files into ``archive_manifest.ManifestFile`` values
    using only work-unit file_rows (approved authority). Never re-reads catalog or
    acquisition policy. No empty-intersection fallback to all file_rows.
    """
    from modelark import archive_manifest
    from modelark.proposal import Refusal

    out: dict[str, tuple] = {}
    for unit in fetch_tasks:
        rows = []
        for missing_name in (unit.missing_files or ()):
            matches = [
                fr for fr in (unit.file_rows or ())
                if getattr(fr, "rfilename", None) == missing_name
            ]
            if len(matches) == 0:
                raise Refusal(
                    "APPROVED_INPUT_CHANGED",
                    {
                        "reason": "missing_proposal_file_authority",
                        "requirement_id": unit.requirement_id,
                        "repo_id": unit.repo_id,
                        "rfilename": missing_name,
                    },
                    ("preview_again",),
                )
            if len(matches) > 1:
                raise Refusal(
                    "APPROVED_INPUT_CHANGED",
                    {
                        "reason": "ambiguous_proposal_file_authority",
                        "requirement_id": unit.requirement_id,
                        "repo_id": unit.repo_id,
                        "rfilename": missing_name,
                        "matches": len(matches),
                    },
                    ("preview_again",),
                )
            fr = matches[0]
            action = getattr(fr, "storage_action", None)
            if action not in ("compress", "raw"):
                raise Refusal(
                    "APPROVED_INPUT_CHANGED",
                    {
                        "reason": "invalid_storage_action",
                        "requirement_id": unit.requirement_id,
                        "repo_id": unit.repo_id,
                        "rfilename": missing_name,
                        "storage_action": action,
                    },
                    ("preview_again",),
                )
            rows.append(archive_manifest.ManifestFile(
                rfilename=fr.rfilename,
                size_bytes=int(fr.size_bytes or 0),
                sha256=getattr(fr, "sha256", None),
                format=getattr(fr, "format", None) or "",
                quant=getattr(fr, "quant", None),
                storage_action=action,
            ))
        out[unit.repo_id] = tuple(rows)
    return out


def _drain_projection(
    ctx, session_start, *, plan_id, max_24h_gb, repo_scope, guided, poll_secs,
    child_fds=(),
):
    """Drain session_start.projection.tasks — fixed map only."""
    from modelark.reconcile import TaskKind

    projection = session_start.projection
    session = getattr(session_start, "session", None)
    # Load approved proposal files once for file authority.
    proposal_files = list(getattr(session_start, "_proposal_files", None) or ())
    # Bound approval + frozen config ⇒ proposal_files are sole authority (finding 37).
    require_proposal_files = bool(
        session is not None
        and getattr(session, "approved_proposal_id", None)
        and getattr(session_start, "execution_config", None) is not None
    )
    # Same gate as require_proposal_files: real SessionStart with freeze + approval.
    has_approval = require_proposal_files
    if not proposal_files and session is not None and require_proposal_files:
        try:
            from modelark.proposal import load_proposal
            prop = load_proposal(ctx.con, session.approved_proposal_id)
            proposal_files = list(prop.get("files") or ())
            session_start._proposal_files = proposal_files  # cache
            session_start._proposal = prop
        except Exception:
            proposal_files = []

    ctx.stats["t0"] = time.monotonic()
    ctx.stats.setdefault("by_drive", {})
    with ctx.lock:
        prow = (plan.get(ctx.con, plan_id) if plan_id else plan.active(ctx.con)) \
            or plan.bootstrap(ctx.con)
    pid, capacity_mode = prow["plan_id"], prow["capacity_mode"]
    ctx.on_progress({
        "phase": "plan", "plan_id": pid, "capacity_mode": capacity_mode,
        "provisioning": plan.legacy_capacity_mode(capacity_mode),
        "deprecated_fields": ["provisioning"],
        "say": (
            f"plan '{pid}' · approved projection "
            f"({len(getattr(projection, 'tasks', ()) or ())} task(s)) · "
            f"capacity mode={capacity_mode}"
        ),
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

    with ctx.lock:
        remaining = _projection_work_units(
            ctx.con, projection, repo_scope, proposal_files=proposal_files,
            require_proposal_files=require_proposal_files)

    attempts: dict[str, int] = {}
    gated_hits: dict[str, int] = {}
    deferred_gated: set[str] = set()
    # Session-local progress by requirement_id (mock fetch may not write archived).
    completed_reqs: set[str] = set()
    made_progress = False
    first = True
    pinned_drive: str | None = None
    batch_order = []
    for u in remaining:
        if u.target_drive and u.target_drive not in batch_order:
            batch_order.append(u.target_drive)

    while not ctx.should_stop():
        with ctx.lock:
            remaining = _projection_work_units(
            ctx.con, projection, repo_scope, proposal_files=proposal_files,
            require_proposal_files=require_proposal_files)
        # re-apply deferred + session-completed filters
        ready = [
            u for u in remaining
            if u.repo_id not in deferred_gated
            and u.requirement_id not in completed_reqs
            and (u.schedule_state or "ready") == "ready"
            and u.kind is not None
            and u.missing_files
        ]
        if not ready:
            waiting = [u for u in remaining if (u.schedule_state or "") == "waiting_dependency"]
            parked = [u for u in remaining if u.repo_id in deferred_gated
                      or (u.schedule_state or "") == "parked_gated"]
            if parked and not waiting and not ready:
                repos = sorted({u.repo_id for u in parked})
                message = (
                    f"fill complete with {len(repos)} gated-access follow-up(s); "
                    "all other feasible work is safe"
                )
                return _terminal(
                    "done", message, code="PLAN_COMPLETE_WITH_FOLLOWUPS",
                    evidence={"access_gated": repos},
                    actions=["review_followups", "start_fill"],
                )
            if waiting and not ready:
                return _terminal(
                    "paused", "approved projection waiting on dependencies",
                    code="WAITING_DEPENDENCY", gate="C",
                    evidence={"requirements": [u.requirement_id for u in waiting]},
                    actions=["resume_same_approval"],
                )
            message = "fill complete — approved projection drained"
            ctx.on_progress({"phase": "done", "code": "PLAN_SATISFIED", "say": "✅ " + message})
            return _terminal("done", message, code="PLAN_SATISFIED")

        if first and not guided:
            involved = {u.target_drive for u in ready if u.target_drive}
            involved.update(u.source_drive for u in ready if u.source_drive)
            unmounted = [
                label for label in sorted(involved)
                if _mounted(ctx, label) == (True, False)
            ]
            if unmounted:
                return _terminal(
                    "blocked",
                    f"required drive(s) not mounted: {', '.join(unmounted)}. "
                    "Mount them, then re-run. (No bytes fetched.)",
                    code="DRIVE_UNAVAILABLE", gate="A",
                    evidence={"drives": unmounted}, actions=["mount_drives", "replan"],
                )
        first = False

        labels = {u.target_drive for u in ready if u.target_drive}
        if not labels:
            return _terminal("done", "no target drives remain", code="PLAN_SATISFIED")
        if pinned_drive not in labels:
            pinned_drive = next(
                (lab for lab in batch_order if lab in labels), sorted(labels)[0])
        batch = [u for u in ready if u.target_drive == pinned_drive]
        batch.sort(key=lambda u: (u.order_key, u.requirement_id or ""))

        if guided and not _await_drive(ctx, pinned_drive, poll_secs):
            return _stop_terminal()
        if ctx.should_stop():
            return _stop_terminal()

        fetch_tasks = [u for u in batch if u.kind == TaskKind.FETCH]
        replica_tasks = [u for u in batch if u.kind == TaskKind.REPLICATE]

        if fetch_tasks:
            ctx.on_progress({
                "phase": "primary", "drive": pinned_drive, "n_repos": len(fetch_tasks),
                "say": f"== {pinned_drive} ({len(fetch_tasks)} exact fetch task(s)) ==",
            })
            # INC-025: typed ManifestFile from frozen approved missing rows only.
            manifests = _fetch_task_manifests(fetch_tasks)

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

            # Per-file capacity guard on the **approved** target only (no re-placement).
            guards = {
                u.repo_id: _file_guard(ctx, pid, capacity_mode, u) for u in fetch_tasks
            }

            outcome = fetch.run(
                drive_label=pinned_drive,
                repos=[u.repo_id for u in fetch_tasks],
                max_24h_gb=max_24h_gb,
                ctx=ctx,
                task_manifests=manifests,
                before_file=lambda repo_id, item: guards[repo_id](repo_id, item),
                on_gated=on_gated,
            )
            if outcome["stored_repos"]:
                made_progress = True
                for u in fetch_tasks:
                    if u.repo_id in outcome["stored_repos"]:
                        completed_reqs.add(u.requirement_id)
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
                    evidence={"max_24h_gb": max_24h_gb},
                    actions=["wait_for_window", "start_fill"],
                )
            if outcome["capacity_failure"] is not None:
                state = "plan-capacity-stop" if made_progress else "blocked"
                return _terminal(
                    state,
                    "approved target capacity exhausted; re-home requires a new approval",
                    code="PLAN_CAPACITY_STOP", gate="B",
                    evidence=(outcome["capacity_failure"]
                              if isinstance(outcome["capacity_failure"], dict)
                              else {"drive": pinned_drive}),
                    actions=["preview_again", "add_capacity"],
                )
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
                # Typed state-changing event: gated retry — refresh before continuing
                # (RFC-002 batch/event cadence; finding 38).
                if has_approval:
                    from modelark.proposal import Refusal as _Refusal
                    try:
                        refreshed = _refresh_projection(
                            ctx, session_start, plan_id=pid,
                            reason="typed_event:gated_retry")
                    except _Refusal as exc:
                        return _terminal(
                            "failed", f"projection refresh refused: {exc.code}",
                            code=str(exc.code),
                            evidence=getattr(exc, "evidence", None),
                            actions=list(getattr(exc, "actions", ()) or ())
                            or ["preview_again"],
                        )
                    except Exception as exc:
                        return _terminal(
                            "failed", f"projection refresh failed: {exc}",
                            code="PROJECTION_REFRESH_FAILED",
                            evidence={"error": str(exc)[:200]},
                            actions=["preview_again", "inspect_session"],
                        )
                    if refreshed is not None:
                        session_start.projection = refreshed
                        projection = refreshed
                continue
            if outcome["drive_unwritable"]:
                return _terminal(
                    "paused" if made_progress else "blocked",
                    f"approved target drive {pinned_drive} is not writable",
                    code="DRIVE_UNWRITABLE", gate="A",
                    evidence={"drive": pinned_drive},
                    actions=["mount_or_reseat_drive", "resume_same_approval"],
                )

        if replica_tasks:
            # Adapt units to AssignedTask-shaped objects for run_replica_tasks
            replica_assigned = []
            for u in replica_tasks:
                replica_assigned.append(SimpleNamespace(
                    task_id=u.task_id, requirement_id=u.requirement_id,
                    repo_id=u.repo_id, kind=u.kind,
                    target_drive=u.target_drive, source_drive=u.source_drive,
                    depends_on_requirement=u.depends_on_requirement,
                    budget=u.budget,
                ))
            ctx.on_progress({
                "phase": "replica", "drive": pinned_drive, "n_repos": len(replica_tasks),
                "say": f"== {pinned_drive} ({len(replica_tasks)} exact replica task(s)) ==",
            })
            outcome = fetch.run_replica_tasks(replica_assigned, ctx=ctx)
            if outcome["copied_files"]:
                made_progress = True
                completed_reqs.update(u.requirement_id for u in replica_tasks)
            if outcome["failed"]:
                return _terminal(
                    "error",
                    f"{len(outcome['failed'])} replica key operation(s) failed verification",
                    code="REPLICA_KEY_FAILED", gate="C",
                    evidence={"failures": outcome["failed"]},
                    actions=["inspect_annex_whereis", "verify_source", "retry_replica"],
                    failed=outcome["failed"][:12],
                )
            if outcome["deferred"]:
                return _terminal(
                    "paused",
                    "copy #1 is safe; copy #2 is deferred until its drive is available",
                    code="SOURCE_UNAVAILABLE", gate="C",
                    evidence={"source_offline": outcome["source_offline"],
                              "deferred_targets": outcome["deferred_targets"]},
                    actions=["mount_or_reseat_drive", "start_fill"],
                )

        pinned_drive = None
        # Heartbeat while running (finding 32/36) — fail closed on required heartbeat failure.
        if session is not None:
            from modelark import execution_session as esess
            from modelark.proposal import Refusal
            try:
                # Reload so worker_identity CAS uses the claimed row (finding 36).
                live = esess.load_session(ctx.con, session.session_id) or session
                esess.heartbeat(
                    ctx.con,
                    session_id=live.session_id,
                    fencing_token=int(live.fencing_token),
                    worker_identity=getattr(live, "worker_identity", None),
                )
                session = live
            except Refusal as exc:
                return _terminal(
                    "failed", f"session heartbeat refused: {exc.code}",
                    code=str(exc.code),
                    evidence=getattr(exc, "evidence", None),
                    actions=list(getattr(exc, "actions", ()) or ()) or ["inspect_session"],
                )
            except Exception as exc:
                return _terminal(
                    "failed", f"session heartbeat failed: {exc}",
                    code="SESSION_HEARTBEAT_FAILED",
                    evidence={"session_id": session.session_id},
                    actions=["inspect_session"],
                )
        # Constrained projection refresh at batch boundary (B8 / finding 37) — fail closed
        # when an approved proposal is bound. Characterization bridges without an approval
        # keep the fixed start projection and re-derive work units only.
        from modelark.proposal import Refusal as _Refusal
        if has_approval:
            try:
                refreshed = _refresh_projection(
                    ctx, session_start, plan_id=pid, reason="batch_boundary")
            except _Refusal as exc:
                return _terminal(
                    "failed", f"projection refresh refused: {exc.code}",
                    code=str(exc.code),
                    evidence=getattr(exc, "evidence", None),
                    actions=list(getattr(exc, "actions", ()) or ()) or ["preview_again"],
                )
            except Exception as exc:
                return _terminal(
                    "failed", f"projection refresh failed: {exc}",
                    code="PROJECTION_REFRESH_FAILED",
                    evidence={"error": str(exc)[:200]},
                    actions=["preview_again", "inspect_session"],
                )
            if refreshed is None:
                return _terminal(
                    "failed", "projection refresh returned no authority",
                    code="APPROVED_INPUT_CHANGED",
                    actions=["preview_again"],
                )
            session_start.projection = refreshed
            projection = refreshed
        try:
            with ctx.lock:
                remaining = _projection_work_units(
                    ctx.con, projection, repo_scope,
                    proposal_files=proposal_files,
                    require_proposal_files=require_proposal_files)
        except _Refusal as exc:
            return _terminal(
                "failed", f"projection work units refused: {exc.code}",
                code=str(exc.code),
                evidence=getattr(exc, "evidence", None),
                actions=list(getattr(exc, "actions", ()) or ()) or ["preview_again"],
            )
        batch_order = []
        for u in remaining:
            if u.target_drive and u.target_drive not in batch_order:
                batch_order.append(u.target_drive)

    return _stop_terminal()
