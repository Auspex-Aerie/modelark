"""DEC-045 Phase-2 deterministic placement, byte ledger, and typed feasibility."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from unittest import mock

import pytest

import _admission_compat
from modelark import capacity, capacity_evidence, reconcile
from modelark.core import db
from modelark.web import plan_api


@pytest.fixture(autouse=True)
def _admission_snapshot_compat():
    """#35-C: synthesize admission evidence from free_bytes (pre-cutover snapshot semantics) so these
    tiered_v1 placement tests keep exercising placement, not the evidence seam (covered by PR-04)."""
    with _admission_compat.seam_patch():
        yield


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    return con


def _drive(con, label, *, role="primary", raid=False, capacity_bytes=10_000, free=None):
    free = capacity_bytes if free is None else free
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes) "
        "VALUES(?,?,?,?,?)",
        [label, role, int(raid), capacity_bytes, free],
    )
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _repo(con, repo, *, copies=1, files=(("model.safetensors", 100, "safetensors", "bf16"),)):
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,?)", [repo, copies])
    con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-01-01')", [repo])
    con.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) VALUES(?,?,?,?,?)",
        [(repo, name, size, fmt, quant) for name, size, fmt, quant in files],
    )


def _archive(con, repo, drive, rows):
    con.executemany(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_bytes,orig_bytes,compressed) "
        "VALUES(?,?,?,?,?,?)",
        [(repo, name, drive, stored, original, int(stored < original))
         for name, stored, original in rows],
    )


def _cfg(**overrides):
    return {
        "max_compress_ram_gb": 0.0,
        "stream_compress": True,
        "stream_chunk_bytes": 64,
        "threads": 1,
        **overrides,
    }


def test_unified_safety_floor_keeps_raid_minimum():
    assert capacity.headroom_bytes(20_000_000_000_000) == 296_000_000_000
    assert capacity.safety_floor(20_000_000_000_000, False) == 296_000_000_000
    assert capacity.safety_floor(20_000_000_000_000, True) == 600_000_000_000


def test_incident_shape_reserves_only_nine_missing_homes_and_fits():
    con = _mem()
    _drive(con, "raid", raid=True, capacity_bytes=100_000)
    _drive(con, "replica", role="replica", capacity_bytes=100_000)
    files = (("model.safetensors", 100, "safetensors", "bf16"),
             ("config.json", 10, "aux", None))
    for index in range(125):
        repo = f"org/model-{index:03d}"
        _repo(con, repo, copies=2, files=files)
        if index < 116:
            _archive(con, repo, "raid", (("model.safetensors", 67, 100),
                                          ("config.json", 10, 10)))
        elif index < 124:
            _archive(con, repo, "raid", (("model.safetensors", 67, 100),))

    graph = reconcile.reconcile_plan(con, "ark")
    result = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
    home_tasks = [item for item in result.tasks
                  if item.requirement_id.startswith("protected_home:")]
    replica_tasks = [item for item in result.tasks
                     if item.requirement_id.startswith("protected_replica:")]
    assert result.feasible
    assert len(home_tasks) == 9 and len(replica_tasks) == 125
    assert sum(item.budget.guaranteed_durable for item in home_tasks) == 190
    assert all(item.target_drive == "raid" for item in home_tasks)
    assert all(item.target_drive == "replica" for item in replica_tasks)


def test_replica_workspace_is_max_while_durable_is_sum():
    con = _mem()
    _drive(con, "raid", raid=True, capacity_bytes=10_000)
    _drive(con, "replica", role="replica", capacity_bytes=1_000)
    for index in range(3):
        repo = f"org/model-{index}"
        _repo(con, repo, copies=2, files=(("model.gguf", 100, "gguf", None),))
        _archive(con, repo, "raid", (("model.gguf", 100, 100),))
    graph = reconcile.reconcile_plan(con, "ark")
    result = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
    ledger = next(item for item in result.ledgers if item.drive_label == "replica")
    assert result.feasible
    assert ledger.guaranteed_durable == 300
    assert ledger.workspace_peak_guaranteed == 100
    assert ledger.required_peak(capacity.CapacityMode.GUARANTEED) == 400


def test_unknown_zero_source_size_falls_back_to_replica_estimate():
    con = _mem()
    _drive(con, "raid", raid=True)
    _drive(con, "replica", role="replica")
    _repo(con, "org/model", copies=2, files=(("model.gguf", 100, "gguf", None),))
    _archive(con, "org/model", "raid", (("model.gguf", 0, 100),))
    graph = reconcile.reconcile_plan(con, "ark")
    result = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
    task = next(item for item in result.tasks if item.kind == reconcile.TaskKind.REPLICATE)
    assert task.budget.evidence == "estimate"
    assert task.budget.guaranteed_durable == 108


def test_manifest_policy_diagnostic_makes_shadow_capacity_infeasible():
    con = _mem()
    _drive(con, "primary")
    _repo(con, "pickle/model", files=(("pytorch_model.bin", 100, "pytorch", "fp16"),))
    graph = reconcile.reconcile_plan(con, "ark")
    result = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
    assert result.blocking_diagnostics == ("MANIFEST_POLICY",)
    assert not result.feasible


def test_workspace_short_is_distinct_from_durable_short():
    con = _mem()
    _drive(con, "primary", capacity_bytes=1_000, free=200)
    _repo(con, "org/model")
    graph = reconcile.reconcile_plan(con, "ark")
    result = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
    failure = result.failures[0]
    assert failure.code == capacity.FailureCode.CAPACITY_WORKSPACE_SHORT
    assert failure.required_bytes > failure.available_bytes
    assert failure.workspace_bytes > 0


def test_dependent_replica_failure_is_deduplicated_to_missing_home_tier():
    con = _mem()
    _drive(con, "replica", role="replica", capacity_bytes=100)
    _repo(con, "org/model", copies=2)
    graph = reconcile.reconcile_plan(con, "ark")
    result = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
    assert [item.code for item in result.failures] == [capacity.FailureCode.TARGET_TIER_MISSING]
    assert result.failures[0].requirement_id == "protected_home:org/model"
    assert any(item.requirement_id == "protected_replica:org/model"
               for item in result.unassigned_intents)


def test_stale_home_pin_cannot_create_an_unledgered_feasible_task():
    con = _mem()
    _drive(con, "raid", raid=True)
    _drive(con, "replica", role="replica")
    _repo(con, "org/model", copies=2)
    graph = reconcile.reconcile_plan(con, "ark")
    home = next(item for item in graph.intents
                if item.requirement_id == "protected_home:org/model")
    stale_home = replace(home, pinned_target="removed-drive")
    graph = replace(
        graph,
        intents=tuple(stale_home if item is home else item for item in graph.intents),
    )
    result = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
    assert not result.feasible
    assert all(item.target_drive != "removed-drive" for item in result.tasks)
    failure = next(item for item in result.failures
                   if item.requirement_id == "protected_home:org/model")
    assert failure.code == capacity.FailureCode.GRAPH_INVARIANT


def test_compression_aware_mode_can_fit_where_guaranteed_durable_cannot():
    con = _mem()
    _drive(con, "primary", capacity_bytes=1_000, free=340)
    _repo(
        con, "org/model",
        files=(("a.safetensors", 100, "safetensors", "bf16"),
               ("b.safetensors", 100, "safetensors", "bf16")),
    )
    graph = reconcile.reconcile_plan(con, "ark")
    guaranteed = capacity.plan_capacity(
        con, graph, capacity_mode="guaranteed", compression_cfg=_cfg()
    )
    aware = capacity.plan_capacity(
        con, graph, capacity_mode="compression_aware", compression_cfg=_cfg()
    )
    assert not guaranteed.feasible
    assert guaranteed.failures[0].code == capacity.FailureCode.CAPACITY_WORKSPACE_SHORT
    assert aware.feasible


def test_file_preflight_is_exact_at_workspace_boundary():
    file_budget = capacity.FileBudget(
        rfilename="model.safetensors",
        guaranteed_durable=100,
        expected_durable=72,
        workspace_peak_guaranteed=109,
        workspace_peak_expected=137,
        evidence="estimate",
    )
    def _drive(admissible):
        # CapacityDrive is now backed by ONE Evidence record; usable_now == admissible_free (floor once).
        return capacity.CapacityDrive(
            drive_label="primary", role="primary", raid_backed=False, capacity_bytes=1_000,
            evidence=capacity_evidence.Evidence(
                kind="live", executable=True, admissible_free=admissible, observed_free=admissible + 50),
            safety_floor=50)

    exact = _drive(209)                                  # usable_now 209 == required peak (fits exactly)
    assert capacity.preflight_file(
        exact, file_budget, capacity.CapacityMode.GUARANTEED
    ) is None
    short = _drive(208)                                  # usable_now 208 -> 1 byte short
    failure = capacity.preflight_file(
        short, file_budget, capacity.CapacityMode.GUARANTEED,
        requirement_id="primary:org/model", task_id="fetch:primary:org/model",
    )
    assert failure is not None
    assert failure.code == capacity.FailureCode.CAPACITY_WORKSPACE_SHORT
    assert failure.shortfall_bytes == 1


def test_tiered_v1_is_deterministic_raid_first_and_smallest_replica():
    con = _mem()
    _drive(con, "raid", raid=True, capacity_bytes=4_000)
    _drive(con, "primary-big", capacity_bytes=3_000)
    _drive(con, "primary-small", capacity_bytes=2_000)
    _drive(con, "replica-small", role="replica", capacity_bytes=1_000)
    _drive(con, "replica-big", role="replica", capacity_bytes=3_000)
    _repo(con, "org/protected", copies=2, files=(("p.gguf", 200, "gguf", None),))
    _repo(con, "org/bulk-a", files=(("a.gguf", 300, "gguf", None),))
    _repo(con, "org/bulk-b", files=(("b.gguf", 250, "gguf", None),))
    graph = reconcile.reconcile_plan(con, "ark")
    first = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
    second = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
    assert first.to_dict() == second.to_dict()
    targets = {item.requirement_id: item.target_drive for item in first.tasks}
    assert targets["protected_home:org/protected"] == "raid"
    assert targets["primary:org/bulk-a"] == "raid"
    assert targets["primary:org/bulk-b"] == "raid"
    assert targets["protected_replica:org/protected"] == "replica-small"
    assert first.batch_order == ("raid", "replica-small")
    shadow = reconcile.shadow_report(con, "ark")
    assert shadow["placement_comparison"]["target_equivalent"] is True


def test_portal_shadow_explain_uses_dedicated_read_only_connection():
    con = _mem()
    _drive(con, "primary")
    _repo(con, "org/model", files=(("model.gguf", 100, "gguf", None),))
    forbidden_lock = mock.MagicMock()
    forbidden_lock.__enter__.side_effect = AssertionError("must not hold data._lock")
    with mock.patch("modelark.core.db.connect", return_value=con) as connect, \
         mock.patch.object(plan_api.data, "_lock", forbidden_lock):
        result = plan_api.shadow_explain()
    connect.assert_called_once_with(read_only=True)
    forbidden_lock.__enter__.assert_not_called()
    assert result["capacity"]["placement_policy"] == "tiered_v1"


def test_portal_shadow_explain_returns_typed_phase2_error_and_closes_connection():
    con = _mem()
    with mock.patch("modelark.core.db.connect", return_value=con), \
         mock.patch("modelark.reconcile.shadow_report", side_effect=KeyError("manifest")):
        result = plan_api.shadow_explain()
    assert result == {
        "ok": False,
        "error": {"code": "SHADOW_CAPACITY_ERROR", "detail": "KeyError: 'manifest'"},
    }
    try:
        con.execute("SELECT 1")
        raise AssertionError("shadow connection must close after an error")
    except sqlite3.ProgrammingError:
        pass


def test_shadow_comparison_never_normalizes_away_changed_target():
    con = _mem()
    _drive(con, "raid", raid=True)
    _drive(con, "wrong-primary")
    _repo(con, "org/model", files=(("model.gguf", 100, "gguf", None),))
    mutated = {
        "primary": {"assign": {"wrong-primary": [{"repo": "org/model", "size": 100}]}},
        "replica": {"assign": {}},
    }
    with mock.patch("modelark.librarian.plan_placements", return_value=mutated):
        report = reconcile.shadow_report(con, "ark")
    comparison = report["placement_comparison"]
    assert comparison["target_equivalent"] is False
    assert comparison["target_mismatches"] == [{
        "requirement_id": "primary:org/model",
        "legacy": "wrong-primary",
        "tiered_v1": "raid",
    }]


def test_phase2_candidate_cross_product_stays_bounded():
    con = _mem()
    drives = [f"drive-{index:02d}" for index in range(10)]
    for drive in drives:
        _drive(con, drive, capacity_bytes=10**9)
    repos = [f"org/model-{index:04d}" for index in range(1000)]
    con.execute("BEGIN")
    con.executemany("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [(repo,) for repo in repos])
    con.executemany(
        "INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-01-01')",
        [(repo,) for repo in repos],
    )
    files = [(repo, "model.gguf", 100, "gguf", None) for repo in repos]
    files.extend(
        (repo, f"ignored-{index:02d}.onnx", 1, "onnx", None)
        for repo in repos for index in range(10)
    )
    con.executemany(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) VALUES(?,?,?,?,?)", files
    )
    archived = [
        (repo, f"ignored-{index:02d}.onnx", drive, 1, 1, 0)
        for repo in repos for index in range(10) for drive in drives
    ]
    con.executemany(
        "INSERT INTO archived(repo_id,rfilename,drive_label,stored_bytes,orig_bytes,compressed) "
        "VALUES(?,?,?,?,?,?)",
        archived,
    )
    con.execute("COMMIT")
    del files, archived

    graph = reconcile.reconcile_plan(con, "ark")
    calls = 0
    original = capacity._fetch_budget

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    try:
        started = time.perf_counter()
        with mock.patch.object(capacity, "_fetch_budget", side_effect=counted):
            result = capacity.plan_capacity(con, graph, compression_cfg=_cfg())
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"10k-candidate placement took {elapsed:.3f}s"
        assert calls == 10_000
        assert len(result.tasks) == 1000 and result.feasible
    finally:
        con.close()


def test_canonical_and_deprecated_capacity_arguments_cannot_disagree():
    try:
        capacity.plan_capacity(
            None, None, capacity_mode="guaranteed", provisioning="compressed"
        )
        raise AssertionError("conflicting compatibility arguments must be refused")
    except ValueError as exc:
        assert "disagree" in str(exc)
