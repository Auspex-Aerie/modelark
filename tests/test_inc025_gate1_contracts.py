"""INC-025 Gate-1 contracts — expected red until fill drain emits typed ManifestFile.

Locked design (Gate 0 accepted):
  - Approved proposal_files are the frozen file authority (fill.py:616).
  - Carry storage_action through _projection_work_units.
  - At the FETCH drain join, build archive_manifest.ManifestFile from approved
    missing rows — never re-read live catalog/policy.
  - Fail closed on ambiguous missing names or invalid storage_action.
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


def _production_fetch_manifests(units):
    """Obtain FETCH task_manifests the way production must expose them.

    Prefers fill._fetch_task_manifests when Gate 2 lands. Until then, reproduces the
    current drain join (including the silent empty→all fallback) so contracts stay
    red for the intended defects.
    """
    fn = getattr(fill_mod, "_fetch_task_manifests", None)
    if callable(fn):
        return fn(units)
    out = {}
    for u in units:
        if getattr(u, "kind", None) != TaskKind.FETCH:
            continue
        selected = tuple(
            fr for fr in (u.file_rows or ())
            if fr.rfilename in (u.missing_files or ())
        )
        # Current production (fill.py:936-941): silent broaden on empty intersection.
        out[u.repo_id] = selected or tuple(u.file_rows or ())
    return out


def _assert_manifest_files(manifest_tuple, *, label: str):
    assert manifest_tuple, f"{label}: empty drain manifest"
    for item in manifest_tuple:
        assert isinstance(item, archive_manifest.ManifestFile), (
            f"{label}: drain must emit archive_manifest.ManifestFile, "
            f"got {type(item).__name__}: {item!r}")
        assert item.storage_action in ("compress", "raw"), (
            f"{label}: storage_action must be compress|raw, got {item.storage_action!r}")


# ---------------------------------------------------------------------------
# storage_action preservation
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
# Drain join → typed ManifestFile, exact missing subset, no ONNX
# ---------------------------------------------------------------------------


def test_c02_drain_manifests_are_genuine_manifest_file():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    units = _fetch_units(con, _proj_fetch(), _gte_proposal_files())
    manifests = _production_fetch_manifests(units)
    assert "org/gte" in manifests
    _assert_manifest_files(manifests["org/gte"], label="c02")


def test_c03_safetensors_compress_aux_raw_on_drain_manifest():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    units = _fetch_units(con, _proj_fetch(), _gte_proposal_files())
    manifests = _production_fetch_manifests(units)
    by_name = {m.rfilename: m for m in manifests["org/gte"]}
    _assert_manifest_files(manifests["org/gte"], label="c03")
    assert by_name["model.safetensors"].storage_action == "compress"
    assert by_name["config.json"].storage_action == "raw"


def test_c04_policy_excluded_onnx_never_in_drain_manifest():
    """Approved authority is planned-only; even if a bad approval listed onnx, design
    pins that production convert path uses approved rows — Gate-1 uses correct approval
    (no onnx). Catalog may still hold onnx; drain must not invent it.
    """
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    # Catalog has onnx (policy-excluded) but approved proposal_files do not.
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/gte','onnx/model.onnx',5000,'onnx',NULL,?)", ["c" * 64])
    pfiles = _gte_proposal_files(include_onnx=False)
    units = _fetch_units(con, _proj_fetch(), pfiles)
    manifests = _production_fetch_manifests(units)
    names = {m.rfilename for m in manifests.get("org/gte", ())}
    assert "onnx/model.onnx" not in names, (
        f"INC-025: ONNX must not appear in drain manifest; got {names}")
    assert "model.safetensors" in names and "config.json" in names


def test_c05_drain_manifest_is_exactly_approved_missing_subset():
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    # One approved file already durable on target → missing is the other only.
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/gte','config.json','d0',0,10,10,?)",
        ["b" * 64])
    pfiles = _gte_proposal_files()
    units = _fetch_units(con, _proj_fetch(), pfiles)
    assert units and units[0].missing_files == ("model.safetensors",), units[0].missing_files
    manifests = _production_fetch_manifests(units)
    names = [m.rfilename for m in manifests["org/gte"]]
    _assert_manifest_files(manifests["org/gte"], label="c05")
    assert names == ["model.safetensors"], (
        f"drain manifest must be exactly approved missing subset, got {names}")


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------


def test_c06_empty_intersection_fails_closed_not_all_rows():
    """Ghost missing name must not broaden to every file_row (fill.py:936-941 fallback)."""
    unit = SimpleNamespace(
        repo_id="org/gte",
        kind=TaskKind.FETCH,
        missing_files=("ghost.bin",),
        file_rows=(
            SimpleNamespace(
                rfilename="model.safetensors", size_bytes=1000, sha256="a" * 64,
                format="safetensors", quant="bf16", storage_action="compress"),
            SimpleNamespace(
                rfilename="config.json", size_bytes=10, sha256="b" * 64,
                format="aux", quant=None, storage_action="raw"),
        ),
    )
    try:
        manifests = _production_fetch_manifests([unit])
    except (Refusal, ValueError, RuntimeError, AssertionError, TypeError):
        return  # green once production fails closed
    got = manifests.get("org/gte") or ()
    names = {getattr(x, "rfilename", None) for x in got}
    assert False, (
        "INC-025: empty intersection must fail closed, not return all file_rows; "
        f"got {names}")


def test_c07_missing_or_invalid_storage_action_fails_closed():
    """Missing storage_action on an approved row must fail closed at typed conversion."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    pfiles = [{
        "requirement_id": "primary:org/gte", "rfilename": "model.safetensors",
        "size_bytes": 1000, "orig_sha256": "a" * 64,
        "format": "safetensors", "quant": "bf16",
        # storage_action intentionally omitted
    }]
    try:
        units = fill_mod._projection_work_units(
            con, _proj_fetch(), proposal_files=pfiles, require_proposal_files=True)
        fetch_units = [u for u in units if getattr(u, "kind", None) == TaskKind.FETCH]
        assert fetch_units, "expected FETCH unit when approved file is missing on target"
        manifests = _production_fetch_manifests(fetch_units)
    except (Refusal, ValueError, RuntimeError, TypeError, AssertionError):
        return  # green: fail-closed during unit build or join conversion

    # Current production reaches here with SimpleNamespace / no valid typed action.
    for item in manifests.get("org/gte") or ():
        action = getattr(item, "storage_action", None)
        is_mf = isinstance(item, archive_manifest.ManifestFile)
        if not is_mf or action not in ("compress", "raw"):
            assert False, (
                "INC-025: missing/invalid storage_action must fail closed before "
                f"emitting a drain manifest; got type={type(item).__name__} "
                f"action={action!r}")
    assert False, (
        "INC-025: missing storage_action must fail closed, not invent a defaulted ManifestFile")


# ---------------------------------------------------------------------------
# Load-bearing seam: drain data → real fetch_model (do not patch consumer)
# ---------------------------------------------------------------------------


def test_c08_drain_manifest_crosses_real_fetch_model():
    """Real fetch_model must accept drain-produced manifests (as_fetch_record).

    Do not patch fetch_model, as_fetch_record, or fetch.run. Isolate only
    transport/download and path prep.
    """
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    pfiles = _gte_proposal_files()
    units = _fetch_units(con, _proj_fetch(), pfiles)
    manifests = _production_fetch_manifests(units)
    assert "org/gte" in manifests
    manifest = manifests["org/gte"]
    # Pin type before consumer — still red on SimpleNamespace today.
    _assert_manifest_files(manifest, label="c08-precondition")

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
    assert download.call_count >= 1, (
        "real fetch_model must reach download for missing ManifestFile rows")


# ---------------------------------------------------------------------------
# Replica path unchanged
# ---------------------------------------------------------------------------


def test_c09_replica_units_unchanged_by_fetch_manifest_join():
    """Replica remains waiting_dependency / non-FETCH; join applies to FETCH only."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/gte",))
    # Source not ready → replica stays waiting; no FETCH conversion applied.
    pfiles = _gte_proposal_files(rid="replica:org/gte")
    units = fill_mod._projection_work_units(
        con, _proj_replica(), proposal_files=pfiles, require_proposal_files=True)
    assert units, "expected a replica-side unit"
    assert units[0].schedule_state == "waiting_dependency"
    assert units[0].kind is None
    # Production join must not invent FETCH manifests for non-FETCH units.
    manifests = _production_fetch_manifests(units)
    assert manifests == {} or all(
        getattr(u, "kind", None) == TaskKind.FETCH for u in units if u.repo_id in manifests
    ), manifests
