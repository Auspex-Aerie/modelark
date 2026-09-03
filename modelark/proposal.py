"""Placement proposal shell: draft persist, approval CAS, graph_write (RFC-002 / PR-08).

Pure serializer lives in ``proposal_canonical`` (A5). This module owns SQLite persistence,
CAS approval, adopt_current, and planner_revision bump discipline for supported writers.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from modelark import (
    archive_manifest,
    capacity,
    drive_fence,
    placement,
    plan as plan_mod,
    planning,
)
from modelark import proposal_canonical as canonical
from modelark.core import db
from modelark.web import fill_worker

# Exact Gate-B actions for multi-repo acquisition-policy INFEASIBLE (INC-024 Q2 / DEC-050).
_MANIFEST_POLICY_ACTIONS = ("review_manifest_policy", "trim_selection", "replan")

# ---------------------------------------------------------------------------
# Inventory of graph-affecting writers (A3). Names are matched loosely by tests.
# ---------------------------------------------------------------------------
GRAPH_AFFECTING_WRITERS = {
    "selection_api.finalize": "portal selection finalize",
    "selection_api.clear": "portal selection clear",
    "selection_api.toggle": "portal selection toggle",
    "selection_api.bulk": "portal selection bulk",
    "discover.discover_one": "discover one repo",
    "discover.discover_repos": "discover many repos",
    "db.replace_files": "manifest file refresh",
    "cli.cmd_protect": "numcopies protect",
    "plan.create": "plan create",
    "plan.add_drive": "plan membership add",
    "plan.remove_drive": "plan membership remove",
    "plan.set_active": "active plan switch",
    "plan.bootstrap": "plan bootstrap membership",
    "plan.set_capacity_mode": "plan capacity mode",
    "drive_mutation.begin_generation": "dirty generation advance",
    "drive_mutation.publish_clean_anchor": "clean anchor publish",
    "drive_bootstrap.reconcile_drive": "drive identity bootstrap",
    "drive_lifecycle.declare_lost": "operator-confirmed drive loss",
    "drive_lifecycle.register_new_identity": "operator-confirmed new drive identity registration",
    "register.register_drive": "drive registration",
    "hash_repair.repair_hashes": "legacy hash repair apply",
    "proposal.approve": "proposal approval CAS",
    "fetch.RunCtx.write": "fetch archived progress/removal write path",
}

graph_affecting_writers = GRAPH_AFFECTING_WRITERS


# ---------------------------------------------------------------------------
# Typed results / refusals
# ---------------------------------------------------------------------------
@dataclass
class GraphResult:
    proven_noop: bool = False
    value: Any = None


@dataclass
class Refusal(Exception):
    code: str
    evidence: Any = None
    actions: tuple = ()

    def __str__(self) -> str:
        return f"{self.code}: {self.evidence!r}"


# ---------------------------------------------------------------------------
# graph_write / revision bump
# ---------------------------------------------------------------------------
def _session_write_authorized(con) -> bool:
    """Connection-scoped session_write authority only (finding 34 — never process-global)."""
    try:
        from modelark.execution_session import session_write_authorized
        return bool(session_write_authorized(con))
    except ImportError:
        return False


def _require_fill_idle(con) -> None:
    """Refuse a proposal/control-plane write while a live Fill owns the catalog."""
    try:
        from modelark.execution_session import live_owner, live_session_exists
        if not _session_write_authorized(con) and live_session_exists(con):
            raise Refusal("FILL_SESSION_ACTIVE", live_owner(con), ("wait_or_stop",))
    except ImportError:
        pass


def bump_revision(con) -> int:
    """Increment planner_revision inside the caller's open transaction.

    Finding 44: never swallow failures. A failed revision update must propagate so
    graph_write rolls back the graph mutation. Partial fixtures must seed planner_state.
    """
    # Drive-mutation and other in-TX bumpers still respect live exclusion when called alone.
    try:
        from modelark.execution_session import live_session_exists, live_owner
        # Only refuse when not already inside an authorized session_write for *this* connection.
        if not _session_write_authorized(con) and live_session_exists(con):
            raise Refusal("FILL_SESSION_ACTIVE", live_owner(con), ("wait_or_stop",))
    except ImportError:
        pass
    con.execute(
        "UPDATE planner_state SET planner_revision = planner_revision + 1, "
        "updated_at = CURRENT_TIMESTAMP WHERE singleton_id = 1")
    row = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()
    if row is None:
        raise RuntimeError(
            "planner_state singleton missing after revision bump "
            "(catalog must be v5 with seeded planner_state)")
    return int(row[0])


def graph_write(con, op: Callable[[Any], Any]) -> Any:
    """Run ``op(con)`` under BEGIN IMMEDIATE; bump revision unless proven_noop.

    Rolls back graph mutation and revision together on any failure (A3 atomicity).
    PR-09: refuse while a live execution session exists (FILL_SESSION_ACTIVE),
    unless the caller is inside ``session_write`` on this same connection.
    """
    # Live-session exclusion before opening the write transaction (B3 / B13).
    _require_fill_idle(con)
    con.execute("BEGIN IMMEDIATE")
    try:
        # Re-check inside TX for races (still allow authorized session_write on this con).
        _require_fill_idle(con)
        result = op(con)
        if result is None:
            result = GraphResult(proven_noop=False)
        if not getattr(result, "proven_noop", False):
            bump_revision(con)
        con.execute("COMMIT")
        return result
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


# ---------------------------------------------------------------------------
# Catalog fact helpers (draft / approve)
# ---------------------------------------------------------------------------
def _selection_hash(con) -> str:
    rows = con.execute(
        "SELECT repo_id, finalized_at FROM selection ORDER BY repo_id").fetchall()
    payload = json.dumps(
        [[r[0], r[1]] for r in rows], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _manifest_hash(con, repo_id: str, planned=None) -> str:
    """Hash the acquisition-planned file set for a repository (DEC-056 / INC-024).

    Narrows internally through ``archive_manifest`` so every consumer — draft write,
    approve-time revalidation, and projection comparison input — measures the same
    planned set. Optional ``planned`` is a precomputed ``ManifestFile`` sequence from
    a batch inspect (efficiency only); it is never an alternate definition of the set.

    When this function must obtain its own planned set and the acquisition policy
    refuses the repository, raise typed ``APPROVED_INPUT_CHANGED`` with
    ``reason=manifest_policy`` rather than leaking raw ``ArchivePolicyError``.
    """
    if planned is None:
        try:
            planned = archive_manifest.manifest_for_repo(con, repo_id)
        except archive_manifest.ArchivePolicyError as exc:
            raise Refusal(
                "APPROVED_INPUT_CHANGED",
                {
                    "reason": "manifest_policy",
                    "repo_id": repo_id,
                    "error": str(exc),
                },
                ("preview_again",),
            ) from exc
    files = [
        (m.rfilename, m.size_bytes, m.sha256, m.format, m.quant)
        for m in planned
    ]
    payload = json.dumps(
        [list(r) for r in files], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _successor_args(mutation: tuple) -> tuple[str, str, str]:
    args = tuple(mutation[1]) if len(mutation) > 1 else ()
    if mutation[0] != "successor_replan" or len(args) != 3:
        raise Refusal(
            "SUCCESSOR_ARGUMENTS_INVALID",
            {"mutation": mutation},
            ("choose_predecessor_and_successor",),
        )
    predecessor, successor, baseline_id = (str(value).strip() for value in args)
    if not predecessor or not successor or not baseline_id or predecessor == successor:
        raise Refusal(
            "SUCCESSOR_ARGUMENTS_INVALID",
            {
                "predecessor_drive": predecessor,
                "successor_drive": successor,
                "baseline_proposal_id": baseline_id,
            },
            ("choose_distinct_drives",),
        )
    return predecessor, successor, baseline_id


def _canonical_requirement_id(con, task: Mapping) -> str:
    """Restore canonical requirement vocabulary from the stable proposal wire names."""
    requirement_id = str(task.get("requirement_id") or "")
    repo_id = str(task.get("repo_id") or requirement_id.partition(":")[2])
    if requirement_id.startswith("replica:"):
        return f"protected_replica:{repo_id}"
    if requirement_id.startswith("primary:"):
        row = con.execute(
            "SELECT numcopies FROM models WHERE repo_id=?", [repo_id]
        ).fetchone()
        if row is not None and int(row[0] or 1) >= 2:
            return f"protected_home:{repo_id}"
    return requirement_id


def successor_preference(
    con,
    plan_id: str,
    mutation: tuple,
    *,
    require_active_baseline: bool = True,
    validate_current_drives: bool = True,
) -> placement.SuccessorPreference:
    """Validate and materialize one proposal-scoped successor planning lane."""
    predecessor, successor, baseline_id = _successor_args(mutation)
    state = con.execute(
        "SELECT active_approved_proposal_id FROM planner_state WHERE singleton_id=1"
    ).fetchone()
    if require_active_baseline and (state is None or state[0] != baseline_id):
        raise Refusal(
            "SUCCESSOR_BASELINE_CHANGED",
            {
                "expected": baseline_id,
                "current": None if state is None else state[0],
            },
            ("review_current_placement",),
        )
    try:
        baseline = load_proposal(con, baseline_id)
    except KeyError as exc:
        raise Refusal(
            "SUCCESSOR_BASELINE_MISSING",
            {"baseline_proposal_id": baseline_id},
            ("review_current_placement",),
        ) from exc
    if baseline.get("plan_id") != plan_id:
        raise Refusal(
            "SUCCESSOR_BASELINE_PLAN_MISMATCH",
            {"baseline_plan": baseline.get("plan_id"), "requested_plan": plan_id},
            ("select_matching_plan",),
        )
    if require_active_baseline and baseline.get("lifecycle") != "approved":
        raise Refusal(
            "SUCCESSOR_BASELINE_NOT_APPROVED",
            {"lifecycle": baseline.get("lifecycle")},
            ("review_current_placement",),
        )
    if require_active_baseline:
        baseline_status = review_input_status(con, baseline)
        if not baseline_status.get("current"):
            raise Refusal(
                "SUCCESSOR_BASELINE_STALE",
                baseline_status,
                ("review_current_placement",),
            )

    rows = con.execute(
        "SELECT d.drive_label,coalesce(d.role,'primary'),coalesce(d.raid_backed,0),"
        "coalesce(d.capacity_bytes,0),"
        "coalesce(d.filesystem_capacity_bytes,d.capacity_bytes,0),"
        "d.lifecycle,d.eligibility,d.identity_epoch,d.identity_fingerprint "
        "FROM plan_drives pd JOIN drives d USING(drive_label) "
        "WHERE pd.plan_id=? AND d.drive_label IN (?,?) ORDER BY d.drive_label",
        [plan_id, predecessor, successor],
    ).fetchall()
    facts = {str(row[0]): row for row in rows}
    missing = sorted({predecessor, successor} - set(facts))
    if missing:
        raise Refusal(
            "SUCCESSOR_DRIVE_NOT_IN_PLAN",
            {"plan_id": plan_id, "drives": missing},
            ("add_drive_to_plan",),
        )
    old = facts[predecessor]
    new = facts[successor]
    if validate_current_drives and old[5] == "active" and old[6] == "enabled":
        raise Refusal(
            "SUCCESSOR_PREDECESSOR_STILL_PLACEABLE",
            {"drive_label": predecessor, "lifecycle": old[5], "eligibility": old[6]},
            ("choose_unavailable_predecessor",),
        )
    if validate_current_drives and (new[5] != "active" or new[6] != "enabled"):
        raise Refusal(
            "SUCCESSOR_NOT_PLACEABLE",
            {"drive_label": successor, "lifecycle": new[5], "eligibility": new[6]},
            ("reconcile_successor_drive",),
        )
    if (validate_current_drives
            and (new[8] in (None, "") or int(new[7] or 0) < 1)):
        raise Refusal(
            "SUCCESSOR_IDENTITY_UNPROVEN",
            {"drive_label": successor},
            ("reconcile_successor_drive",),
        )
    if validate_current_drives and old[1] != new[1]:
        raise Refusal(
            "SUCCESSOR_ROLE_MISMATCH",
            {"predecessor_role": old[1], "successor_role": new[1]},
            ("choose_same_role_successor",),
        )

    predecessor_capacity = min(int(old[3] or 0), int(old[4] or old[3] or 0))
    lane_bytes = max(
        0,
        predecessor_capacity - capacity.safety_floor(predecessor_capacity, bool(old[2])),
    )
    if lane_bytes <= 0:
        raise Refusal(
            "SUCCESSOR_PREDECESSOR_CAPACITY_UNKNOWN",
            {"drive_label": predecessor},
            ("repair_drive_capacity",),
        )
    baseline_targets = tuple(sorted(
        (
            _canonical_requirement_id(con, task),
            str(task.get("target_drive") or task.get("satisfying_drive") or ""),
        )
        for task in baseline.get("tasks") or ()
        if task.get("row_kind") == "executable"
        and (task.get("target_drive") or task.get("satisfying_drive"))
    ))
    return placement.SuccessorPreference(
        predecessor_drive=predecessor,
        successor_drive=successor,
        lane_bytes=lane_bytes,
        baseline_targets=baseline_targets,
    )


def _semantic_input_hash(con, plan_id: str, mutation: tuple) -> str:
    sel = con.execute(
        "SELECT repo_id, finalized_at FROM selection ORDER BY repo_id").fetchall()
    models = con.execute(
        "SELECT repo_id, numcopies FROM models ORDER BY repo_id").fetchall()
    drives = con.execute(
        "SELECT d.drive_label, d.identity_epoch, d.identity_fingerprint, d.lifecycle, "
        "d.eligibility FROM plan_drives pd JOIN drives d USING(drive_label) "
        "WHERE pd.plan_id=? ORDER BY d.drive_label", [plan_id]).fetchall()
    payload = {
        "selection": [list(r) for r in sel],
        "models": [list(r) for r in models],
        "drives": [list(r) for r in drives],
        "mutation": [mutation[0], list(mutation[1]) if len(mutation) > 1 else []],
    }
    if mutation[0] == "successor_replan":
        preference = successor_preference(
            con,
            plan_id,
            mutation,
            require_active_baseline=False,
            validate_current_drives=False,
        )
        payload["successor_preference"] = {
            "predecessor_drive": preference.predecessor_drive,
            "successor_drive": preference.successor_drive,
            "lane_bytes": preference.lane_bytes,
            "baseline_targets": [list(item) for item in preference.baseline_targets],
        }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _execution_versions(mutation: tuple) -> tuple[str, str]:
    if mutation[0] == "successor_replan":
        return "successor_v1", "successor_lane_v1"
    return "1", "1"


def _current_execution_config_hash(
    con,
    plan_id: str,
    mutation: tuple = ("adopt_current", ()),
) -> str:
    """Hash the same graph-affecting execution config used by pure preview."""
    from modelark import wishlist as _wl
    from modelark.execution_config import hash_config

    selected_plan = plan_mod.get(con, plan_id)
    capacity_mode = selected_plan["capacity_mode"] if selected_plan else "guaranteed"
    try:
        compression = dict(_wl.compression() or {})
    except Exception:
        compression = {"enabled": True, "codec": "streamznn", "level": 3}
    policy_version, solver_version = _execution_versions(mutation)
    return hash_config({
        "capacity_mode": capacity_mode,
        "policy_version": policy_version,
        "solver_version": solver_version,
        "compression": compression,
        "numcopies_default": 1,
    })


def _requirement_set_hash(tasks: Sequence[Mapping]) -> str:
    ids = sorted(t["requirement_id"] for t in tasks)
    return hashlib.sha256(
        json.dumps(ids, separators=(",", ":")).encode()).hexdigest()


def _selected_repos(con, mutation: tuple) -> list[str]:
    kind = mutation[0]
    args = mutation[1] if len(mutation) > 1 else ()
    if kind == "finalize":
        # Hypothetical: current cart + named repos become finalized.
        current = [r[0] for r in con.execute(
            "SELECT repo_id FROM selection ORDER BY repo_id").fetchall()]
        extra = list(args) if args else []
        return sorted(set(current) | set(extra))
    # adopt_current and default: finalized selection only.
    return [r[0] for r in con.execute(
        "SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL "
        "ORDER BY repo_id").fetchall()]


def _append_missing_files(
        con, files: list, requirement_id: str, repo_id: str, planned=None) -> None:
    """Append proposal_files rows for the acquisition-planned set (INC-024).

    ``storage_action`` comes from ``ManifestFile``, not a hard-coded compress default.
    """
    if planned is None:
        planned = archive_manifest.manifest_for_repo(con, repo_id)
    for m in planned:
        files.append({
            "requirement_id": requirement_id,
            "rfilename": m.rfilename,
            "role": "missing",
            "size_bytes": m.size_bytes,
            "orig_sha256": m.sha256,
            "format": m.format,
            "quant": m.quant,
            "storage_action": m.storage_action,
        })


def _baseline_file_evidence(con, repo_id: str, drive_label: str) -> list[dict]:
    """Per-file durable evidence for A10 baseline certificates (archived content identity).

    Intentionally archived-unfiltered (INC-024 Q6 / c14): certificate payload records
    what is on the drive, not the acquisition policy's planned subset.
    """
    rows = con.execute(
        "SELECT a.rfilename, a.orig_sha256, a.orig_bytes, a.annex_key, a.stored_bytes "
        "FROM archived a WHERE a.repo_id=? AND a.drive_label=? ORDER BY a.rfilename",
        [repo_id, drive_label]).fetchall()
    return [
        {
            "rfilename": r[0],
            "orig_sha256": r[1],
            "orig_bytes": r[2],
            "annex_key": r[3],
            "stored_bytes": r[4],
        }
        for r in rows
    ]


def _repo_workspace_peak(con, repo_id: str) -> int:
    """Peak transient workspace for one repo placement, aligned with execution budgets.

    Only safetensors (compress path) need codec workspace. Raw formats charge durable only.
    Workspace = max codec output cap over safetensors files (same seam as ``budgets.file_budget``),
    not full raw size (which over-rejects feasible placements).
    """
    from modelark import compress, streamznn, wishlist

    cfg = dict(wishlist.compression())
    peak = 0
    for size, in con.execute(
            "SELECT size_bytes FROM files WHERE repo_id=? AND format='safetensors' "
            "AND size_bytes IS NOT NULL AND size_bytes > 0",
            [repo_id]):
        size = int(size)
        codec = compress.plan_codec(size, cfg)
        if codec == compress.CODEC_RAW:
            continue
        cap = compress.codec_output_cap(
            size, codec, stream_chunk_bytes=streamznn.DEFAULT_CHUNK)
        if cap > peak:
            peak = int(cap)
    return peak


def _admissible_map_from_evidence(evidence_by_drive: Mapping[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for label, ev in (evidence_by_drive or {}).items():
        if ev is None:
            continue
        if hasattr(ev, "executable") and not ev.executable:
            out[label] = 0
            continue
        free = getattr(ev, "admissible_free", None)
        out[label] = int(free) if free is not None else 0
    return out


def joint_capacity_shortfall(
    tasks: Sequence[Mapping],
    remaining_by_drive: Mapping[str, int],
) -> dict | None:
    """Joint assignment check in stable order.

    For each executable task against current remaining free:
      required_peak = guaranteed_durable + workspace_peak
      if required_peak > remaining → shortfall
      remaining -= guaranteed_durable   # workspace is transient; only durable depletes

    Baseline-satisfied tasks do not charge. Returns None if the whole assignment fits.
    """
    remaining = {k: int(v) for k, v in remaining_by_drive.items()}
    ordered = sorted(
        (t for t in tasks if (t.get("row_kind") or "") == "executable"),
        key=lambda t: (int(t.get("order_key") or 0), t.get("requirement_id") or ""),
    )
    for t in ordered:
        label = t.get("target_drive")
        if not label:
            return {
                "reason": "missing_target",
                "requirement_id": t.get("requirement_id"),
            }
        durable = int(t.get("guaranteed_durable") or 0)
        workspace = int(t.get("workspace_peak") or 0)
        peak = durable + workspace
        have = remaining.get(label)
        if have is None:
            return {
                "reason": "unknown_drive",
                "drive": label,
                "requirement_id": t.get("requirement_id"),
                "need": peak,
            }
        if peak > have:
            return {
                "reason": "capacity_overcommit",
                "drive": label,
                "requirement_id": t.get("requirement_id"),
                "need": peak,
                "durable": durable,
                "workspace_peak": workspace,
                "remaining": have,
            }
        remaining[label] = have - durable
    return None


def _build_assignment(
        con, plan_id: str, mutation: tuple
) -> tuple[list[dict], list[dict], str, dict | None, str]:
    """Adapt the canonical planning result into immutable proposal rows (DEC-067).

    This function performs no placement, capacity calculation, satisfaction decision, or source
    selection.  It only serializes the exact canonical assignment and durable evidence into the
    proposal's stable wire/storage shape.
    """
    repos = _selected_repos(con, mutation)
    plan_row = plan_mod.get(con, plan_id)
    capacity_mode = plan_row["capacity_mode"] if plan_row else "guaranteed"
    preference = None
    if mutation[0] == "successor_replan":
        preference = successor_preference(con, plan_id, mutation)
    result = planning.preview(
        con,
        plan_id,
        repos,
        capacity_mode=capacity_mode,
        preference=preference,
    )
    graph = result.graph
    ledger = result.capacity

    def proposal_requirement_id(requirement_id: str) -> str:
        # Preserve the v5 proposal/executor wire vocabulary while adapting canonical requirement
        # identities.  This is a name mapping only; it does not alter the requirement or assignment.
        if requirement_id.startswith("protected_home:"):
            return "primary:" + requirement_id.split(":", 1)[1]
        if requirement_id.startswith("protected_replica:"):
            return "replica:" + requirement_id.split(":", 1)[1]
        return requirement_id

    labels = sorted({
        label for _rid, label in graph.matches
    } | {
        task.target_drive for task in ledger.tasks
    } | {
        task.source_drive for task in ledger.tasks if task.source_drive
    })
    drive_identity = {}
    if labels:
        placeholders = ",".join("?" for _ in labels)
        drive_identity = {
            row[0]: (int(row[1]), row[2] or "")
            for row in con.execute(
                "SELECT drive_label,identity_epoch,identity_fingerprint FROM drives "
                f"WHERE drive_label IN ({placeholders})",
                labels,
            ).fetchall()
        }

    tasks: list[dict] = []
    files: list[dict] = []
    order = 0
    task_target = {task.requirement_id: task.target_drive for task in ledger.tasks}

    # Proven complete copies are reviewable certificate rows, not executable work.
    for canonical_rid, label in sorted(graph.matches):
        order += 1
        repo = canonical_rid.split(":", 1)[1]
        rid = proposal_requirement_id(canonical_rid)
        planned = graph.manifests[repo]
        manifest_hash = _manifest_hash(con, repo, planned=planned)
        epoch, fingerprint = drive_identity.get(label, (0, ""))
        cert = canonical.baseline_satisfaction_certificate(
            requirement_id=rid,
            full_manifest_hash=manifest_hash,
            drive_label=label,
            identity_epoch=epoch,
            identity_fingerprint=fingerprint,
            files=_baseline_file_evidence(con, repo, label),
        )
        durable = sum(int(item.size_bytes or 0) for item in planned)
        tasks.append({
            "requirement_id": rid,
            "row_kind": "baseline_satisfied",
            "repo_id": repo,
            "target_drive": label,
            "source_drive": None,
            "satisfying_drive": label,
            "full_manifest_hash": manifest_hash,
            "order_key": order,
            "guaranteed_durable": durable,
            "expected_durable": durable,
            "identity_epoch": epoch,
            "baseline_certificate": cert,
        })

    # Canonical execution rank is stable and keeps fetch/home work before dependent replicas.
    assigned = sorted(ledger.tasks, key=lambda item: capacity.execution_rank(item, graph))
    for task in assigned:
        order += 1
        rid = proposal_requirement_id(task.requirement_id)
        planned = graph.manifests[task.repo_id]
        epoch, _fingerprint = drive_identity.get(task.target_drive, (0, ""))
        source = task.source_drive
        if source is None and task.depends_on_requirement:
            source = task_target.get(task.depends_on_requirement)
        tasks.append({
            "requirement_id": rid,
            "row_kind": "executable",
            "repo_id": task.repo_id,
            "target_drive": task.target_drive,
            "source_drive": source,
            "satisfying_drive": None,
            "full_manifest_hash": _manifest_hash(con, task.repo_id, planned=planned),
            "order_key": order,
            "guaranteed_durable": task.budget.guaranteed_durable,
            "expected_durable": task.budget.expected_durable,
            "identity_epoch": epoch,
            "baseline_certificate": None,
        })
        missing = set(task.budget.missing_files)
        _append_missing_files(
            con,
            files,
            rid,
            task.repo_id,
            planned=tuple(item for item in planned if item.rfilename in missing),
        )

    policy_errors = {
        str(dict(item.detail).get("repo_id")): str(dict(item.detail).get("message"))
        for item in graph.diagnostics
        if item.code == "MANIFEST_POLICY" and dict(item.detail).get("repo_id")
    }
    policy_gate = None
    if policy_errors:
        policy_gate = {
            "code": "MANIFEST_POLICY",
            "gate": "B",
            "evidence": {
                "blocked_repositories": [
                    {"repo_id": repo, "reason": policy_errors[repo]}
                    for repo in sorted(policy_errors)
                ]
            },
            "actions": list(_MANIFEST_POLICY_ACTIONS),
        }

    gate = result.proposal_gate_code
    derivation_mode = ledger.derivation_mode or _derivation_mode_for_gate(gate)
    return tasks, files, gate, policy_gate, derivation_mode


_DERIVATION_MODES = frozenset({"optimized", "state_truncated", "canonical_fallback"})


def _derivation_mode_for_gate(gate_b_code: str) -> str:
    """Default placement derivation from gate; state_truncated is assignment-provided."""
    return "optimized" if gate_b_code == "FEASIBLE" else "canonical_fallback"


def _header_from_facts(
    *,
    plan_id: str,
    based_on: int,
    mutation: tuple,
    tasks: Sequence[Mapping],
    capacity_mode: str,
    gate_b_code: str,
    selection_before: str,
    selection_after: str,
    semantic: str,
    derivation_mode: str | None = None,
) -> dict:
    mode = derivation_mode or _derivation_mode_for_gate(gate_b_code)
    policy_version, solver_version = _execution_versions(mutation)
    return {
        "plan_id": plan_id,
        "based_on_revision": based_on,
        "mutation_kind": mutation[0],
        "mutation_args": tuple(mutation[1]) if len(mutation) > 1 else (),
        "requirement_set_hash": _requirement_set_hash(tasks),
        "semantic_input_hash": semantic,
        "selection_before_hash": selection_before,
        "selection_after_hash": selection_after,
        "capacity_mode": capacity_mode,
        "policy_version": policy_version,
        "solver_version": solver_version,
        "serializer_version": canonical.SERIALIZER_VERSION,
        "gate_b_code": gate_b_code,
        # Placement audit evidence only (optimized | state_truncated | canonical_fallback).
        # Never store config hashes here (finding 35). Carried from assignment fifth result.
        "derivation_mode": mode,
    }


# ---------------------------------------------------------------------------
# Preview / draft
# ---------------------------------------------------------------------------
def preview_pure(con, plan_id: str = "ark", mutation: tuple = ("adopt_current", ())) -> dict:
    """Pure planning outside BEGIN IMMEDIATE: build payload without writing."""
    if not isinstance(mutation, tuple):
        mutation = tuple(mutation)
    rev = int(con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])
    p = plan_mod.get(con, plan_id)
    capacity_mode = p["capacity_mode"] if p else "guaranteed"
    sel_before = _selection_hash(con)
    # Hypothetical selection-after for finalize: treat as post-finalize identity.
    if mutation[0] == "finalize":
        # Deterministic hash of the post-mutation set without writing.
        repos = _selected_repos(con, mutation)
        payload = json.dumps(
            [[r, "pending"] for r in repos], sort_keys=True, separators=(",", ":"))
        sel_after = hashlib.sha256(payload.encode()).hexdigest()
    else:
        sel_after = sel_before
    assignment = _build_assignment(con, plan_id, mutation)
    # Support fifth derivation_mode (production) and legacy 4-tuple patches.
    if isinstance(assignment, dict):
        tasks = assignment["tasks"]
        files = assignment["files"]
        gate = assignment.get("gate_b_code") or assignment.get("gate") or "FEASIBLE"
        policy_gate = assignment.get("policy_gate")
        derivation_mode = assignment.get("derivation_mode") or _derivation_mode_for_gate(gate)
    else:
        tasks, files = assignment[0], assignment[1]
        gate = assignment[2] if len(assignment) > 2 else "FEASIBLE"
        policy_gate = assignment[3] if len(assignment) > 3 else None
        if len(assignment) > 4 and assignment[4] is not None:
            derivation_mode = assignment[4]
        else:
            derivation_mode = _derivation_mode_for_gate(gate)
    if derivation_mode not in _DERIVATION_MODES:
        raise Refusal(
            "INVALID_DERIVATION_MODE",
            {"derivation_mode": derivation_mode},
            ("fix_assignment",),
        )
    tasks_n = _normalize_tasks_for_hash(tasks)
    files_n = _normalize_files_for_hash(files)
    semantic = _semantic_input_hash(con, plan_id, mutation)
    # Bind complete graph-affecting config at draft time (B7 / finding 25/35).
    cfg_hash = _current_execution_config_hash(con, plan_id, mutation)
    header = _header_from_facts(
        plan_id=plan_id, based_on=rev, mutation=mutation, tasks=tasks_n,
        capacity_mode=capacity_mode, gate_b_code=gate,
        selection_before=sel_before, selection_after=sel_after, semantic=semantic,
        derivation_mode=derivation_mode,
    )
    # Authoritative config binding is its own header field (included in proposal_hash).
    # derivation_mode remains placement audit evidence only.
    header["execution_config_hash"] = cfg_hash
    digest = canonical.proposal_hash(header, tasks_n, files_n)
    payload = {
        "header": header,
        "tasks": tasks_n,
        "files": files_n,
        "canonical_hash": digest,
        "mutation": mutation,
    }
    # Single-container MANIFEST_POLICY observability (INC-024 Q2). Prefer named
    # gate_b_refusal so Gate-1 contracts select one block without cross-container mix.
    if policy_gate is not None:
        payload["gate_b_refusal"] = policy_gate
    return payload


compute_draft_payload = preview_pure


def require_execution_config_hash_column(con) -> None:
    """Refuse if the versioned v6 column is absent (no opportunistic ALTER)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(placement_proposals)").fetchall()}
    if "execution_config_hash" not in cols:
        raise RuntimeError(
            "placement_proposals.execution_config_hash missing — catalog requires "
            "schema v6 migration (open once with a writable ModelArk connect)"
        )


# Backward-compatible name used by older call sites / tests.
ensure_execution_config_hash_column = require_execution_config_hash_column


def current_draft_ids(con, *, plan_id: str | None = None) -> tuple[str, ...]:
    """Return immutable drafts based on the catalog's exact current planner revision."""
    row = con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1"
    ).fetchone()
    if row is None:
        raise RuntimeError("planner_state singleton is missing")
    sql = (
        "SELECT proposal_id FROM placement_proposals "
        "WHERE lifecycle='draft' AND based_on_revision=?"
    )
    params: list[Any] = [int(row[0])]
    if plan_id is not None:
        sql += " AND plan_id=?"
        params.append(str(plan_id))
    sql += " ORDER BY created_at,proposal_id"
    return tuple(str(item[0]) for item in con.execute(sql, params).fetchall())


def require_unambiguous_current_draft(con) -> tuple[str, ...]:
    """Return current drafts or refuse when unresolved intent is not singular."""
    pending = current_draft_ids(con)
    if len(pending) > 1:
        raise Refusal(
            "MULTIPLE_CURRENT_DRAFTS",
            {"proposal_ids": list(pending)},
            ("inspect_pending_proposals",),
        )
    return pending


def publish_draft(con, payload: dict | None = None, *, plan_id: str | None = None,
                  **_kw) -> dict:
    """Persist an immutable draft under BEGIN IMMEDIATE; no selection/revision mutation."""
    if payload is None:
        raise TypeError("payload required")
    # Allow publish(con, plan_id=..., payload=...) from TypeError fallbacks.
    if plan_id is not None and "header" not in payload:
        raise TypeError("payload must be the pure preview result")

    def _persist():
        require_execution_config_hash_column(con)
        header = dict(payload["header"])
        tasks = list(payload["tasks"])
        files = list(payload["files"])
        digest = payload["canonical_hash"]
        # DEC-060: every newly published proposal must have a named non-null derivation mode.
        mode = header.get("derivation_mode")
        if mode is None or mode == "":
            raise Refusal(
                "INVALID_DERIVATION_MODE",
                {"derivation_mode": mode, "detail": "missing or null derivation_mode"},
                ("set_named_derivation_mode",),
            )
        if mode not in _DERIVATION_MODES:
            raise Refusal(
                "INVALID_DERIVATION_MODE",
                {"derivation_mode": mode, "detail": "invalid derivation_mode"},
                ("set_named_derivation_mode",),
            )
        # Recompute authority hash; never trust client-supplied overrides on the payload object
        # beyond the pure-preview shape (client kwargs are ignored at create_draft).
        recomputed = canonical.proposal_hash(header, tasks, files)
        if recomputed != digest:
            digest = recomputed
        # CAS: revision still matches based_on.
        cur = int(con.execute(
            "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])
        if cur != int(header["based_on_revision"]):
            raise Refusal("PREVIEW_STALE", {"current": cur, "based_on": header["based_on_revision"]},
                          ("preview_again",))
        pending = require_unambiguous_current_draft(con)
        if pending:
            raise Refusal(
                "PROPOSAL_REVIEW_PENDING",
                {"proposal_ids": list(pending), "planner_revision": cur},
                ("review_or_discard_pending",),
            )
        proposal_id = str(uuid.uuid4())
        mut_args = header.get("mutation_args") or ()
        cols = {r[1] for r in con.execute("PRAGMA table_info(placement_proposals)").fetchall()}
        has_cfg = "execution_config_hash" in cols
        if has_cfg:
            con.execute(
                "INSERT INTO placement_proposals("
                "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
                "mutation_kind,mutation_args_json,serializer_version,"
                "requirement_set_hash,semantic_input_hash,"
                "selection_before_hash,selection_after_hash,"
                "capacity_mode,policy_version,solver_version,gate_b_code,derivation_mode,"
                "execution_config_hash"
                ") VALUES(?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    proposal_id, header["plan_id"], int(header["based_on_revision"]), digest,
                    header["mutation_kind"],
                    json.dumps(list(mut_args), separators=(",", ":")),
                    header["serializer_version"],
                    header.get("requirement_set_hash"),
                    header.get("semantic_input_hash"),
                    header.get("selection_before_hash"),
                    header.get("selection_after_hash"),
                    header.get("capacity_mode") or "guaranteed",
                    header.get("policy_version") or "1",
                    header.get("solver_version") or "1",
                    header.get("gate_b_code") or "FEASIBLE",
                    mode,
                    header.get("execution_config_hash"),
                ],
            )
        else:
            con.execute(
                "INSERT INTO placement_proposals("
                "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
                "mutation_kind,mutation_args_json,serializer_version,"
                "requirement_set_hash,semantic_input_hash,"
                "selection_before_hash,selection_after_hash,"
                "capacity_mode,policy_version,solver_version,gate_b_code,derivation_mode"
                ") VALUES(?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    proposal_id, header["plan_id"], int(header["based_on_revision"]), digest,
                    header["mutation_kind"],
                    json.dumps(list(mut_args), separators=(",", ":")),
                    header["serializer_version"],
                    header.get("requirement_set_hash"),
                    header.get("semantic_input_hash"),
                    header.get("selection_before_hash"),
                    header.get("selection_after_hash"),
                    header.get("capacity_mode") or "guaranteed",
                    header.get("policy_version") or "1",
                    header.get("solver_version") or "1",
                    header.get("gate_b_code") or "FEASIBLE",
                    mode,
                ],
            )
        for t in tasks:
            con.execute(
                "INSERT INTO proposal_tasks("
                "proposal_id,requirement_id,row_kind,repo_id,target_drive,source_drive,"
                "satisfying_drive,full_manifest_hash,order_key,guaranteed_durable,"
                "expected_durable,identity_epoch,baseline_certificate"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    proposal_id, t["requirement_id"], t["row_kind"], t["repo_id"],
                    t.get("target_drive"), t.get("source_drive"), t.get("satisfying_drive"),
                    t["full_manifest_hash"], int(t.get("order_key") or 0),
                    t.get("guaranteed_durable"), t.get("expected_durable"),
                    t.get("identity_epoch"), t.get("baseline_certificate"),
                ],
            )
        for f in files:
            con.execute(
                "INSERT INTO proposal_files("
                "proposal_id,requirement_id,rfilename,role,size_bytes,orig_sha256,"
                "format,quant,storage_action) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    proposal_id, f["requirement_id"], f["rfilename"],
                    f.get("role") or "missing", f.get("size_bytes"), f.get("orig_sha256"),
                    f.get("format"), f.get("quant"), f.get("storage_action"),
                ],
            )
        return {
            "proposal_id": proposal_id,
            "canonical_hash": digest,
            "lifecycle": "draft",
            "plan_id": header["plan_id"],
        }

    _require_fill_idle(con)
    con.execute("BEGIN IMMEDIATE")
    try:
        _require_fill_idle(con)
        out = _persist()
        con.execute("COMMIT")
        return out
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


persist_draft = publish_draft


def discard_draft(con, proposal_id: str) -> dict:
    """Supersede one exact unapproved draft without changing planner revision."""
    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT lifecycle,based_on_revision FROM placement_proposals WHERE proposal_id=?",
            [proposal_id],
        ).fetchone()
        if row is None:
            raise Refusal(
                "PROPOSAL_NOT_FOUND",
                {"proposal_id": proposal_id},
                ("review_again",),
            )
        if row[0] != "draft":
            raise Refusal(
                "PROPOSAL_NOT_DRAFT",
                {"proposal_id": proposal_id, "lifecycle": row[0]},
                ("review_again",),
            )
        con.execute(
            "UPDATE placement_proposals SET lifecycle='superseded', "
            "superseded_at=CURRENT_TIMESTAMP WHERE proposal_id=? AND lifecycle='draft'",
            [proposal_id],
        )
        con.execute("COMMIT")
        return {
            "proposal_id": proposal_id,
            "lifecycle": "superseded",
            "based_on_revision": int(row[1]),
        }
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


def create_draft(con, plan_id: str = "ark", mutation: tuple = ("adopt_current", ()),
                 **kwargs) -> dict:
    """Preview pure then publish. Client-supplied hash/blob kwargs are not authority."""
    # Explicitly ignore client authority kwargs (finding 33).
    kwargs.pop("canonical_hash", None)
    kwargs.pop("serialized_proposal", None)
    kwargs.pop("blob", None)
    if not isinstance(mutation, tuple):
        mutation = tuple(mutation) if mutation is not None else ("adopt_current", ())
    # Positional form create(con, "ark", ("adopt_current", ()))
    payload = preview_pure(con, plan_id=plan_id, mutation=mutation)
    return publish_draft(con, payload)


preview_and_draft = create_draft


def load_proposal(con, proposal_id: str) -> dict:
    table_cols = {r[1] for r in con.execute("PRAGMA table_info(placement_proposals)").fetchall()}
    has_cfg = "execution_config_hash" in table_cols
    select_cols = (
        "proposal_id, plan_id, based_on_revision, lifecycle, canonical_hash, "
        "mutation_kind, mutation_args_json, serializer_version, requirement_set_hash, "
        "semantic_input_hash, selection_before_hash, selection_after_hash, "
        "capacity_mode, policy_version, solver_version, gate_b_code, derivation_mode, "
        + ("execution_config_hash, " if has_cfg else "")
        + "created_at, approved_at, superseded_at"
    )
    row = con.execute(
        f"SELECT {select_cols} FROM placement_proposals WHERE proposal_id=?",
        [proposal_id]).fetchone()
    if row is None:
        raise KeyError(proposal_id)
    cols = [
        "proposal_id", "plan_id", "based_on_revision", "lifecycle", "canonical_hash",
        "mutation_kind", "mutation_args_json", "serializer_version", "requirement_set_hash",
        "semantic_input_hash", "selection_before_hash", "selection_after_hash",
        "capacity_mode", "policy_version", "solver_version", "gate_b_code", "derivation_mode",
    ]
    if has_cfg:
        cols.append("execution_config_hash")
    cols.extend(["created_at", "approved_at", "superseded_at"])
    d = dict(zip(cols, row))
    if "execution_config_hash" not in d:
        d["execution_config_hash"] = None
    d["mutation_args"] = tuple(json.loads(d.pop("mutation_args_json") or "[]"))
    d["tasks"] = [
        dict(zip(
            ["requirement_id", "row_kind", "repo_id", "target_drive", "source_drive",
             "satisfying_drive", "full_manifest_hash", "order_key", "guaranteed_durable",
             "expected_durable", "identity_epoch", "baseline_certificate"],
            r))
        for r in con.execute(
            "SELECT requirement_id,row_kind,repo_id,target_drive,source_drive,"
            "satisfying_drive,full_manifest_hash,order_key,guaranteed_durable,"
            "expected_durable,identity_epoch,baseline_certificate FROM proposal_tasks "
            "WHERE proposal_id=? ORDER BY order_key, requirement_id", [proposal_id])
    ]
    d["files"] = [
        dict(zip(
            ["requirement_id", "rfilename", "role", "size_bytes", "orig_sha256",
             "format", "quant", "storage_action"],
            r))
        for r in con.execute(
            "SELECT requirement_id,rfilename,role,size_bytes,orig_sha256,format,quant,"
            "storage_action FROM proposal_files WHERE proposal_id=? "
            "ORDER BY requirement_id, rfilename", [proposal_id])
    ]
    return d


def review_input_status(con, stored: Mapping) -> dict:
    """Read-only operator status for the inputs bound by a stored proposal.

    This is deliberately not execution admission. It tells the portal whether selection/plan/
    identity/config inputs still match the reviewed artifact; approval and Fill start still perform
    their fenced capacity and exact-assignment revalidation.
    """
    mutation = (
        stored.get("mutation_kind") or "adopt_current",
        tuple(stored.get("mutation_args") or ()),
    )
    try:
        semantic_matches = (
            _semantic_input_hash(con, stored["plan_id"], mutation)
            == stored.get("semantic_input_hash")
        )
    except Refusal:
        # Historical successor facts may no longer be valid planning inputs. That is
        # stale authority, not a status-endpoint failure.
        semantic_matches = False
    execution_config_matches = (
        _current_execution_config_hash(con, stored["plan_id"], mutation)
        == stored.get("execution_config_hash")
    )
    return {
        "current": semantic_matches and execution_config_matches,
        "semantic_input_matches": semantic_matches,
        "execution_config_matches": execution_config_matches,
    }


get_proposal = load_proposal


_TASK_HASH_FIELDS = (
    "requirement_id", "row_kind", "repo_id", "target_drive", "source_drive",
    # satisfying_drive is assignment-significant for baseline_satisfied (fencing + exact check).
    "satisfying_drive",
    "full_manifest_hash", "order_key", "guaranteed_durable", "expected_durable",
    "identity_epoch",
    # A10: baseline certificate is part of reviewed assignment identity.
    "baseline_certificate",
)
_FILE_HASH_FIELDS = (
    "requirement_id", "rfilename", "role", "size_bytes", "orig_sha256",
    "format", "quant", "storage_action",
)


def _normalize_tasks_for_hash(tasks: Sequence[Mapping]) -> list[dict]:
    out = []
    for t in tasks:
        row = {k: t.get(k) for k in _TASK_HASH_FIELDS}
        if row.get("order_key") is not None:
            row["order_key"] = int(row["order_key"])
        if row.get("identity_epoch") is not None:
            row["identity_epoch"] = int(row["identity_epoch"])
        out.append(row)
    return out


def _normalize_files_for_hash(files: Sequence[Mapping]) -> list[dict]:
    return [{k: f.get(k) for k in _FILE_HASH_FIELDS} for f in files]


def hash_stored_proposal(con, proposal_id: str) -> str:
    p = load_proposal(con, proposal_id)
    header = {
        "plan_id": p["plan_id"],
        "based_on_revision": int(p["based_on_revision"]),
        "mutation_kind": p["mutation_kind"],
        "mutation_args": tuple(p["mutation_args"]),
        "requirement_set_hash": p["requirement_set_hash"],
        "semantic_input_hash": p["semantic_input_hash"],
        "selection_before_hash": p["selection_before_hash"],
        "selection_after_hash": p["selection_after_hash"],
        "capacity_mode": p["capacity_mode"],
        "policy_version": p["policy_version"],
        "solver_version": p["solver_version"],
        "serializer_version": p["serializer_version"],
        "gate_b_code": p["gate_b_code"],
        "derivation_mode": p["derivation_mode"],
    }
    # Include config binding when present so hash matches draft authority (finding 35).
    if p.get("execution_config_hash"):
        header["execution_config_hash"] = p["execution_config_hash"]
    return canonical.proposal_hash(
        header,
        _normalize_tasks_for_hash(p["tasks"]),
        _normalize_files_for_hash(p["files"]),
    )


recompute_hash = hash_stored_proposal


def proposal_drive_ids(proposal: Mapping) -> list[tuple[str, int]]:
    """RFC-002: (identity_fingerprint, epoch) for exact target/source drives — filled by caller
    with catalog joins. This returns drive labels; approve resolves to fence keys."""
    labels: set[str] = set()
    for t in proposal.get("tasks") or ():
        for key in ("target_drive", "source_drive", "satisfying_drive"):
            v = t.get(key) if isinstance(t, Mapping) else None
            if v:
                labels.add(v)
    return sorted(labels)


def _fence_keys(con, labels: Sequence[str]) -> list[tuple[str, int]]:
    """Resolve proposal-relevant labels to sorted (fingerprint, epoch) fence keys.

    Finding 38: refuse missing identity instead of silently omitting a drive from the fence set.
    """
    keys = []
    for label in labels:
        row = con.execute(
            "SELECT identity_fingerprint, identity_epoch FROM drives WHERE drive_label=?",
            [label]).fetchone()
        if not row or not row[0]:
            raise Refusal(
                "DRIVE_IDENTITY_UNPROVEN",
                {"drive": label, "reason": "missing_identity_fingerprint"},
                ("reconcile_drive", "preview_again"),
            )
        keys.append((row[0], int(row[1])))
    return sorted(keys)


# ---------------------------------------------------------------------------
# Exact assignment validation (no optimizer)
# ---------------------------------------------------------------------------
def validate_exact_assignment(con, proposal: Mapping,
                              evidence_by_drive: Mapping[str, Any] | None = None) -> None:
    """Re-validate the stored assignment as a joint plan against current evidence.

    Never re-optimizes. Checks:
    - A10: every task's full_manifest_hash matches current catalog content;
    - baseline certificate recomputed from durable archive evidence;
    - distinct media per repo (numcopies durability);
    - every executable has a target;
    - non-executable evidence is refused;
    - cumulative charges fit safety-adjusted admissible free (order_key order).
    """
    evidence_by_drive = evidence_by_drive or {}
    # Enrich workspace_peak from catalog when not present on stored rows (column not required).
    tasks = []
    for raw in proposal.get("tasks") or ():
        t = dict(raw)
        if t.get("row_kind") == "executable" and t.get("repo_id"):
            t["workspace_peak"] = int(
                t.get("workspace_peak")
                if t.get("workspace_peak") is not None
                else _repo_workspace_peak(con, t["repo_id"]))
        tasks.append(t)

    # Distinct target/satisfying drives per repo: numcopies must not collapse onto one medium.
    by_repo: dict[str, list[str]] = {}
    for t in tasks:
        repo = t.get("repo_id") or ""
        label = t.get("target_drive") or t.get("satisfying_drive")
        if label:
            by_repo.setdefault(repo, []).append(label)
    for repo, labels in by_repo.items():
        if len(labels) != len(set(labels)):
            raise Refusal("EXACT_ASSIGNMENT_REJECTED",
                          {"repo_id": repo, "reason": "duplicate_target_drives", "labels": labels},
                          ("preview_again",))

    for t in tasks:
        # A10: every proposal task is pinned to catalog content at draft time.
        # Recompute current full_manifest_hash for all rows (baseline + executable),
        # not only baseline_satisfied — otherwise executable content drift can approve.
        if t.get("repo_id") is not None and t.get("full_manifest_hash") is not None:
            current_mh = _manifest_hash(con, t["repo_id"])
            if current_mh != t.get("full_manifest_hash"):
                raise Refusal(
                    "APPROVED_INPUT_CHANGED",
                    {"task": t.get("requirement_id"), "reason": "full_manifest_hash",
                     "stored": t.get("full_manifest_hash"), "current": current_mh},
                    ("preview_again",))
        else:
            current_mh = None

        if t.get("row_kind") == "baseline_satisfied":
            # A10: durable archive evidence + certificate (not annex-key alone).
            label = t.get("satisfying_drive") or t.get("target_drive")
            if not label:
                raise Refusal("EXACT_ASSIGNMENT_REJECTED",
                              {"task": t.get("requirement_id"), "reason": "missing_satisfying_drive"},
                              ("preview_again",))
            fp_row = con.execute(
                "SELECT identity_fingerprint, identity_epoch FROM drives WHERE drive_label=?",
                [label]).fetchone()
            if not fp_row or not fp_row[0]:
                raise Refusal("EXACT_ASSIGNMENT_REJECTED",
                              {"task": t.get("requirement_id"), "drive": label,
                               "reason": "missing_identity_fingerprint"},
                              ("preview_again",))
            if current_mh is None:
                current_mh = _manifest_hash(con, t["repo_id"])
            cert_files = _baseline_file_evidence(con, t["repo_id"], label)
            recomputed = canonical.baseline_satisfaction_certificate(
                requirement_id=t["requirement_id"],
                full_manifest_hash=current_mh,
                drive_label=label,
                identity_epoch=int(fp_row[1]),
                identity_fingerprint=fp_row[0],
                files=cert_files,
            )
            stored_cert = t.get("baseline_certificate")
            if not stored_cert or stored_cert != recomputed:
                raise Refusal(
                    "EXACT_ASSIGNMENT_REJECTED",
                    {"task": t.get("requirement_id"), "reason": "baseline_certificate_mismatch",
                     "stored": stored_cert, "recomputed": recomputed},
                    ("preview_again",))
            continue
        if t.get("row_kind") != "executable":
            continue
        label = t.get("target_drive")
        if not label:
            raise Refusal("EXACT_ASSIGNMENT_REJECTED", {"task": t.get("requirement_id")},
                          ("preview_again",))
        # Replica / later copies must name a durable source (finding 40).
        rid = t.get("requirement_id") or ""
        if rid.startswith("replica") and not t.get("source_drive"):
            raise Refusal(
                "EXACT_ASSIGNMENT_REJECTED",
                {"task": rid, "reason": "missing_source_drive"},
                ("preview_again",))
        ev = evidence_by_drive.get(label)
        if ev is not None and hasattr(ev, "executable") and not ev.executable:
            raise Refusal("EXACT_ASSIGNMENT_REJECTED", {"drive": label, "evidence": ev},
                          ("preview_again",))

    # Joint remaining capacity across the full executable assignment (not pointwise full free).
    remaining = _admissible_map_from_evidence(evidence_by_drive)
    for t in tasks:
        if t.get("row_kind") != "executable":
            continue
        label = t.get("target_drive")
        if label and label not in remaining:
            remaining[label] = 0  # no evidence → fail closed

    short = joint_capacity_shortfall(tasks, remaining)
    if short is not None:
        raise Refusal("EXACT_ASSIGNMENT_REJECTED", short, ("preview_again",))


revalidate_assignment_evidence = validate_exact_assignment


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------
class _DefaultClock:
    def now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _DefaultServices:
    def __init__(self, observe_live=None):
        self.clock = _DefaultClock()
        # Optional: observe_live(label) -> Observation | None for true live evidence under fences.
        self.observe_live = observe_live

    def observe_exact_capacity(self, con, labels, **_k):
        """Authoritative admission evidence under held fences (A6 / finding 38).

        Never promotes ``drives.free_bytes`` to live evidence. Paths:
        - if ``observe_live`` returns an observation → ``execution_evidence`` (fenced live);
        - else offline derivation with ``fence_held=True`` → clean-anchor evidence or typed unknown.

        Missing fingerprints refuse (not silent omit).
        """
        from modelark import admission

        now = self.clock.now()
        out = {}
        for label in labels:
            facts = admission._facts(con, label)
            if not facts.fingerprint:
                raise Refusal(
                    "DRIVE_IDENTITY_UNPROVEN",
                    {"drive": label, "reason": "missing_identity_fingerprint"},
                    ("reconcile_drive", "preview_again"),
                )
            live = None
            if callable(self.observe_live):
                live = self.observe_live(label)
            if live is not None:
                out[label] = admission.execution_evidence(con, label, live, now=now)
            else:
                # Fences already held by approve; offline/anchor path only (no catalog free→live).
                out[label] = admission._derive(
                    con, label, observation=None, fence_held=True, now=now)
        return out


def _apply_mutation(con, mutation: tuple) -> None:
    kind = mutation[0]
    args = mutation[1] if len(mutation) > 1 else ()
    if kind == "adopt_current":
        return
    if kind == "successor_replan":
        # The exact replacement intent is proposal-scoped authority. Approval installs the
        # immutable assignment and bumps planner_revision; no mutable affinity row is needed.
        return
    if kind == "finalize":
        # Apply named repos first so EventCon injection can fire on selection mutate.
        for repo in args:
            con.execute(
                "INSERT INTO selection(repo_id, finalized_at) VALUES(?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(repo_id) DO UPDATE SET finalized_at=CURRENT_TIMESTAMP",
                [repo])
            # Also emit an UPDATE so inject hooks matching UPDATE selection fire.
            con.execute(
                "UPDATE selection SET finalized_at=CURRENT_TIMESTAMP WHERE repo_id=?",
                [repo])
        con.execute(
            "UPDATE selection SET finalized_at = CURRENT_TIMESTAMP "
            "WHERE finalized_at IS NULL")
        return
    raise Refusal("MUTATION_MISMATCH", {"kind": kind}, ("use_previewed_mutation",))


def approve(con, proposal_id: str, *, mutation=None, services=None, **_extra):
    """Approval CAS: fences → evidence → guarded_mutation → short IMMEDIATE TX.

    No optimizer / plan_capacity inside the commit path.
    """
    if services is None:
        services = _DefaultServices()

    # Load read-only before fences (RFC).
    proposal = load_proposal(con, proposal_id)
    require_unambiguous_current_draft(con)
    labels = proposal_drive_ids(proposal)
    keys = _fence_keys(con, labels)
    catalog_path = getattr(db, "DB_PATH", None) or ":memory:"

    def _run_approve():
        # PR-09: no approval while a live execution session exists.
        try:
            from modelark.execution_session import live_session_exists, live_owner
            if live_session_exists(con):
                raise Refusal("FILL_SESSION_ACTIVE", live_owner(con), ("wait_or_stop",))
        except ImportError:
            pass
        with drive_fence.hold_controller(catalog_path, blocking=True):
            with drive_fence.hold_drives_sorted(keys, blocking=True):
                # Evidence after fences, before BEGIN IMMEDIATE (A6).
                observe = getattr(services, "observe_exact_capacity", None)
                if observe is not None:
                    try:
                        evidence = observe(con, labels)
                    except TypeError:
                        evidence = observe()
                else:
                    evidence = _DefaultServices().observe_exact_capacity(con, labels)

                def mutate():
                    return _approve_tx(con, proposal_id, mutation=mutation,
                                       evidence_by_drive=evidence)

                result = fill_worker.WORKER.guarded_mutation(mutate)
                if result is None:
                    raise Refusal("FILL_SESSION_ACTIVE", {"running": True},
                                  ("stop_or_pause_fill",))
                return result

    return _run_approve()


approve_proposal = approve


def _approve_tx(con, proposal_id: str, *, mutation, evidence_by_drive) -> dict:
    con.execute("BEGIN IMMEDIATE")
    try:
        proposal = load_proposal(con, proposal_id)
        if proposal["lifecycle"] != "draft":
            raise Refusal("PROPOSAL_NOT_DRAFT", {"lifecycle": proposal["lifecycle"]},
                          ("preview_again",))
        if proposal.get("gate_b_code") not in (None, "FEASIBLE"):
            raise Refusal("PROPOSAL_NOT_FEASIBLE", {"gate_b": proposal.get("gate_b_code")},
                          ("resolve_blocker", "preview_again"))

        # CAS order: request mismatches and revision/semantic before hash integrity so
        # typed refusals surface the operator-facing cause (tests pin exact codes).
        stored_mut = (proposal["mutation_kind"], tuple(proposal["mutation_args"]))
        if mutation is not None:
            if not isinstance(mutation, tuple):
                mutation = tuple(mutation)
            req_args = tuple(mutation[1]) if len(mutation) > 1 else ()
            if mutation[0] != stored_mut[0] or req_args != stored_mut[1]:
                raise Refusal("MUTATION_MISMATCH",
                              {"stored": stored_mut, "request": mutation},
                              ("use_previewed_mutation",))

        rev = int(con.execute(
            "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])
        if rev != int(proposal["based_on_revision"]):
            raise Refusal("PREVIEW_STALE", {"current": rev, "based_on": proposal["based_on_revision"]},
                          ("preview_again",))

        # Fail closed inside the approval transaction as well: publication can race
        # the pre-fence read, and approving one of several drafts would orphan intent.
        require_unambiguous_current_draft(con)

        # Semantic recompute independent of revision integer (missed-bump protection).
        current_semantic = _semantic_input_hash(
            con, proposal["plan_id"], stored_mut)
        if current_semantic != proposal.get("semantic_input_hash"):
            raise Refusal("APPROVED_INPUT_CHANGED",
                          {"stored": proposal.get("semantic_input_hash"),
                           "current": current_semantic},
                          ("preview_again",))

        # Canonical integrity first: a mutated task row without a matching
        # canonical_hash is proposal tampering (PROPOSAL_HASH_MISMATCH), not
        # ordinary catalog drift. Exact-assignment revalidation runs only after.
        stored_hash = proposal["canonical_hash"]
        recomputed = hash_stored_proposal(con, proposal_id)
        if recomputed != stored_hash:
            raise Refusal("PROPOSAL_HASH_MISMATCH",
                          {"stored": stored_hash, "recomputed": recomputed},
                          ("preview_again",))

        validate_exact_assignment(con, proposal, evidence_by_drive=evidence_by_drive)

        if proposal["mutation_kind"] == "adopt_current":
            if proposal.get("selection_before_hash") != proposal.get("selection_after_hash"):
                raise Refusal("ADOPT_SELECTION_CHANGED", {}, ("preview_again",))
        else:
            _apply_mutation(con, stored_mut)

        # Supersede prior approvals, mark approved, set pointer, bump revision.
        con.execute(
            "UPDATE placement_proposals SET lifecycle='superseded', "
            "superseded_at=CURRENT_TIMESTAMP WHERE lifecycle='approved'")
        con.execute(
            "UPDATE placement_proposals SET lifecycle='approved', "
            "approved_at=CURRENT_TIMESTAMP WHERE proposal_id=?", [proposal_id])
        con.execute(
            "UPDATE planner_state SET active_approved_proposal_id=? WHERE singleton_id=1",
            [proposal_id])
        bump_revision(con)
        con.execute("COMMIT")
        return {"proposal_id": proposal_id, "lifecycle": "approved"}
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
