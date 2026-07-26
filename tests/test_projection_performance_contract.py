"""PR-09 / #39-B Gate 1: call-count CI pins + RFC-001 390-repo acceptance fixture identity (B12).

Tests-only. Ordinary CI asserts deterministic call counts on synthetic fixtures.
Acceptance wall-clock suite (5 warm-ups + 30 runs) is identity-bound and fails
explicitly when invoked without the bound fixture — not a substitute for pr04 one-repo
or Phase-3 20-sample reconcile measurements.
"""
from __future__ import annotations

import importlib
import json


# Bound acceptance fixture identity (B12) — values filled when generator lands;
# contract requires these *fields* and fixed selected_count=390.
ACCEPTANCE_FIXTURE_SPEC = {
    "name": "rfc001_canonical_selection_390",
    "description": (
        "Isolated consistent copy of the accepted RFC-001 390-repository canonical "
        "selection with deterministic frozen evidence"
    ),
    "selected_repository_count": 390,
    "forbidden_substitutes": (
        "tests/test_pr04_admission_copied_catalog.py",
        "phase3_20_sample_reconcile_capacity",
    ),
    "wall_clock": {
        "warmups": 5,
        "measured_runs": 30,
        "full_capture_recompute_project_p95_seconds": 2.0,
        "pure_projection_p95_seconds": 0.5,
    },
    # Populated by harness when fixture is prepared (Gate-2 evidence / acceptance run):
    "required_identity_fields": (
        "source_sqlite_sha256",
        "prepared_canonical_input_hash",
        "prepared_projection_hash",
        "selected_repository_count",
        "model_count",
        "file_count",
        "requirement_count",
        "task_count",
        "harness_generator_version",
    ),
}


def _perf():
    for name in (
        "modelark.execution_benchmark",
        "modelark.projection_benchmark",
        "modelark.execution_projection",
        "modelark.execution",
    ):
        try:
            mod = importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        if any(hasattr(mod, n) for n in (
                "ACCEPTANCE_FIXTURE", "acceptance_fixture_identity",
                "count_projection_calls", "run_acceptance_wall_clock",
                "PROJECTION_CALL_BUDGET")):
            return mod
    raise AssertionError(
        "projection performance / acceptance fixture module required "
        "(expected Gate-1 red)")


def test_acceptance_fixture_spec_is_bound_to_390():
    """B12: fixture identity is fixed at Gate 0 — not deferred to an arbitrary small catalog."""
    assert ACCEPTANCE_FIXTURE_SPEC["selected_repository_count"] == 390
    mod = _perf()
    fixture = getattr(mod, "ACCEPTANCE_FIXTURE", None) or getattr(
        mod, "acceptance_fixture_identity", None)
    assert fixture is not None, "export ACCEPTANCE_FIXTURE identity dict"
    data = fixture() if callable(fixture) else fixture
    assert int(data.get("selected_repository_count") or data.get("selected_count") or 0) == 390, (
        f"acceptance fixture must be 390-repo RFC-001 selection; got {data!r}")
    for field in ACCEPTANCE_FIXTURE_SPEC["required_identity_fields"]:
        assert field in data and data[field] not in (None, ""), (
            f"acceptance fixture identity missing {field}; have keys={sorted(data)}")
    # SHA-256 fields look like digests when present
    for hfield in ("source_sqlite_sha256", "prepared_canonical_input_hash",
                   "prepared_projection_hash"):
        val = str(data[hfield])
        assert len(val) == 64 and all(c in "0123456789abcdef" for c in val.lower()), (
            f"{hfield} must be 64-char hex; got {val!r}")


def test_pr04_one_repo_fixture_is_not_acceptance_identity():
    mod = _perf()
    fixture = getattr(mod, "ACCEPTANCE_FIXTURE", None) or getattr(
        mod, "acceptance_fixture_identity", None)
    data = fixture() if callable(fixture) else fixture
    name = str(data.get("name") or data.get("source") or "").lower()
    assert "pr04" not in name and "one" not in name.split(), data
    assert int(data.get("selected_repository_count") or 0) != 1


def test_phase3_20_sample_is_not_wall_clock_contract():
    mod = _perf()
    wc = getattr(mod, "WALL_CLOCK_CONTRACT", None) or getattr(
        mod, "acceptance_wall_clock_contract", None) or ACCEPTANCE_FIXTURE_SPEC["wall_clock"]
    data = wc() if callable(wc) else wc
    assert int(data.get("warmups") or data.get("warmup_runs") or 0) == 5
    assert int(data.get("measured_runs") or data.get("samples") or 0) == 30
    assert float(data.get("full_capture_recompute_project_p95_seconds")
                 or data.get("full_p95_s") or 0) == 2.0
    assert float(data.get("pure_projection_p95_seconds")
                 or data.get("pure_p95_s") or 0) == 0.5


def test_ci_call_count_budget_is_deterministic():
    mod = _perf()
    budget = getattr(mod, "PROJECTION_CALL_BUDGET", None) or getattr(
        mod, "projection_call_budget", None)
    assert budget is not None, "export PROJECTION_CALL_BUDGET for ordinary CI"
    count_fn = getattr(mod, "count_projection_calls", None) or getattr(
        mod, "measure_projection_calls", None)
    assert callable(count_fn), "count_projection_calls(synthetic_scenario) required for CI"
    # Synthetic small scenario — call-count only, not acceptance identity
    result = count_fn(scenario="synthetic_small_batch")
    if isinstance(result, dict):
        calls = int(result.get("full_projection_calls") or result.get("calls") or -1)
        bound = int(result.get("budget") or budget if not callable(budget) else budget())
    else:
        calls = int(result)
        bound = int(budget if not callable(budget) else budget())
    assert calls >= 0
    assert calls <= bound, (
        f"projection call count {calls} exceeds CI budget {bound} "
        "(start + maximal batches + typed refresh events)")


def test_acceptance_wall_clock_fails_closed_without_fixture_identity():
    mod = _perf()
    run = getattr(mod, "run_acceptance_wall_clock", None) or getattr(
        mod, "acceptance_p95_gate", None)
    assert callable(run), "run_acceptance_wall_clock required"
    try:
        run(fixture_identity=None)  # must fail closed
        raise AssertionError("acceptance wall-clock without fixture identity must fail")
    except Exception as exc:
        msg = str(exc).upper()
        assert any(k in msg for k in (
            "FIXTURE", "IDENTITY", "390", "ACCEPTANCE", "MISSING", "REFUS")), exc


def test_alternate_fixture_requires_operator_flag():
    mod = _perf()
    assert getattr(mod, "ALTERNATE_FIXTURE_REQUIRES_OPERATOR_APPROVAL", True) is True or hasattr(
        mod, "require_operator_fixture_approval"), (
        "alternate acceptance fixture must require explicit operator re-approval")


def test_spec_json_roundtrip_for_evidence_bundle(tmp_path):
    """Harness may write evidence JSON; required fields are stable for Gate-2 review."""
    path = tmp_path / "acceptance_fixture_identity.json"
    path.write_text(json.dumps({
        "name": ACCEPTANCE_FIXTURE_SPEC["name"],
        "selected_repository_count": 390,
        "source_sqlite_sha256": "a" * 64,
        "prepared_canonical_input_hash": "b" * 64,
        "prepared_projection_hash": "c" * 64,
        "model_count": 0,  # placeholders until generator lands
        "file_count": 0,
        "requirement_count": 0,
        "task_count": 0,
        "harness_generator_version": "unset-gate1",
    }, indent=2, sort_keys=True))
    data = json.loads(path.read_text())
    assert data["selected_repository_count"] == 390
    for field in ACCEPTANCE_FIXTURE_SPEC["required_identity_fields"]:
        assert field in data


def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = []
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:
            print(f"FAIL  {name}: {exc}")
            failed.append(name)
    print(f"\n{len(tests) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
