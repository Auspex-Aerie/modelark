"""Capacity placement and byte ledger for the reconciled DEC-045 executor.

This turns derived work intents into deterministically assigned tasks, accounts durable and
transient bytes once, and returns typed feasibility evidence used by both execution and read-only
diagnostics.
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, replace
from enum import Enum
from typing import Mapping, Sequence

from modelark import candidates, capacity_evidence, compress, placement, reconcile
from modelark.budgets import CandidateBudget, EXPECTED_MARGIN, FileBudget  # shared budget truth (#36a)
from modelark.reconcile import (
    DiagnosticSeverity,
    ReconcileResult,
    TaskKind,
    WorkIntent,
)

__all__ = ["CandidateBudget", "EXPECTED_MARGIN", "FileBudget"]  # re-exported from the shared seam

DEFAULT_FLOAT_RATIO = 0.67
RATIO_MIN_SAMPLE = 50_000_000_000
RAID_MIN_HEADROOM_FRAC = 0.03
_HEADROOM_TRANCHES = (
    (1_000_000_000_000, 0.05),
    (4_000_000_000_000, 0.02),
    (16_000_000_000_000, 0.0125),
    (math.inf, 0.009),
)


class CapacityMode(str, Enum):
    GUARANTEED = "guaranteed"
    COMPRESSION_AWARE = "compression_aware"


class FreeEvidence(str, Enum):
    LIVE = "live"
    SNAPSHOT = "snapshot"


class FailureCode(str, Enum):
    CAPACITY_DURABLE_SHORT = "CAPACITY_DURABLE_SHORT"
    CAPACITY_WORKSPACE_SHORT = "CAPACITY_WORKSPACE_SHORT"
    CAPACITY_EVIDENCE_UNKNOWN = "CAPACITY_EVIDENCE_UNKNOWN"
    TARGET_DRIVE_CHANGED = "TARGET_DRIVE_CHANGED"
    TARGET_TIER_MISSING = "TARGET_TIER_MISSING"
    UNPROVEN_PROVENANCE = "UNPROVEN_PROVENANCE"
    REQUIREMENT_EXCEEDS_USABLE_MAX = "REQUIREMENT_EXCEEDS_USABLE_MAX"
    FAILURE_DOMAIN_UNSATISFIABLE = "FAILURE_DOMAIN_UNSATISFIABLE"
    GRAPH_DEPENDENCY_INVARIANT = "GRAPH_DEPENDENCY_INVARIANT"
    GRAPH_INVARIANT = "GRAPH_INVARIANT"  # legacy envelope; prefer typed structural codes above


@dataclass(frozen=True)
class CapacityDrive:
    """A plan drive paired with ONE admission-evidence record (#35-C). Usable free is the evidence's
    already-floor-adjusted ``admissible_free`` — there is no second, independently-writable free scalar.
    ``capacity_bytes`` is nominal device capacity for display/structural sizing only, never evidence."""
    drive_label: str
    role: str
    raid_backed: bool
    capacity_bytes: int
    evidence: capacity_evidence.Evidence
    safety_floor: int                              # reporting only — the floor is already applied in evidence

    @property
    def usable_now(self) -> int:
        return self.evidence.admissible_free       # floor subtracted exactly once, inside `derive`

    @property
    def observed_free(self) -> int | None:
        return self.evidence.observed_free

    @property
    def evidence_kind(self) -> str:
        return self.evidence.kind

    @property
    def evidence_code(self) -> str | None:
        return self.evidence.code

    @property
    def observed_at(self) -> str | None:
        return self.evidence.observed_at

    @property
    def identity_epoch(self) -> int | None:
        return self.evidence.identity_epoch

    @property
    def free_evidence(self) -> FreeEvidence | None:
        # One-release compatibility alias mapping the evidence kind to the legacy diagnostic enum.
        return {"live": FreeEvidence.LIVE, "anchor": FreeEvidence.SNAPSHOT}.get(self.evidence.kind)


@dataclass(frozen=True)
class TaskBudget:
    task_id: str
    requirement_id: str
    repo_id: str
    kind: TaskKind
    target_drive: str
    source_drive: str | None
    missing_files: tuple[str, ...]
    file_budgets: tuple[FileBudget, ...]
    guaranteed_durable: int
    expected_durable: int
    workspace_peak_guaranteed: int
    workspace_peak_expected: int
    evidence: str

    def durable_for(self, mode: CapacityMode) -> int:
        return (self.guaranteed_durable if mode == CapacityMode.GUARANTEED
                else self.expected_durable)

    def workspace_for(self, mode: CapacityMode) -> int:
        return (self.workspace_peak_guaranteed if mode == CapacityMode.GUARANTEED
                else self.workspace_peak_expected)


@dataclass(frozen=True)
class AssignedTask:
    task_id: str
    requirement_id: str
    repo_id: str
    kind: TaskKind
    target_drive: str
    source_drive: str | None
    depends_on_requirement: str | None
    budget: TaskBudget


@dataclass(frozen=True)
class DriveLedger:
    drive_label: str
    observed_free: int | None                      # raw admission observation (None when evidence unknown)
    free_evidence: FreeEvidence | None
    evidence_kind: str
    evidence_code: str | None
    observed_at: str | None
    identity_epoch: int | None
    safety_floor: int
    usable_now: int
    guaranteed_durable: int
    expected_durable: int
    workspace_peak_guaranteed: int
    workspace_peak_expected: int

    def required_peak(self, mode: CapacityMode) -> int:
        if mode == CapacityMode.GUARANTEED:
            return self.guaranteed_durable + self.workspace_peak_guaranteed
        return self.expected_durable + self.workspace_peak_expected


@dataclass(frozen=True)
class CapacityFailure:
    code: FailureCode
    capacity_mode: CapacityMode
    requirement_id: str | None
    task_ids: tuple[str, ...]
    target_tier: str | None
    eligible_drives: tuple[str, ...]
    required_bytes: int
    available_bytes: int
    safety_floor_bytes: int
    workspace_bytes: int
    shortfall_bytes: int
    evidence: FreeEvidence | None
    actions: tuple[str, ...]
    blocked_by_requirement: str | None = None
    evidence_code: str | None = None               # the drive's typed evidence code (e.g. unknown), if any


@dataclass(frozen=True)
class CapacityPlan:
    mode: CapacityMode
    placement_policy: str
    tasks: tuple[AssignedTask, ...]
    batch_order: tuple[str, ...]
    blocking_diagnostics: tuple[str, ...]
    unassigned_intents: tuple[WorkIntent, ...]
    ledgers: tuple[DriveLedger, ...]
    failures: tuple[CapacityFailure, ...]
    # Graded Gate-B projection (#38 / tiered_v2). feasible is True only when gate_b_code == FEASIBLE.
    gate_b_code: str = "FEASIBLE"
    gate_b_diagnostics: object | None = None
    gate_b_actions: tuple[str, ...] = ()
    derivation_mode: str | None = None
    solver_bound_version: str | None = None

    @property
    def feasible(self) -> bool:
        # Gate-B FEASIBLE is necessary but not sufficient: reconcile-level blocking diagnostics
        # (e.g. MANIFEST_POLICY on a mixed cart) keep the plan non-executable even when the
        # placeable subset packs. Gate-1 exclusivity still holds for pure packing outcomes
        # (no blocking diagnostics / failures).
        return (
            self.gate_b_code == "FEASIBLE"
            and not self.blocking_diagnostics
            and not self.failures
            and not self.unassigned_intents
        )

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "placement_policy": self.placement_policy,
            "feasible": self.feasible,
            "gate_b_code": self.gate_b_code,
            "gate_b_diagnostics": self.gate_b_diagnostics,
            "gate_b_actions": list(self.gate_b_actions),
            "derivation_mode": self.derivation_mode,
            "solver_bound_version": self.solver_bound_version,
            "batch_order": list(self.batch_order),
            "blocking_diagnostics": list(self.blocking_diagnostics),
            "tasks": [
                {
                    "id": item.task_id,
                    "requirement_id": item.requirement_id,
                    "repo": item.repo_id,
                    "kind": item.kind.value,
                    "target": item.target_drive,
                    "source": item.source_drive,
                    "depends_on_requirement": item.depends_on_requirement,
                    "missing_files": list(item.budget.missing_files),
                    "file_budgets": [
                        {
                            "rfilename": file.rfilename,
                            "guaranteed_durable": file.guaranteed_durable,
                            "expected_durable": file.expected_durable,
                            "workspace_peak_guaranteed": file.workspace_peak_guaranteed,
                            "workspace_peak_expected": file.workspace_peak_expected,
                            "evidence": file.evidence,
                        }
                        for file in item.budget.file_budgets
                    ],
                    "guaranteed_durable": item.budget.guaranteed_durable,
                    "expected_durable": item.budget.expected_durable,
                    "workspace_peak_guaranteed": item.budget.workspace_peak_guaranteed,
                    "workspace_peak_expected": item.budget.workspace_peak_expected,
                    "evidence": item.budget.evidence,
                }
                for item in self.tasks
            ],
            "unassigned": [item.requirement_id for item in self.unassigned_intents],
            "ledgers": [
                {
                    "drive": item.drive_label,
                    "observed_free": item.observed_free,
                    "free_evidence": item.free_evidence.value if item.free_evidence else None,
                    "evidence_kind": item.evidence_kind,
                    "evidence_code": item.evidence_code,
                    "observed_at": item.observed_at,
                    "identity_epoch": item.identity_epoch,
                    "safety_floor": item.safety_floor,
                    "usable_now": item.usable_now,
                    "guaranteed_durable": item.guaranteed_durable,
                    "expected_durable": item.expected_durable,
                    "workspace_peak_guaranteed": item.workspace_peak_guaranteed,
                    "workspace_peak_expected": item.workspace_peak_expected,
                    "required_peak": item.required_peak(self.mode),
                    "margin": item.usable_now - item.required_peak(self.mode),
                }
                for item in self.ledgers
            ],
            "failures": [
                {
                    "code": item.code.value,
                    "capacity_mode": item.capacity_mode.value,
                    "requirement_id": item.requirement_id,
                    "task_ids": list(item.task_ids),
                    "target_tier": item.target_tier,
                    "eligible_drives": list(item.eligible_drives),
                    "required_bytes": item.required_bytes,
                    "available_bytes": item.available_bytes,
                    "safety_floor_bytes": item.safety_floor_bytes,
                    "workspace_bytes": item.workspace_bytes,
                    "shortfall_bytes": item.shortfall_bytes,
                    "evidence": item.evidence.value if item.evidence else None,
                    "evidence_code": item.evidence_code,
                    "actions": list(item.actions),
                    "blocked_by_requirement": item.blocked_by_requirement,
                }
                for item in self.failures
            ],
        }


def mode_from_value(value: str | CapacityMode) -> CapacityMode:
    if isinstance(value, CapacityMode):
        return value
    aliases = {
        "uncompressed": CapacityMode.GUARANTEED,
        "compressed": CapacityMode.COMPRESSION_AWARE,
        "guaranteed": CapacityMode.GUARANTEED,
        "compression_aware": CapacityMode.COMPRESSION_AWARE,
    }
    try:
        return aliases[value]
    except KeyError as exc:
        raise ValueError(f"unsupported capacity mode {value!r}") from exc


def mode_from_legacy(value: str) -> CapacityMode:
    """Deprecated one-release adapter for callers using storage-sounding mode names."""
    warnings.warn(
        "mode_from_legacy() is deprecated; use mode_from_value() with a canonical capacity mode",
        DeprecationWarning,
        stacklevel=2,
    )
    return mode_from_value(value)


def headroom_bytes(capacity: int) -> int:
    reserved = 0.0
    low = 0.0
    for high, rate in _HEADROOM_TRANCHES:
        band = min(capacity, high) - low
        if band <= 0:
            break
        reserved += band * rate
        low = high
    return int(reserved)


def safety_floor(capacity: int, raid_backed: bool) -> int:
    floor = headroom_bytes(capacity)
    return max(floor, int(capacity * RAID_MIN_HEADROOM_FRAC)) if raid_backed else floor


def observed_float_ratio(con) -> float | None:
    stored, original = con.execute(
        "SELECT coalesce(sum(a.stored_bytes),0),coalesce(sum(a.orig_bytes),0) "
        "FROM archived a JOIN files f USING(repo_id,rfilename) "
        "WHERE f.format='safetensors' AND a.orig_bytes>0 AND "
        "(f.quant IS NULL OR lower(f.quant) IN "
        "('bf16','bfloat16','fp16','f16','float16','fp32','f32','float32'))"
    ).fetchone()
    return stored / original if original >= RATIO_MIN_SAMPLE else None


def plan_float_ratio(con) -> float:
    return max(observed_float_ratio(con) or DEFAULT_FLOAT_RATIO, DEFAULT_FLOAT_RATIO)


def zstd_output_cap(raw_size: int) -> int:
    return compress.zstd_output_cap(raw_size)


def codec_output_cap(raw_size: int, codec: str, *, stream_chunk_bytes: int) -> int:
    return compress.codec_output_cap(
        raw_size, codec, stream_chunk_bytes=stream_chunk_bytes
    )


def inspect_drives(
    con,
    plan_id: str,
    *,
    evidence_by_drive: Mapping[str, capacity_evidence.Evidence] | None = None,
) -> tuple[CapacityDrive, ...]:
    """Pair each plan drive with its admission Evidence (#35-C). Usable free is the evidence's
    admissible_free — the admission fact loader never reads the legacy per-drive free column and never
    reconstructs free as capacity minus archived bytes. A drive with no supplied evidence is fail-closed
    ``unknown`` (zero executable). ``capacity_bytes`` (nominal) stays for display/structural sizing; the
    reporting safety floor uses the current-epoch filesystem capacity."""
    evidence_by_drive = evidence_by_drive or {}
    rows = con.execute(
        "SELECT d.drive_label,coalesce(d.role,'primary'),coalesce(d.raid_backed,0),"
        "coalesce(d.capacity_bytes,0),coalesce(d.filesystem_capacity_bytes,d.capacity_bytes,0) "
        "FROM plan_drives pd JOIN drives d USING(drive_label) WHERE pd.plan_id=? "
        "ORDER BY d.drive_label",
        [plan_id],
    ).fetchall()
    facts = []
    for label, role, raid, nominal_capacity, epoch_capacity in rows:
        evidence = evidence_by_drive.get(label) or capacity_evidence.Evidence(
            kind="unknown", executable=False, admissible_free=0, code="CAPACITY_EVIDENCE_UNKNOWN")
        facts.append(CapacityDrive(
            drive_label=label,
            role=role,
            raid_backed=bool(raid),
            capacity_bytes=int(nominal_capacity or 0),
            evidence=evidence,
            safety_floor=safety_floor(int(epoch_capacity or 0), bool(raid)),
        ))
    return tuple(facts)


def _task_budget(candidate: candidates.Candidate) -> TaskBudget:
    """Wrap a canonical Candidate's shared-seam budget as a capacity TaskBudget — no recomputation, so
    the tiered_v1 adapter and the CandidateSet cannot drift. Candidates are the sole authority for the
    reused/missing sets and their per-file budgets."""
    budget = candidate.budget
    source = candidate.source
    source_drive = source.drive_label if isinstance(source, candidates.SourceIdentity) else None
    evidence = budget.file_budgets[0].evidence if budget.file_budgets else "estimate"
    return TaskBudget(
        task_id=f"{candidate.task_kind.value}:{candidate.requirement_id}",
        requirement_id=candidate.requirement_id,
        repo_id=candidate.requirement_id.split(":", 1)[1],
        kind=candidate.task_kind,
        target_drive=candidate.target_drive,
        source_drive=source_drive,
        missing_files=tuple(item.rfilename for item in candidate.missing_files),
        file_budgets=budget.file_budgets,
        guaranteed_durable=budget.guaranteed_durable,
        expected_durable=budget.expected_durable,
        workspace_peak_guaranteed=budget.workspace_peak_guaranteed,
        workspace_peak_expected=budget.workspace_peak_expected,
        evidence=evidence,
    )


def _drive_tier(drive: CapacityDrive) -> str:
    if drive.role == "replica":
        return "replica"
    return "raid_home" if drive.raid_backed else "primary"


def _actions_for(drive: CapacityDrive, base: tuple[str, ...]) -> tuple[str, ...]:
    """When a block is due to UNKNOWN evidence (zero executable), lead with mount/reconcile so the
    operator is not told to free/trim observed space that was never actually observed. The complete
    mixed-fleet outcome ladder is #38; this only preserves the typed cause and the right first action."""
    if drive.evidence_kind == "unknown":
        return ("mount_or_reconcile_drive", *base)
    return base


def preflight_file(
    drive: CapacityDrive,
    file_budget: FileBudget,
    mode: CapacityMode,
    *,
    requirement_id: str | None = None,
    task_id: str = "file-preflight",
) -> CapacityFailure | None:
    """Fresh-operation guard; the drive carries current admission evidence (``usable_now`` is the
    already-floor-adjusted admissible free), so this never re-subtracts the safety floor."""
    guaranteed = mode == CapacityMode.GUARANTEED
    durable = file_budget.durable_for(guaranteed)
    workspace = file_budget.workspace_for(guaranteed)
    required = durable + workspace
    if required <= drive.usable_now:
        return None
    code = (FailureCode.CAPACITY_DURABLE_SHORT if durable > drive.usable_now
            else FailureCode.CAPACITY_WORKSPACE_SHORT)
    return CapacityFailure(
        code=code,
        capacity_mode=mode,
        requirement_id=requirement_id,
        task_ids=(task_id,),
        target_tier=_drive_tier(drive),
        eligible_drives=(drive.drive_label,),
        required_bytes=required,
        available_bytes=drive.usable_now,
        safety_floor_bytes=drive.safety_floor,
        workspace_bytes=workspace,
        shortfall_bytes=required - drive.usable_now,
        evidence=drive.free_evidence,
        evidence_code=drive.evidence_code,
        actions=_actions_for(drive, ("free_target_space", "add_eligible_drive", "replan")),
    )


def target_drive_changed_failure(
    task: AssignedTask,
    mode: CapacityMode,
) -> CapacityFailure:
    """Typed stale-snapshot evidence when a task target leaves its Plan before execution."""
    durable = task.budget.durable_for(mode)
    workspace = task.budget.workspace_for(mode)
    required = durable + workspace
    return CapacityFailure(
        code=FailureCode.TARGET_DRIVE_CHANGED,
        capacity_mode=mode,
        requirement_id=task.requirement_id,
        task_ids=(task.task_id,),
        target_tier=("replica" if task.kind == TaskKind.REPLICATE else "primary"),
        eligible_drives=(task.target_drive,),
        required_bytes=required,
        available_bytes=0,
        safety_floor_bytes=0,
        workspace_bytes=workspace,
        shortfall_bytes=required,
        evidence=None,
        actions=("reconcile_plan", "restore_target_drive_to_plan"),
    )


class _Placement:
    def __init__(self, drives: Sequence[CapacityDrive], mode: CapacityMode):
        self.drives = {item.drive_label: item for item in drives}
        self.mode = mode
        self.tasks: list[AssignedTask] = []

    def totals(self, label: str, extra: TaskBudget | None = None) -> tuple[int, int]:
        budgets = [item.budget for item in self.tasks if item.target_drive == label]
        if extra is not None:
            budgets.append(extra)
        durable = sum(item.durable_for(self.mode) for item in budgets)
        workspace = max((item.workspace_for(self.mode) for item in budgets), default=0)
        return durable, workspace

    def fits(self, label: str, budget: TaskBudget) -> bool:
        durable, workspace = self.totals(label, budget)
        return durable + workspace <= self.drives[label].usable_now

    def add(self, intent: WorkIntent, budget: TaskBudget) -> None:
        self.tasks.append(AssignedTask(
            task_id=intent.task_id,
            requirement_id=intent.requirement_id,
            repo_id=intent.repo_id,
            kind=intent.kind,
            target_drive=budget.target_drive,
            source_drive=budget.source_drive,
            depends_on_requirement=intent.depends_on_requirement,
            budget=budget,
        ))


def _task_order(item: tuple[WorkIntent, TaskBudget], mode: CapacityMode) -> tuple:
    intent, budget = item
    return (-budget.durable_for(mode), intent.requirement_id)


def _failure_for_unassigned(
    intent: WorkIntent,
    candidates: Sequence[TaskBudget],
    placement: _Placement,
) -> CapacityFailure:
    eligible = tuple(item.target_drive for item in candidates) or intent.eligible_drives
    if not candidates:
        missing_tier = not intent.eligible_drives
        return CapacityFailure(
            code=(FailureCode.TARGET_TIER_MISSING if missing_tier
                  else FailureCode.GRAPH_INVARIANT),
            capacity_mode=placement.mode,
            requirement_id=intent.requirement_id,
            task_ids=(intent.task_id,),
            target_tier=("replica" if intent.kind == TaskKind.REPLICATE else "primary"),
            eligible_drives=eligible,
            required_bytes=0,
            available_bytes=0,
            safety_floor_bytes=0,
            workspace_bytes=0,
            shortfall_bytes=0,
            evidence=None,
            actions=(("add_eligible_drive", "change_plan_policy") if missing_tier
                     else ("reconcile_plan", "restore_pinned_drive_to_plan")),
            blocked_by_requirement=intent.depends_on_requirement,
        )
    best = max(candidates, key=lambda item: placement.drives[item.target_drive].usable_now)
    drive = placement.drives[best.target_drive]
    current_durable, current_workspace = placement.totals(best.target_drive)
    durable = current_durable + best.durable_for(placement.mode)
    workspace = max(current_workspace, best.workspace_for(placement.mode))
    required = durable + workspace
    code = (FailureCode.CAPACITY_DURABLE_SHORT if durable > drive.usable_now
            else FailureCode.CAPACITY_WORKSPACE_SHORT)
    return CapacityFailure(
        code=code,
        capacity_mode=placement.mode,
        requirement_id=intent.requirement_id,
        task_ids=(intent.task_id,),
        target_tier=_drive_tier(drive),
        eligible_drives=eligible,
        required_bytes=required,
        available_bytes=drive.usable_now,
        safety_floor_bytes=drive.safety_floor,
        workspace_bytes=workspace,
        shortfall_bytes=max(0, required - drive.usable_now),
        evidence=drive.free_evidence,
        evidence_code=drive.evidence_code,
        actions=_actions_for(drive, ("expand_eligible_tier", "trim_selection", "change_capacity_mode")),
        blocked_by_requirement=intent.depends_on_requirement,
    )


def _ledgers(drives: Sequence[CapacityDrive], tasks: Sequence[AssignedTask]) -> tuple[DriveLedger, ...]:
    out = []
    for drive in drives:
        budgets = [item.budget for item in tasks if item.target_drive == drive.drive_label]
        out.append(DriveLedger(
            drive_label=drive.drive_label,
            observed_free=drive.observed_free,
            free_evidence=drive.free_evidence,
            evidence_kind=drive.evidence_kind,
            evidence_code=drive.evidence_code,
            observed_at=drive.observed_at,
            identity_epoch=drive.identity_epoch,
            safety_floor=drive.safety_floor,
            usable_now=drive.usable_now,
            guaranteed_durable=sum(item.guaranteed_durable for item in budgets),
            expected_durable=sum(item.expected_durable for item in budgets),
            workspace_peak_guaranteed=max(
                (item.workspace_peak_guaranteed for item in budgets), default=0
            ),
            workspace_peak_expected=max(
                (item.workspace_peak_expected for item in budgets), default=0
            ),
        ))
    return tuple(out)


def execution_rank(task: AssignedTask, result: ReconcileResult) -> tuple:
    """Stable within/between-drive priority without weakening bulk-before-replica."""
    manifest = result.manifests[task.repo_id]
    raw_size = sum(item.size_bytes for item in manifest)
    resumes_partial = len(task.budget.missing_files) < len(manifest)
    if task.kind == TaskKind.FETCH and resumes_partial:
        tier = 0
    elif task.kind == TaskKind.FETCH and raw_size > 250_000_000_000:
        tier = 1
    elif task.kind == TaskKind.FETCH and task.requirement_id.startswith("protected_home:"):
        tier = 2
    elif task.kind == TaskKind.FETCH:
        tier = 3
    else:
        tier = 4
    return tier, -raw_size, task.repo_id, task.requirement_id


def _batch_order(tasks: Sequence[AssignedTask], result: ReconcileResult) -> tuple[str, ...]:
    """DEC-034: global priority chooses a drive batch; tasks never change target."""
    by_drive: dict[str, list[tuple]] = {}
    for task in tasks:
        by_drive.setdefault(task.target_drive, []).append(execution_rank(task, result))
    return tuple(
        label for label, _ in sorted(
            ((label, min(ranks)) for label, ranks in by_drive.items()),
            key=lambda item: (*item[1], item[0]),
        )
    )


@dataclass
class _Placeable:
    """A requirement's placement inputs derived from the canonical CandidateSet by the legacy tiered_v1
    adapter. Duck-types WorkIntent for _Placement/_failure_for_unassigned. Carries pre-computed candidate
    budgets per target and the finish-in-place target that reproduces the pre-#36a proven-partial pin."""
    requirement_id: str
    repo_id: str
    kind: TaskKind
    task_id: str
    eligible_drives: tuple[str, ...]
    depends_on_requirement: str | None
    budgets_by_target: dict[str, TaskBudget]
    finish_in_place: str | None


def _rank_home_drive(drive: CapacityDrive) -> tuple:
    return (0 if drive.raid_backed else 1, -drive.capacity_bytes, drive.drive_label)


def _best_finish_in_place(cands, drive_by_label) -> str | None:
    """Reproduce the pre-#36a ``_choose_partial`` preference over canonical finish-in-place candidates:
    least missing bytes, then most reused files, then tier/label. This is a legacy placement choice."""
    partials = [item for item in cands if item.reused_files and item.target_drive in drive_by_label]
    if not partials:
        return None

    def rank(candidate) -> tuple:
        missing_bytes = sum(item.size_bytes for item in candidate.missing_files)
        drive = drive_by_label[candidate.target_drive]
        if candidate.task_kind == TaskKind.REPLICATE:
            drank = (drive.capacity_bytes, drive.drive_label)
        else:
            drank = _rank_home_drive(drive)
        return (missing_bytes, -len(candidate.reused_files), drank, candidate.target_drive)

    return sorted(partials, key=rank)[0].target_drive


def _legacy_placeables(cset, drive_by_label) -> list[_Placeable]:
    """LEGACY tiered_v1 adapter (removed at #38): choose among CANONICAL candidates only — never legacy
    filename facts. Collapses each replica to one canonical home source (or PendingHome) as the pre-#36a
    path did, and pre-computes per-target budgets from the shared seam via the candidates."""
    placeables = []
    for requirement_id, cands in cset.by_requirement:
        if not cands:
            continue
        kind = cands[0].task_kind
        depends_on = None
        if kind == TaskKind.REPLICATE:
            source_labels = sorted({
                item.source.drive_label for item in cands
                if isinstance(item.source, candidates.SourceIdentity) and item.source.drive_label in drive_by_label
            })
            if source_labels:
                chosen = sorted(source_labels, key=lambda label: _rank_home_drive(drive_by_label[label]))[0]
                selected = [
                    item for item in cands
                    if isinstance(item.source, candidates.SourceIdentity) and item.source.drive_label == chosen
                ]
            else:
                selected = [item for item in cands if isinstance(item.source, candidates.PendingHome)]
                depends_on = next((item.depends_on_requirement for item in selected), None)
        else:
            selected = list(cands)
        budgets_by_target = {
            item.target_drive: _task_budget(item)
            for item in selected if item.target_drive in drive_by_label
        }
        if not budgets_by_target:
            continue
        placeables.append(_Placeable(
            requirement_id=requirement_id,
            repo_id=requirement_id.split(":", 1)[1],
            kind=kind,
            task_id=f"{kind.value}:{requirement_id}",
            eligible_drives=tuple(sorted(budgets_by_target)),
            depends_on_requirement=depends_on,
            budgets_by_target=budgets_by_target,
            finish_in_place=_best_finish_in_place(selected, drive_by_label),
        ))
    return placeables


def _adapter_solver_bounds() -> placement.SolverBounds:
    """Production solver bounds for the plan_capacity adapter (patchable; not a public kwarg).

    Measured 2026-07-24 (local) on the Gate-1 10k-candidate scale fixture (100 requirements ×
    100 drives = 10_000 candidates) and the phase-2 1000×10 characterization:

    | phase / case                         | states      | wall time   | notes |
    |--------------------------------------|-------------|-------------|-------|
    | gate_b 10k first-feasible            | 101         | ~0.03 s     | root + 100  |
    | improve 10k @ optim=50_000           | 50_000 cap  | ~30 s       | truncated   |
    | improve 10k @ optim=5_000            | 5_000 cap   | ~2–3 s      | over 2 s budget |
    | phase-2 plan_capacity alone @ 1_500  | ≤1_500      | ~1.16–1.19 s| solver window only |
    | phase-2 reconcile+plan (asserted)    | —           | ~1.46–1.54 s| pytest budget 2.0 s covers this full window |

    Frozen production defaults (must match the return values below):
      feasibility_state_limit = 200_000  — headroom over easy first-feasible; matches Gate-1 scale
                                           fixture cap; adversarial multi-drive packing << 50k.
      optimization_state_limit = 1_500   — keeps phase-2 reconcile+plan under the 2.0 s budget
                                           (~23–27% headroom on that full window; plan_capacity alone
                                           is ~1.2 s). Returns best-so-far under state_truncated.
                                           Do not ship 5_000: it breaches the 2 s budget.
    """
    return placement.SolverBounds(
        feasibility_state_limit=200_000,
        optimization_state_limit=1_500,
    )


def _diagnostics_for_serialize(diagnostics) -> object:
    """Normalize Gate-B diagnostics into a JSON-friendly structure for CapacityPlan.to_dict()."""
    if diagnostics is None:
        return None
    if isinstance(diagnostics, dict):
        out = {}
        for key, value in diagnostics.items():
            if isinstance(value, tuple):
                out[key] = list(value)
            else:
                out[key] = value
        return out
    return diagnostics


def _match_candidate(
    cset: candidates.CandidateSet,
    task: placement.AssignmentTask,
) -> candidates.Candidate | None:
    """Locate the Candidate that produced an assignment task (target + resolved source)."""
    for rid, cs in cset.by_requirement:
        if rid != task.requirement_id:
            continue
        matches = [c for c in cs if c.target_drive == task.target_drive]
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        for c in matches:
            src = c.source
            if isinstance(src, candidates.SourceIdentity) and task.source is not None:
                if (src.drive_label == task.source.drive_label
                        and src.annex_key == task.source.annex_key
                        and src.orig_sha256 == task.source.orig_sha256):
                    return c
            if isinstance(src, candidates.PendingHome) and task.source is not None:
                # PendingHome was resolved to SourceIdentity(home, None, None)
                if (task.source.drive_label is not None
                        and task.source.annex_key is None
                        and task.source.orig_sha256 is None):
                    return c
            if src is None and task.source is None:
                return c
        return matches[0]
    return None


def _project_assigned_tasks(
    assignment: placement.CanonicalAssignment,
    cset: candidates.CandidateSet,
) -> tuple[AssignedTask, ...]:
    """Project pure CanonicalAssignment tasks onto capacity AssignedTask / TaskBudget records."""
    out: list[AssignedTask] = []
    for task in assignment.tasks:
        cand = _match_candidate(cset, task)
        if cand is None:
            # Satisfied requirements never appear in assignment; synthesize a minimal budget.
            repo_id = task.requirement_id.split(":", 1)[1]
            source_drive = task.source.drive_label if task.source is not None else None
            budget = TaskBudget(
                task_id=f"{task.task_kind.value}:{task.requirement_id}",
                requirement_id=task.requirement_id,
                repo_id=repo_id,
                kind=task.task_kind,
                target_drive=task.target_drive,
                source_drive=source_drive,
                missing_files=task.missing_files,
                file_budgets=(),
                guaranteed_durable=task.guaranteed_durable,
                expected_durable=task.expected_durable,
                workspace_peak_guaranteed=task.workspace_peak_guaranteed,
                workspace_peak_expected=task.workspace_peak_expected,
                evidence=task.budget_evidence,
            )
        else:
            budget = _task_budget(cand)
            if task.source is not None:
                budget = replace(budget, source_drive=task.source.drive_label)
            elif isinstance(cand.source, candidates.PendingHome):
                # Resolved home source on the pure assignment
                budget = replace(budget, source_drive=None)
        out.append(AssignedTask(
            task_id=budget.task_id,
            requirement_id=task.requirement_id,
            repo_id=budget.repo_id,
            kind=task.task_kind,
            target_drive=task.target_drive,
            source_drive=budget.source_drive,
            depends_on_requirement=task.depends_on_requirement,
            budget=budget,
        ))
    out.sort(key=lambda item: (item.target_drive, item.kind.value, item.requirement_id))
    return tuple(out)


def _placeable_drive_labels(con, plan_id: str) -> frozenset[str]:
    """State-only Gate-B revalidation: active+enabled plan members (no manifest/archive recapture).

    Missing/malformed lifecycle or eligibility never coerce to placeable (#37 finding 14).
    """
    rows = con.execute(
        "SELECT d.drive_label, d.lifecycle, d.eligibility "
        "FROM plan_drives pd JOIN drives d USING(drive_label) WHERE pd.plan_id=?",
        [plan_id],
    ).fetchall()
    return frozenset(
        label for label, lifecycle, eligibility in rows
        if lifecycle == "active" and eligibility == "enabled"
    )


def _build_solver_input(
    con,
    result: ReconcileResult,
    *,
    mode: CapacityMode,
    evidence_by_drive: Mapping[str, capacity_evidence.Evidence] | None,
    bounds: placement.SolverBounds,
) -> tuple[placement.SolverInput, tuple[CapacityDrive, ...], frozenset[str]]:
    """Shell: admission evidence → pure SolverInput (no floor recomputation).

    Placeability is re-read with a state-only drive query at Gate-B time so a lifecycle/
    eligibility flip after reconcile capture fail-closes as TARGET_DRIVE_CHANGED (#37).
    Drive facts for the pure solver come from the same light plan-membership reread (not a
    full planner recapture of manifests/archives/config/ratios).
    """
    capacity_drives = inspect_drives(con, result.plan_id, evidence_by_drive=evidence_by_drive)
    drive_facts = reconcile._drive_facts(con, result.plan_id)
    placeable = _placeable_drive_labels(con, result.plan_id)
    # Prefer the CandidateSet already on the reconcile result (same snapshot as the caller saw).
    graph = candidates.RequirementGraph(
        desired=tuple(result.requirements),
        requirement_set_hash="",
    )
    executable: list[tuple[str, int]] = []
    maxima: list[tuple[str, int | None]] = []
    evidence_pairs: list[tuple[str, placement.DriveEvidenceFact]] = []
    for drive in capacity_drives:
        ev = drive.evidence
        free = int(ev.admissible_free) if ev.executable else 0
        executable.append((drive.drive_label, free))
        # Prefer admission-supplied optimistic max; for executable live/anchor evidence with a
        # missing max, fall back to capacity − safety floor so known free shortfalls still
        # classify as proven infeasible rather than CAPACITY_EVIDENCE_UNKNOWN.
        mx = ev.optimistic_usable_max
        if mx is None and ev.executable:
            mx = max(0, int(drive.capacity_bytes) - int(drive.safety_floor))
        maxima.append((drive.drive_label, mx))
        evidence_pairs.append((
            drive.drive_label,
            placement.DriveEvidenceFact(
                drive_label=drive.drive_label,
                executable=bool(ev.executable),
                kind=str(ev.kind),
                code=ev.code,
            ),
        ))
    executable.sort(key=lambda item: item[0])
    maxima.sort(key=lambda item: item[0])
    evidence_pairs.sort(key=lambda item: item[0])
    inp = placement.SolverInput(
        graph=graph,
        candidates=result.candidates,
        drives=drive_facts,
        executable_budget=tuple(executable),
        max_usable_for_epoch=tuple(maxima),
        drive_evidence=tuple(evidence_pairs),
        capacity_mode=mode.value,
        policy_version=placement.POLICY_VERSION,
        bounds=bounds,
    )
    return inp, capacity_drives, placeable


def _stale_target_failures(
    result: ReconcileResult,
    capacity_drives: Sequence[CapacityDrive],
    mode: CapacityMode,
    *,
    placeable_labels: frozenset[str] | None = None,
) -> list[CapacityFailure]:
    """Detect candidates whose targets left the plan or lost placeability between reconcile and placement.

    **Any** captured candidate target that is no longer plan-member + active+enabled fail-closes the
    requirement as TARGET_DRIVE_CHANGED — including multi-target races where another target remains
    placeable (#37 finding 13). Do not strip the stale target and re-solve.
    """
    in_plan = {d.drive_label for d in capacity_drives}
    usable = placeable_labels if placeable_labels is not None else frozenset(in_plan)
    # Placement may only land on labels that are still plan members AND still placeable.
    usable = frozenset(lab for lab in usable if lab in in_plan)
    failures: list[CapacityFailure] = []
    for rid, cs in result.candidates.by_requirement:
        if not cs:
            continue
        # Fail closed if any captured target is no longer usable (not: if any remains usable).
        if all(c.target_drive in usable for c in cs):
            continue
        is_replica = rid.startswith("protected_replica:")
        failures.append(CapacityFailure(
            code=FailureCode.TARGET_DRIVE_CHANGED,
            capacity_mode=mode,
            requirement_id=rid,
            task_ids=(f"{'replicate' if is_replica else 'fetch'}:{rid}",),
            target_tier=("replica" if is_replica else "primary"),
            eligible_drives=tuple(sorted({c.target_drive for c in cs})),
            required_bytes=0, available_bytes=0, safety_floor_bytes=0, workspace_bytes=0,
            shortfall_bytes=0, evidence=None,
            actions=("reconcile_plan", "restore_target_drive_to_plan"),
        ))
    return failures


# Gate-B structural code → FailureCode (1:1; never collapse non-graph codes into GRAPH_INVARIANT).
_STRUCTURAL_FAILURE_CODES: dict[str, FailureCode] = {
    "TARGET_TIER_MISSING": FailureCode.TARGET_TIER_MISSING,
    "UNPROVEN_PROVENANCE": FailureCode.UNPROVEN_PROVENANCE,
    "REQUIREMENT_EXCEEDS_USABLE_MAX": FailureCode.REQUIREMENT_EXCEEDS_USABLE_MAX,
    "FAILURE_DOMAIN_UNSATISFIABLE": FailureCode.FAILURE_DOMAIN_UNSATISFIABLE,
    "GRAPH_DEPENDENCY_INVARIANT": FailureCode.GRAPH_DEPENDENCY_INVARIANT,
}


def _structural_failure_projection(
    code: str,
    gate: placement.GateBResult,
    mode: CapacityMode,
    capacity_drives: Sequence[CapacityDrive],
) -> tuple[CapacityFailure, ...]:
    """Project a Gate-B structural code onto one CapacityFailure for CLI/library operators.

    FailureCode matches the structural ladder code. Drive lists are taken from the diagnostic
    payload keys that pure gate_b actually emits (eligible_drives, drives, or maxima labels) —
    no silent or-defaults that invent or drop known drives.
    """
    del capacity_drives  # reserved for future ledger correlation; projection is diagnostic-driven
    diag = gate.diagnostics if isinstance(gate.diagnostics, dict) else {}
    if not isinstance(diag, dict):
        diag = {}

    if "requirement_id" in diag:
        rid = diag["requirement_id"]
    elif "replica_requirement_id" in diag:
        rid = diag["replica_requirement_id"]
    elif "home_requirement_id" in diag:
        rid = diag["home_requirement_id"]
    else:
        rid = None

    if "eligible_drives" in diag:
        raw_eligible = diag["eligible_drives"]
    elif "drives" in diag:
        raw_eligible = diag["drives"]
    elif "maxima" in diag:
        # REQUIREMENT_EXCEEDS_USABLE_MAX: maxima is ((drive, max_bytes), ...)
        raw_eligible = tuple(lab for lab, _mx in diag["maxima"])
    else:
        raw_eligible = ()
    if isinstance(raw_eligible, list):
        eligible = tuple(raw_eligible)
    else:
        eligible = tuple(raw_eligible) if raw_eligible else ()

    peak = int(diag["peak_bytes"]) if "peak_bytes" in diag else 0
    actions = tuple(gate.actions) if gate.actions else ()
    fcode = _STRUCTURAL_FAILURE_CODES.get(code)
    if fcode is None:
        raise ValueError(f"unmapped structural gate_b code for failure projection: {code!r}")

    return (CapacityFailure(
        code=fcode,
        capacity_mode=mode,
        requirement_id=str(rid) if rid is not None else None,
        task_ids=(),
        target_tier=None,
        eligible_drives=eligible,
        required_bytes=peak,
        available_bytes=0,
        safety_floor_bytes=0,
        workspace_bytes=0,
        shortfall_bytes=peak,
        evidence=None,
        actions=actions,
    ),)


def _unknown_evidence_failures(
    capacity_drives: Sequence[CapacityDrive],
    mode: CapacityMode,
    solver_inp: placement.SolverInput,
) -> tuple[CapacityFailure, ...]:
    """Project fail-closed unknown evidence onto CapacityFailure rows (evidence_code preserved)."""
    unknown = [d for d in capacity_drives if not d.evidence.executable]
    if not unknown:
        unknown = list(capacity_drives)
    rid = None
    for req_id, cs in solver_inp.candidates.by_requirement:
        if cs:
            rid = req_id
            break
    if rid is None and solver_inp.graph.desired:
        rid = solver_inp.graph.desired[0].requirement_id
    out: list[CapacityFailure] = []
    for drive in unknown:
        # Not CAPACITY_*_SHORT — Gate-1 forbids projecting CAPACITY_EVIDENCE_UNKNOWN as a
        # proven capacity short alone. Primary taxonomy is FailureCode.CAPACITY_EVIDENCE_UNKNOWN
        # (not GRAPH_INVARIANT). evidence_code is the admission code when present — never a
        # silent or-default substitute for the FailureCode.
        out.append(CapacityFailure(
            code=FailureCode.CAPACITY_EVIDENCE_UNKNOWN,
            capacity_mode=mode,
            requirement_id=rid,
            task_ids=(),
            target_tier=_drive_tier(drive),
            eligible_drives=(drive.drive_label,),
            required_bytes=0,
            available_bytes=0,
            safety_floor_bytes=drive.safety_floor,
            workspace_bytes=0,
            shortfall_bytes=0,
            evidence=drive.free_evidence,
            evidence_code=drive.evidence_code,
            actions=_actions_for(drive, ("mount_or_reconcile_drive", "retry_preview")),
        ))
    return tuple(out)


def _capacity_short_failures(
    solver_inp: placement.SolverInput,
    capacity_drives: Sequence[CapacityDrive],
    mode: CapacityMode,
) -> tuple[CapacityFailure, ...]:
    """Compatibility projection for proven known-budget infeasibility (not structural/unknown).

    Surfaces a single root capacity-short failure from the tightest unsatisfied candidate set so
    legacy callers that read ``failures[0].code`` still see CAPACITY_*_SHORT. Not used for
    PACKING_INCONCLUSIVE / CAPACITY_EVIDENCE_UNKNOWN / structural codes.
    """
    drive_by = {d.drive_label: d for d in capacity_drives}
    free_map = {k: v for k, v in solver_inp.executable_budget}
    best: CapacityFailure | None = None
    for rid, cs in solver_inp.candidates.by_requirement:
        if not cs:
            continue
        # Prefer the candidate on the roomiest drive for the diagnostic envelope.
        ranked = sorted(
            cs,
            key=lambda c: (-free_map.get(c.target_drive, 0), c.target_drive),
        )
        c = ranked[0]
        drive = drive_by.get(c.target_drive)
        if drive is None:
            continue
        durable = (c.budget.guaranteed_durable if mode == CapacityMode.GUARANTEED
                   else c.budget.expected_durable)
        workspace = (c.budget.workspace_peak_guaranteed if mode == CapacityMode.GUARANTEED
                     else c.budget.workspace_peak_expected)
        required = durable + workspace
        available = free_map.get(c.target_drive, 0)
        if required <= available:
            continue
        code = (FailureCode.CAPACITY_DURABLE_SHORT if durable > available
                else FailureCode.CAPACITY_WORKSPACE_SHORT)
        failure = CapacityFailure(
            code=code,
            capacity_mode=mode,
            requirement_id=rid,
            task_ids=(f"{c.task_kind.value}:{rid}",),
            target_tier=_drive_tier(drive),
            eligible_drives=tuple(sorted({x.target_drive for x in cs})),
            required_bytes=required,
            available_bytes=available,
            safety_floor_bytes=drive.safety_floor,
            workspace_bytes=workspace,
            shortfall_bytes=required - available,
            evidence=drive.free_evidence,
            evidence_code=drive.evidence_code,
            actions=_actions_for(drive, ("expand_eligible_tier", "trim_selection", "change_capacity_mode")),
            blocked_by_requirement=c.depends_on_requirement,
        )
        if best is None or failure.shortfall_bytes > best.shortfall_bytes:
            best = failure
    return (best,) if best is not None else ()


def plan_capacity(
    con,
    result: ReconcileResult,
    *,
    capacity_mode: str | CapacityMode | None = None,
    evidence_by_drive: Mapping[str, capacity_evidence.Evidence] | None = None,
    compression_cfg: Mapping[str, object] | None = None,
    provisioning: str | None = None,
) -> CapacityPlan:
    """Materialize deterministic ``tiered_v2`` assignments via pure Gate-B + improve (#38).

    Outer signature is preserved. Usable free and optimistic maxima come from ``evidence_by_drive``
    (shared admission authority); a drive absent from it is fail-closed ``unknown`` with zero
    executable capacity (#35-C). Solver bounds come from the private :func:`_adapter_solver_bounds`
    hook (patchable in tests; not a public kwarg).
    """
    if compression_cfg is not None:
        # #36a: candidate budgets are captured during reconcile_plan() (from PlannerInput.compression_cfg),
        # so a codec config here would be a safety-affecting no-op. Reject it loudly rather than mislead.
        raise TypeError(
            "plan_capacity(compression_cfg=...) is no longer honoured: candidate budgets are captured "
            "during reconciliation. Pass compression_cfg to reconcile_plan()/capture_planner_input().")
    if provisioning is not None:
        warnings.warn(
            "plan_capacity(provisioning=...) is deprecated; use capacity_mode=...",
            DeprecationWarning,
            stacklevel=2,
        )
        legacy = mode_from_value(provisioning)
        if capacity_mode is not None and mode_from_value(capacity_mode) != legacy:
            raise ValueError("capacity_mode and deprecated provisioning disagree")
        capacity_mode = legacy
    mode = mode_from_value(capacity_mode or CapacityMode.GUARANTEED)

    bounds = _adapter_solver_bounds()
    solver_inp, capacity_drives, placeable_labels = _build_solver_input(
        con, result, mode=mode, evidence_by_drive=evidence_by_drive, bounds=bounds,
    )

    blocking = tuple(sorted({
        item.code for item in result.diagnostics
        if item.severity in {DiagnosticSeverity.BLOCKING, DiagnosticSeverity.ERROR}
    }))

    # Stale snapshot: candidate targets left the plan or lost placeability after reconcile.
    stale = _stale_target_failures(
        result, capacity_drives, mode, placeable_labels=placeable_labels)
    if stale:
        return CapacityPlan(
            mode=mode,
            placement_policy="tiered_v2",
            tasks=(),
            batch_order=(),
            blocking_diagnostics=blocking,
            unassigned_intents=(),
            ledgers=_ledgers(capacity_drives, ()),
            failures=tuple(stale),
            gate_b_code="INFEASIBLE_UNDER_ADMISSION_BUDGET",
            gate_b_diagnostics={"reason": "target_drive_changed"},
            gate_b_actions=("reconcile_plan", "restore_target_drive_to_plan"),
            derivation_mode=None,
            solver_bound_version=placement.SOLVER_BOUND_VERSION,
        )

    gate = placement.gate_b(solver_inp)
    diag = _diagnostics_for_serialize(gate.diagnostics)
    actions = tuple(gate.actions) if gate.actions else ()
    code = gate.code

    # gate_b_code is the pure Gate-B / structural ladder only — never a reconcile diagnostic
    # (e.g. MANIFEST_POLICY). Reconcile blockers live on blocking_diagnostics; feasible already
    # requires gate_b_code == FEASIBLE and not blocking_diagnostics (evidence levels stay separate).

    if code != "FEASIBLE":
        # Graded non-feasible: no executable tasks.
        # - proven known-budget infeasibility → CAPACITY_*_SHORT compatibility projection
        # - CAPACITY_EVIDENCE_UNKNOWN → typed unknown evidence on failures (fail-closed seam)
        # - structural → one CapacityFailure row so library/CLI projections still see a root cause
        # - packing-inconclusive → no false proven-short failures
        failures: tuple[CapacityFailure, ...] = ()
        if code == "INFEASIBLE_UNDER_ADMISSION_BUDGET":
            failures = _capacity_short_failures(solver_inp, capacity_drives, mode)
        elif code == "CAPACITY_EVIDENCE_UNKNOWN":
            failures = _unknown_evidence_failures(capacity_drives, mode, solver_inp)
        elif code in {
            "TARGET_TIER_MISSING", "UNPROVEN_PROVENANCE", "REQUIREMENT_EXCEEDS_USABLE_MAX",
            "FAILURE_DOMAIN_UNSATISFIABLE", "GRAPH_DEPENDENCY_INVARIANT",
        }:
            failures = _structural_failure_projection(code, gate, mode, capacity_drives)
        return CapacityPlan(
            mode=mode,
            placement_policy="tiered_v2",
            tasks=(),
            batch_order=(),
            blocking_diagnostics=blocking,
            unassigned_intents=(),
            ledgers=_ledgers(capacity_drives, ()),
            failures=failures,
            gate_b_code=code,
            gate_b_diagnostics=diag,
            gate_b_actions=actions,
            derivation_mode=None,
            solver_bound_version=gate.solver_bound_version,
        )

    # FEASIBLE → deterministic improve (emergency monitor is shell-owned; none here yet).
    improved = placement.improve(solver_inp, gate.assignment, emergency=None)
    tasks = _project_assigned_tasks(improved.assignment, result.candidates)
    return CapacityPlan(
        mode=mode,
        placement_policy="tiered_v2",
        tasks=tasks,
        batch_order=_batch_order(tasks, result),
        blocking_diagnostics=blocking,
        unassigned_intents=(),
        ledgers=_ledgers(capacity_drives, tasks),
        failures=(),
        gate_b_code="FEASIBLE",
        gate_b_diagnostics=None,
        gate_b_actions=(),
        derivation_mode=improved.derivation_mode,
        solver_bound_version=improved.solver_bound_version,
    )
