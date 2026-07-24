"""PR-06 / issue #38 pure Gate B + deterministic tiered_v2 (tests-first, RFC-002 / DEC-049).

Gate 1 pins the pure feasibility/improvement contract and the plan_capacity adapter projection
BEFORE production. #38 consumes #36a's no-pin CandidateSet, runs the mixed-evidence Gate-B ladder,
and (only on FEASIBLE) a deterministic tiered_v2 improvement pass.

Binding Gate-0 amend-1 + Gate-1 clarifications + Gate-1 amendment (exact outcomes):

  1. max_usable_for_epoch is admission-supplied; placement never recomputes a safety floor.
  2. REQUIREMENT_EXCEEDS_USABLE_MAX only when every valid candidate has a known max and each
     candidate's own peak exceeds its own target's max.
  3. GRAPH_DEPENDENCY_INVARIANT is only for malformed dependencies; domain failure is distinct;
     a blocked home keeps its earlier structural code.
  4. Dependencies precede constrainedness; PendingHome resolves to the selected home source.
  5. Root state counts; limit L → exhaustion before entering L+1; states_visited == L on exhaust.
  6. Lex objectives: movement ≻ free-space vector ≻ idle ≻ canonical (adversarial fixtures).
  7. Emergency monitor only on improve(...); never in SolverInput / results / hash surface.
  8. Explicit SolverBounds required; every result carries policy/bound version/mode metadata.
  9. Deep immutability: no nested mutable dict/list/set/bytearray; no leaked callables.
 10. Adapter deterministically projects every graded code; feasible=True only for FEASIBLE.

RED until modelark.placement and the plan_capacity tiered_v2 cutover exist.
Self-running: CI executes ``python tests/test_gate_b_tiered_v2.py`` directly.
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import pkgutil
import sqlite3
import time
from pathlib import Path
from types import MappingProxyType

from modelark import archive_manifest, budgets, capacity, capacity_evidence, candidates, reconcile
from modelark.core import db

try:
    import modelark.placement as placement
    _HAS_PLACEMENT = True
except ModuleNotFoundError as exc:               # ONLY the exact absent submodule
    if exc.name != "modelark.placement":
        raise
    placement = None
    _HAS_PLACEMENT = False


HW = "1" * 64
HW2 = "3" * 64
HW3 = "4" * 64
HC = "2" * 64
MARGIN = capacity.EXPECTED_MARGIN
RATIO = capacity.DEFAULT_FLOAT_RATIO
_CFG = (("max_compress_ram_gb", 64), ("stream_compress", True), ("threads", 4))
_CFG_DICT = dict(_CFG)

# Exact structural diagnostics / operator actions (passoff §6.1 goldens).
STRUCTURAL_GOLDENS = {
    "TARGET_TIER_MISSING": {
        "actions": ("add_eligible_drive", "change_plan_policy"),
    },
    "UNPROVEN_PROVENANCE": {
        "actions": ("repair_or_remove_unproven_rows", "provide_hash_evidence"),
    },
    "GRAPH_DEPENDENCY_INVARIANT": {
        "actions": ("inspect_integrity", "reconcile_plan"),
    },
    "FAILURE_DOMAIN_UNSATISFIABLE": {
        "actions": ("add_independent_drive", "change_failure_domain_policy"),
    },
    "REQUIREMENT_EXCEEDS_USABLE_MAX": {
        "actions": ("add_larger_drive", "trim_selection", "change_hard_constraints"),
    },
}
STRUCTURAL_CODES = tuple(STRUCTURAL_GOLDENS)
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
            "graded codes FEASIBLE|PACKING_INCONCLUSIVE|CAPACITY_EVIDENCE_UNKNOWN|"
            "INFEASIBLE_UNDER_ADMISSION_BUDGET|INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX|"
            "TARGET_TIER_MISSING|UNPROVEN_PROVENANCE|GRAPH_DEPENDENCY_INVARIANT|"
            "FAILURE_DOMAIN_UNSATISFIABLE|REQUIREMENT_EXCEEDS_USABLE_MAX, "
            "solver_bound_version on every result, and plan_capacity projecting "
            "placement_policy=tiered_v2 + gate_b_code.")


def _bounds(feasibility: int, optimization: int):
    _require_placement()
    return placement.SolverBounds(
        feasibility_state_limit=feasibility,
        optimization_state_limit=optimization,
    )


# --------------------------------------------------------------------------------------------------
# Builders
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
    return tuple(sorted((str(k), v) for k, v in labels_to_values.items()))


def _solver_input(
    planner_inp,
    *,
    executable_budget,
    max_usable_for_epoch,
    capacity_mode="guaranteed",
    feasibility_limit=10_000,
    optimization_limit=10_000,
    graph=None,
    cset=None,
):
    _require_placement()
    if graph is None or cset is None:
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


def _actions(result) -> tuple:
    raw = getattr(result, "actions", None) or ()
    if raw is None:
        raw = ()
    return tuple(a.value if hasattr(a, "value") else str(a) for a in raw)


def _visited(result) -> int:
    v = getattr(result, "feasibility_states_visited", None)
    if v is None:
        v = getattr(result, "states_visited", None)
    if v is None:
        raise AssertionError("GateBResult must expose feasibility_states_visited (or states_visited)")
    return int(v)


def _relevant_unknowns(result) -> set[str]:
    drives = getattr(result, "relevant_unknown_drives", None)
    if drives is None:
        drives = getattr(result, "drives", None)
    if drives is None:
        return set()
    if isinstance(drives, (str, bytes)):
        return {str(drives)}
    return {str(d) for d in drives}


def _assert_metadata(result, *, capacity_mode, feasibility_limit=None, optimization_limit=None):
    """Every outcome carries non-empty policy / solver-bound version / mode / explicit bounds."""
    mode = getattr(result, "capacity_mode", None)
    if hasattr(mode, "value"):
        mode = mode.value
    assert mode == capacity_mode, f"capacity_mode must be labelled; got {mode!r}"

    policy = getattr(result, "policy_version", None) or getattr(result, "placement_policy", None)
    assert policy == "tiered_v2", f"policy_version must be tiered_v2; got {policy!r}"

    bound_ver = getattr(result, "solver_bound_version", None)
    assert bound_ver is not None and str(bound_ver).strip(), (
        "every GateB/Placement result must carry non-empty solver_bound_version")

    bounds = getattr(result, "bounds", None) or getattr(result, "bounds_used", None)
    if bounds is not None:
        fl = getattr(bounds, "feasibility_state_limit", None)
        ol = getattr(bounds, "optimization_state_limit", None)
    else:
        assert hasattr(result, "feasibility_state_limit") or hasattr(result, "optimization_state_limit"), (
            "result must expose bounds / bounds_used or explicit limit fields")
        fl = getattr(result, "feasibility_state_limit", None)
        ol = getattr(result, "optimization_state_limit", None)
    if feasibility_limit is not None:
        assert fl is not None, "feasibility_state_limit missing from result bounds metadata"
        assert fl == feasibility_limit
    if optimization_limit is not None:
        assert ol is not None, "optimization_state_limit missing from result bounds metadata"
        assert ol == optimization_limit


def _assert_no_executable_assignment(result):
    assignment = getattr(result, "assignment", None)
    assert assignment is None, (
        f"non-FEASIBLE must not expose an executable assignment; got {type(assignment)!r}")


def _assert_structural(result, code: str):
    assert _code(result) == code
    _assert_no_executable_assignment(result)
    _assert_metadata(result, capacity_mode="guaranteed")
    golden = STRUCTURAL_GOLDENS[code]
    actions = _actions(result)
    assert actions == golden["actions"], (
        f"{code} actions must be exact golden {golden['actions']!r}; got {actions!r}")
    diagnostics = getattr(result, "diagnostics", None)
    assert diagnostics is not None and diagnostics != () and diagnostics != {}, (
        f"{code} must carry exact diagnostics payload (non-empty)")


def _assert_frozen(obj, label="record"):
    assert dataclasses.is_dataclass(obj), f"{label} must be a dataclass"
    assert obj.__dataclass_params__.frozen, f"{label} must be frozen"
    fields = dataclasses.fields(obj)
    if not fields:
        return
    try:
        setattr(obj, fields[0].name, getattr(obj, fields[0].name))  # type: ignore[misc]
        raise AssertionError(f"{label} must raise FrozenInstanceError on setattr")
    except dataclasses.FrozenInstanceError:
        pass


def _assert_deep_immutable(obj, *, path="root", reject_callables=True):
    """Reject nested mutable dict/list/set/bytearray and leaked callables."""
    if isinstance(obj, dict):
        raise AssertionError(f"mutable dict at {path}")
    if isinstance(obj, list):
        raise AssertionError(f"mutable list at {path}")
    if isinstance(obj, set):
        raise AssertionError(f"mutable set at {path}")
    if isinstance(obj, bytearray):
        raise AssertionError(f"mutable bytearray at {path}")
    if reject_callables and callable(obj) and not isinstance(obj, type):
        # Allow type objects / enums; reject emergency monitors and other callables.
        if not isinstance(obj, (str, bytes, int, float, bool, type(None))):
            # dataclasses, tuples, etc. are not callable; plain functions/instances with __call__ are.
            if hasattr(obj, "__call__") and not dataclasses.is_dataclass(obj):
                # Enum values / bound methods on frozen records shouldn't appear as field values.
                raise AssertionError(f"callable leaked into canonical state at {path}: {type(obj)!r}")
    if isinstance(obj, MappingProxyType):
        for k, v in obj.items():
            _assert_deep_immutable(v, path=f"{path}[{k!r}]", reject_callables=reject_callables)
        return
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            _assert_deep_immutable(getattr(obj, f.name), path=f"{path}.{f.name}",
                                   reject_callables=reject_callables)
        return
    if isinstance(obj, tuple):
        for i, item in enumerate(obj):
            _assert_deep_immutable(item, path=f"{path}[{i}]", reject_callables=reject_callables)
        return
    if isinstance(obj, frozenset):
        for i, item in enumerate(obj):
            _assert_deep_immutable(item, path=f"{path}{{frozenset:{i}}}", reject_callables=reject_callables)


def _task_targets(assignment) -> dict[str, str]:
    """Map requirement_id → target_drive from a flexible assignment shape."""
    out: dict[str, str] = {}
    if assignment is None:
        return out
    if hasattr(assignment, "by_requirement"):
        for rid, task in assignment.by_requirement:
            out[str(rid)] = getattr(task, "target_drive", task if isinstance(task, str) else str(task))
        return out
    tasks = getattr(assignment, "tasks", None)
    if tasks is not None:
        for t in tasks:
            out[str(t.requirement_id)] = t.target_drive
        return out
    if isinstance(assignment, (list, tuple)):
        for t in assignment:
            if hasattr(t, "requirement_id"):
                out[str(t.requirement_id)] = t.target_drive
        return out
    if isinstance(assignment, tuple) and assignment and isinstance(assignment[0], tuple):
        for rid, task in assignment:
            out[str(rid)] = getattr(task, "target_drive", task)
        return out
    raise AssertionError(f"cannot read tasks from assignment type {type(assignment)!r}")


def _task_sources(assignment) -> dict[str, object]:
    out: dict[str, object] = {}
    if hasattr(assignment, "by_requirement"):
        for rid, task in assignment.by_requirement:
            out[str(rid)] = getattr(task, "source", None)
        return out
    tasks = getattr(assignment, "tasks", None) or (
        assignment if isinstance(assignment, (list, tuple)) else ())
    for t in tasks:
        if hasattr(t, "requirement_id"):
            out[str(t.requirement_id)] = getattr(t, "source", None)
    return out


def _file_ws(size: int) -> tuple[int, int]:
    """Return (guaranteed_durable, workspace_peak_guaranteed) for a compress float shard."""
    mf = _mf("w.safetensors", size, HW)
    fb = budgets.file_budget(mf, RATIO, _CFG_DICT)
    return fb.guaranteed_durable, fb.workspace_peak_guaranteed


# --------------------------------------------------------------------------------------------------
# Pure contracts — API / purity / immutability
# --------------------------------------------------------------------------------------------------
def test_contract_module_api_surface():
    _require_placement()
    for name in ("SolverBounds", "SolverInput", "gate_b", "improve"):
        assert hasattr(placement, name), f"modelark.placement must expose {name}"
    for fn_name in ("gate_b", "improve"):
        params = set(inspect.signature(getattr(placement, fn_name)).parameters)
        assert "con" not in params and "connection" not in params
    improve_params = inspect.signature(placement.improve).parameters
    assert "emergency" in improve_params
    input_fields = {f.name for f in dataclasses.fields(placement.SolverInput)}
    banned = {"emergency", "emergency_monitor", "clock", "now", "check"}
    assert not (input_fields & banned)


def test_contract_pure_no_io_boundary():
    _require_placement()
    banned = {
        "sqlite3", "socket", "wishlist", "fetch", "reconcile", "db", "drive_fence", "admission",
        # Direct clock / resource / path I/O (Gate-1 amendment §6)
        "time", "datetime", "resource", "psutil", "pathlib", "os", "tempfile", "shutil", "subprocess",
    }
    modules = [placement]
    if getattr(placement, "__path__", None) is not None:
        for modinfo in pkgutil.walk_packages(placement.__path__, placement.__name__ + "."):
            modules.append(importlib.import_module(modinfo.name))
    names: set[str] = set()
    for mod in modules:
        try:
            source = inspect.getsource(mod)
        except (OSError, TypeError):
            path = Path(inspect.getfile(mod))
            source = path.read_text(encoding="utf-8") if path.is_file() else ""
        for node in ast.walk(ast.parse(source)):
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
        planner, executable_budget={"d0": 5_000}, max_usable_for_epoch={"d0": 9_000},
        feasibility_limit=100, optimization_limit=100,
    )
    _assert_frozen(inp, "SolverInput")
    _assert_frozen(inp.bounds, "SolverBounds")
    _assert_deep_immutable(inp)
    assert not isinstance(inp.executable_budget, dict)
    assert not isinstance(inp.max_usable_for_epoch, dict)

    result = placement.gate_b(inp)
    _assert_frozen(result, "GateBResult")
    _assert_deep_immutable(result)
    _assert_metadata(result, capacity_mode="guaranteed", feasibility_limit=100, optimization_limit=100)
    if _code(result) == "FEASIBLE":
        class _Boom:
            def __call__(self, *a, **k):
                raise RuntimeError("should not leak")

        improved = placement.improve(inp, result.assignment, emergency=_Boom())
        # May or may not fire emergency depending on search size; either way no leak.
        if getattr(improved, "derivation_mode", None) != "canonical_fallback":
            improved = placement.improve(inp, result.assignment, emergency=None)
        _assert_frozen(improved, "PlacementResult")
        _assert_deep_immutable(improved)
        _assert_metadata(improved, capacity_mode="guaranteed", optimization_limit=100)
        assert "emergency" not in {f.name for f in dataclasses.fields(improved)}
        assert not any(isinstance(getattr(improved, f.name, None), _Boom)
                       for f in dataclasses.fields(improved))


def test_contract_explicit_bounds_required():
    _require_placement()
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 10, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d0")],
    )
    graph, cset = _graph_cset(planner)
    try:
        placement.SolverInput(
            graph=graph, candidates=cset, drives=planner.drives,
            executable_budget=_budget_pairs({"d0": 100}),
            max_usable_for_epoch=_budget_pairs({"d0": 1000}),
            capacity_mode="guaranteed", policy_version="tiered_v2",
        )
        raised = False
    except TypeError:
        raised = True
    assert raised, "SolverInput must require explicit SolverBounds"


# --------------------------------------------------------------------------------------------------
# Shuffle / determinism
# --------------------------------------------------------------------------------------------------
def test_contract_shuffled_input_byte_equivalence():
    """Permute drives, budgets, AND requirement/candidate tuples; full result equality."""
    _require_placement()
    files = [_mf("a.safetensors", 100, HW), _mf("b.safetensors", 100, HW2)]
    drives_n = [_drive("d0", cap=10_000), _drive("d1", cap=8_000), _drive("r0", role="replica", cap=8_000)]
    drives_s = list(reversed(drives_n))

    def run(drives, budget_order, *, reverse_req=False, reverse_cands=False):
        planner = _planner(
            selection=["org/m"],
            manifests=[("org/m", files)],
            numcopies=[("org/m", 1)],
            drives=drives,
        )
        graph, cset = _graph_cset(planner)
        if reverse_req:
            # Re-order by_requirement (still same candidates content).
            items = list(cset.by_requirement)
            items.reverse()
            cset = dataclasses.replace(cset, by_requirement=tuple(items)) if dataclasses.is_dataclass(cset) else cset
            # Also reverse desired requirements order if mutable via replace
            if dataclasses.is_dataclass(graph):
                graph = dataclasses.replace(graph, desired=tuple(reversed(graph.desired)))
        if reverse_cands and cset.by_requirement:
            rebuilt = []
            for rid, cs in cset.by_requirement:
                rebuilt.append((rid, tuple(reversed(cs))))
            if dataclasses.is_dataclass(cset):
                cset = dataclasses.replace(cset, by_requirement=tuple(rebuilt))
        budgets = {"d0": 4_000, "d1": 4_000, "r0": 0}
        maxima = {"d0": 9_000, "d1": 7_000, "r0": 7_000}
        if budget_order == "rev":
            budgets = {k: budgets[k] for k in reversed(list(budgets))}
            maxima = {k: maxima[k] for k in reversed(list(maxima))}
        inp = _solver_input(
            planner, executable_budget=budgets, max_usable_for_epoch=maxima,
            feasibility_limit=5_000, optimization_limit=5_000, graph=graph, cset=cset,
        )
        gb = placement.gate_b(inp)
        place = placement.improve(inp, gb.assignment, emergency=None) if _code(gb) == "FEASIBLE" else None
        return gb, place

    variants = [
        run(drives_n, "fwd"),
        run(drives_s, "rev"),
        run(drives_n, "fwd", reverse_req=True),
        run(drives_s, "rev", reverse_cands=True),
        run(drives_s, "fwd", reverse_req=True, reverse_cands=True),
    ]
    base_gb, base_pl = variants[0]
    for gb, pl in variants[1:]:
        assert gb == base_gb, "GateBResult must be fully order-independent under all input permutations"
        assert pl == base_pl, "PlacementResult must be fully order-independent under all input permutations"


# --------------------------------------------------------------------------------------------------
# Mixed-evidence ladder — exact codes only
# --------------------------------------------------------------------------------------------------
def test_contract_mixed_known_unknown_precedence():
    _require_placement()
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("known", cap=1_000), _drive("unk", cap=1_000)],
    )

    # (1) Known-only fit → FEASIBLE even with unknown present.
    r = placement.gate_b(_solver_input(
        planner, executable_budget={"known": 200, "unk": 0},
        max_usable_for_epoch={"known": 900, "unk": 900},
        feasibility_limit=1_000, optimization_limit=1,
    ))
    assert _code(r) == "FEASIBLE"
    _assert_metadata(r, capacity_mode="guaranteed", feasibility_limit=1_000)
    assert r.assignment is not None

    # (2) Known infeasible; relevant unknown with max can host → CAPACITY_EVIDENCE_UNKNOWN
    r2 = placement.gate_b(_solver_input(
        planner, executable_budget={"known": 10, "unk": 0},
        max_usable_for_epoch={"known": 900, "unk": 900},
        feasibility_limit=5_000, optimization_limit=1,
    ))
    assert _code(r2) == "CAPACITY_EVIDENCE_UNKNOWN"
    _assert_no_executable_assignment(r2)
    _assert_metadata(r2, capacity_mode="guaranteed")
    assert "unk" in _relevant_unknowns(r2), "must name the relevant unknown drive(s)"

    # (3) Known infeasible; unknown has no usable max → CAPACITY_EVIDENCE_UNKNOWN
    r3 = placement.gate_b(_solver_input(
        planner, executable_budget={"known": 10, "unk": 0},
        max_usable_for_epoch={"known": 900, "unk": None},
        feasibility_limit=5_000, optimization_limit=1,
    ))
    assert _code(r3) == "CAPACITY_EVIDENCE_UNKNOWN"
    _assert_no_executable_assignment(r3)

    # (4) Known-only exhaustive infeasible, no relevant unknown → INFEASIBLE_UNDER_ADMISSION_BUDGET
    planner_one = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("known", cap=1_000)],
    )
    r5 = placement.gate_b(_solver_input(
        planner_one, executable_budget={"known": 10}, max_usable_for_epoch={"known": 900},
        feasibility_limit=5_000, optimization_limit=1,
    ))
    assert _code(r5) == "INFEASIBLE_UNDER_ADMISSION_BUDGET"
    _assert_no_executable_assignment(r5)
    _assert_metadata(r5, capacity_mode="guaranteed")


def test_contract_requirement_exceeds_usable_max_exact():
    """All-maxima-too-small → exactly REQUIREMENT_EXCEEDS_USABLE_MAX (not a code set)."""
    _require_placement()
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 500, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("small", cap=10_000), _drive("also_small", cap=10_000)],
    )
    r = placement.gate_b(_solver_input(
        planner, executable_budget={"small": 0, "also_small": 0},
        max_usable_for_epoch={"small": 100, "also_small": 100},
        feasibility_limit=100, optimization_limit=1,
    ))
    _assert_structural(r, "REQUIREMENT_EXCEEDS_USABLE_MAX")

    # Unknown max on any candidate blocks structural exceed-max.
    r2 = placement.gate_b(_solver_input(
        planner, executable_budget={"small": 0, "also_small": 0},
        max_usable_for_epoch={"small": 100, "also_small": None},
        feasibility_limit=100, optimization_limit=1,
    ))
    assert _code(r2) == "CAPACITY_EVIDENCE_UNKNOWN"
    _assert_no_executable_assignment(r2)


def test_contract_collective_infeasible_with_unknown_at_usable_max():
    """Every requirement fits individually on some drive, but optimistic fleet cannot pack globally.

    Exact code: INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX (not structural exceed-max).
    """
    _require_placement()
    # Three items of size 6; known drive free 0; two unknowns each max 10.
    # Individually each item fits a 10-max drive; collectively 18 > 10+10=20? 6+6+6=18 ≤ 20 — fits.
    # Use items 8,8,8 and unknowns max 10 each: individually OK, collectively 24 > 20.
    sizes = (8, 8, 8)
    repos = [f"org/m{i}" for i in range(3)]
    planner = _planner(
        selection=repos,
        manifests=[(r, [_mf("w.safetensors", s, HW)]) for r, s in zip(repos, sizes)],
        numcopies=[(r, 1) for r in repos],
        drives=[
            _drive("known", cap=1_000),
            _drive("unk0", cap=1_000),
            _drive("unk1", cap=1_000),
        ],
    )
    r = placement.gate_b(_solver_input(
        planner,
        executable_budget={"known": 0, "unk0": 0, "unk1": 0},
        max_usable_for_epoch={"known": 5, "unk0": 10, "unk1": 10},  # each 8 fits a 10; 24>20 collective
        feasibility_limit=50_000, optimization_limit=1,
    ))
    assert _code(r) == "INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX"
    _assert_no_executable_assignment(r)
    _assert_metadata(r, capacity_mode="guaranteed")
    # Must not claim freeing known capacity cannot help (actions/diagnostics acknowledge free/trim).
    blob = str(getattr(r, "diagnostics", "")) + str(_actions(r)) + str(getattr(r, "message", ""))
    assert blob, "must carry diagnostics/actions"
    # Freeing known still may help is the semantic; do not assert "impossible physically".
    assert "physical" not in blob.lower() or "free" in blob.lower()


def test_contract_known_search_bound_exhaustion_with_unknowns():
    """Known-search bound exhaustion with unknown drives present → exactly PACKING_INCONCLUSIVE."""
    _require_placement()
    repos = [f"org/m{i}" for i in range(8)]
    planner = _planner(
        selection=repos,
        manifests=[(r, [_mf("w.safetensors", 10, HW)]) for r in repos],
        numcopies=[(r, 1) for r in repos],
        drives=[_drive("known", cap=10_000), _drive("unk", cap=10_000)],
    )
    # Enough known free that packing may exist; tiny limit forces inconclusiveness first.
    r = placement.gate_b(_solver_input(
        planner,
        executable_budget={"known": 200, "unk": 0},
        max_usable_for_epoch={"known": 9_000, "unk": 9_000},
        feasibility_limit=1, optimization_limit=1,
    ))
    assert _code(r) == "PACKING_INCONCLUSIVE"
    _assert_no_executable_assignment(r)
    assert _visited(r) == 1
    _assert_metadata(r, capacity_mode="guaranteed", feasibility_limit=1)


def test_contract_optimistic_search_bound_exhaustion():
    """After known exhaustively fails, optimistic bound exhaustion → PACKING_INCONCLUSIVE + named unknowns."""
    _require_placement()
    # Known free 0; many requirements; unknown has max enough that packing may exist but bound hits first.
    repos = [f"org/m{i}" for i in range(12)]
    planner = _planner(
        selection=repos,
        manifests=[(r, [_mf("w.safetensors", 10, HW)]) for r in repos],
        numcopies=[(r, 1) for r in repos],
        drives=[_drive("known", cap=10_000), _drive("unk", cap=10_000)],
    )
    # Known free 0 → known search exhaustively infeasible quickly; optimistic has room but tiny bound.
    # Use a dedicated optimistic bound if API supports separate limits; otherwise a low feasibility
    # limit that still allows known to finish as infeasible then exhausts optimistic.
    # Contract: when known is proven infeasible and optimistic search hits the state bound,
    # code is PACKING_INCONCLUSIVE (not CAPACITY_EVIDENCE_UNKNOWN).
    r = placement.gate_b(_solver_input(
        planner,
        executable_budget={"known": 0, "unk": 0},
        max_usable_for_epoch={"known": 5, "unk": 10_000},  # known cannot host 10; unk can host all
        feasibility_limit=2,  # root + at most one more — optimistic cannot finish a 12-req tree
        optimization_limit=1,
    ))
    # Known free 0 and max 5 < 10 for each item → may structural REQUIREMENT_EXCEEDS on known-only
    # candidates, but unk is a valid candidate with max 10000 so not structural. Known search finds
    # nothing; optimistic may be inconclusive at bound 2.
    assert _code(r) == "PACKING_INCONCLUSIVE", (
        f"optimistic bound exhaustion must be PACKING_INCONCLUSIVE; got {_code(r)}")
    _assert_no_executable_assignment(r)
    assert "unk" in _relevant_unknowns(r), "must name relevant unknown drives on optimistic inconclusive"
    _assert_metadata(r, capacity_mode="guaranteed", feasibility_limit=2)


def test_contract_adversarial_greedy_false_negative_is_feasible():
    _require_placement()
    sizes = (4, 4, 3, 3)
    repos = [f"org/m{i}" for i in range(4)]
    planner = _planner(
        selection=repos,
        manifests=[(repo, [_mf("w.safetensors", size, HW)]) for repo, size in zip(repos, sizes)],
        numcopies=[(r, 1) for r in repos],
        drives=[_drive("A", cap=100), _drive("B", cap=100), _drive("C", cap=100)],
    )
    r = placement.gate_b(_solver_input(
        planner, executable_budget={"A": 6, "B": 6, "C": 5},
        max_usable_for_epoch={"A": 6, "B": 6, "C": 5},
        feasibility_limit=50_000, optimization_limit=1,
    ))
    assert _code(r) == "FEASIBLE"
    _assert_metadata(r, capacity_mode="guaranteed", feasibility_limit=50_000)
    assert r.assignment is not None


# --------------------------------------------------------------------------------------------------
# Structural codes + dependency resolution
# --------------------------------------------------------------------------------------------------
def test_contract_huge_shard_structural():
    _require_placement()
    planner = _planner(
        selection=["org/giant"],
        manifests=[("org/giant", [_mf("shard.safetensors", 10_000, HW)])],
        numcopies=[("org/giant", 1)],
        drives=[_drive("d0", cap=100_000)],
    )
    r = placement.gate_b(_solver_input(
        planner, executable_budget={"d0": 50_000}, max_usable_for_epoch={"d0": 500},
        feasibility_limit=50, optimization_limit=1,
    ))
    _assert_structural(r, "REQUIREMENT_EXCEEDS_USABLE_MAX")


def test_contract_structural_target_tier_and_unproven():
    _require_placement()
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("rep_only", role="replica", cap=10_000)],
    )
    r = placement.gate_b(_solver_input(
        planner, executable_budget={"rep_only": 9_000}, max_usable_for_epoch={"rep_only": 9_000},
        feasibility_limit=50, optimization_limit=1,
    ))
    _assert_structural(r, "TARGET_TIER_MISSING")

    planner2 = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d0", cap=10_000)],
        archived=[_arch("org/m", "d0", "w.safetensors", sha="0" * 64, obytes=100, sbytes=100)],
    )
    r2 = placement.gate_b(_solver_input(
        planner2, executable_budget={"d0": 9_000}, max_usable_for_epoch={"d0": 9_000},
        feasibility_limit=50, optimization_limit=1,
    ))
    _assert_structural(r2, "UNPROVEN_PROVENANCE")


def test_contract_failure_domain_vs_graph_dependency():
    _require_placement()
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
    r = placement.gate_b(_solver_input(
        planner,
        executable_budget={"H": 5_000, "R1": 5_000, "R2": 5_000},
        max_usable_for_epoch={"H": 9_000, "R1": 9_000, "R2": 9_000},
        feasibility_limit=5_000, optimization_limit=1,
    ))
    _assert_structural(r, "FAILURE_DOMAIN_UNSATISFIABLE")

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
        graph=bad_graph, candidates=empty_cset, drives=planner.drives,
        executable_budget=_budget_pairs({"H": 5_000, "R1": 5_000, "R2": 5_000}),
        max_usable_for_epoch=_budget_pairs({"H": 9_000, "R1": 9_000, "R2": 9_000}),
        capacity_mode="guaranteed", policy_version="tiered_v2", bounds=_bounds(50, 1),
    )
    r_bad = placement.gate_b(bad_inp)
    _assert_structural(r_bad, "GRAPH_DEPENDENCY_INVARIANT")


def test_contract_blocked_home_retains_structural_not_graph_invariant():
    """Valid blocked home (no eligible tier) keeps TARGET_TIER_MISSING; not GRAPH_DEPENDENCY_INVARIANT."""
    _require_placement()
    # numcopies=2 but no primary/raid → home blocked no_eligible_tier; replica may also block.
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", 100, HW)])],
        numcopies=[("org/m", 2)],
        drives=[_drive("R", role="replica", cap=10_000, fs_uuid="r")],
    )
    r = placement.gate_b(_solver_input(
        planner, executable_budget={"R": 9_000}, max_usable_for_epoch={"R": 9_000},
        feasibility_limit=100, optimization_limit=1,
    ))
    assert _code(r) == "TARGET_TIER_MISSING", (
        f"blocked home must retain TARGET_TIER_MISSING, not become GRAPH_*; got {_code(r)}")
    assert _code(r) != "GRAPH_DEPENDENCY_INVARIANT"
    _assert_no_executable_assignment(r)
    _assert_structural(r, "TARGET_TIER_MISSING")


def test_contract_pending_home_resolves_source_and_domain():
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
    _, cset = _graph_cset(planner)
    rep = [c for rid, cs in cset.by_requirement if rid.startswith("protected_replica:") for c in cs]
    assert rep and any(isinstance(c.source, candidates.PendingHome) for c in rep)

    r = placement.gate_b(_solver_input(
        planner, executable_budget={"H": 5_000, "R": 5_000},
        max_usable_for_epoch={"H": 9_000, "R": 9_000},
        feasibility_limit=5_000, optimization_limit=5_000,
    ))
    assert _code(r) == "FEASIBLE"
    targets = _task_targets(r.assignment)
    sources = _task_sources(r.assignment)
    home_t = targets.get("protected_home:org/m")
    rep_t = targets.get("protected_replica:org/m")
    assert home_t == "H"
    assert rep_t == "R"
    rep_src = sources.get("protected_replica:org/m")
    # Normalized source equals selected home target (complete identity, not PendingHome).
    if isinstance(rep_src, candidates.SourceIdentity):
        assert rep_src.drive_label == home_t
    elif isinstance(rep_src, str):
        assert rep_src == home_t
    else:
        # Assignment may nest source.drive_label
        assert getattr(rep_src, "drive_label", None) == home_t, (
            f"PendingHome must resolve to home target {home_t!r}; got {rep_src!r}")
    # Failure-domain independent.
    assert home_t != rep_t
    _assert_metadata(r, capacity_mode="guaranteed")


# --------------------------------------------------------------------------------------------------
# Bounds: exact truncation (no alternatives)
# --------------------------------------------------------------------------------------------------
def test_contract_feasibility_truncation_exact():
    _require_placement()
    repos = [f"org/m{i}" for i in range(5)]
    planner = _planner(
        selection=repos,
        manifests=[(r, [_mf("w.safetensors", 10, HW)]) for r in repos],
        numcopies=[(r, 1) for r in repos],
        drives=[_drive("d0", cap=10_000), _drive("d1", cap=10_000)],
    )
    r = placement.gate_b(_solver_input(
        planner, executable_budget={"d0": 30, "d1": 30},
        max_usable_for_epoch={"d0": 9_000, "d1": 9_000},
        feasibility_limit=1, optimization_limit=1,
    ))
    assert _code(r) == "PACKING_INCONCLUSIVE"
    _assert_no_executable_assignment(r)
    assert _visited(r) == 1
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
        planner, executable_budget={"d0": 5_000, "d1": 5_000},
        max_usable_for_epoch={"d0": 9_000, "d1": 9_000},
        feasibility_limit=50_000, optimization_limit=1,
    )
    gb = placement.gate_b(inp)
    assert _code(gb) == "FEASIBLE"
    truncated = placement.improve(inp, gb.assignment, emergency=None)
    assert getattr(truncated, "derivation_mode", None) == "state_truncated"
    assert getattr(truncated, "diagnostic", None) == "optimization_truncated"
    assert truncated.assignment is not None
    _assert_metadata(truncated, capacity_mode="guaranteed", optimization_limit=1)

    class _Boom:
        def __init__(self):
            self.n = 0

        def __call__(self, *args, **kwargs):
            self.n += 1
            if self.n >= 1:
                raise getattr(placement, "EmergencyResourceLimit", RuntimeError)("injected")

    inp2 = _solver_input(
        planner, executable_budget={"d0": 5_000, "d1": 5_000},
        max_usable_for_epoch={"d0": 9_000, "d1": 9_000},
        feasibility_limit=50_000, optimization_limit=50_000,
    )
    gb2 = placement.gate_b(inp2)
    assert _code(gb2) == "FEASIBLE"
    a = placement.improve(inp2, gb2.assignment, emergency=_Boom())
    b = placement.improve(inp2, gb2.assignment, emergency=_Boom())
    assert getattr(a, "derivation_mode", None) == "canonical_fallback"
    assert getattr(a, "diagnostic", None) == "optimization_resource_exhausted"
    assert a.assignment == gb2.assignment
    assert a.assignment == b.assignment
    _assert_deep_immutable(a)
    assert not any(isinstance(getattr(a, f.name, None), _Boom) for f in dataclasses.fields(a))


# --------------------------------------------------------------------------------------------------
# Adversarial objective precedence
# --------------------------------------------------------------------------------------------------
def test_contract_objective_precedence_adversarial():
    """Movement ≻ free-space ≻ idle ≻ canonical — each step adversarially against the next."""
    _require_placement()

    def target_of(assignment, rid_prefix="primary:"):
        for rid, tgt in _task_targets(assignment).items():
            if rid.startswith(rid_prefix):
                return tgt
        raise AssertionError(f"no task with prefix {rid_prefix}")

    # (1) Movement wins despite strictly worse free-space vector.
    # Partial on P (movement 40) leaves free vector worse than full re-download on F (movement 80).
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("a.safetensors", 40, HW), _mf("b.safetensors", 40, HW2)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("partial_drive", cap=10_000), _drive("fresh_drive", cap=10_000)],
        archived=[_arch("org/m", "partial_drive", "a.safetensors", sha=HW, obytes=40, sbytes=40)],
    )
    # P free 45 → after 40 rem 5; F free 1000 → after 80 rem 920. Free prefers F: (920,45)>(100,5) wait
    # partial: rem P=5, F=1000 → (1000,5); fresh: rem P=45, F=920 → (920,45). Free prefers partial!
    # Need free prefer fresh: partial rem (5, 100) = (100,5); fresh rem (45, 920)=(920,45).
    inp = _solver_input(
        planner,
        executable_budget={"partial_drive": 45, "fresh_drive": 1000},
        max_usable_for_epoch={"partial_drive": 9_000, "fresh_drive": 9_000},
        feasibility_limit=10_000, optimization_limit=50_000,
    )
    gb = placement.gate_b(inp)
    assert _code(gb) == "FEASIBLE"
    imp = placement.improve(inp, gb.assignment, emergency=None)
    assert target_of(imp.assignment) == "partial_drive", (
        "movement must dominate: partial (40) wins even though free-space prefers fresh_drive")
    score = getattr(imp, "score", None)
    assert isinstance(score, tuple) and len(score) >= 4

    # (2) Free-space wins despite worse (higher) idle count.
    # Two tasks size 50; three drives budget 100. Consolidation on one drive: free vector better, idle=2.
    # Spread: free vector worse, idle=1. Free must pick consolidation.
    planner2 = _planner(
        selection=["org/p", "org/q"],
        manifests=[
            ("org/p", [_mf("w.safetensors", 50, HW)]),
            ("org/q", [_mf("w.safetensors", 50, HW2)]),
        ],
        numcopies=[("org/p", 1), ("org/q", 1)],
        drives=[_drive("a", cap=10_000), _drive("b", cap=10_000), _drive("c", cap=10_000)],
    )
    inp2 = _solver_input(
        planner2,
        executable_budget={"a": 100, "b": 100, "c": 100},
        max_usable_for_epoch={"a": 9_000, "b": 9_000, "c": 9_000},
        feasibility_limit=10_000, optimization_limit=50_000,
    )
    gb2 = placement.gate_b(inp2)
    assert _code(gb2) == "FEASIBLE"
    imp2 = placement.improve(inp2, gb2.assignment, emergency=None)
    score2 = getattr(imp2, "score", None)
    assert isinstance(score2, tuple) and len(score2) >= 3
    assert score2[2] == 2, (
        "free-space must dominate idle: consolidation onto one drive leaves idle=2 "
        f"(got idle={score2[2]}; targets={_task_targets(imp2.assignment)})")

    # (3) Idle wins when movement and free-space vectors equal (zero-byte tasks).
    planner3 = _planner(
        selection=["org/z1", "org/z2"],
        manifests=[
            ("org/z1", [_mf("e.json", 0, HC, fmt="json", quant=None)]),
            ("org/z2", [_mf("f.json", 0, HW, fmt="json", quant=None)]),
        ],
        numcopies=[("org/z1", 1), ("org/z2", 1)],
        drives=[_drive("a", cap=10_000), _drive("b", cap=10_000), _drive("c", cap=10_000)],
    )
    inp3 = _solver_input(
        planner3,
        executable_budget={"a": 100, "b": 100, "c": 100},
        max_usable_for_epoch={"a": 9_000, "b": 9_000, "c": 9_000},
        feasibility_limit=10_000, optimization_limit=50_000,
    )
    gb3 = placement.gate_b(inp3)
    assert _code(gb3) == "FEASIBLE"
    imp3 = placement.improve(inp3, gb3.assignment, emergency=None)
    score3 = getattr(imp3, "score", None)
    assert isinstance(score3, tuple) and len(score3) >= 3
    assert score3[2] == 1, (
        "idle must dominate when movement/free equal: fewer idle drives (use 2 of 3) wins; "
        f"got idle={score3[2]} targets={_task_targets(imp3.assignment)}")

    # (4) Canonical tie-break when first three objectives equal: exact target label order.
    planner4 = _planner(
        selection=["org/t"],
        manifests=[("org/t", [_mf("w.safetensors", 10, HW)])],
        numcopies=[("org/t", 1)],
        drives=[_drive("drive-b", cap=10_000), _drive("drive-a", cap=10_000)],
    )
    inp4 = _solver_input(
        planner4,
        executable_budget={"drive-a": 100, "drive-b": 100},
        max_usable_for_epoch={"drive-a": 9_000, "drive-b": 9_000},
        feasibility_limit=10_000, optimization_limit=50_000,
    )
    gb4 = placement.gate_b(inp4)
    assert _code(gb4) == "FEASIBLE"
    imp4 = placement.improve(inp4, gb4.assignment, emergency=None)
    # Lexicographically smaller drive label wins the canonical tie.
    assert target_of(imp4.assignment) == "drive-a", (
        f"canonical tie-break must prefer drive-a over drive-b; got {target_of(imp4.assignment)!r}")
    score4 = getattr(imp4, "score", None)
    assert isinstance(score4, tuple) and len(score4) >= 4


def test_contract_remaining_free_zero_byte_task_not_idle():
    _require_placement()
    planner = _planner(
        selection=["org/z"],
        manifests=[("org/z", [_mf("empty.json", 0, HC, fmt="json", quant=None)])],
        numcopies=[("org/z", 1)],
        drives=[_drive("d0", cap=10_000), _drive("d1", cap=10_000)],
    )
    gb = placement.gate_b(_solver_input(
        planner, executable_budget={"d0": 100, "d1": 100},
        max_usable_for_epoch={"d0": 9_000, "d1": 9_000},
        feasibility_limit=1_000, optimization_limit=10_000,
    ))
    assert _code(gb) == "FEASIBLE"
    imp = placement.improve(
        _solver_input(
            planner, executable_budget={"d0": 100, "d1": 100},
            max_usable_for_epoch={"d0": 9_000, "d1": 9_000},
            feasibility_limit=1_000, optimization_limit=10_000,
        ),
        gb.assignment, emergency=None,
    )
    score = getattr(imp, "score", None)
    assert score is not None and len(score) >= 3
    assert score[2] == 1, f"zero-byte task still uses a drive; idle={score[2]}"


# --------------------------------------------------------------------------------------------------
# Workspace max accounting + capacity modes
# --------------------------------------------------------------------------------------------------
def test_contract_workspace_peak_not_summed():
    """Two nonzero-workspace tasks: fit at durable_sum+max(ws); sum-of-ws would wrongly fail."""
    _require_placement()
    # Separate repos → separate tasks. Compress shards → nonzero workspace peaks.
    size_a, size_b = 100, 250
    d_a, w_a = _file_ws(size_a)
    d_b, w_b = _file_ws(size_b)
    assert w_a > 0 and w_b > 0
    durable_sum = d_a + d_b
    max_ws = max(w_a, w_b)
    sum_ws = w_a + w_b
    assert sum_ws > max_ws
    fit_budget = durable_sum + max_ws          # correct rule
    sum_ws_budget = durable_sum + sum_ws       # incorrect sum-of-workspaces rule
    assert fit_budget < sum_ws_budget

    planner = _planner(
        selection=["org/a", "org/b"],
        manifests=[
            ("org/a", [_mf("w.safetensors", size_a, HW)]),
            ("org/b", [_mf("w.safetensors", size_b, HW2)]),
        ],
        numcopies=[("org/a", 1), ("org/b", 1)],
        drives=[_drive("d0", cap=10**12)],
    )
    r_ok = placement.gate_b(_solver_input(
        planner, executable_budget={"d0": fit_budget}, max_usable_for_epoch={"d0": 10**12},
        feasibility_limit=1_000, optimization_limit=1,
    ))
    assert _code(r_ok) == "FEASIBLE", (
        f"durable_sum+max(ws)={fit_budget} must fit (a sum-of-ws impl needs {sum_ws_budget})")

    r_short = placement.gate_b(_solver_input(
        planner, executable_budget={"d0": fit_budget - 1}, max_usable_for_epoch={"d0": 10**12},
        feasibility_limit=1_000, optimization_limit=1,
    ))
    assert _code(r_short) != "FEASIBLE"
    _assert_no_executable_assignment(r_short)


def test_contract_both_capacity_modes():
    _require_placement()
    size = 1_000
    planner = _planner(
        selection=["org/m"],
        manifests=[("org/m", [_mf("w.safetensors", size, HW)])],
        numcopies=[("org/m", 1)],
        drives=[_drive("d0", cap=10_000)],
    )
    expected = int(size * RATIO * MARGIN)
    mid = (expected + size) // 2
    for mode, want_feasible in (("guaranteed", False), ("compression_aware", True)):
        r = placement.gate_b(_solver_input(
            planner, executable_budget={"d0": mid}, max_usable_for_epoch={"d0": 9_000},
            capacity_mode=mode, feasibility_limit=1_000, optimization_limit=1,
        ))
        _assert_metadata(r, capacity_mode=mode)
        if want_feasible:
            assert _code(r) == "FEASIBLE"
        else:
            assert _code(r) != "FEASIBLE"
            _assert_no_executable_assignment(r)


# --------------------------------------------------------------------------------------------------
# 10k scale — exact FEASIBLE + complete assignment + determinism
# --------------------------------------------------------------------------------------------------
def test_contract_10k_candidates_scale():
    _require_placement()
    n_repos, n_drives = 100, 100
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
    lim_f, lim_o = 200_000, 50_000
    inp = _solver_input(
        planner, executable_budget=exec_b, max_usable_for_epoch=max_u,
        feasibility_limit=lim_f, optimization_limit=lim_o,
    )
    t0 = time.perf_counter()
    r1 = placement.gate_b(inp)
    assert time.perf_counter() - t0 < 120.0
    assert _code(r1) == "FEASIBLE"
    _assert_metadata(r1, capacity_mode="guaranteed", feasibility_limit=lim_f, optimization_limit=lim_o)
    targets = _task_targets(r1.assignment)
    assert len(targets) == n_repos, f"must assign all {n_repos} requirements; got {len(targets)}"
    assert all(rid.startswith("primary:") for rid in targets)
    visited = _visited(r1)
    assert 1 <= visited <= lim_f

    r2 = placement.gate_b(inp)
    assert r1 == r2, "gate_b must be deterministic on repeated calls"

    t1 = time.perf_counter()
    imp = placement.improve(inp, r1.assignment, emergency=None)
    assert time.perf_counter() - t1 < 120.0
    assert getattr(imp, "derivation_mode", None) in {"optimized", "state_truncated"}
    if getattr(imp, "derivation_mode", None) == "state_truncated":
        assert getattr(imp, "diagnostic", None) == "optimization_truncated"
    assert imp.assignment is not None
    assert len(_task_targets(imp.assignment)) == n_repos
    _assert_metadata(imp, capacity_mode="guaranteed", optimization_limit=lim_o)


# --------------------------------------------------------------------------------------------------
# Adapter contracts — force every graded outcome
# --------------------------------------------------------------------------------------------------
def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    return con


def _db_drive(con, label, *, role="primary", raid=False, capacity_bytes=10_000, free=None,
              fs_uuid=None):
    free = capacity_bytes if free is None else free
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes,fs_uuid) "
        "VALUES(?,?,?,?,?,?)",
        [label, role, int(raid), capacity_bytes, free, fs_uuid],
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


def _evidence(label, *, free, executable=True, max_usable=None, kind="live"):
    if not executable:
        return capacity_evidence.Evidence(
            kind="unknown", executable=False, admissible_free=0,
            code="CAPACITY_EVIDENCE_UNKNOWN", optimistic_usable_max=max_usable,
            observed_at="2026-01-01", identity_epoch=1)
    return capacity_evidence.Evidence(
        kind=kind, executable=True, admissible_free=free, observed_free=free,
        optimistic_usable_max=max_usable if max_usable is not None else free,
        observed_at="2026-01-01", identity_epoch=1)


def _plan(con, evidence_by_drive, **kwargs):
    graph = reconcile.reconcile_plan(con, "ark")
    return capacity.plan_capacity(con, graph, evidence_by_drive=evidence_by_drive, **kwargs)


def _gate_b_code_from_plan(plan) -> str:
    code = getattr(plan, "gate_b_code", None)
    if code is None:
        payload = plan.to_dict() if hasattr(plan, "to_dict") else {}
        code = payload.get("gate_b_code")
    if code is None:
        raise AssertionError(
            "#38 adapter must expose gate_b_code on CapacityPlan and in to_dict()")
    return code.value if hasattr(code, "value") else str(code)


def _assert_adapter_nonfeasible(plan, expected_code: str):
    code = _gate_b_code_from_plan(plan)
    assert code == expected_code, f"adapter gate_b_code: expected {expected_code}, got {code}"
    assert plan.feasible is False
    assert not plan.tasks, f"{expected_code} must not expose executable tasks"
    payload = plan.to_dict()
    assert payload.get("gate_b_code") == expected_code
    assert payload.get("feasible") is False
    assert payload.get("placement_policy") == "tiered_v2"
    # No false standalone capacity-short classification without the graded code authority.
    failure_codes = {
        f.code.value if hasattr(f.code, "value") else str(f.code) for f in plan.failures
    }
    if failure_codes and failure_codes <= {"CAPACITY_DURABLE_SHORT", "CAPACITY_WORKSPACE_SHORT"}:
        raise AssertionError(
            f"{expected_code} must not be projected only as proven capacity short {failure_codes}")


def test_adapter_placement_policy_tiered_v2():
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=100_000, free=100_000)
    _db_repo(con, "org/m")
    plan = _plan(con, {"d0": _evidence("d0", free=100_000, max_usable=100_000)})
    assert plan.placement_policy == "tiered_v2", (
        f"plan_capacity must project placement_policy=tiered_v2; got {plan.placement_policy!r}")
    assert plan.to_dict().get("placement_policy") == "tiered_v2"
    con.close()


def test_adapter_feasible_exclusivity_when_feasible():
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=100_000, free=100_000)
    _db_repo(con, "org/m", files=(("model.safetensors", 100, "safetensors", "bf16"),))
    plan = _plan(con, {"d0": _evidence("d0", free=100_000, max_usable=100_000)})
    code = _gate_b_code_from_plan(plan)
    assert code == "FEASIBLE"
    assert plan.feasible is True
    assert plan.tasks
    assert plan.to_dict()["feasible"] is True
    assert plan.mode.value in {"guaranteed", "compression_aware"}
    con.close()


def test_adapter_mode_labelling_both_modes():
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=100_000, free=100_000)
    _db_repo(con, "org/m")
    for mode in ("guaranteed", "compression_aware"):
        plan = _plan(con, {"d0": _evidence("d0", free=100_000, max_usable=100_000)},
                     capacity_mode=mode)
        assert plan.mode.value == mode
        assert _gate_b_code_from_plan(plan) in ALL_GATE_B_CODES
        assert plan.feasible == (_gate_b_code_from_plan(plan) == "FEASIBLE")
    con.close()


def test_adapter_structural_target_tier_missing():
    con = _mem()
    _db_drive(con, "rep", role="replica", capacity_bytes=100_000, free=100_000)
    _db_repo(con, "org/m", copies=1)
    plan = _plan(con, {"rep": _evidence("rep", free=100_000, max_usable=100_000)})
    _assert_adapter_nonfeasible(plan, "TARGET_TIER_MISSING")
    con.close()


def test_adapter_structural_unproven_provenance():
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=100_000, free=100_000)
    _db_repo(con, "org/m", files=(("model.safetensors", 100, "safetensors", "bf16"),))
    # Mismatched hash → unproven on the only eligible target.
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,stored_bytes,orig_bytes,compressed) "
        "VALUES('org/m','model.safetensors','d0',?,?,?,0)",
        ["0" * 64, 100, 100],
    )
    plan = _plan(con, {"d0": _evidence("d0", free=100_000, max_usable=100_000)})
    _assert_adapter_nonfeasible(plan, "UNPROVEN_PROVENANCE")
    con.close()


def test_adapter_structural_requirement_exceeds_usable_max():
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=1_000, free=50)
    _db_repo(con, "org/giant", files=(("model.safetensors", 10_000, "safetensors", "bf16"),))
    # Admission-supplied max below the peak; executable free also small.
    plan = _plan(con, {"d0": _evidence("d0", free=50, max_usable=100)})
    _assert_adapter_nonfeasible(plan, "REQUIREMENT_EXCEEDS_USABLE_MAX")
    con.close()


def test_adapter_capacity_evidence_unknown():
    con = _mem()
    _db_drive(con, "known", capacity_bytes=1_000, free=10)
    _db_drive(con, "unk", capacity_bytes=10_000, free=0)
    _db_repo(con, "org/m", files=(("model.safetensors", 100, "safetensors", "bf16"),))
    evidence = {
        "known": _evidence("known", free=10, max_usable=900),
        "unk": _evidence("unk", free=0, executable=False, max_usable=900),
    }
    plan = _plan(con, evidence)
    _assert_adapter_nonfeasible(plan, "CAPACITY_EVIDENCE_UNKNOWN")
    con.close()


def test_adapter_infeasible_under_admission_budget():
    con = _mem()
    _db_drive(con, "known", capacity_bytes=1_000, free=10)
    _db_repo(con, "org/m", files=(("model.safetensors", 100, "safetensors", "bf16"),))
    plan = _plan(con, {"known": _evidence("known", free=10, max_usable=900)})
    _assert_adapter_nonfeasible(plan, "INFEASIBLE_UNDER_ADMISSION_BUDGET")
    con.close()


def test_adapter_infeasible_with_unknown_at_usable_max():
    con = _mem()
    for label in ("known", "unk0", "unk1"):
        _db_drive(con, label, capacity_bytes=1_000, free=0)
    for i, size in enumerate((8, 8, 8)):
        _db_repo(con, f"org/m{i}", files=(("model.safetensors", size, "safetensors", "bf16"),))
    evidence = {
        "known": _evidence("known", free=0, max_usable=5),
        "unk0": _evidence("unk0", free=0, executable=False, max_usable=10),
        "unk1": _evidence("unk1", free=0, executable=False, max_usable=10),
    }
    plan = _plan(con, evidence)
    _assert_adapter_nonfeasible(plan, "INFEASIBLE_WITH_UNKNOWN_AT_USABLE_MAX")
    con.close()


def test_adapter_packing_inconclusive_via_bounds():
    """Deterministically drive PACKING_INCONCLUSIVE through the adapter (explicit bounds kwarg)."""
    con = _mem()
    _db_drive(con, "d0", capacity_bytes=100_000, free=100_000)
    _db_drive(con, "unk", capacity_bytes=100_000, free=0)
    for i in range(8):
        _db_repo(con, f"org/m{i}")
    evidence = {
        "d0": _evidence("d0", free=200, max_usable=9_000),
        "unk": _evidence("unk", free=0, executable=False, max_usable=9_000),
    }
    # Production adapter must accept bounds= or feasibility_state_limit= to force this outcome.
    kwargs = {}
    sig = inspect.signature(capacity.plan_capacity)
    if "bounds" in sig.parameters:
        kwargs["bounds"] = _bounds(1, 1) if _HAS_PLACEMENT else None
    elif "feasibility_state_limit" in sig.parameters:
        kwargs["feasibility_state_limit"] = 1
    else:
        raise AssertionError(
            "plan_capacity must accept bounds= or feasibility_state_limit= so the adapter can "
            "deterministically project PACKING_INCONCLUSIVE")
    if kwargs.get("bounds") is None and "bounds" in kwargs:
        raise AssertionError("placement.SolverBounds required to inject adapter bounds")
    plan = _plan(con, evidence, **{k: v for k, v in kwargs.items() if v is not None})
    _assert_adapter_nonfeasible(plan, "PACKING_INCONCLUSIVE")
    con.close()


def test_adapter_failure_domain_unsatisfiable():
    con = _mem()
    _db_drive(con, "H", role="primary", raid=True, capacity_bytes=100_000, free=100_000, fs_uuid="same")
    _db_drive(con, "R1", role="replica", capacity_bytes=100_000, free=100_000, fs_uuid="same")
    _db_drive(con, "R2", role="replica", capacity_bytes=100_000, free=100_000, fs_uuid="same")
    _db_repo(con, "org/m", copies=2)
    evidence = {
        "H": _evidence("H", free=50_000, max_usable=90_000),
        "R1": _evidence("R1", free=50_000, max_usable=90_000),
        "R2": _evidence("R2", free=50_000, max_usable=90_000),
    }
    plan = _plan(con, evidence)
    _assert_adapter_nonfeasible(plan, "FAILURE_DOMAIN_UNSATISFIABLE")
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
            short = msg.replace("\n", " ")[:180]
            print(f"  {name}: {etype}: {short}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
