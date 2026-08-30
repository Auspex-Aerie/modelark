"""INC-025 Gate-1 contracts — expected red until fill drain emits typed ManifestFile.

Locked design (Gate 0 accepted):
  - Approved proposal_files are the frozen file authority (fill.py:616).
  - Carry storage_action through _projection_work_units.
  - At the FETCH drain join, build archive_manifest.ManifestFile from approved
    missing rows — never re-read live catalog/policy.
  - Fail closed with typed APPROVED_INPUT_CHANGED refusals (exact shapes below).
  - Never broaden empty intersection to all unit rows.
  - fetch.py, archive_manifest.py, schema, replica path unchanged.

No production code in this gate.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import _pr09_gate1_fixtures as f
from modelark import archive_manifest, fetch
from modelark import fill as fill_mod
from modelark.proposal import Refusal
from modelark.reconcile import TaskKind


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _proj_fetch(repo="org/gte", target="d0", rid=None):
    rid = rid or f"primary:{repo}"
    return SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id=repo, target_drive=target,
        source_drive=None, requirement_id=rid,
        schedule_state="ready", order_key=1,
        guaranteed_durable=1010, expected_durable=1010,
    ),))


def _proj_replica(repo="org/gte", target="d1", source="d0", rid=None):
    rid = rid or f"replica:{repo}"
    return SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id=repo, target_drive=target,
        source_drive=source, requirement_id=rid,
        schedule_state="waiting_dependency", order_key=1,
        guaranteed_durable=1010, expected_durable=1010,
    ),))


def _gte_proposal_files(rid="primary:org/gte", *, include_onnx=False):
    """Approved rows as INC-024 would store them (planned set + storage_action)."""
    rows = [
        {
            "requirement_id": rid, "rfilename": "model.safetensors",
            "size_bytes": 1000, "orig_sha256": "a" * 64,
            "format": "safetensors", "quant": "bf16", "storage_action": "compress",
        },
        {
            "requirement_id": rid, "rfilename": "config.json",
            "size_bytes": 10, "orig_sha256": "b" * 64,
            "format": "aux", "quant": None, "storage_action": "raw",
        },
    ]
    if include_onnx:
        rows.append({
            "requirement_id": rid, "rfilename": "onnx/model.onnx",
            "size_bytes": 5000, "orig_sha256": "c" * 64,
            "format": "onnx", "quant": None, "storage_action": "raw",
        })
    return rows


def _fetch_units(con, projection, proposal_files):
    units = fill_mod._projection_work_units(
        con, projection, proposal_files=proposal_files, require_proposal_files=True)
    return [u for u in units if getattr(u, "kind", None) == TaskKind.FETCH]


def _empty_fetch_run_outcome():
    return {
        "stored_repos": [],
        "failed_repos": [],
        "capacity_failure": None,
        "terminal_failure": None,
        "terminal_repo": None,
        "throttled": False,
        "stopped": False,
        "drive_unwritable": False,
        "gated_repos": [],
        "gated_retry": None,
    }


def _run_real_drain_capture_task_manifests(con, projection, proposal_files):
    """Execute real fill._drain_projection FETCH branch; capture task_manifests.

    Proves wiring through the drain, not an unused helper. Patches only:
      - fill._mounted → always mounted
      - fill.fetch.run → capture spy (does not patch fetch_model / as_fetch_record)
    """
    captured: dict = {}

    def spy_run(*args, **kwargs):
        captured["task_manifests"] = kwargs.get("task_manifests")
        captured["repos"] = kwargs.get("repos")
        captured["drive_label"] = kwargs.get("drive_label")
        return _empty_fetch_run_outcome()

    ctx = fetch.RunCtx(con=con, check_hf_auth=False)
    session_start = SimpleNamespace(
        projection=projection,
        session=SimpleNamespace(
            approved_proposal_id="inc025-gate1",
            fencing_token=1,
            session_id="s-inc025",
        ),
        execution_config=SimpleNamespace(capacity_mode="guaranteed"),
        _proposal_files=list(proposal_files),
    )
    with mock.patch.object(fill_mod, "_mounted", return_value=(True, True)), \
            mock.patch.object(fill_mod.fetch, "run", side_effect=spy_run):
        result = fill_mod._drain_projection(
            ctx, session_start,
            plan_id="ark",
            max_24h_gb=0,
            repo_scope=None,
            guided=False,
            poll_secs=0.01,
            child_fds=(),
        )
    assert "task_manifests" in captured, (
        f"drain must reach fetch.run with task_manifests; result={result!r} "
        f"captured_keys={sorted(captured)}")
    return captured["task_manifests"], result


def _assert_manifest_files(manifest_tuple, *, label: str):
    assert manifest_tuple, f"{label}: empty drain manifest"
    for item in manifest_tuple:
        assert isinstance(item, archive_manifest.ManifestFile), (
            f"{label}: drain must emit archive_manifest.ManifestFile, "
            f"got {type(item).__name__}: {item!r}")
        assert item.storage_action in ("compress", "raw"), (
            f"{label}: storage_action must be compress|raw, got {item.storage_action!r}")


def _assert_refusal_shape(exc: Refusal, *, evidence: dict):
    """Exact evidence dict equality — no extra unasserted fields, no nonempty-only checks."""
    assert exc.code == "APPROVED_INPUT_CHANGED", exc
    got = dict(exc.evidence) if isinstance(exc.evidence, dict) else {}
    assert got == evidence, f"evidence must equal exactly {evidence!r}, got {got!r}"
    assert tuple(exc.actions or ()) == ("preview_again",), exc.actions


# ---------------------------------------------------------------------------
# storage_action on work units
# ---------------------------------------------------------------------------


def test_c01_work_units_preserve_storage_action_from_proposal_files():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    pfiles = _gte_proposal_files()
    units = _fetch_units(con, _proj_fetch(), pfiles)
    assert units, "expected a FETCH unit when approved files are missing on target"
    by_name = {fr.rfilename: fr for fr in units[0].file_rows}
    assert "model.safetensors" in by_name and "config.json" in by_name
    assert getattr(by_name["model.safetensors"], "storage_action", None) == "compress", (
        "INC-025: work-unit file_rows must carry storage_action=compress for safetensors")
    assert getattr(by_name["config.json"], "storage_action", None) == "raw", (
        "INC-025: work-unit file_rows must carry storage_action=raw for aux")


# ---------------------------------------------------------------------------
# Real drain wiring (prevents helper-without-wiring false green)
# ---------------------------------------------------------------------------


def test_c02_real_drain_passes_manifest_file_to_fetch_run():
    """Load-bearing: real drain must call _fetch_task_manifests and pass its return to fetch.run.

    Prevents: unused helper + old inline mapping, or unused helper + separate happy-path
    inline conversion. Wraps the real helper (does not replace its behavior).
    Expected-red until Gate 2: helper must be callable and wired through the drain.
    """
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    pfiles = _gte_proposal_files()

    real_helper = getattr(fill_mod, "_fetch_task_manifests", None)
    assert callable(real_helper), (
        "INC-025: fill._fetch_task_manifests must exist and be callable "
        "(expected-red until Gate 2; drain must use this helper)")

    helper_trace: dict = {"n": 0, "return": None}

    def recording_helper(*args, **kwargs):
        helper_trace["n"] += 1
        out = real_helper(*args, **kwargs)
        helper_trace["return"] = out
        return out

    run_capture: dict = {}

    def spy_run(*args, **kwargs):
        run_capture["task_manifests"] = kwargs.get("task_manifests")
        return _empty_fetch_run_outcome()

    ctx = fetch.RunCtx(con=con, check_hf_auth=False)
    session_start = SimpleNamespace(
        projection=_proj_fetch(),
        session=SimpleNamespace(
            approved_proposal_id="inc025-gate1",
            fencing_token=1,
            session_id="s-inc025",
        ),
        execution_config=SimpleNamespace(capacity_mode="guaranteed"),
        _proposal_files=list(pfiles),
    )
    with mock.patch.object(fill_mod, "_mounted", return_value=(True, True)), \
            mock.patch.object(
                fill_mod, "_fetch_task_manifests", side_effect=recording_helper), \
            mock.patch.object(fill_mod.fetch, "run", side_effect=spy_run):
        result = fill_mod._drain_projection(
            ctx, session_start,
            plan_id="ark", max_24h_gb=0, repo_scope=None,
            guided=False, poll_secs=0.01, child_fds=(),
        )

    assert helper_trace["n"] == 1, (
        f"one-batch FETCH fixture must call _fetch_task_manifests exactly once, "
        f"got n={helper_trace['n']}; result={result!r}")
    assert "task_manifests" in run_capture, (
        f"drain must pass task_manifests to fetch.run; result={result!r}")
    assert run_capture["task_manifests"] is helper_trace["return"], (
        "task_manifests passed to fetch.run must be the exact object returned by "
        "_fetch_task_manifests (not a separate inline conversion)")

    manifests = run_capture["task_manifests"]
    assert "org/gte" in manifests, manifests
    _assert_manifest_files(manifests["org/gte"], label="c02-drain")
    by_name = {m.rfilename: m for m in manifests["org/gte"]}
    assert set(by_name) == {"model.safetensors", "config.json"}
    assert by_name["model.safetensors"].storage_action == "compress"
    assert by_name["config.json"].storage_action == "raw"


def test_c03_real_drain_manifest_is_exact_approved_missing_subset():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/gte','config.json','d0',0,10,10,?)",
        ["b" * 64])
    pfiles = _gte_proposal_files()
    manifests, _result = _run_real_drain_capture_task_manifests(
        con, _proj_fetch(), pfiles)
    names = [m.rfilename for m in manifests["org/gte"]]
    _assert_manifest_files(manifests["org/gte"], label="c03-drain")
    assert names == ["model.safetensors"], (
        f"drain-captured task_manifests must be exact approved missing subset, got {names}")
    assert manifests["org/gte"][0].storage_action == "compress"


def test_c04_catalog_onnx_not_invented_in_drain_manifest():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/gte','onnx/model.onnx',5000,'onnx',NULL,?)", ["c" * 64])
    pfiles = _gte_proposal_files(include_onnx=False)
    manifests, _result = _run_real_drain_capture_task_manifests(
        con, _proj_fetch(), pfiles)
    names = {
        (m.rfilename if hasattr(m, "rfilename") else None)
        for m in manifests.get("org/gte", ())
    }
    assert "onnx/model.onnx" not in names, names
    assert "model.safetensors" in names and "config.json" in names


def test_c05_drain_captured_manifest_crosses_real_fetch_model():
    """Capture from real drain, then real fetch_model (no consumer-side patches)."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    pfiles = _gte_proposal_files()
    manifests, _result = _run_real_drain_capture_task_manifests(
        con, _proj_fetch(), pfiles)
    manifest = manifests["org/gte"]
    _assert_manifest_files(manifest, label="c05-precondition")

    ctx = fetch.RunCtx(con=con)
    marker = RuntimeError("download attempted for approved missing file")
    with mock.patch.object(fetch, "_download_shard", side_effect=marker) as download, \
            mock.patch.object(Path, "mkdir", return_value=None):
        with pytest.raises(RuntimeError) as ei:
            fetch.fetch_model(
                ctx, "org/gte", Path("/tmp/modelark-inc025-gate1"), "d0", False,
                {"max_compress_ram_gb": 4.0, "threads": 1},
                manifest=manifest,
            )
        assert ei.value is marker or "download attempted" in str(ei.value)
    assert download.call_count >= 1


# ---------------------------------------------------------------------------
# Typed fail-closed (exact Refusal only — no broad exception success)
# ---------------------------------------------------------------------------


def test_c06_missing_approved_row_refuses_typed():
    """missing rfilename with no approved row → missing_proposal_file_authority.

    Calls fill._fetch_task_manifests (Gate-2 surface the drain must invoke). Only
    exact Refusal shapes count as success — AttributeError / other exceptions fail
    the contract (expected-red until production lands).
    """
    unit = SimpleNamespace(
        requirement_id="primary:org/gte",
        repo_id="org/gte",
        kind=TaskKind.FETCH,
        missing_files=("ghost.bin",),
        file_rows=(
            SimpleNamespace(
                rfilename="model.safetensors", size_bytes=1000, sha256="a" * 64,
                format="safetensors", quant="bf16", storage_action="compress"),
        ),
    )
    with pytest.raises(Refusal) as ei:
        fill_mod._fetch_task_manifests([unit])
    _assert_refusal_shape(ei.value, evidence={
        "reason": "missing_proposal_file_authority",
        "requirement_id": "primary:org/gte",
        "repo_id": "org/gte",
        "rfilename": "ghost.bin",
    })


def test_c07_ambiguous_duplicate_approved_rows_refuses_typed():
    """Two approved rows for one missing rfilename → ambiguous_proposal_file_authority."""
    unit = SimpleNamespace(
        requirement_id="primary:org/gte",
        repo_id="org/gte",
        kind=TaskKind.FETCH,
        missing_files=("model.safetensors",),
        file_rows=(
            SimpleNamespace(
                rfilename="model.safetensors", size_bytes=1000, sha256="a" * 64,
                format="safetensors", quant="bf16", storage_action="compress"),
            SimpleNamespace(
                rfilename="model.safetensors", size_bytes=1000, sha256="a" * 64,
                format="safetensors", quant="bf16", storage_action="raw"),
        ),
    )
    with pytest.raises(Refusal) as ei:
        fill_mod._fetch_task_manifests([unit])
    _assert_refusal_shape(ei.value, evidence={
        "reason": "ambiguous_proposal_file_authority",
        "requirement_id": "primary:org/gte",
        "repo_id": "org/gte",
        "rfilename": "model.safetensors",
        "matches": 2,
    })


def test_c08_absent_storage_action_refuses_typed():
    unit = SimpleNamespace(
        requirement_id="primary:org/gte",
        repo_id="org/gte",
        kind=TaskKind.FETCH,
        missing_files=("model.safetensors",),
        file_rows=(
            SimpleNamespace(
                rfilename="model.safetensors", size_bytes=1000, sha256="a" * 64,
                format="safetensors", quant="bf16"),  # no storage_action → None
        ),
    )
    with pytest.raises(Refusal) as ei:
        fill_mod._fetch_task_manifests([unit])
    _assert_refusal_shape(ei.value, evidence={
        "reason": "invalid_storage_action",
        "requirement_id": "primary:org/gte",
        "repo_id": "org/gte",
        "rfilename": "model.safetensors",
        "storage_action": None,
    })


def test_c09_invalid_storage_action_archive_refuses_typed():
    unit = SimpleNamespace(
        requirement_id="primary:org/gte",
        repo_id="org/gte",
        kind=TaskKind.FETCH,
        missing_files=("model.safetensors",),
        file_rows=(
            SimpleNamespace(
                rfilename="model.safetensors", size_bytes=1000, sha256="a" * 64,
                format="safetensors", quant="bf16", storage_action="archive"),
        ),
    )
    with pytest.raises(Refusal) as ei:
        fill_mod._fetch_task_manifests([unit])
    _assert_refusal_shape(ei.value, evidence={
        "reason": "invalid_storage_action",
        "requirement_id": "primary:org/gte",
        "repo_id": "org/gte",
        "rfilename": "model.safetensors",
        "storage_action": "archive",
    })


# ---------------------------------------------------------------------------
# Replica unchanged
# ---------------------------------------------------------------------------


def test_c10_replica_units_unchanged_by_fetch_manifest_join():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    pfiles = _gte_proposal_files(rid="replica:org/gte")
    units = fill_mod._projection_work_units(
        con, _proj_replica(), proposal_files=pfiles, require_proposal_files=True)
    assert units, "expected a replica-side unit"
    assert units[0].schedule_state == "waiting_dependency"
    assert units[0].kind is None
    # Real drain with only waiting replica must not call fetch.run for FETCH.
    captured: dict = {}

    def spy_run(*a, **k):
        captured["called"] = True
        return _empty_fetch_run_outcome()

    ctx = fetch.RunCtx(con=con, check_hf_auth=False)
    session_start = SimpleNamespace(
        projection=_proj_replica(),
        session=SimpleNamespace(
            approved_proposal_id="inc025-gate1-r", fencing_token=1, session_id="s-r"),
        execution_config=SimpleNamespace(capacity_mode="guaranteed"),
        _proposal_files=list(pfiles),
    )
    with mock.patch.object(fill_mod, "_mounted", return_value=(True, True)), \
            mock.patch.object(fill_mod.fetch, "run", side_effect=spy_run):
        result = fill_mod._drain_projection(
            ctx, session_start, plan_id="ark", max_24h_gb=0,
            repo_scope=None, guided=False, poll_secs=0.01, child_fds=())
    assert not captured.get("called"), (
        f"replica-only waiting projection must not enter FETCH fetch.run; result={result!r}")
    assert result.get("code") in {
        "WAITING_DEPENDENCY", "PLAN_SATISFIED", "PLAN_COMPLETE_WITH_FOLLOWUPS",
    }, result
