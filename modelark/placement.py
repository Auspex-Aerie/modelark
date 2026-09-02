"""Pure Gate-B feasibility and deterministic ``tiered_v2`` improvement (RFC-002 / DEC-049, issue #38).

Functional core only: no SQLite, filesystem, config loaders, clock, fence, or network. The impure shell
builds :class:`SolverInput` from a consistent fact snapshot plus admission-derived budgets/evidence and
calls :func:`gate_b` / :func:`improve`. Emergency monitors are injected only into :func:`improve`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from modelark import candidates
from modelark.candidates import (
    Candidate,
    CandidateSet,
    CopyRequirement,
    PendingHome,
    RequirementGraph,
    RequirementKind,
    SourceIdentity,
    TaskKind,
)

SOLVER_BOUND_VERSION = "tiered_v2-bound-1"
POLICY_VERSION = "tiered_v2"


# --------------------------------------------------------------------------------------------------
# Types
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DriveEvidenceFact:
    """Admission-derived evidence classification for one drive (not free-space itself)."""
    drive_label: str
    executable: bool
    kind: str                          # "live" | "anchor" | "unknown" | ...
    code: str | None = None


@dataclass(frozen=True)
class SolverBounds:
    feasibility_state_limit: int
    optimization_state_limit: int


@dataclass(frozen=True)
class SuccessorPreference:
    """Proposal-bound preference for one replacement drive.

    ``lane_bytes`` is the predecessor's policy-adjusted capacity envelope.  The
    shell caps the successor's executable budget to this value; the pure solver
    then prefers filling that lane while retaining current approved targets for
    work outside it whenever the higher-order movement objective permits.
    """

    predecessor_drive: str
    successor_drive: str
    lane_bytes: int
    baseline_targets: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SolverInput:
    graph: RequirementGraph
    candidates: CandidateSet
    drives: tuple[candidates.DriveFact, ...]
    executable_budget: tuple[tuple[str, int], ...]          # free-space only
    max_usable_for_epoch: tuple[tuple[str, int | None], ...]
    drive_evidence: tuple[tuple[str, DriveEvidenceFact], ...]
    capacity_mode: str
    policy_version: str
    bounds: SolverBounds
    preference: SuccessorPreference | None = None


@dataclass(frozen=True)
class AssignmentTask:
    requirement_id: str
    task_kind: TaskKind
    target_drive: str
    source: SourceIdentity | None
    depends_on_requirement: str | None
    movement_cost: int
    durable: int
    workspace: int
    missing_files: tuple[str, ...]
    reused_files: tuple[str, ...]
    guaranteed_durable: int
    expected_durable: int
    workspace_peak_guaranteed: int
    workspace_peak_expected: int
    budget_evidence: str


@dataclass(frozen=True)
class CanonicalAssignment:
    """Ordered requirement → task map plus residual free by drive."""
    tasks: tuple[AssignmentTask, ...]
    remaining_free: tuple[tuple[str, int], ...]   # sorted drive → remaining free

    @property
    def by_requirement(self) -> tuple[tuple[str, AssignmentTask], ...]:
        return tuple((t.requirement_id, t) for t in self.tasks)


@dataclass(frozen=True)
class GateBResult:
    code: str
    capacity_mode: str
    assignment: CanonicalAssignment | None
    diagnostics: Any = None
    actions: tuple[str, ...] = ()
    policy_version: str = POLICY_VERSION
    solver_bound_version: str = SOLVER_BOUND_VERSION
    bounds: SolverBounds | None = None
    feasibility_states_visited: int = 0
    relevant_unknown_drives: tuple[str, ...] = ()
    message: str | None = None

    # Compatibility aliases used by some tests
    @property
    def feasibility_state_limit(self) -> int | None:
        return None if self.bounds is None else self.bounds.feasibility_state_limit

    @property
    def optimization_state_limit(self) -> int | None:
        return None if self.bounds is None else self.bounds.optimization_state_limit


@dataclass(frozen=True)
class PlacementResult:
    assignment: CanonicalAssignment
    derivation_mode: str                 # optimized | state_truncated | canonical_fallback
    diagnostic: str | None
    score: tuple
    capacity_mode: str
    policy_version: str = POLICY_VERSION
    solver_bound_version: str = SOLVER_BOUND_VERSION
    bounds: SolverBounds | None = None
    optimization_states_visited: int = 0

    @property
    def feasibility_state_limit(self) -> int | None:
        return None if self.bounds is None else self.bounds.feasibility_state_limit

    @property
    def optimization_state_limit(self) -> int | None:
        return None if self.bounds is None else self.bounds.optimization_state_limit


class DeterministicStateLimit(Exception):
    """Semantic state-count bound exhausted."""

    def __init__(self, *, best: CanonicalAssignment | None, visited: int, score: tuple | None = None):
        self.best = best
        self.visited = visited
        self.score = score
        super().__init__(f"state limit exhausted after {visited} states")


class EmergencyResourceLimit(Exception):
    """Wall-clock/memory emergency (improvement only)."""


# --------------------------------------------------------------------------------------------------
# Budget / evidence maps
# --------------------------------------------------------------------------------------------------
def _as_map(pairs) -> dict:
    return {k: v for k, v in pairs}


def _guaranteed(mode: str) -> bool:
    return mode == "guaranteed" or mode == "uncompressed"


def _cand_durable(c: Candidate, mode: str) -> int:
    return c.budget.guaranteed_durable if _guaranteed(mode) else c.budget.expected_durable


def _cand_workspace(c: Candidate, mode: str) -> int:
    return (c.budget.workspace_peak_guaranteed if _guaranteed(mode)
            else c.budget.workspace_peak_expected)


def _cand_peak(c: Candidate, mode: str) -> int:
    return _cand_durable(c, mode) + _cand_workspace(c, mode)


def _source_key(source) -> tuple:
    if isinstance(source, SourceIdentity):
        return ("source", source.drive_label, source.annex_key, source.orig_sha256)
    if isinstance(source, PendingHome):
        return ("pending", source.requirement_id)
    return ("none",)


def _same_failure_domain(a: candidates.DriveFact, b: candidates.DriveFact) -> bool:
    return any(
        x and y and x == y
        for x, y in (
            (a.fs_uuid, b.fs_uuid),
            (a.annex_uuid, b.annex_uuid),
            (a.serial, b.serial),
        )
    )


def _drive_map(inp: SolverInput) -> dict[str, candidates.DriveFact]:
    return {d.drive_label: d for d in inp.drives}


def _evidence_map(inp: SolverInput) -> dict[str, DriveEvidenceFact]:
    return {k: v for k, v in inp.drive_evidence}


def _req_by_id(inp: SolverInput) -> dict[str, CopyRequirement]:
    return {r.requirement_id: r for r in inp.graph.desired}


def _cands_by_req(inp: SolverInput) -> dict[str, tuple[Candidate, ...]]:
    return {rid: cs for rid, cs in inp.candidates.by_requirement}


def _satisfied_ids(inp: SolverInput) -> frozenset[str]:
    return frozenset(s.requirement_id for s in inp.candidates.satisfied)


def _blocked_by_id(inp: SolverInput) -> dict[str, candidates.BlockedRequirement]:
    return {b.requirement_id: b for b in inp.candidates.blocked}


# --------------------------------------------------------------------------------------------------
# Structural checks
# --------------------------------------------------------------------------------------------------
def _structural(inp: SolverInput) -> GateBResult | None:
    """Return the first structural failure in deterministic requirement order, or None."""
    reqs = sorted(inp.graph.desired, key=lambda r: r.requirement_id)
    req_ids = {r.requirement_id for r in reqs}
    cands = _cands_by_req(inp)
    blocked = _blocked_by_id(inp)
    satisfied = _satisfied_ids(inp)
    maxima = _as_map(inp.max_usable_for_epoch)
    mode = inp.capacity_mode
    bounds = inp.bounds

    # 1 TARGET_TIER_MISSING / 2 UNPROVEN_PROVENANCE from blocked set
    for req in reqs:
        b = blocked.get(req.requirement_id)
        if b is None:
            continue
        if b.reason == "no_eligible_tier":
            return GateBResult(
                code="TARGET_TIER_MISSING",
                capacity_mode=mode,
                assignment=None,
                diagnostics={
                    "requirement_id": req.requirement_id,
                    "repo_id": req.repo_id,
                    "kind": req.kind.value,
                    "eligible_drives": tuple(req.eligible_drives),
                },
                actions=("add_eligible_drive", "change_plan_policy"),
                bounds=bounds,
                feasibility_states_visited=0,
            )
        if b.reason == "all_targets_unproven":
            drives = tuple(sorted({
                row.drive_label for row in inp.candidates.drift
                if row.requirement_id == req.requirement_id and row.reason == "unproven_provenance"
            }))
            return GateBResult(
                code="UNPROVEN_PROVENANCE",
                capacity_mode=mode,
                assignment=None,
                diagnostics={
                    "requirement_id": req.requirement_id,
                    "repo_id": req.repo_id,
                    "kind": req.kind.value,
                    "drives": drives,
                },
                actions=("repair_or_remove_unproven_rows", "provide_hash_evidence"),
                bounds=bounds,
                feasibility_states_visited=0,
            )

    # 3 GRAPH_DEPENDENCY_INVARIANT — malformed depends_on
    for req in reqs:
        if req.independent_of is not None and req.independent_of not in req_ids:
            return GateBResult(
                code="GRAPH_DEPENDENCY_INVARIANT",
                capacity_mode=mode,
                assignment=None,
                diagnostics={
                    "requirement_id": req.requirement_id,
                    "depends_on": req.independent_of,
                    "invariant": "missing_dependency_reference",
                },
                actions=("inspect_integrity", "reconcile_plan"),
                bounds=bounds,
                feasibility_states_visited=0,
            )
    # cycles (simple DFS)
    visiting: set[str] = set()
    seen: set[str] = set()

    def visit(rid: str) -> bool:
        if rid in seen:
            return False
        if rid in visiting:
            return True
        visiting.add(rid)
        dep = _req_by_id(inp).get(rid)
        if dep and dep.independent_of and visit(dep.independent_of):
            return True
        visiting.discard(rid)
        seen.add(rid)
        return False

    for req in reqs:
        if visit(req.requirement_id):
            return GateBResult(
                code="GRAPH_DEPENDENCY_INVARIANT",
                capacity_mode=mode,
                assignment=None,
                diagnostics={
                    "requirement_id": req.requirement_id,
                    "depends_on": req.independent_of,
                    "invariant": "dependency_cycle",
                },
                actions=("inspect_integrity", "reconcile_plan"),
                bounds=bounds,
                feasibility_states_visited=0,
            )

    # 4 FAILURE_DOMAIN_UNSATISFIABLE — pending home+replica with no domain-separated pair
    dmap = _drive_map(inp)
    for req in reqs:
        if req.kind != RequirementKind.PROTECTED_REPLICA:
            continue
        if req.requirement_id in satisfied or req.requirement_id in blocked:
            continue
        home_id = req.independent_of
        if home_id is None or home_id not in req_ids:
            continue
        home = _req_by_id(inp)[home_id]
        # Home already satisfied: check replica targets vs satisfying drives
        home_targets: list[str] = []
        for sat in inp.candidates.satisfied:
            if sat.requirement_id == home_id:
                home_targets = [c.drive_label for c in sat.copies]
                break
        if not home_targets:
            home_targets = list(home.eligible_drives)
        rep_cands = cands.get(req.requirement_id, ())
        if not rep_cands and req.requirement_id not in blocked:
            continue
        if not rep_cands:
            continue
        # If every (home_target, rep_target) pair shares a failure domain → unsatisfiable
        ok_pair = False
        domain_bits: list[str] = []
        for ht in home_targets:
            hd = dmap.get(ht)
            if hd is None:
                continue
            for c in rep_cands:
                rd = dmap.get(c.target_drive)
                if rd is None:
                    continue
                if _same_failure_domain(hd, rd):
                    for attr, val in (("fs_uuid", hd.fs_uuid), ("annex_uuid", hd.annex_uuid),
                                      ("serial", hd.serial)):
                        if val and getattr(rd, attr) == val:
                            domain_bits.append(f"{attr}:{val}")
                    continue
                ok_pair = True
                break
            if ok_pair:
                break
        # Only fire when home is also unsatisfied (both must be placed) OR home is satisfied on
        # domain-colliding drives only.
        home_unsat = home_id not in satisfied and home_id not in blocked
        if not ok_pair and (home_unsat or home_targets):
            # If home unsatisfied and both have candidates but all domain-collide
            if home_unsat or not ok_pair:
                # When home is satisfied on a drive sharing domain with all replica targets
                if not ok_pair and rep_cands:
                    # Check: is there any non-colliding pair with possible homes?
                    any_home = home_targets or list(home.eligible_drives)
                    still_ok = False
                    for ht in any_home:
                        hd = dmap.get(ht)
                        if not hd:
                            continue
                        for c in rep_cands:
                            rd = dmap.get(c.target_drive)
                            if rd and not _same_failure_domain(hd, rd):
                                still_ok = True
                                break
                        if still_ok:
                            break
                    if not still_ok and any_home:
                        return GateBResult(
                            code="FAILURE_DOMAIN_UNSATISFIABLE",
                            capacity_mode=mode,
                            assignment=None,
                            diagnostics={
                                "home_requirement_id": home_id,
                                "replica_requirement_id": req.requirement_id,
                                "domain_evidence": tuple(sorted(set(domain_bits))) or ("shared_identity",),
                            },
                            actions=("add_independent_drive", "change_failure_domain_policy"),
                            bounds=bounds,
                            feasibility_states_visited=0,
                        )

    # 5 REQUIREMENT_EXCEEDS_USABLE_MAX
    for req in reqs:
        if req.requirement_id in satisfied or req.requirement_id in blocked:
            continue
        cs = cands.get(req.requirement_id, ())
        if not cs:
            continue
        # Every candidate must have a known max; each peak must exceed its own target's max
        all_known = True
        all_exceed = True
        peak = 0
        maxima_list: list[tuple[str, int]] = []
        for c in cs:
            mx = maxima.get(c.target_drive)
            if mx is None:
                all_known = False
                break
            p = _cand_peak(c, mode)
            peak = max(peak, p)
            maxima_list.append((c.target_drive, int(mx)))
            if p <= int(mx):
                all_exceed = False
        if all_known and all_exceed and maxima_list:
            return GateBResult(
                code="REQUIREMENT_EXCEEDS_USABLE_MAX",
                capacity_mode=mode,
                assignment=None,
                diagnostics={
                    "requirement_id": req.requirement_id,
                    "repo_id": req.repo_id,
                    "peak_bytes": peak,
                    "maxima": tuple(sorted(maxima_list)),
                },
                actions=("add_larger_drive", "trim_selection", "change_hard_constraints"),
                bounds=bounds,
                feasibility_states_visited=0,
            )

    return None


# --------------------------------------------------------------------------------------------------
# Search
# --------------------------------------------------------------------------------------------------
class _Partial:
    """Mutable search node with push/pop (avoids O(n) dict copies at every depth)."""

    __slots__ = ("assigned", "durable_load", "workspace_peak", "free0",
                 "_undo", "_state_hash", "movement", "used_drives")

    def __init__(self, free0: dict[str, int]):
        self.assigned: dict[str, Candidate] = {}
        self.durable_load: dict[str, int] = {}
        self.workspace_peak: dict[str, int] = {}
        self.free0 = free0
        self._undo: list[tuple] = []
        # Incremental XOR hash of (rid, target, source, kind) — O(1) enter/skip.
        self._state_hash = 0
        self.movement = 0
        self.used_drives: dict[str, int] = {}  # drive → assignment count

    def state_key(self) -> int:
        return self._state_hash

    def fits(self, c: Candidate, mode: str) -> bool:
        d = _cand_durable(c, mode)
        w = _cand_workspace(c, mode)
        lab = c.target_drive
        new_d = self.durable_load.get(lab, 0) + d
        new_w = max(self.workspace_peak.get(lab, 0), w)
        return new_d + new_w <= self.free0.get(lab, 0)

    def _src_key_for(self, c: Candidate) -> tuple:
        src = c.source
        if isinstance(src, PendingHome):
            home_c = self.assigned.get(src.requirement_id)
            if home_c is not None:
                return ("source", home_c.target_drive, None, None)
            return _source_key(src)
        return _source_key(src)

    def push(self, c: Candidate, mode: str) -> None:
        lab = c.target_drive
        old_d = self.durable_load.get(lab, 0)
        old_w = self.workspace_peak.get(lab, 0)
        d = _cand_durable(c, mode)
        w = _cand_workspace(c, mode)
        mv = c.movement_cost.transfer_bytes
        self.assigned[c.requirement_id] = c
        self.durable_load[lab] = old_d + d
        self.workspace_peak[lab] = max(old_w, w)
        self.movement += mv
        self.used_drives[lab] = self.used_drives.get(lab, 0) + 1
        key_part = (c.requirement_id, c.target_drive, self._src_key_for(c), c.task_kind.value)
        part_hash = hash(key_part)
        self._state_hash ^= part_hash
        self._undo.append((c.requirement_id, lab, old_d, old_w, part_hash, mv))

    def pop(self) -> None:
        rid, lab, old_d, old_w, part_hash, mv = self._undo.pop()
        del self.assigned[rid]
        self._state_hash ^= part_hash
        self.movement -= mv
        cnt = self.used_drives.get(lab, 1) - 1
        if cnt <= 0:
            self.used_drives.pop(lab, None)
        else:
            self.used_drives[lab] = cnt
        if old_d == 0:
            self.durable_load.pop(lab, None)
        else:
            self.durable_load[lab] = old_d
        if old_w == 0:
            self.workspace_peak.pop(lab, None)
        else:
            self.workspace_peak[lab] = old_w


def _resolve_source(c: Candidate, partial: _Partial) -> SourceIdentity | None:
    src = c.source
    if isinstance(src, SourceIdentity):
        return src
    if isinstance(src, PendingHome):
        home_c = partial.assigned.get(src.requirement_id)
        if home_c is None:
            return None
        return SourceIdentity(home_c.target_drive, None, None)
    return None


def _domain_ok(c: Candidate, partial: _Partial, dmap: dict[str, candidates.DriveFact],
               req_by_id: dict[str, CopyRequirement]) -> bool:
    """Replica must not share failure domain with its home (assigned or SourceIdentity)."""
    req = req_by_id.get(c.requirement_id)
    if req is None or req.kind != RequirementKind.PROTECTED_REPLICA:
        return True
    home_drive = None
    src = c.source
    if isinstance(src, SourceIdentity):
        home_drive = src.drive_label
    elif isinstance(src, PendingHome):
        home_c = partial.assigned.get(src.requirement_id)
        if home_c is None:
            return True  # not yet resolved; readiness handles order
        home_drive = home_c.target_drive
    if home_drive is None:
        return True
    hd = dmap.get(home_drive)
    rd = dmap.get(c.target_drive)
    if hd is None or rd is None:
        return True
    return not _same_failure_domain(hd, rd)


def _ready_requirements(unsat: list[str], partial: _Partial,
                        req_by_id: dict[str, CopyRequirement]) -> list[str]:
    ready = []
    for rid in unsat:
        if rid in partial.assigned:
            continue
        req = req_by_id[rid]
        if req.independent_of and req.independent_of not in partial.assigned:
            # Home must be assigned (unless home is already satisfied outside unsat)
            # If home is not in unsat list, it's satisfied — replica is ready
            if req.independent_of in unsat or req.independent_of in partial.assigned:
                if req.independent_of not in partial.assigned:
                    continue
        ready.append(rid)
    return ready


def _static_order_keys(inp: SolverInput, unsat: list[str], free0: dict[str, int]
                       ) -> dict[str, tuple]:
    """Root static keys: constrainedness, peak, requirement_id."""
    mode = inp.capacity_mode
    cands = _cands_by_req(inp)
    keys = {}
    for rid in unsat:
        cs = cands.get(rid, ())
        alone = 0
        peak = 0
        for c in cs:
            p = _cand_peak(c, mode)
            peak = max(peak, p)
            if p <= free0.get(c.target_drive, 0):
                alone += 1
        keys[rid] = (alone, -peak, rid)
    return keys


def _to_assignment(partial: _Partial, inp: SolverInput) -> CanonicalAssignment:
    mode = inp.capacity_mode
    tasks = []
    for rid in sorted(partial.assigned):
        c = partial.assigned[rid]
        src = _resolve_source(c, partial)
        # If still PendingHome unresolved, leave None (shouldn't complete)
        if isinstance(c.source, PendingHome) and src is None:
            src = None
        tasks.append(AssignmentTask(
            requirement_id=rid,
            task_kind=c.task_kind,
            target_drive=c.target_drive,
            source=src,
            depends_on_requirement=c.depends_on_requirement,
            movement_cost=c.movement_cost.transfer_bytes,
            durable=_cand_durable(c, mode),
            workspace=_cand_workspace(c, mode),
            missing_files=tuple(m.rfilename for m in c.missing_files),
            reused_files=tuple(r.rfilename for r in c.reused_files),
            guaranteed_durable=c.budget.guaranteed_durable,
            expected_durable=c.budget.expected_durable,
            workspace_peak_guaranteed=c.budget.workspace_peak_guaranteed,
            workspace_peak_expected=c.budget.workspace_peak_expected,
            budget_evidence=(c.budget.file_budgets[0].evidence if c.budget.file_budgets else "estimate"),
        ))
    remaining = []
    for lab in sorted(partial.free0):
        rem = (partial.free0[lab]
               - partial.durable_load.get(lab, 0)
               - partial.workspace_peak.get(lab, 0))
        remaining.append((lab, rem))
    return CanonicalAssignment(tuple(tasks), tuple(remaining))


def _score(assignment: CanonicalAssignment, free0: dict[str, int],
           candidate_targets: frozenset[str],
           preference: SuccessorPreference | None = None) -> tuple:
    """Lex score exposed as (movement, free_vec↓, idle, canon); lower movement/idle/canon wins,
    higher free_vec wins (compare via :func:`_score_key`)."""
    movement = sum(t.movement_cost for t in assignment.tasks)
    loads_d: dict[str, int] = {}
    loads_w: dict[str, int] = {}
    used: set[str] = set()
    for t in assignment.tasks:
        loads_d[t.target_drive] = loads_d.get(t.target_drive, 0) + t.durable
        loads_w[t.target_drive] = max(loads_w.get(t.target_drive, 0), t.workspace)
        used.add(t.target_drive)
    free_vec = []
    for lab in sorted(candidate_targets):
        rem = free0.get(lab, 0) - loads_d.get(lab, 0) - loads_w.get(lab, 0)
        free_vec.append(rem)
    free_vec.sort(reverse=True)
    idle = sum(1 for lab in candidate_targets if lab not in used)
    canon = tuple(
        (t.requirement_id, t.task_kind.value, t.target_drive,
         _source_key(t.source), t.missing_files)
        for t in sorted(assignment.tasks, key=lambda x: x.requirement_id)
    )
    if preference is None:
        return (movement, tuple(free_vec), idle, canon)
    preferred_load = loads_d.get(preference.successor_drive, 0)
    preference_gap = max(0, int(preference.lane_bytes) - preferred_load)
    baseline = dict(preference.baseline_targets)
    changed = [
        t for t in assignment.tasks
        if baseline.get(t.requirement_id) not in (None, t.target_drive)
    ]
    changed_bytes = sum(t.durable for t in changed)
    return (
        movement,
        preference_gap,
        len(changed),
        changed_bytes,
        tuple(free_vec),
        idle,
        canon,
    )


def _score_key(score: tuple) -> tuple:
    """Lexicographic minimization key: movement ≻ −free_vec ≻ idle ≻ canon."""
    if len(score) == 4:
        movement, free_vec, idle, canon = score
        return (movement, tuple(-x for x in free_vec), idle, canon)
    movement, preference_gap, changed, changed_bytes, free_vec, idle, canon = score
    return (
        movement,
        preference_gap,
        changed,
        changed_bytes,
        tuple(-x for x in free_vec),
        idle,
        canon,
    )


def _candidate_sort_key(
    c: Candidate,
    preference: SuccessorPreference | None = None,
) -> tuple:
    """Deterministic candidate order independent of CandidateSet input permutation."""
    baseline = dict(preference.baseline_targets) if preference is not None else {}
    if preference is not None and c.target_drive == preference.successor_drive:
        target_preference = 0
    elif baseline.get(c.requirement_id) == c.target_drive:
        target_preference = 1
    else:
        target_preference = 2
    return (
        target_preference,
        c.target_drive,
        c.movement_cost.transfer_bytes,
        _source_key(c.source),
        c.task_kind.value,
        tuple(m.rfilename for m in c.missing_files),
    )


def _search(
    inp: SolverInput,
    free0: dict[str, int],
    state_limit: int,
    *,
    collect_all: bool = False,
    emergency: Callable | None = None,
) -> tuple[str, CanonicalAssignment | None, int, CanonicalAssignment | None, tuple | None]:
    """Iterative DFS search (explicit stack — requirement depth can exceed sys recursion limit).

    Returns (kind, first_or_none, visited, best_or_none, best_score)
    kind: found | bound_exhausted | infeasible
    """
    mode = inp.capacity_mode
    cands = _cands_by_req(inp)
    req_by_id = _req_by_id(inp)
    dmap = _drive_map(inp)
    satisfied = _satisfied_ids(inp)
    blocked = _blocked_by_id(inp)

    unsat = sorted(
        r.requirement_id for r in inp.graph.desired
        if r.requirement_id not in satisfied and r.requirement_id not in blocked
    )
    if not unsat:
        empty = _Partial(free0)
        visited = 1
        asn = _to_assignment(empty, inp)
        return "found", asn, visited, asn, _score(
            asn, free0, frozenset(free0), inp.preference)

    order_keys = _static_order_keys(inp, unsat, free0)
    cand_targets = frozenset(
        c.target_drive for rid in unsat for c in cands.get(rid, ())
    )
    # Pre-sort candidate lists once (canonical order independent of input permutation).
    sorted_cands = {
        rid: tuple(sorted(
            cands.get(rid, ()),
            key=lambda candidate: _candidate_sort_key(candidate, inp.preference),
        ))
        for rid in unsat
    }

    visited = 0
    seen: set[int] = set()
    first: CanonicalAssignment | None = None
    best: CanonicalAssignment | None = None
    best_score: tuple | None = None
    exhausted = False
    partial = _Partial(free0)

    def enter() -> str:
        nonlocal visited, exhausted
        key = partial.state_key()
        if key in seen:
            return "skip"
        if visited >= state_limit:
            exhausted = True
            return "exhaust"
        seen.add(key)
        visited += 1
        if emergency is not None:
            try:
                emergency(visited)
            except EmergencyResourceLimit:
                raise
            except Exception as exc:
                raise EmergencyResourceLimit() from exc
        return "ok"

    unsat_set = set(unsat)

    def expand() -> list[Candidate]:
        if len(partial.assigned) == len(unsat_set):
            return []
        # Ready = unassigned whose dependency is assigned (or satisfied outside).
        ready = []
        for rid in unsat:
            if rid in partial.assigned:
                continue
            req = req_by_id[rid]
            dep = req.independent_of
            if dep is not None and dep not in partial.assigned and dep not in satisfied:
                continue
            ready.append(rid)
        if not ready:
            return []
        ready.sort(key=lambda rid: order_keys[rid])
        rid = ready[0]
        out: list[Candidate] = []
        for c in sorted_cands.get(rid, ()):
            if not partial.fits(c, mode):
                continue
            if not _domain_ok(c, partial, dmap, req_by_id):
                continue
            if isinstance(c.source, PendingHome) and c.source.requirement_id not in partial.assigned:
                if c.source.requirement_id not in satisfied:
                    continue
            out.append(c)
        return out

    # Precompute sorted candidate-target list for free-vector (stable order).
    cand_target_list = tuple(sorted(cand_targets))

    def _score_partial() -> tuple:
        """Score current partial using incremental loads; canon only when needed for ties."""
        free_vec = tuple(sorted(
            (free0.get(lab, 0)
             - partial.durable_load.get(lab, 0)
             - partial.workspace_peak.get(lab, 0)
             for lab in cand_target_list),
            reverse=True,
        ))
        idle = len(cand_target_list) - len(partial.used_drives)
        # Canon: full identity tuple — only built when comparing equals on higher keys would need it.
        # Always build for correctness of lex order (cheap relative to materializing tasks).
        canon_parts = tuple(
            (
                rid,
                c.task_kind.value,
                c.target_drive,
                _source_key(_resolve_source(c, partial)),
                tuple(m.rfilename for m in c.missing_files),
            )
            for rid, c in sorted(partial.assigned.items())
        )
        if inp.preference is None:
            return (partial.movement, free_vec, idle, canon_parts)
        preference_gap = max(
            0,
            int(inp.preference.lane_bytes)
            - partial.durable_load.get(inp.preference.successor_drive, 0),
        )
        baseline = dict(inp.preference.baseline_targets)
        changed = [
            c for rid, c in partial.assigned.items()
            if baseline.get(rid) not in (None, c.target_drive)
        ]
        changed_bytes = sum(_cand_durable(c, mode) for c in changed)
        return (
            partial.movement,
            preference_gap,
            len(changed),
            changed_bytes,
            free_vec,
            idle,
            canon_parts,
        )

    # Snapshots of assigned maps — materialize CanonicalAssignment only once at the end.
    first_snap: dict[str, Candidate] | None = None
    best_snap: dict[str, Candidate] | None = None

    def record_complete() -> bool:
        """Record a complete assignment. Returns True if search should stop (feasibility only)."""
        nonlocal first_snap, best_snap, best_score
        sc = _score_partial()
        snap = dict(partial.assigned)
        if first_snap is None:
            first_snap = snap
            best_snap = snap
            best_score = sc
        elif _score_key(sc) < _score_key(best_score):  # type: ignore[arg-type]
            best_snap = snap
            best_score = sc
        return not collect_all

    status = enter()
    if status == "exhaust":
        return "bound_exhausted", None, visited, None, None

    # Frames: remaining candidates to try at this depth (reversed for pop).
    # On backtrack we pop the candidate that was pushed onto partial.
    stack: list[list[Candidate]] = [list(reversed(expand()))]
    pushed: list[Candidate] = []  # parallel stack of live assignments

    while stack and not exhausted:
        remaining_cs = stack[-1]
        if not remaining_cs:
            stack.pop()
            if pushed:
                partial.pop()
                pushed.pop()
            continue

        c = remaining_cs.pop()
        partial.push(c, mode)
        status = enter()
        if status == "exhaust":
            partial.pop()
            break
        if status == "skip":
            partial.pop()
            continue

        if all(rid in partial.assigned for rid in unsat):
            stop = record_complete()
            partial.pop()
            if stop:
                stack.clear()
                break
            continue

        child_cs = expand()
        if not child_cs:
            # Dead end
            partial.pop()
            continue
        pushed.append(c)
        stack.append(list(reversed(child_cs)))

    def _materialize(snap: dict[str, Candidate] | None) -> CanonicalAssignment | None:
        if snap is None:
            return None
        # Temporarily install snapshot into a fresh partial for _to_assignment.
        tmp = _Partial(free0)
        for rid in sorted(snap):
            tmp.push(snap[rid], mode)
        return _to_assignment(tmp, inp)

    first = _materialize(first_snap)
    best = _materialize(best_snap)

    if exhausted:
        return "bound_exhausted", first, visited, best, best_score
    if first is not None:
        return "found", first, visited, best, best_score
    return "infeasible", None, visited, None, None


def _unsat_candidate_targets(inp: SolverInput) -> tuple[str, ...]:
    cands = _cands_by_req(inp)
    satisfied = _satisfied_ids(inp)
    blocked = _blocked_by_id(inp)
    labs: set[str] = set()
    for r in inp.graph.desired:
        rid = r.requirement_id
        if rid in satisfied or rid in blocked:
            continue
        for c in cands.get(rid, ()):
            labs.add(c.target_drive)
    return tuple(sorted(labs))


def _relevant_unknowns(inp: SolverInput) -> tuple[str, ...]:
    """Non-executable candidate targets for unsatisfied requirements (evidence-class unknowns)."""
    evidence = _evidence_map(inp)
    relevant = []
    for lab in _unsat_candidate_targets(inp):
        ev = evidence.get(lab)
        if ev is not None and not ev.executable:
            relevant.append(lab)
    return tuple(relevant)


def _targets_with_unknown_max(inp: SolverInput) -> tuple[str, ...]:
    """Candidate targets whose max_usable_for_epoch is unknown (None)."""
    maxima = _as_map(inp.max_usable_for_epoch)
    return tuple(lab for lab in _unsat_candidate_targets(inp) if maxima.get(lab) is None)


def gate_b(inp: SolverInput) -> GateBResult:
    """Pure Gate-B ladder."""
    mode = inp.capacity_mode
    bounds = inp.bounds
    structural = _structural(inp)
    if structural is not None:
        return structural

    free_map = _as_map(inp.executable_budget)
    evidence = _evidence_map(inp)
    # Zero free for non-executable is already expected; trust maps
    for lab, ev in evidence.items():
        if not ev.executable:
            free_map[lab] = 0

    kind, first, visited, _best, _sc = _search(
        inp, free_map, bounds.feasibility_state_limit, collect_all=False,
    )
    if kind == "found":
        return GateBResult(
            code="FEASIBLE", capacity_mode=mode, assignment=first,
            bounds=bounds, feasibility_states_visited=visited,
        )
    if kind == "bound_exhausted":
        return GateBResult(
            code="PACKING_INCONCLUSIVE", capacity_mode=mode, assignment=None,
            bounds=bounds, feasibility_states_visited=visited,
            relevant_unknown_drives=_relevant_unknowns(inp),
            diagnostics={"reason": "feasibility_state_limit", "visited": visited},
            actions=("retry_higher_bound", "trim_selection"),
        )

    # Known-search infeasible
    relevant = _relevant_unknowns(inp)
    unknown_max = _targets_with_unknown_max(inp)
    if not relevant and not unknown_max:
        return GateBResult(
            code="INFEASIBLE_UNDER_ADMISSION_BUDGET", capacity_mode=mode, assignment=None,
            bounds=bounds, feasibility_states_visited=visited,
            diagnostics={"reason": "known_exhaustive_infeasible"},
            actions=("add_admissible_capacity", "trim_selection", "change_capacity_mode"),
            message="proven infeasible under known admission budgets; free known capacity may still help",
        )

    # Unknown max blocks optimistic proof (and non-executable without max cannot be assumed).
    if unknown_max:
        named = tuple(sorted(set(relevant) | set(unknown_max)))
        return GateBResult(
            code="CAPACITY_EVIDENCE_UNKNOWN", capacity_mode=mode, assignment=None,
            bounds=bounds, feasibility_states_visited=visited,
            relevant_unknown_drives=relevant,
            diagnostics={"reason": "unknown_may_help", "drives": named},
            actions=("mount_or_reconcile_drive", "retry_preview"),
            message="known budgets cannot fit; named unknown drives could change the answer",
        )

    # Optimistic search: treat non-executable relevant drives as free=max_usable.
    opt_free = dict(free_map)
    maxima = _as_map(inp.max_usable_for_epoch)
    for lab in relevant:
        mx = maxima.get(lab)
        if mx is not None:
            opt_free[lab] = int(mx)

    okind, ofirst, ovisited, _, _ = _search(
        inp, opt_free, bounds.feasibility_state_limit, collect_all=False,
    )
    total_visited = visited + ovisited
    if okind == "found":
        return GateBResult(
            code="CAPACITY_EVIDENCE_UNKNOWN", capacity_mode=mode, assignment=None,
            bounds=bounds, feasibility_states_visited=total_visited,
            relevant_unknown_drives=relevant,
            diagnostics={"reason": "unknown_may_help", "drives": relevant},
            actions=("mount_or_reconcile_drive", "retry_preview"),
            message="known budgets cannot fit; named unknown drives could change the answer",
        )
    if okind == "bound_exhausted":
        return GateBResult(
            code="PACKING_INCONCLUSIVE", capacity_mode=mode, assignment=None,
            bounds=bounds, feasibility_states_visited=total_visited,
            relevant_unknown_drives=relevant,
            diagnostics={"reason": "optimistic_state_limit", "drives": relevant, "visited": ovisited},
            actions=("retry_higher_bound", "mount_or_reconcile_drive", "trim_selection"),
        )
    return GateBResult(
        code="INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX", capacity_mode=mode, assignment=None,
        bounds=bounds, feasibility_states_visited=total_visited,
        relevant_unknown_drives=relevant,
        diagnostics={
            "reason": "optimistic_exhaustive_infeasible",
            "drives": relevant,
            "note": "freeing known capacity may still help",
        },
        actions=("free_known_capacity", "add_capacity", "trim_selection", "change_hard_constraints"),
        message="resolving unknown evidence alone cannot help; freeing known capacity may still help",
    )


def improve(
    inp: SolverInput,
    first_feasible: CanonicalAssignment,
    *,
    emergency: Callable | None = None,
) -> PlacementResult:
    """Deterministic tiered_v2 improvement over a first-feasible assignment."""
    mode = inp.capacity_mode
    bounds = inp.bounds
    free_map = _as_map(inp.executable_budget)
    evidence = _evidence_map(inp)
    for lab, ev in evidence.items():
        if not ev.executable:
            free_map[lab] = 0

    cands = _cands_by_req(inp)
    satisfied = _satisfied_ids(inp)
    blocked = _blocked_by_id(inp)
    unsat = [
        r.requirement_id for r in inp.graph.desired
        if r.requirement_id not in satisfied and r.requirement_id not in blocked
    ]
    cand_targets = frozenset(
        c.target_drive for rid in unsat for c in cands.get(rid, ())
    ) or frozenset(free_map)

    first_score = _score(first_feasible, free_map, cand_targets, inp.preference)

    # Emergency is improvement-only and only EmergencyResourceLimit is a semantic fallback.
    # Other exceptions from a monitor must not be swallowed; non-firing monitors are ignored.
    try:
        kind, _first, visited, best, best_score = _search(
            inp, free_map, bounds.optimization_state_limit,
            collect_all=True, emergency=emergency,
        )
    except EmergencyResourceLimit:
        return PlacementResult(
            assignment=first_feasible,
            derivation_mode="canonical_fallback",
            diagnostic="optimization_resource_exhausted",
            score=first_score,
            capacity_mode=mode,
            bounds=bounds,
            optimization_states_visited=0,
        )

    if kind == "bound_exhausted":
        # Use best-so-far if any complete assignment found; else first_feasible
        asn = best if best is not None else first_feasible
        sc = best_score if best_score is not None else first_score
        return PlacementResult(
            assignment=asn,
            derivation_mode="state_truncated",
            diagnostic="optimization_truncated",
            score=sc,
            capacity_mode=mode,
            bounds=bounds,
            optimization_states_visited=visited,
        )

    # Full exploration
    asn = best if best is not None else first_feasible
    sc = best_score if best_score is not None else first_score
    return PlacementResult(
        assignment=asn,
        derivation_mode="optimized",
        diagnostic=None,
        score=sc,
        capacity_mode=mode,
        bounds=bounds,
        optimization_states_visited=visited,
    )
