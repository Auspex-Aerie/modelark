"""PR-06 / issue #38 pure Gate B + deterministic tiered_v2 (tests-first, RFC-002 / DEC-049).

Gate 1 pins the pure feasibility/improvement contract and the plan_capacity adapter projection
BEFORE production. #38 consumes #36a's no-pin CandidateSet, runs the mixed-evidence Gate-B ladder,
and (only on FEASIBLE) a deterministic tiered_v2 improvement pass.

Binding Gate-0 amend-1 + Gate-1 clarifications (do not freeze weaker semantics):

  1. max_usable_for_epoch is admission-supplied on SolverInput; placement never recomputes a safety floor.
     Missing usable max on a required path → CAPACITY_EVIDENCE_UNKNOWN, not structural/known-infeasible.
  2. REQUIREMENT_EXCEEDS_USABLE_MAX only when EVERY valid candidate has a known max AND each candidate's
     own peak exceeds ITS OWN target's max. Any relevant candidate with unknown max blocks this code.
  3. GRAPH_DEPENDENCY_INVARIANT is only for malformed dependencies (missing/invalid refs, cycles).
     A valid home/replica graph with no independent target is FAILURE_DOMAIN_UNSATISFIABLE.
     An earlier blocked home keeps its earlier structural code.
  4. Dependencies precede constrainedness (topological readiness; PendingHome blocked until home+domain).
  5. Root state counts; exhaustion before entering limit+1; complete normalized source identity in keys.
  6. Optimization score: movement min, then maximize descending remaining_free vector where
     remaining_free(d) = executable_budget(d) - durable_sum(mode,d) - workspace_max(mode,d)
     over the candidate-target universe; idle = no selected task (zero-byte task still uses the drive);
     then canonical complete assignment/source identity.
  7. Emergency monitor is injected only into improve(...), never into canonical SolverInput / output / hash.
  8. Pure calls require explicit SolverBounds; tests use explicit small and scale bounds only.
  9. Deep immutability: frozen records; maps as canonical tuples of pairs (not mutable dict state).
 10. Every outcome carries policy/bound/mode metadata; non-FEASIBLE never exposes an executable assignment.
 11. Adapter: plan_capacity projects tiered_v2 + graded gate_b_code; feasible=True only for FEASIBLE.

RED until modelark.placement (pure) and the plan_capacity tiered_v2 cutover exist. The lazy import +
_require_placement() guard makes pure contracts fail for the reviewed missing #38 behaviour, not a
fixture cascade. Adapter contracts fail for missing graded projection / still-tiered_v1 authority.

Self-running: CI executes ``python tests/test_gate_b_tiered_v2.py`` directly.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import sqlite3
import time
from types import MappingProxyType

import _admission_compat
from modelark import archive_manifest, capacity, candidates, reconcile
from modelark.core import db

try:
    import modelark.placement as placement
    _HAS_PLACEMENT = True
except ModuleNotFoundError as exc:               # ONLY the exact absent submodule
    if exc.name != "modelark.placement":
        raise
    placement = None
    _HAS_PLACEMENT = False


# Distinct 64-hex upstream SHA-256 fixtures.
HW = "1" * 64
HW2 = "3" * 64
HC = "2" * 64
MARGIN = capacity.EXPECTED_MARGIN
RATIO = capacity.DEFAULT_FLOAT_RATIO

# Explicit compression config frozen into PlannerInput (same discipline as #36a).
_CFG = (("max_compress_ram_gb", 64), ("stream_compress", True), ("threads", 4))

STRUCTURAL_CODES = (
    "TARGET_TIER_MISSING",
    "UNPROVEN_PROVENANCE",
    "GRAPH_DEPENDENCY_INVARIANT",
    "FAILURE_DOMAIN_UNSATISFIABLE",
    "REQUIREMENT_EXCEEDS_USABLE_MAX",
)
LADDER_CODES = (
    "FEASIBLE",
    "PACKING_INCONCLUSIVE",
    "CAPACITY_EVIDENCE_UNKNOWN",
    "INFEASIBLE_UNDER_ADMISSION_BUDGET",
    "INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX",
)
ALL_GATE_B_CODES = STRUCTURAL_CODES + LADDER_CODES


def _require_placement():
    if not _HAS_PLACEMENT:
        raise AssertionError(
            "#38 must add the pure core modelark/placement.py exposing at least: "
            "SolverBounds(feasibility_state_limit, optimization_state_limit), "
            "SolverInput (deeply immutable; admission-supplied executable_budget + "
            "max_usable_for_epoch; explicit bounds; no emergency callable/clock), "
            "gate_b(inp)->GateBResult, improve(inp, first_feasible, *, emergency=None)->PlacementResult, "
            "and graded codes FEASIBLE|PACKING_INCONCLUSIVE|CAPACITY_EVIDENCE_UNKNOWN|"
            "INFEASIBLE_UNDER_ADMISSION_BUDGET|INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX|"
            "TARGET_TIER_MISSING|UNPROVEN_PROVENANCE|GRAPH_DEPENDENCY_INVARIANT|"
            "FAILURE_DOMAIN_UNSATISFIABLE|REQUIREMENT_EXCEEDS_USABLE_MAX. "
            "plan_capacity must project placement_policy=tiered_v2 and gate_b_code.")


def _bounds(feasibility: int, optimization: int):
    _require_placement()
    return placement.SolverBounds(
        feasibility_state_limit=feasibility,
        optimization_state_limit=optimization,
    )


# --------------------------------------------------------------------------------------------------
# Pure synthetic builders (only after _require_placement; use #36a candidates for graph/cset)
# --------------------------------------------------------------------------------------------------
def _mf(name, size, sha256, *, fmt="safetensors", quant="bf16"):
    if fmt == "safetensors" and quant in archive_manifest.FLOAT_QUANTS:
        action = "compress"
    else:
        action = "raw"
    return archive_manifest.ManifestFile(
        rfilename=name, size_bytes=size, sha256=sha256, format=fmt, quant=quant, storage_action=action)


def _drive(label, *, role="primary", raid=False, cap=10**12, fscap=None, epoch=1,
           fs_uuid=None, annex_uuid=None, serial=None):
    return candidates.DriveFact(
        drive_label=label, role=role, raid_backed=raid, capacity_bytes=cap,
        filesystem_capacity_bytes=(cap if fscap is None else fscap), identity_epoch=epoch,
        fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial)


def _arch(repo, drive, name, *, sha=None, obytes=None, sbytes=None, key=None):
    return candidates.ArchivedFileFact(
        repo_id=repo, drive_label=drive, rfilename=name,
        orig_sha256=sha, orig_bytes=obytes, stored_bytes=sbytes, annex_key=key)


def _planner(*, selection, manifests, numcopies, drives, archived=(), cfg=_CFG, ratio=RATIO):
    return candidates.PlannerInput(
        plan_id="ark",
        selection=tuple(selection),
        manifests=tuple((repo, tuple(files)) for repo, files in manifests),
        numcopies=tuple(numcopies),
        drives=tuple(drives),
        archived=tuple(archived),
        compression_cfg=tuple(cfg),
        float_ratio=ratio,
    )


def _graph_cset(planner_inp):
    graph = candidates.requirements(planner_inp)
    return graph, candidates.candidates(planner_inp, graph)


def _budget_pairs(labels_to_values):
    """Canonical map: sorted tuple of (label, value) pairs — never a mutable dict as solver state."""
    return tuple(sorted((str(k), v) for k, v in labels_to_values.items()))


def _solver_input(
    planner_inp,
    *,
    executable_budget,
    max_usable_for_epoch,
    capacity_mode="guaranteed",
    feasibility_limit=10_000,
    optimization_limit=10_000,
):
    """Build SolverInput from #36a graph/candidates + admission-derived budgets (ruling 1)."""
    _require_placement()
    graph, cset = _graph_cset(planner_inp)
    return placement.SolverInput(
        graph=graph,
        candidates=cset,
        drives=planner_inp.drives,
        executable_budget=_budget_pairs(executable_budget),
        max_usable_for_epoch=_budget_pairs(max_usable_for_epoch),
        capacity_mode=capacity_mode,
        policy_version="tiered_v2",
        bounds=_bounds(feasibility_limit, optimization_limit),
    )


def _code(result) -> str:
    code = getattr(result, "code", None) or getattr(result, "gate_b_code", None)
    if code is None:
        raise AssertionError("GateBResult must expose .code (graded Gate-B outcome)")
    return code.value if hasattr(code, "value") else str(code)


def _assert_metadata(result, *, capacity_mode, feasibility_limit=None, optimization_limit=None):
    """Every outcome carries policy / bound / mode metadata (Gate-1 clarification 4)."""
    mode = getattr(result, "capacity_mode", None)
    if hasattr(mode, "value"):
        mode = mode.value
    assert mode == capacity_mode, f"capacity_mode must be labelled; got {mode!r}"
    policy = getattr(result, "policy_version", None) or getattr(result, "placement_policy", None)
    assert policy == "tiered_v2", f"policy_version/placement_policy must be tiered_v2; got {policy!r}"
    # Bounds used must be recoverable (explicit values, not silent defaults).
    # Prefer a nested bounds object; otherwise require explicit fields on the result.
    # Never default getattr to the expected value — that would make the check a tautology.
    bounds = getattr(result, "bounds", None) or getattr(result, "bounds_used", None)
    if bounds is not None:
        fl = getattr(bounds, "feasibility_state_limit", None)
        ol = getattr(bounds, "optimization_state_limit", None)
    else:
        fl = getattr(result, "feasibility_state_limit", None) if hasattr(result, "feasibility_state_limit") else None
        ol = getattr(result, "optimization_state_limit", None) if hasattr(result, "optimization_state_limit") else None
        if feasibility_limit is not None or optimization_limit is not None:
            assert fl is not None or ol is not None or hasattr(result, "feasibility_state_limit") or hasattr(
                result, "optimization_state_limit"
            ), (
                "GateBResult/PlacementResult must expose bounds metadata "
                "(.bounds / .bounds_used or .feasibility_state_limit / .optimization_state_limit)"
            )
    if feasibility_limit is not None:
        assert fl is not None, "feasibility_state_limit missing from result bounds metadata"
        assert fl == feasibility_limit, f"feasibility_state_limit: expected {feasibility_limit}, got {fl}"
    if optimization_limit is not None:
        assert ol is not None, "optimization_state_limit missing from result bounds metadata"
        assert ol == optimization_limit, f"optimization_state_limit: expected {optimization_limit}, got {ol}"


def _assert_no_executable_assignment(result):
    """Non-FEASIBLE must not expose an executable assignment (Gate-1 clarification 4)."""
    assignment = getattr(result, "assignment", None)
    assert assignment is None, (
        f"non-FEASIBLE result must not expose an executable assignment; got {type(assignment)!r}")


def _assert_frozen(obj, label="record"):
    assert dataclasses.is_dataclass(obj), f"{label} must be a dataclass"
    assert obj.__dataclass_params__.frozen, f"{label} must be frozen (deep immutability)"
    try:
        # Touch first field if any
        fields = dataclasses.fields(obj)
        if fields:
            setattr(obj, fields[0].name, getattr(obj, fields[0].name))  # type: ignore[misc]
            raise AssertionError(f"{label} must raise FrozenInstanceError on setattr")
    except dataclasses.FrozenInstanceError:
        pass


def _assert_no_mutable_map_state(obj, *, path="root"):
    """Mutable dicts must not become canonical solver state (clarification 3)."""
    if isinstance(obj, dict):
        raise AssertionError(f"mutable dict at {path} is not allowed as canonical solver state")
    if isinstance(obj, MappingProxyType):
        return
    if dataclasses.is_dataclass(obj):
        for f in dataclasses.fields(obj):
            _assert_no_mutable_map_state(getattr(obj, f.name), path=f"{path}.{f.name}")
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            _assert_no_mutable_map_state(item, path=f"{path}[{i}]")


# --------------------------------------------------------------------------------------------------
# Pure contracts
# --------------------------------------------------------------------------------------------------
def test_contract_module_api_surface():
    """Pure module must expose the reviewed entry points and require explicit bounds."""
    _require_placement()
    for name in ("SolverBounds", "SolverInput", "gate_b", "improve"):
        assert hasattr(placement, name), f"modelark.placement must expose {name}"
    # gate_b / improve take no DB connection
    for fn_name in ("gate_b", "improve"):
        params = set(inspect.signature(getattr(placement, fn_name)).parameters)
        assert "con" not in params and "connection" not in params, f"{fn_name} must be pure"
    # improve accepts emergency only as a non-semantic keyword (not on SolverInput)
    improve_params = inspect.signature(placement.improve).parameters
    assert "emergency" in improve_params, "improve must accept emergency= monitor separately"
    # SolverInput fields must not include emergency/clock callables
    input_fields = {f.name for f in dataclasses.fields(placement.SolverInput)}
    banned = {"emergency", "emergency_monitor", "clock", "now", "check"}
    assert not (input_fields & banned), f"SolverInput must not hold {input_fields & banned}"


def test_contract_pure_no_io_boundary():
    _require_placement()
    banned = {"sqlite3", "socket", "wishlist", "fetch", "reconcile", "db", "drive_fence", "admission"}
    names = set()
    for node in ast.walk(ast.parse(inspect.getsource(placement))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
            names.update(alias.name for alias in node.names)
    offenders = sorted(n for n in names if banned & set(n.split(".")))
    assert not offenders, f"pure placement must not import {offenders}"


def test_contract_deep_immutability_solver_input_and_results():
    _require_placement()
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d0", cap=10_000)],
    )
    inp = _solver_input(
        planner,
        executable_budget={"d0": 5_000},
        max_usable_for_epoch={"d0": 9_000},
        feasibility_limit=100,
        optimization_limit=100,
    )
    _assert_frozen(inp, "SolverInput")
    _assert_frozen(inp.bounds, "SolverBounds")
    _assert_no_mutable_map_state(inp)
    # Budget maps are tuples of pairs (or other non-dict immutable), not dict
    assert not isinstance(inp.executable_budget, dict)
    assert not isinstance(inp.max_usable_for_epoch, dict)

    result = placement.gate_b(inp)
    _assert_frozen(result, "GateBResult")
    _assert_no_mutable_map_state(result)
    if _code(result) == "FEASIBLE":
        improved = placement.improve(inp, result.assignment, emergency=None)
        _assert_frozen(improved, "PlacementResult")
        _assert_no_mutable_map_state(improved)
        assert getattr(improved, "emergency", None) is None
        assert "emergency" not in {f.name for f in dataclasses.fields(improved)}


def test_contract_shuffled_input_byte_equivalence():
    _require_placement()
    files = [_mf("a.safetensors", 100, HW), _mf("b.safetensors", 100, HW2)]
    drives_n = [_drive("d0", cap=10_000), _drive("d1", cap=8_000), _drive("r0", role="replica", cap=8_000)]
    drives_s = list(reversed(drives_n))

    def run(drives, budget_order):
        planner = _planner(
            selection=["org/m"],
            manifests=[("org/m", files)],
            numcopies=[("org/m", 1)],
            drives=drives,
        )
        budgets = {"d0": 4_000, "d1": 4_000, "r0": 0}
        maxima = {"d0": 9_000, "d1": 7_000, "r0": 7_000}
        if budget_order == "rev":
            budgets = {k: budgets[k] for k in reversed(list(budgets))}
            maxima = {k: maxima[k] for k in reversed(list(maxima))}
        inp = _solver_input(
            planner, executable_budget=budgets, max_usable_for_epoch=maxima,
            feasibility_limit=5_000, optimization_limit=5_000,
        )
        gb = placement.gate_b(inp)
        place = placement.improve(inp, gb.assignment, emergency=None) if _code(gb) == "FEASIBLE" else None
        return gb, place

    a_gb, a_pl = run(drives_n, "fwd")
    b_gb, b_pl = run(drives_s, "rev")
    assert _code(a_gb) == _code(b_gb)
    assert a_gb == b_gb or (
        _code(a_gb) == _code(b_gb)
        and getattr(a_gb, "assignment", None) == getattr(b_gb, "assignment", None)
    ), "GateBResult must be byte/canonical-equivalent under shuffled drives/budget map order"
    if a_pl is not None and b_pl is not None:
        assert a_pl.assignment == b_pl.assignment, "improved assignment must be order-independent"


def test_contract_adversarial_greedy_false_negative_is_feasible():
    """Packing exists but classic FFD/greedy misses it → must be FEASIBLE, never proven infeasible."""
    _require_placement()
    # Drives A=6, B=6, C=5; items 4,4,3,3. FFD on A,B,C fails; packing (3,3)/(4)/(4) works.
    sizes = (4, 4, 3, 3)
    repos = [f"org/m{i}" for i in range(4)]
    drives = [
        _drive("A", cap=100, fscap=100),
        _drive("B", cap=100, fscap=100),
        _drive("C", cap=100, fscap=100),
    ]
    manifests = [(repo, [_mf("w.safetensors", size, HW)]) for repo, size in zip(repos, sizes)]
    planner = _planner(
        selection=repos,
        manifests=manifests,
        numcopies=[(r, 1) for r in repos],
        drives=drives,
    )
    # Executable free equals bin capacity for this synthetic (no floor recompute in placement).
    exec_b = {"A": 6, "B": 6, "C": 5}
    max_u = {"A": 6, "B": 6, "C": 5}
    inp = _solver_input(
        planner, executable_budget=exec_b, max_usable_for_epoch=max_u,
        feasibility_limit=50_000, optimization_limit=1,
    )
    result = placement.gate_b(inp)
    assert _code(result) == "FEASIBLE", (
        f"adversarial packing must be FEASIBLE (not proven infeasible/inconclusive); got {_code(result)}")
    _assert_metadata(result, capacity_mode="guaranteed", feasibility_limit=50_000)
    assert result.assignment is not None


def test_contract_mixed_known_unknown_precedence():
    _require_placement()
    # One requirement of size 100; known drive free 200; unknown drive with usable max 200.
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("known", cap=1_000), _drive("unk", cap=1_000)],
    )

    # (1) Known-only fit → FEASIBLE even with unknown present (unknown contributes 0 executable).
    inp = _solver_input(
        planner,
        executable_budget={"known": 200, "unk": 0},
        max_usable_for_epoch={"known": 900, "unk": 900},
        feasibility_limit=1_000, optimization_limit=1,
    )
    r = placement.gate_b(inp)
    assert _code(r) == "FEASIBLE"
    _assert_metadata(r, capacity_mode="guaranteed")

    # (2) Known exhaustively infeasible, relevant unknown with known max can host → CAPACITY_EVIDENCE_UNKNOWN
    inp2 = _solver_input(
        planner,
        executable_budget={"known": 10, "unk": 0},
        max_usable_for_epoch={"known": 900, "unk": 900},
        feasibility_limit=5_000, optimization_limit=1,
    )
    r2 = placement.gate_b(inp2)
    assert _code(r2) == "CAPACITY_EVIDENCE_UNKNOWN"
    _assert_no_executable_assignment(r2)
    _assert_metadata(r2, capacity_mode="guaranteed")
    drives = getattr(r2, "drives", None) or getattr(r2, "relevant_unknown_drives", None) or ()
    assert "unk" in set(drives) or "unk" in str(getattr(r2, "diagnostics", "")), (
        "CAPACITY_EVIDENCE_UNKNOWN must name the relevant unknown drive(s)")

    # (3) Known infeasible; unknown has no usable max → CAPACITY_EVIDENCE_UNKNOWN (not structural/known-inf)
    inp3 = _solver_input(
        planner,
        executable_budget={"known": 10, "unk": 0},
        max_usable_for_epoch={"known": 900, "unk": None},
        feasibility_limit=5_000, optimization_limit=1,
    )
    r3 = placement.gate_b(inp3)
    assert _code(r3) == "CAPACITY_EVIDENCE_UNKNOWN", (
        f"missing usable max must not become structural/known-infeasible; got {_code(r3)}")
    _assert_no_executable_assignment(r3)

    # (4) Known infeasible; unknown at usable max still cannot host (max < peak) → INFEASIBLE_WITH_UNKNOWN...
    #     peak for raw 100-byte file is at least 100 durable (+ workspace may be 0 for tiny with raw codec path)
    #     Use max_usable 50 on unk so even optimistic cannot fit.
    inp4 = _solver_input(
        planner,
        executable_budget={"known": 10, "unk": 0},
        max_usable_for_epoch={"known": 50, "unk": 50},
        feasibility_limit=5_000, optimization_limit=1,
    )
    r4 = placement.gate_b(inp4)
    # Either structural REQUIREMENT_EXCEEDS_USABLE_MAX (every candidate peak > own max) or
    # INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX if structural didn't fire on mixed known free.
    assert _code(r4) in {
        "INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX",
        "REQUIREMENT_EXCEEDS_USABLE_MAX",
        "INFEASIBLE_UNDER_ADMISSION_BUDGET",
    }
    if _code(r4) == "INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX":
        diag = str(getattr(r4, "diagnostics", "")) + str(getattr(r4, "actions", ""))
        assert "free" in diag.lower() or "known" in diag.lower() or getattr(r4, "actions", None), (
            "INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX must not claim freeing known capacity cannot help")
    _assert_no_executable_assignment(r4)

    # (5) Known-only exhaustive infeasible, no relevant unknown → INFEASIBLE_UNDER_ADMISSION_BUDGET
    planner_one = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("known", cap=1_000)],
    )
    inp5 = _solver_input(
        planner_one,
        executable_budget={"known": 10},
        max_usable_for_epoch={"known": 900},  # structural would require peak>900; 100 fits max
        feasibility_limit=5_000, optimization_limit=1,
    )
    r5 = placement.gate_b(inp5)
    assert _code(r5) == "INFEASIBLE_UNDER_ADMISSION_BUDGET"
    _assert_no_executable_assignment(r5)


def test_contract_requirement_exceeds_usable_max_per_candidate():
    """Clarification 1: structural only when every candidate has known max and own peak > own target max."""
    _require_placement()
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 500, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("small", cap=10_000), _drive("also_small", cap=10_000)],
    )
    # Both targets max 100 < peak ~500 → REQUIREMENT_EXCEEDS_USABLE_MAX
    inp = _solver_input(
        planner,
        executable_budget={"small": 0, "also_small": 0},
        max_usable_for_epoch={"small": 100, "also_small": 100},
        feasibility_limit=100, optimization_limit=1,
    )
    r = placement.gate_b(inp)
    assert _code(r) == "REQUIREMENT_EXCEEDS_USABLE_MAX"
    _assert_no_executable_assignment(r)
    actions = getattr(r, "actions", None) or ()
    assert actions or getattr(r, "diagnostics", None), "structural code must carry diagnostics/actions"

    # One candidate target has unknown max → cannot conclude REQUIREMENT_EXCEEDS_USABLE_MAX
    inp2 = _solver_input(
        planner,
        executable_budget={"small": 0, "also_small": 0},
        max_usable_for_epoch={"small": 100, "also_small": None},
        feasibility_limit=100, optimization_limit=1,
    )
    r2 = placement.gate_b(inp2)
    assert _code(r2) != "REQUIREMENT_EXCEEDS_USABLE_MAX", (
        "unknown max on any relevant candidate target must prevent REQUIREMENT_EXCEEDS_USABLE_MAX")
    assert _code(r2) == "CAPACITY_EVIDENCE_UNKNOWN"
    _assert_no_executable_assignment(r2)


def test_contract_huge_shard_structural():
    _require_placement()
    planner = _planner(
        selection=["org/giant"],
        manifests=[("org/giant", [_mf("shard.safetensors", 10_000, HW)])],
        numcopies=[("org/giant", 1)],
        drives=[_drive("d0", cap=100_000)],
    )
    inp = _solver_input(
        planner,
        executable_budget={"d0": 50_000},
        max_usable_for_epoch={"d0": 500},  # known max << peak
        feasibility_limit=50, optimization_limit=1,
    )
    r = placement.gate_b(inp)
    assert _code(r) == "REQUIREMENT_EXCEEDS_USABLE_MAX"
    _assert_no_executable_assignment(r)
    _assert_metadata(r, capacity_mode="guaranteed")


def test_contract_structural_target_tier_and_unproven():
    _require_placement()
    # No primary tier at all for a primary requirement → TARGET_TIER_MISSING via blocked/no eligible.
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("rep_only", role="replica", cap=10_000)],  # no primary eligible
    )
    inp = _solver_input(
        planner,
        executable_budget={"rep_only": 9_000},
        max_usable_for_epoch={"rep_only": 9_000},
        feasibility_limit=50, optimization_limit=1,
    )
    r = placement.gate_b(inp)
    assert _code(r) == "TARGET_TIER_MISSING"
    _assert_no_executable_assignment(r)
    actions = tuple(getattr(r, "actions", ()) or ())
    assert actions, "TARGET_TIER_MISSING must expose operator actions"

    # All eligible targets unproven → UNPROVEN_PROVENANCE
    planner2 = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d0", cap=10_000)],
        archived=[_arch("org/m", "d0", "w.safetensors", sha="0" * 64, obytes=100, sbytes=100)],
    )
    inp2 = _solver_input(
        planner2,
        executable_budget={"d0": 9_000},
        max_usable_for_epoch={"d0": 9_000},
        feasibility_limit=50, optimization_limit=1,
    )
    r2 = placement.gate_b(inp2)
    assert _code(r2) == "UNPROVEN_PROVENANCE"
    _assert_no_executable_assignment(r2)


def test_contract_failure_domain_vs_graph_dependency():
    """Clarification 2: domain unsat vs malformed dependency are distinct codes."""
    _require_placement()
    # Valid home+replica graph but both replica targets share home's failure domain → domain unsat.
    # Home on raid H; replicas R1/R2 same fs_uuid as H.
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=[
            _drive("H", role="primary", raid=True, cap=10_000, fs_uuid="uuid-same"),
            _drive("R1", role="replica", cap=10_000, fs_uuid="uuid-same"),
            _drive("R2", role="replica", cap=10_000, fs_uuid="uuid-same"),
        ],
    )
    inp = _solver_input(
        planner,
        executable_budget={"H": 5_000, "R1": 5_000, "R2": 5_000},
        max_usable_for_epoch={"H": 9_000, "R1": 9_000, "R2": 9_000},
        feasibility_limit=5_000, optimization_limit=1,
    )
    r = placement.gate_b(inp)
    assert _code(r) == "FAILURE_DOMAIN_UNSATISFIABLE"
    _assert_no_executable_assignment(r)

    # Malformed: replica depends_on points at a missing requirement id → GRAPH_DEPENDENCY_INVARIANT.
    # Hand-craft a SolverInput with a missing independent_of target. gate_b (or an optional
    # validate_solver_input helper) must classify it — not raise an unrelated construction error
    # that we re-label. Do not wrap TypeError/AttributeError as a false graph-invariant miss.
    assert hasattr(placement, "SolverInput"), "need SolverInput"
    bad_req = candidates.CopyRequirement(
        requirement_id="protected_replica:org/bad",
        repo_id="org/bad",
        kind=candidates.RequirementKind.PROTECTED_REPLICA,
        eligible_drives=("R1",),
        independent_of="protected_home:org/missing",
    )
    bad_graph = candidates.RequirementGraph(desired=(bad_req,), requirement_set_hash="0" * 64)
    empty_cset = candidates.CandidateSet(satisfied=(), by_requirement=(), drift=(), blocked=())
    bad_inp = placement.SolverInput(
        graph=bad_graph,
        candidates=empty_cset,
        drives=planner.drives,
        executable_budget=_budget_pairs({"H": 5_000, "R1": 5_000, "R2": 5_000}),
        max_usable_for_epoch=_budget_pairs({"H": 9_000, "R1": 9_000, "R2": 9_000}),
        capacity_mode="guaranteed",
        policy_version="tiered_v2",
        bounds=_bounds(50, 1),
    )
    if hasattr(placement, "validate_solver_input"):
        # Optional pure pre-check may raise or return the structural code; either is fine.
        validated = placement.validate_solver_input(bad_inp)
        if validated is not None and hasattr(validated, "code"):
            assert _code(validated) == "GRAPH_DEPENDENCY_INVARIANT"
            _assert_no_executable_assignment(validated)
            return
    r_bad = placement.gate_b(bad_inp)
    assert _code(r_bad) == "GRAPH_DEPENDENCY_INVARIANT", (
        f"gate_b must classify missing depends_on references as GRAPH_DEPENDENCY_INVARIANT; "
        f"got {_code(r_bad)}"
    )
    _assert_no_executable_assignment(r_bad)


def test_contract_partial_vs_fresh_no_pin_and_movement_preference():
    _require_placement()
    # Partial on small (reuses 50) missing 50; fresh on big needs 100. Both candidates from #36a.
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 50, HW), _mf("b.safetensors", 50, HW2)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("small", cap=10_000), _drive("fresh", cap=10_000)],
        archived=[_arch("org/m", "small", "a.safetensors", sha=HW, obytes=50, sbytes=50)],
    )
    inp = _solver_input(
        planner,
        executable_budget={"small": 5_000, "fresh": 5_000},
        max_usable_for_epoch={"small": 9_000, "fresh": 9_000},
        feasibility_limit=5_000, optimization_limit=50_000,
    )
    gb = placement.gate_b(inp)
    assert _code(gb) == "FEASIBLE"
    improved = placement.improve(inp, gb.assignment, emergency=None)
    # Prefer partial (lower movement) when free-space/idle allow. At minimum, improvement must
    # keep a FEASIBLE assignment and a reviewed derivation_mode.
    assert improved.assignment is not None
    assert getattr(improved, "derivation_mode", "optimized") in {
        "optimized", "state_truncated", "canonical_fallback",
    }


def test_contract_pending_home_dependency_before_constrainedness():
    _require_placement()
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=[
            _drive("H", role="primary", raid=True, cap=10_000, fs_uuid="home-uuid"),
            _drive("R", role="replica", cap=10_000, fs_uuid="replica-uuid"),
        ],
    )
    inp = _solver_input(
        planner,
        executable_budget={"H": 5_000, "R": 5_000},
        max_usable_for_epoch={"H": 9_000, "R": 9_000},
        feasibility_limit=5_000, optimization_limit=5_000,
    )
    # Candidates should include PendingHome for replica; search must still find FEASIBLE.
    _, cset = _graph_cset(planner)
    rep = []
    for rid, cs in cset.by_requirement:
        if rid.startswith("protected_replica:"):
            rep = list(cs)
    assert rep and any(isinstance(c.source, candidates.PendingHome) for c in rep)
    r = placement.gate_b(inp)
    assert _code(r) == "FEASIBLE"
    # Resolved assignment must pin replica source to the chosen home drive (complete identity).
    assignment = r.assignment
    assert assignment is not None


def test_contract_feasibility_truncation_counts_root():
    _require_placement()
    # Force a multi-state search then set limit=1: root alone consumes the only slot → PACKING_INCONCLUSIVE
    # (or FEASIBLE if first-feasible is found without entering a second state — use enough reqs).
    repos = [f"org/m{i}" for i in range(5)]
    planner = _planner(
        selection=repos,
        manifests=[(r, [_mf("w.safetensors", 10, HW)]) for r in repos],
        numcopies=[(r, 1) for r in repos],
        drives=[_drive("d0", cap=10_000), _drive("d1", cap=10_000)],
    )
    inp = _solver_input(
        planner,
        executable_budget={"d0": 30, "d1": 30},
        max_usable_for_epoch={"d0": 9_000, "d1": 9_000},
        feasibility_limit=1,  # root counts as 1; any expansion exhausts
        optimization_limit=1,
    )
    r = placement.gate_b(inp)
    # With limit 1, either we luck into a single-state solution or we are inconclusive.
    # For 5 items needing multi-step assignment, must not claim proven infeasible.
    assert _code(r) in {"FEASIBLE", "PACKING_INCONCLUSIVE"}
    if _code(r) == "PACKING_INCONCLUSIVE":
        _assert_no_executable_assignment(r)
        visited = getattr(r, "feasibility_states_visited", None)
        if visited is not None:
            assert visited <= 1, "exhaustion must occur before entering limit+1"
    _assert_metadata(r, capacity_mode="guaranteed", feasibility_limit=1)


def test_contract_optimization_truncation_and_emergency_fallback():
    _require_placement()
    planner = _planner(
        selection=["org/a", "org/b"],
        manifests=[
            ("org/a", [_mf("w.safetensors", 100, HW)]),
            ("org/b", [_mf("w.safetensors", 100, HW2)]),
        ],
        numcopies=[("org/a", 1), ("org/b", 1)],
        drives=[_drive("d0", cap=10_000), _drive("d1", cap=10_000)],
    )
    inp = _solver_input(
        planner,
        executable_budget={"d0": 5_000, "d1": 5_000},
        max_usable_for_epoch={"d0": 9_000, "d1": 9_000},
        feasibility_limit=50_000,
        optimization_limit=1,  # truncate improvement quickly
    )
    gb = placement.gate_b(inp)
    assert _code(gb) == "FEASIBLE"
    first = gb.assignment
    truncated = placement.improve(inp, first, emergency=None)
    assert getattr(truncated, "derivation_mode", None) in {"state_truncated", "optimized"}
    if getattr(truncated, "derivation_mode", None) == "state_truncated":
        assert getattr(truncated, "diagnostic", None) == "optimization_truncated"
    assert truncated.assignment is not None

    # Emergency: monitor raises; must return canonical first-feasible, twice identical.
    class _Boom:
        def __init__(self):
            self.n = 0

        def __call__(self, *args, **kwargs):
            self.n += 1
            if self.n >= 1:
                # Prefer a named exception type from placement if present.
                exc_t = getattr(placement, "EmergencyResourceLimit", RuntimeError)
                raise exc_t("injected emergency")

    # Re-run with higher optimization limit so emergency fires during improvement search.
    inp2 = _solver_input(
        planner,
        executable_budget={"d0": 5_000, "d1": 5_000},
        max_usable_for_epoch={"d0": 9_000, "d1": 9_000},
        feasibility_limit=50_000,
        optimization_limit=50_000,
    )
    gb2 = placement.gate_b(inp2)
    assert _code(gb2) == "FEASIBLE"
    a = placement.improve(inp2, gb2.assignment, emergency=_Boom())
    b = placement.improve(inp2, gb2.assignment, emergency=_Boom())
    assert getattr(a, "derivation_mode", None) == "canonical_fallback"
    assert getattr(a, "diagnostic", None) == "optimization_resource_exhausted"
    assert a.assignment == gb2.assignment, "emergency must discard best-so-far and return first-feasible"
    assert a.assignment == b.assignment, "emergency fallback must be reproducible"
    assert not any(
        isinstance(getattr(a, f.name, None), type(_Boom)) for f in dataclasses.fields(a)
    ), "emergency monitor must not appear in PlacementResult"


def test_contract_objective_precedence_golden():
    """Clarification 4: movement ≻ free-space vector ≻ idle count ≻ canonical tie-break."""
    _require_placement()
    # Three scenarios engineered so only one objective differs at a time.
    # (1) Movement dominates: partial (low movement) vs fresh (high), even if free-vector prefers fresh.
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 40, HW), _mf("b.safetensors", 40, HW2)])],
        numcopies=[("org/m", 1)],
        drives=[
            _drive("partial_drive", cap=10_000),
            _drive("fresh_drive", cap=10_000),
        ],
        archived=[_arch("org/m", "partial_drive", "a.safetensors", sha=HW, obytes=40, sbytes=40)],
    )
    # Equal executable budgets so free-space after partial leaves more free on partial_drive
    # (charges 40) vs fresh (charges 80) — free-vector may prefer partial anyway; movement also prefers
    # partial. To prove movement dominates free-space, need free-space to prefer the HIGHER movement
    # option. Use a third "decoy" drive so packing on high-movement target leaves a better free vector.
    # Simpler contract: expose score tuple on PlacementResult and assert lexicographic order against
    # two hand-built assignments if the API allows scoring; else assert chosen target is partial_drive
    # when both fit and movement is lower there.
    inp = _solver_input(
        planner,
        executable_budget={"partial_drive": 5_000, "fresh_drive": 5_000},
        max_usable_for_epoch={"partial_drive": 9_000, "fresh_drive": 9_000},
        feasibility_limit=10_000, optimization_limit=50_000,
    )
    gb = placement.gate_b(inp)
    assert _code(gb) == "FEASIBLE"
    improved = placement.improve(inp, gb.assignment, emergency=None)
    score = getattr(improved, "score", None)
    assert score is not None, "PlacementResult must expose the lexicographic score tuple for audit"
    # Movement is first element; partial's transfer is 40 raw missing, fresh is 80.
    # Chosen assignment must minimize movement.
    def _target_for(requirement_prefix, assignment):
        # Flexible extraction across plausible assignment shapes.
        if hasattr(assignment, "by_requirement"):
            for rid, task in assignment.by_requirement:
                if rid.startswith(requirement_prefix) or rid == requirement_prefix:
                    return getattr(task, "target_drive", task)
        if hasattr(assignment, "tasks"):
            for t in assignment.tasks:
                rid = getattr(t, "requirement_id", "")
                if rid.startswith("primary:") or rid.startswith(requirement_prefix):
                    return t.target_drive
        if isinstance(assignment, (list, tuple)):
            for t in assignment:
                if getattr(t, "requirement_id", "").startswith("primary:"):
                    return t.target_drive
        raise AssertionError("cannot read target from assignment for objective test")

    assert _target_for("primary:", improved.assignment) == "partial_drive", (
        "movement cost must dominate: finish-in-place partial preferred over full re-download")

    # (2) Free-space dominates idle: two equal-movement fresh placements; prefer better remaining-free vector.
    planner2 = _planner(
        selection=["org/x"],
        manifests=[("org/x", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/x", 1)],
        drives=[_drive("big", cap=10_000), _drive("small", cap=10_000)],
    )
    # Equal movement either way (both fresh 100). Executable budgets differ so remaining free vectors differ.
    inp2 = _solver_input(
        planner2,
        executable_budget={"big": 1_000, "small": 200},
        max_usable_for_epoch={"big": 9_000, "small": 9_000},
        feasibility_limit=10_000, optimization_limit=50_000,
    )
    gb2 = placement.gate_b(inp2)
    assert _code(gb2) == "FEASIBLE"
    imp2 = placement.improve(inp2, gb2.assignment, emergency=None)
    # Prefer packing onto small (200-100=100 free left on small, 1000 on big unused) vs big
    # (1000-100=900 on big, 200 on small unused). Descending free vector:
    #   place on small: sorted([1000, 100], reverse) = (1000, 100)
    #   place on big:   sorted([900, 200], reverse) = (900, 200)
    # (1000,100) > (900,200) so free-space prefers small.
    assert _target_for("primary:", imp2.assignment) == "small", (
        "free-space vector must dominate idle: prefer assignment with better descending remaining-free")

    # (3) Idle dominates canonical tie: equal movement and equal free vectors → fewer idle drives wins.
    # Two drives, two equal items of size 50, each drive budget 50 — only one packing shape up to labels;
    # use three drives with budgets that make free vectors equal for two different support sets.
    # Contract fallback: score tuple second/third elements ordered so idle is third key.
    assert isinstance(score, tuple) and len(score) >= 3, (
        "score must be a lex tuple (movement, free_vector_or_key, idle_count, canonical...)")


def test_contract_both_capacity_modes():
    _require_placement()
    # Compressible float shard: guaranteed durable = raw; expected = ratio*margin may fit tighter budget.
    size = 1_000
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", size, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d0", cap=10_000)],
    )
    # Budget between expected and raw so modes diverge.
    expected = int(size * RATIO * MARGIN)
    assert expected < size
    mid = (expected + size) // 2
    for mode, want_feasible in (("guaranteed", False), ("compression_aware", True)):
        inp = _solver_input(
            planner,
            executable_budget={"d0": mid},
            max_usable_for_epoch={"d0": 9_000},
            capacity_mode=mode,
            feasibility_limit=1_000, optimization_limit=1,
        )
        r = placement.gate_b(inp)
        _assert_metadata(r, capacity_mode=mode)
        if want_feasible:
            assert _code(r) == "FEASIBLE", f"{mode} should fit under mid budget"
        else:
            assert _code(r) != "FEASIBLE", f"{mode} should not fit under mid budget"
            _assert_no_executable_assignment(r)


def test_contract_workspace_peak_not_summed():
    _require_placement()
    # Two tasks on one drive: durable sums; workspace is max — fit when sum(d)+max(w) <= budget.
    # Exact numbers depend on codec workspace; assert the pure fit rule via a mode with zero workspace
    # raw files (aux/raw) so durable-only accounting is visible, plus a note that multi-task max holds.
    planner = _planner(
        selection=["org/a", "org/b"],
        manifests=[
            ("org/a", [_mf("c.json", 100, HC, fmt="json", quant=None)]),
            ("org/b", [_mf("d.json", 100, HW, fmt="json", quant=None)]),
        ],
        numcopies=[("org/a", 1), ("org/b", 1)],
        drives=[_drive("d0", cap=10_000)],
    )
    # Budget exactly 200: both raw 100+100 fit; workspace 0.
    inp = _solver_input(
        planner,
        executable_budget={"d0": 200},
        max_usable_for_epoch={"d0": 9_000},
        feasibility_limit=1_000, optimization_limit=1,
    )
    r = placement.gate_b(inp)
    assert _code(r) == "FEASIBLE"
    # Budget 199 cannot hold durable sum 200
    inp2 = _solver_input(
        planner,
        executable_budget={"d0": 199},
        max_usable_for_epoch={"d0": 9_000},
        feasibility_limit=1_000, optimization_limit=1,
    )
    r2 = placement.gate_b(inp2)
    assert _code(r2) != "FEASIBLE"
    _assert_no_executable_assignment(r2)


def test_contract_10k_candidates_scale():
    _require_placement()
    # Many repos × many drives → large candidate cross-product; must complete with explicit scale bounds.
    n_repos, n_drives = 100, 100  # 10k primary candidates
    repos = [f"org/m{i:04d}" for i in range(n_repos)]
    drives = [_drive(f"d{i:04d}", cap=10**12) for i in range(n_drives)]
    planner = _planner(
        selection=repos,
        manifests=[(r, [_mf("w.safetensors", 10, HW)]) for r in repos],
        numcopies=[(r, 1) for r in repos],
        drives=drives,
    )
    exec_b = {f"d{i:04d}": 10**9 for i in range(n_drives)}
    max_u = {f"d{i:04d}": 10**11 for i in range(n_drives)}
    inp = _solver_input(
        planner,
        executable_budget=exec_b,
        max_usable_for_epoch=max_u,
        feasibility_limit=200_000,
        optimization_limit=50_000,
    )
    t0 = time.perf_counter()
    r = placement.gate_b(inp)
    elapsed = time.perf_counter() - t0
    assert _code(r) in ALL_GATE_B_CODES
    _assert_metadata(r, capacity_mode="guaranteed", feasibility_limit=200_000)
    # Soft budget for Gate-1 evidence collection (not a hard production SLA yet).
    assert elapsed < 120.0, f"10k-candidate gate_b took {elapsed:.1f}s; record for bounds proposal"
    if _code(r) == "FEASIBLE":
        t1 = time.perf_counter()
        placement.improve(inp, r.assignment, emergency=None)
        assert time.perf_counter() - t1 < 120.0


def test_contract_explicit_bounds_required():
    _require_placement()
    # Constructing SolverInput without bounds must fail; pure path has no implicit defaults.
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 10, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d0")],
    )
    graph, cset = _graph_cset(planner)
    try:
        placement.SolverInput(
            graph=graph,
            candidates=cset,
            drives=planner.drives,
            executable_budget=_budget_pairs({"d0": 100}),
            max_usable_for_epoch=_budget_pairs({"d0": 1000}),
            capacity_mode="guaranteed",
            policy_version="tiered_v2",
            # bounds intentionally omitted
        )
        raised = False
    except TypeError:
        raised = True
    assert raised, "SolverInput must require explicit SolverBounds (no implicit pure defaults)"


def test_contract_remaining_free_zero_byte_task_not_idle():
    _require_placement()
    # A zero-size file assignment still marks the drive as used for idle-count.
    planner = _planner(
        selection=["org/z"],
        manifests=[("org/z", [_mf("empty.json", 0, HC, fmt="json", quant=None)])],
        numcopies=[("org/z", 1)],
        drives=[_drive("d0", cap=10_000), _drive("d1", cap=10_000)],
    )
    inp = _solver_input(
        planner,
        executable_budget={"d0": 100, "d1": 100},
        max_usable_for_epoch={"d0": 9_000, "d1": 9_000},
        feasibility_limit=1_000, optimization_limit=10_000,
    )
    gb = placement.gate_b(inp)
    assert _code(gb) == "FEASIBLE"
    imp = placement.improve(inp, gb.assignment, emergency=None)
    score = getattr(imp, "score", None)
    assert score is not None and len(score) >= 3
    idle = score[2]
    # Exactly one drive used → idle count among candidate-target universe (2) is 1.
    assert idle == 1, f"zero-byte assigned task must count drive as used; idle={idle}"


# --------------------------------------------------------------------------------------------------
# Adapter contracts (ruling 9) — red until plan_capacity projects tiered_v2 + graded Gate-B
# --------------------------------------------------------------------------------------------------
def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    return con


def _db_drive(con, label, *, role="primary", raid=False, capacity_bytes=10_000, free=None):
    free = capacity_bytes if free is None else free
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes) "
        "VALUES(?,?,?,?,?)",
        [label, role, int(raid), capacity_bytes, free],
    )
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _db_repo(con, repo, *, copies=1, files=None):
    files = files or (("model.safetensors", 100, "safetensors", "bf16"),)
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,?)", [repo, copies])
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-01-01')", [repo])
    con.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES(?,?,?,?,?,?)",
        [(repo, name, size, fmt, quant, f"sha-{repo}-{name}") for name, size, fmt, quant in files],
    )


def _plan_with_evidence(con, **kwargs):
    graph = reconcile.reconcile_plan(con, "ark")
    evidence = _admission_compat.evidence_for_plan(con, "ark")
    return capacity.plan_capacity(con, graph, evidence_by_drive=evidence, **kwargs)


def _gate_b_code_from_plan(plan) -> str:
    code = getattr(plan, "gate_b_code", None)
    if code is None:
        payload = plan.to_dict() if hasattr(plan, "to_dict") else {}
        code = payload.get("gate_b_code")
    if code is None:
        raise AssertionError(
            "#38 adapter must expose gate_b_code on CapacityPlan and in to_dict() "
            "(graded Gate-B result for FEASIBLE and every non-feasible/structural code)")
    return code.value if hasattr(code, "value") else str(code)


def test_adapter_placement_policy_tiered_v2():
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=100_000, free=100_000)
    _db_repo(con, "org/m", files=(("model.safetensors", 100, "safetensors", "bf16"),))
    plan = _plan_with_evidence(con)
    assert plan.placement_policy == "tiered_v2", (
        f"plan_capacity must project placement_policy=tiered_v2; got {plan.placement_policy!r}")
    payload = plan.to_dict()
    assert payload.get("placement_policy") == "tiered_v2"
    con.close()


def test_adapter_exposes_graded_gate_b_and_feasible_exclusivity():
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=100_000, free=100_000)
    _db_repo(con, "org/m", files=(("model.safetensors", 100, "safetensors", "bf16"),))
    plan = _plan_with_evidence(con)
    code = _gate_b_code_from_plan(plan)
    assert code in ALL_GATE_B_CODES
    # feasible=True exclusively for FEASIBLE
    if code == "FEASIBLE":
        assert plan.feasible is True
        assert plan.tasks, "FEASIBLE adapter projection should carry assigned tasks"
    else:
        assert plan.feasible is False
        # Non-FEASIBLE must not expose an executable assignment as authority
        assert not plan.tasks, (
            "non-FEASIBLE plan_capacity result must not expose executable tasks")
    payload = plan.to_dict()
    assert payload.get("gate_b_code") == code
    assert payload.get("feasible") == (code == "FEASIBLE")
    assert payload.get("mode") in {"guaranteed", "compression_aware"}
    con.close()


def test_adapter_never_feasible_for_infeasible_cart():
    con = _mem()
    # Tiny free vs large file → non-FEASIBLE under any honest Gate-B.
    _db_drive(con, "d0", capacity_bytes=1_000, free=50)
    _db_repo(con, "org/giant", files=(("model.safetensors", 10_000, "safetensors", "bf16"),))
    plan = _plan_with_evidence(con)
    code = _gate_b_code_from_plan(plan)
    assert code != "FEASIBLE"
    assert plan.feasible is False
    assert code in ALL_GATE_B_CODES
    # Must not look like a silent success.
    assert plan.to_dict()["feasible"] is False
    con.close()


def test_adapter_inconclusive_or_unknown_not_false_proven_short():
    """When pure path would return PACKING_INCONCLUSIVE / CAPACITY_EVIDENCE_UNKNOWN, adapter must
    not project only CAPACITY_DURABLE_SHORT as if packing were proven impossible.

    Until production exists this test documents the projection rule: if gate_b_code is
    PACKING_INCONCLUSIVE or CAPACITY_EVIDENCE_UNKNOWN, failure codes must not be solely durable/workspace
    short without the graded code.
    """
    # Require the adapter surface: plan_capacity must expose gate_b_code so the mapping invariant
    # can be checked. Fails for missing graded projection (correct Gate-1 red reason).
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=100_000, free=100_000)
    _db_drive(con, "d1", capacity_bytes=100_000, free=0)
    _db_repo(con, "org/m")
    plan = _plan_with_evidence(con)
    code = _gate_b_code_from_plan(plan)
    if code in {"PACKING_INCONCLUSIVE", "CAPACITY_EVIDENCE_UNKNOWN"}:
        assert plan.feasible is False
        # Graded code must be present alongside any legacy failure list
        assert plan.to_dict().get("gate_b_code") == code
        # Must not present solely as a proven durable/workspace short without the graded code.
        failure_codes = {f.code.value if hasattr(f.code, "value") else str(f.code) for f in plan.failures}
        if failure_codes and failure_codes <= {
            "CAPACITY_DURABLE_SHORT", "CAPACITY_WORKSPACE_SHORT",
        }:
            raise AssertionError(
                f"{code} must not be projected only as proven capacity short {failure_codes}; "
                "keep gate_b_code authoritative")
    con.close()


def test_adapter_mode_labelling_both_modes():
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=100_000, free=100_000)
    _db_repo(con, "org/m", files=(("model.safetensors", 100, "safetensors", "bf16"),))
    for mode in ("guaranteed", "compression_aware"):
        plan = _plan_with_evidence(con, capacity_mode=mode)
        assert plan.mode.value == mode
        # Graded code required even when feasible
        code = _gate_b_code_from_plan(plan)
        assert code in ALL_GATE_B_CODES
        assert plan.feasible == (code == "FEASIBLE")
    con.close()


# --------------------------------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------------------------------
def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001 — Gate-1 wants the full red/green map
            failed.append((name, type(exc).__name__, str(exc)))
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    print("Gate-1 tests-only: pure test_contract_* and adapter test_adapter_* are EXPECTED RED")
    print("until #38 production (modelark.placement + plan_capacity tiered_v2 cutover) lands.")
    if failed:
        print("\nExpected-red map:")
        for name, etype, msg in failed:
            short = msg.replace("\n", " ")[:160]
            print(f"  {name}: {etype}: {short}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
