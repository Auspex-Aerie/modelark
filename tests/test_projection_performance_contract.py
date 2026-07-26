"""PR-09 / #39-B Gate 1: B12 call-count instrumentation + acceptance fixture descriptor.

Harness validates a *supplied* runtime fixture descriptor and emits evidence.
It must not require production to embed operator-specific hashes.
Wall-clock contract is not satisfied by falling back to test constants.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import _pr09_gate1_fixtures as f


REQUIRED_DESCRIPTOR_FIELDS = (
    "source_sqlite_sha256",
    "prepared_canonical_input_hash",
    "prepared_projection_hash",
    "selected_repository_count",
    "model_count",
    "file_count",
    "requirement_count",
    "task_count",
    "harness_generator_version",
)


def _bench():
    import importlib
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
        if any(callable(getattr(mod, n, None)) for n in (
                "validate_acceptance_fixture_descriptor",
                "emit_acceptance_evidence",
                "count_projection_refresh_calls",
                "run_acceptance_wall_clock",
        )):
            return mod
    raise AssertionError(
        "benchmark harness APIs required: validate_acceptance_fixture_descriptor, "
        "emit_acceptance_evidence, count_projection_refresh_calls, run_acceptance_wall_clock "
        "(expected Gate-1 red)")


def test_validate_descriptor_requires_390_and_nonzero_counts():
    mod = _bench()
    validate = getattr(mod, "validate_acceptance_fixture_descriptor")
    bad = {
        "source_sqlite_sha256": "a" * 64,
        "prepared_canonical_input_hash": "b" * 64,
        "prepared_projection_hash": "c" * 64,
        "selected_repository_count": 390,
        "model_count": 0,
        "file_count": 0,
        "requirement_count": 0,
        "task_count": 0,
        "harness_generator_version": "unset-gate1",
    }
    f.assert_refuses(
        lambda: validate(bad),
        code="ACCEPTANCE_FIXTURE_INVALID",
        label="zero counts / unset generator must not validate",
    )
    one_repo = dict(bad, selected_repository_count=1, model_count=1, file_count=1,
                    requirement_count=1, task_count=1, harness_generator_version="v1")
    f.assert_refuses(
        lambda: validate(one_repo),
        code="ACCEPTANCE_FIXTURE_INVALID",
        label="pr04-like one-repo must not validate as acceptance fixture",
    )


def test_validate_accepts_complete_390_descriptor_and_emits_evidence(tmp_path):
    mod = _bench()
    validate = getattr(mod, "validate_acceptance_fixture_descriptor")
    emit = getattr(mod, "emit_acceptance_evidence")
    good = {
        "name": "rfc001_canonical_selection_390",
        "source_sqlite_sha256": "a" * 64,
        "prepared_canonical_input_hash": "b" * 64,
        "prepared_projection_hash": "c" * 64,
        "selected_repository_count": 390,
        "model_count": 390,
        "file_count": 4000,
        "requirement_count": 500,
        "task_count": 500,
        "harness_generator_version": "phase3-adapter+pr09-1",
    }
    out = validate(good)
    assert not f.is_refusal(out), out
    path = tmp_path / "evidence.json"
    emit(good, path)
    data = json.loads(path.read_text())
    for field in REQUIRED_DESCRIPTOR_FIELDS:
        assert field in data and data[field] not in (None, "", 0, "unset-gate1"), field
    assert data["selected_repository_count"] == 390


def test_wall_clock_contract_not_test_constant_fallback():
    mod = _bench()
    # Must not default warmups/runs from test file constants silently
    contract = getattr(mod, "wall_clock_contract", None) or getattr(
        mod, "WALL_CLOCK_CONTRACT", None)
    assert contract is not None and contract is not True
    data = contract() if callable(contract) else contract
    assert int(data["warmups"]) == 5
    assert int(data["measured_runs"]) == 30
    assert float(data["full_p95_seconds"]) == 2.0
    assert float(data["pure_p95_seconds"]) == 0.5
    # Explicit marker that this is production/harness owned, not test fallback
    assert data.get("source") in ("harness", "module", "rfc002") or data.get(
        "operator_approved_fixture") is True or "source" in data


def test_run_acceptance_wall_clock_fails_without_validated_descriptor():
    mod = _bench()
    run = getattr(mod, "run_acceptance_wall_clock")
    f.assert_refuses(
        lambda: run(fixture_descriptor=None),
        code="ACCEPTANCE_FIXTURE_INVALID",
        label="wall-clock without descriptor",
    )
    f.assert_refuses(
        lambda: run(fixture_descriptor={
            "selected_repository_count": 390,
            "model_count": 0,
            "harness_generator_version": "unset-gate1",
        }),
        code="ACCEPTANCE_FIXTURE_INVALID",
        label="wall-clock with incomplete descriptor",
    )


def test_alternate_fixture_requires_explicit_operator_approval_flag():
    mod = _bench()
    validate = getattr(mod, "validate_acceptance_fixture_descriptor")
    alt = {
        "name": "alternate_small",
        "source_sqlite_sha256": "d" * 64,
        "prepared_canonical_input_hash": "e" * 64,
        "prepared_projection_hash": "f" * 64,
        "selected_repository_count": 50,
        "model_count": 50,
        "file_count": 100,
        "requirement_count": 50,
        "task_count": 50,
        "harness_generator_version": "alt-1",
        "operator_approved_alternate": False,
    }
    f.assert_refuses(
        lambda: validate(alt),
        code="ACCEPTANCE_FIXTURE_INVALID",
        label="alternate without operator approval",
    )
    alt["operator_approved_alternate"] = True
    # Still may refuse if not 390 unless operator flag allows — pin behavior:
    out = validate(alt)
    # Either accepts with flag or refuses non-390 even with flag (document in production);
    # must not silently accept without the flag (covered above).
    assert out is not None


def test_call_count_instruments_real_refresh_events():
    """CI call-count: initial + completed maximal batches + typed events, independent of N files."""
    mod = _bench()
    count_fn = getattr(mod, "count_projection_refresh_calls")
    # Scenario with known batch/event structure, not file cardinality
    scenario = SimpleNamespace(
        start_events=1,
        completed_maximal_batches=3,
        typed_refresh_events=("dirty_clean", "capacity_evidence", "gated_park"),
        file_count=10_000,  # must not drive call count
        task_count=10_000,
    )
    result = count_fn(scenario)
    if isinstance(result, dict):
        calls = int(result["calls"])
        budget = int(result["budget"])
        breakdown = result.get("breakdown") or {}
    else:
        calls = int(result)
        budget = int(getattr(mod, "projection_call_budget")(scenario))
        breakdown = {}
    expected = 1 + 3 + len(scenario.typed_refresh_events)
    assert calls == expected, (
        f"calls must be start+batches+typed events={expected}, not file/task scaled; got {calls} "
        f"breakdown={breakdown}")
    assert calls <= budget
    assert budget == expected or budget >= expected
