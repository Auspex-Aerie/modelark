"""PR-09 Gate 1: B12 — recompute fixture identity from SQLite; instrument real refresh seam."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import _pr09_gate1_fixtures as f
from modelark.core import db


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
                "recompute_fixture_identity",
        )):
            return mod
    raise AssertionError(
        "benchmark harness APIs required including recompute_fixture_identity "
        "(expected Gate-1 red)")


def _make_sqlite_fixture(tmp_path: Path, *, n_repos: int) -> Path:
    path = tmp_path / f"fix_{n_repos}.sqlite"
    con = sqlite3.connect(str(path))
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    for i in range(n_repos):
        repo = f"org/m{i:04d}"
        con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?,?,100,'safetensors','bf16',?)",
            [repo, "model.safetensors", f"{i:064d}"[:64].ljust(64, "0")])
        con.execute(
            "INSERT INTO selection(repo_id,finalized_at) VALUES(?, '2026-01-01')", [repo])
    con.commit()
    con.close()
    return path


def test_recompute_identity_from_actual_sqlite_rejects_fabricated_descriptor(tmp_path):
    mod = _bench()
    recompute = getattr(mod, "recompute_fixture_identity")
    validate = getattr(mod, "validate_acceptance_fixture_descriptor")
    path = _make_sqlite_fixture(tmp_path, n_repos=3)
    actual = recompute(path)
    assert actual["selected_repository_count"] == 3
    assert actual["model_count"] == 3
    assert actual["file_count"] == 3
    assert len(actual["source_sqlite_sha256"]) == 64
    # Fabricated 390 descriptor must not validate against this file
    fabricated = {
        "source_sqlite_sha256": "a" * 64,
        "prepared_canonical_input_hash": "b" * 64,
        "prepared_projection_hash": "c" * 64,
        "selected_repository_count": 390,
        "model_count": 390,
        "file_count": 4000,
        "requirement_count": 500,
        "task_count": 500,
        "harness_generator_version": "v1",
        "sqlite_path": str(path),
    }
    f.assert_refuses(
        lambda: validate(fabricated, recompute=recompute),
        code="ACCEPTANCE_FIXTURE_MISMATCH",
        label="fabricated counts/hashes vs actual sqlite",
    )


def test_validate_requires_operator_identity_match_on_390_copy(tmp_path):
    """Operator-approved identity is separate; harness recomputes and compares."""
    mod = _bench()
    recompute = getattr(mod, "recompute_fixture_identity")
    validate = getattr(mod, "validate_acceptance_fixture_descriptor")
    # Build a 390-repo fixture (may be heavy but synthetic/minimal rows)
    path = _make_sqlite_fixture(tmp_path, n_repos=390)
    actual = recompute(path)
    operator_identity = {
        **actual,
        "prepared_projection_hash": actual.get(
            "prepared_projection_hash") or actual["prepared_canonical_input_hash"],
        "requirement_count": actual.get("requirement_count") or actual["selected_repository_count"],
        "task_count": actual.get("task_count") or actual["selected_repository_count"],
        "harness_generator_version": "rfc001-390-v1",
        "sqlite_path": str(path),
    }
    # Missing operator bundle refuses
    f.assert_refuses(
        lambda: validate(operator_identity, operator_approved_identity=None),
        code="ACCEPTANCE_FIXTURE_INVALID",
        label="missing operator identity",
    )
    # Matching operator identity accepts
    out = validate(operator_identity, operator_approved_identity=dict(operator_identity))
    assert not f.is_refusal(out), out


def test_zero_counts_and_unset_generator_refuse(tmp_path):
    mod = _bench()
    validate = getattr(mod, "validate_acceptance_fixture_descriptor")
    path = _make_sqlite_fixture(tmp_path, n_repos=1)
    bad = {
        "sqlite_path": str(path),
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
        label="zero/unset",
    )


def test_alternate_cannot_self_authorize(tmp_path):
    mod = _bench()
    validate = getattr(mod, "validate_acceptance_fixture_descriptor")
    recompute = getattr(mod, "recompute_fixture_identity")
    path = _make_sqlite_fixture(tmp_path, n_repos=50)
    actual = recompute(path)
    alt = {
        **actual,
        "prepared_projection_hash": actual["prepared_canonical_input_hash"],
        "requirement_count": 50,
        "task_count": 50,
        "harness_generator_version": "alt-1",
        "sqlite_path": str(path),
        "operator_approved_alternate": True,  # self-flag insufficient
    }
    f.assert_refuses(
        lambda: validate(alt, operator_approved_identity=None),
        code="ACCEPTANCE_FIXTURE_INVALID",
        label="self-authorized alternate without separate operator identity",
    )


def test_wall_clock_fails_without_validated_descriptor():
    mod = _bench()
    run = getattr(mod, "run_acceptance_wall_clock")
    f.assert_refuses(
        lambda: run(fixture_descriptor=None),
        code="ACCEPTANCE_FIXTURE_INVALID",
        label="no descriptor",
    )


def test_call_count_instruments_executor_refresh_seam():
    """Instrument real project_pure / refresh calls — not arithmetic on a fake object."""
    mod = _bench()
    count_fn = getattr(mod, "count_projection_refresh_calls")
    # Provide a callable refresh seam that records invocations
    calls = []

    def refresh_seam(event_type):
        calls.append(event_type)
        return SimpleNamespace(projection_hash="d" * 64, tasks=())

    scenario = SimpleNamespace(
        events=("start", "batch_complete", "batch_complete", "batch_complete",
                "dirty_clean", "capacity_evidence", "gated_park"),
        file_count=10_000,
        task_count=10_000,
        refresh=refresh_seam,
    )
    result = count_fn(scenario)
    # Must have invoked refresh for each event
    assert len(calls) == len(scenario.events), (
        f"must instrument real refresh seam per event; calls={calls}")
    n = int(result["calls"] if isinstance(result, dict) else result)
    assert n == len(scenario.events), (
        f"call count must equal refresh events ({len(scenario.events)}), "
        f"independent of file/task count; got {n}")
    assert n == 1 + 3 + 3  # start + 3 batches + 3 typed
