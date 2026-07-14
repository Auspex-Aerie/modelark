"""Shadow capacity placement and byte ledger for DEC-045 Phase 2.

The legacy fill executor does not consume this module.  It turns Phase-1 work intents into
deterministically assigned tasks, accounts durable and transient bytes once, and returns typed
feasibility evidence suitable for read-only comparison.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence

from modelark import archive_manifest, compress, streamznn, wishlist
from modelark.reconcile import (
    CopyFact,
    DiagnosticSeverity,
    ReconcileResult,
    TaskKind,
    WorkIntent,
)


DEFAULT_FLOAT_RATIO = 0.67
EXPECTED_MARGIN = 1.08
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
    TARGET_TIER_MISSING = "TARGET_TIER_MISSING"
    GRAPH_INVARIANT = "GRAPH_INVARIANT"


@dataclass(frozen=True)
class CapacityDrive:
    drive_label: str
    role: str
    raid_backed: bool
    capacity_bytes: int
    physical_free: int
    free_evidence: FreeEvidence
    safety_floor: int

    @property
    def usable_now(self) -> int:
        return max(0, self.physical_free - self.safety_floor)


@dataclass(frozen=True)
class FileBudget:
    rfilename: str
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
    physical_free: int
    free_evidence: FreeEvidence
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

    @property
    def feasible(self) -> bool:
        return not self.blocking_diagnostics and not self.failures and not self.unassigned_intents

    def to_dict(self) -> dict:
        return {
            "mode": self.mode.value,
            "placement_policy": self.placement_policy,
            "feasible": self.feasible,
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
                    "physical_free": item.physical_free,
                    "free_evidence": item.free_evidence.value,
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
                    "actions": list(item.actions),
                    "blocked_by_requirement": item.blocked_by_requirement,
                }
                for item in self.failures
            ],
        }


def mode_from_legacy(value: str) -> CapacityMode:
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
    live_free_by_drive: Mapping[str, int] | None = None,
) -> tuple[CapacityDrive, ...]:
    """Load safe capacity facts; supplied live free is already net of every physical byte."""
    live_free_by_drive = live_free_by_drive or {}
    archived = dict(con.execute(
        "SELECT drive_label,coalesce(sum(stored_bytes),0) FROM archived GROUP BY drive_label"
    ).fetchall())
    rows = con.execute(
        "SELECT d.drive_label,coalesce(d.role,'primary'),coalesce(d.raid_backed,0),"
        "coalesce(d.capacity_bytes,d.free_bytes,0),coalesce(d.free_bytes,0) "
        "FROM plan_drives pd JOIN drives d USING(drive_label) WHERE pd.plan_id=? "
        "ORDER BY d.drive_label",
        [plan_id],
    ).fetchall()
    facts = []
    for label, role, raid, capacity, snapshot_free in rows:
        if label in live_free_by_drive:
            physical_free = max(0, int(live_free_by_drive[label]))
            evidence = FreeEvidence.LIVE
        else:
            physical_free = max(0, int(snapshot_free or 0) - int(archived.get(label, 0) or 0))
            evidence = FreeEvidence.SNAPSHOT
        facts.append(CapacityDrive(
            drive_label=label,
            role=role,
            raid_backed=bool(raid),
            capacity_bytes=int(capacity or 0),
            physical_free=physical_free,
            free_evidence=evidence,
            safety_floor=safety_floor(int(capacity or 0), bool(raid)),
        ))
    return tuple(facts)


def _fact_by_repo_drive(result: ReconcileResult) -> dict[tuple[str, str], CopyFact]:
    return {(item.repo_id, item.drive_label): item for item in result.facts}


def _expected_file_bytes(item: archive_manifest.ManifestFile, ratio: float) -> int:
    basis = item.size_bytes * ratio if item.storage_action == "compress" else item.size_bytes
    return int(basis * EXPECTED_MARGIN)


def _fetch_budget(
    intent: WorkIntent,
    target: str,
    result: ReconcileResult,
    ratio: float,
    compression_cfg: Mapping[str, object],
    facts: Mapping[tuple[str, str], CopyFact],
) -> TaskBudget:
    manifest = result.manifests[intent.repo_id]
    present = facts.get((intent.repo_id, target))
    present_names = present.present_files if present else frozenset()
    missing = tuple(item for item in manifest if item.rfilename not in present_names)
    file_budgets = []
    chunk_bytes = streamznn.DEFAULT_CHUNK
    for item in missing:
        expected_file = _expected_file_bytes(item, ratio)
        workspace_g = workspace_e = 0
        if item.storage_action == "compress":
            codec = compress.plan_codec(item.size_bytes, dict(compression_cfg))
            if codec != compress.CODEC_RAW:
                output_cap = codec_output_cap(
                    item.size_bytes, codec, stream_chunk_bytes=chunk_bytes
                )
                workspace_g = output_cap
                workspace_e = max(0, item.size_bytes + output_cap - expected_file)
        file_budgets.append(FileBudget(
            rfilename=item.rfilename,
            guaranteed_durable=item.size_bytes,
            expected_durable=expected_file,
            workspace_peak_guaranteed=workspace_g,
            workspace_peak_expected=workspace_e,
            evidence="estimate",
        ))
    guaranteed = sum(item.guaranteed_durable for item in file_budgets)
    expected = sum(item.expected_durable for item in file_budgets)
    workspace_g = max((item.workspace_peak_guaranteed for item in file_budgets), default=0)
    workspace_e = max((item.workspace_peak_expected for item in file_budgets), default=0)
    return TaskBudget(
        task_id=intent.task_id,
        requirement_id=intent.requirement_id,
        repo_id=intent.repo_id,
        kind=intent.kind,
        target_drive=target,
        source_drive=None,
        missing_files=tuple(item.rfilename for item in missing),
        file_budgets=tuple(file_budgets),
        guaranteed_durable=guaranteed,
        expected_durable=expected,
        workspace_peak_guaranteed=workspace_g,
        workspace_peak_expected=workspace_e,
        evidence="estimate",
    )


def _replica_budget(
    intent: WorkIntent,
    target: str,
    result: ReconcileResult,
    ratio: float,
    facts: Mapping[tuple[str, str], CopyFact],
) -> TaskBudget:
    manifest = result.manifests[intent.repo_id]
    target_fact = facts.get((intent.repo_id, target))
    present = target_fact.present_files if target_fact else frozenset()
    missing = tuple(item for item in manifest if item.rfilename not in present)
    source_fact = facts.get((intent.repo_id, intent.source_drive or ""))
    source_sizes = dict(source_fact.stored_bytes_by_file) if source_fact is not None else {}
    exact = bool(
        source_fact is not None
        and source_fact.complete
        and all(source_sizes.get(item.rfilename, 0) > 0 or item.size_bytes == 0 for item in missing)
    )
    file_budgets = tuple(
        FileBudget(
            rfilename=item.rfilename,
            guaranteed_durable=(
                int(source_sizes[item.rfilename]) if exact else _expected_file_bytes(item, ratio)
            ),
            expected_durable=(
                int(source_sizes[item.rfilename]) if exact else _expected_file_bytes(item, ratio)
            ),
            workspace_peak_guaranteed=(
                int(source_sizes[item.rfilename]) if exact else _expected_file_bytes(item, ratio)
            ),
            workspace_peak_expected=(
                int(source_sizes[item.rfilename]) if exact else _expected_file_bytes(item, ratio)
            ),
            evidence="exact" if exact else "estimate",
        )
        for item in missing
    )
    durable = sum(item.guaranteed_durable for item in file_budgets)
    return TaskBudget(
        task_id=intent.task_id,
        requirement_id=intent.requirement_id,
        repo_id=intent.repo_id,
        kind=intent.kind,
        target_drive=target,
        source_drive=intent.source_drive,
        missing_files=tuple(item.rfilename for item in missing),
        file_budgets=file_budgets,
        guaranteed_durable=durable,
        expected_durable=durable,
        workspace_peak_guaranteed=durable,
        workspace_peak_expected=durable,
        evidence="exact" if exact else "estimate",
    )


def _drive_tier(drive: CapacityDrive) -> str:
    if drive.role == "replica":
        return "replica"
    return "raid_home" if drive.raid_backed else "primary"


def preflight_file(
    drive: CapacityDrive,
    file_budget: FileBudget,
    mode: CapacityMode,
    *,
    requirement_id: str | None = None,
    task_id: str = "file-preflight",
) -> CapacityFailure | None:
    """Fresh-operation guard; callers replace ``physical_free`` with current live evidence."""
    durable = file_budget.durable_for(mode)
    workspace = file_budget.workspace_for(mode)
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
        actions=("free_target_space", "add_eligible_drive", "replan"),
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
        actions=("expand_eligible_tier", "trim_selection", "change_capacity_mode"),
        blocked_by_requirement=intent.depends_on_requirement,
    )


def _ledgers(drives: Sequence[CapacityDrive], tasks: Sequence[AssignedTask]) -> tuple[DriveLedger, ...]:
    out = []
    for drive in drives:
        budgets = [item.budget for item in tasks if item.target_drive == drive.drive_label]
        out.append(DriveLedger(
            drive_label=drive.drive_label,
            physical_free=drive.physical_free,
            free_evidence=drive.free_evidence,
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


def plan_capacity(
    con,
    result: ReconcileResult,
    *,
    provisioning: str = "uncompressed",
    live_free_by_drive: Mapping[str, int] | None = None,
    compression_cfg: Mapping[str, object] | None = None,
) -> CapacityPlan:
    """Materialize deterministic ``tiered_v1`` assignments and shadow feasibility."""
    mode = mode_from_legacy(provisioning)
    drives = inspect_drives(con, result.plan_id, live_free_by_drive=live_free_by_drive)
    drive_by_label = {item.drive_label: item for item in drives}
    facts = _fact_by_repo_drive(result)
    ratio = plan_float_ratio(con)
    compression_cfg = dict(compression_cfg or wishlist.compression())
    placement = _Placement(drives, mode)
    failures: list[CapacityFailure] = []
    unassigned: list[WorkIntent] = []

    fetch_intents = [item for item in result.intents if item.kind == TaskKind.FETCH]
    homes = [item for item in fetch_intents
             if item.requirement_id.startswith("protected_home:")]
    bulk = [item for item in fetch_intents if item.requirement_id.startswith("primary:")]

    # Protected partials remain pinned. All other protected homes share the distinguished
    # largest-usable eligible home, preserving the legacy single-home policy.
    for intent in sorted(homes, key=lambda item: item.requirement_id):
        eligible = [drive_by_label[label] for label in intent.eligible_drives if label in drive_by_label]
        if intent.pinned_target in drive_by_label:
            target = intent.pinned_target
        elif intent.pinned_target:
            # Durable partials may never be silently re-homed. A stale pin is an
            # unassigned typed failure, never a task omitted from every drive ledger.
            target = None
        elif eligible:
            target = sorted(
                eligible,
                key=lambda item: (-item.usable_now, -item.capacity_bytes, item.drive_label),
            )[0].drive_label
        else:
            target = None
        candidates = ([] if target is None else [
            _fetch_budget(intent, target, result, ratio, compression_cfg, facts)
        ])
        if candidates:
            placement.add(intent, candidates[0])
        else:
            unassigned.append(intent)
            failures.append(_failure_for_unassigned(intent, candidates, placement))

    # Durable partials are not repacked. Unpinned bulk uses RAID-first FFD, then largest primaries.
    pinned_bulk = [item for item in bulk if item.pinned_target]
    free_bulk = [item for item in bulk if not item.pinned_target]
    for intent in sorted(pinned_bulk, key=lambda item: item.requirement_id):
        candidates = [
            _fetch_budget(intent, intent.pinned_target, result, ratio, compression_cfg, facts)
        ] if intent.pinned_target in drive_by_label else []
        if candidates and placement.fits(intent.pinned_target, candidates[0]):
            placement.add(intent, candidates[0])
        else:
            unassigned.append(intent)
            failures.append(_failure_for_unassigned(intent, candidates, placement))

    primary_order = sorted(
        (item for item in drives if item.role == "primary"),
        key=lambda item: (0 if item.raid_backed else 1, -item.capacity_bytes, item.drive_label),
    )
    sized_bulk = []
    for intent in free_bulk:
        candidates = [
            _fetch_budget(intent, drive.drive_label, result, ratio, compression_cfg, facts)
            for drive in primary_order if drive.drive_label in intent.eligible_drives
        ]
        largest = max((item.durable_for(mode) for item in candidates), default=0)
        sized_bulk.append((intent, candidates, largest))
    for intent, candidates, _ in sorted(
        sized_bulk, key=lambda item: (-item[2], item[0].requirement_id)
    ):
        chosen = next((item for item in candidates if placement.fits(item.target_drive, item)), None)
        if chosen:
            placement.add(intent, chosen)
        else:
            unassigned.append(intent)
            failures.append(_failure_for_unassigned(intent, candidates, placement))

    replica_intents = [item for item in result.intents if item.kind == TaskKind.REPLICATE]
    pinned_replica = [item for item in replica_intents if item.pinned_target]
    free_replica = [item for item in replica_intents if not item.pinned_target]
    for intent in sorted(pinned_replica, key=lambda item: item.requirement_id):
        candidates = [
            _replica_budget(intent, intent.pinned_target, result, ratio, facts)
        ] if intent.pinned_target in drive_by_label else []
        if candidates and placement.fits(intent.pinned_target, candidates[0]):
            placement.add(intent, candidates[0])
        else:
            unassigned.append(intent)
            failures.append(_failure_for_unassigned(intent, candidates, placement))

    replica_drives = sorted(
        (item for item in drives if item.role == "replica"),
        key=lambda item: (item.capacity_bytes, item.drive_label),
    )
    replica_candidates = {
        intent.requirement_id: [
            _replica_budget(intent, drive.drive_label, result, ratio, facts)
            for drive in replica_drives if drive.drive_label in intent.eligible_drives
        ]
        for intent in free_replica
    }
    # Prefer the smallest single target that can accept the complete remaining replica set.
    group_target = None
    for drive in replica_drives:
        budgets = [
            next((item for item in replica_candidates[intent.requirement_id]
                  if item.target_drive == drive.drive_label), None)
            for intent in free_replica
        ]
        if all(item is not None for item in budgets):
            durable, workspace = placement.totals(drive.drive_label)
            durable += sum(item.durable_for(mode) for item in budgets if item is not None)
            workspace = max(
                [workspace, *(item.workspace_for(mode) for item in budgets if item is not None)]
            )
            if durable + workspace <= drive.usable_now:
                group_target = drive.drive_label
                break
    if group_target:
        for intent in sorted(free_replica, key=lambda item: item.requirement_id):
            budget = next(
                item for item in replica_candidates[intent.requirement_id]
                if item.target_drive == group_target
            )
            placement.add(intent, budget)
    else:
        sized_replica = []
        for intent in free_replica:
            candidates = replica_candidates[intent.requirement_id]
            largest = max((item.durable_for(mode) for item in candidates), default=0)
            sized_replica.append((intent, candidates, largest))
        for intent, candidates, _ in sorted(
            sized_replica, key=lambda item: (-item[2], item[0].requirement_id)
        ):
            chosen = next((item for item in candidates if placement.fits(item.target_drive, item)), None)
            if chosen:
                placement.add(intent, chosen)
            else:
                unassigned.append(intent)
                failures.append(_failure_for_unassigned(intent, candidates, placement))

    # A forced protected-home assignment can exceed its tier; report it after complete ledger math.
    ledgers = _ledgers(drives, placement.tasks)
    failed_requirements = {item.requirement_id for item in failures}
    for ledger in ledgers:
        required = ledger.required_peak(mode)
        if required <= ledger.usable_now:
            continue
        tasks = [item for item in placement.tasks if item.target_drive == ledger.drive_label]
        roots = [item for item in tasks if item.requirement_id not in failed_requirements]
        if not roots:
            continue
        durable = (ledger.guaranteed_durable if mode == CapacityMode.GUARANTEED
                   else ledger.expected_durable)
        workspace = (ledger.workspace_peak_guaranteed if mode == CapacityMode.GUARANTEED
                     else ledger.workspace_peak_expected)
        code = (FailureCode.CAPACITY_DURABLE_SHORT if durable > ledger.usable_now
                else FailureCode.CAPACITY_WORKSPACE_SHORT)
        drive = drive_by_label[ledger.drive_label]
        failures.append(CapacityFailure(
            code=code,
            capacity_mode=mode,
            requirement_id=roots[0].requirement_id,
            task_ids=tuple(item.task_id for item in roots),
            target_tier=_drive_tier(drive),
            eligible_drives=(ledger.drive_label,),
            required_bytes=required,
            available_bytes=ledger.usable_now,
            safety_floor_bytes=ledger.safety_floor,
            workspace_bytes=workspace,
            shortfall_bytes=required - ledger.usable_now,
            evidence=ledger.free_evidence,
            actions=("expand_eligible_tier", "trim_selection", "change_capacity_mode"),
        ))
        failed_requirements.update(item.requirement_id for item in roots)

    root_failures = {item.requirement_id for item in failures}
    failures = [
        item for item in failures
        if not item.blocked_by_requirement or item.blocked_by_requirement not in root_failures
    ]
    placement.tasks.sort(key=lambda item: (item.target_drive, item.kind.value, item.requirement_id))
    failures.sort(key=lambda item: (item.code.value, item.requirement_id or ""))
    return CapacityPlan(
        mode=mode,
        placement_policy="tiered_v1",
        tasks=tuple(placement.tasks),
        batch_order=_batch_order(placement.tasks, result),
        blocking_diagnostics=tuple(sorted({
            item.code for item in result.diagnostics
            if item.severity in {DiagnosticSeverity.BLOCKING, DiagnosticSeverity.ERROR}
        })),
        unassigned_intents=tuple(sorted(unassigned, key=lambda item: item.requirement_id)),
        ledgers=ledgers,
        failures=tuple(failures),
    )
