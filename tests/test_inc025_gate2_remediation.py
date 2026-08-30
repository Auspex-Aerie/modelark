"""INC-025 Gate-2 remediation — non-approval characterization policy.

Pin that the non-approval characterization branch uses the canonical
``archive_manifest.manifest_for_repo`` helper (policy-selected files only),
and that the approved ``proposal_files`` path never calls it.

Must fail on tip ``1be2150`` (FLOAT_QUANTS raw-catalog derivation) and pass
after the Gate-2 remediation.

Does not edit accepted Gate-1 contracts in ``test_inc025_gate1_contracts.py``.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import _pr09_gate1_fixtures as f
from modelark import archive_manifest, fill as fill_mod
from modelark.reconcile import TaskKind


def _proj_fetch(repo="org/gte", target="d0", rid=None):
    rid = rid or f"primary:{repo}"
    return SimpleNamespace(tasks=(SimpleNamespace(
        row_kind="executable", repo_id=repo, target_drive=target,
        source_drive=None, requirement_id=rid,
        schedule_state="ready", order_key=1,
        guaranteed_durable=1010, expected_durable=1010,
    ),))


def _seed_safetensors_and_onnx(con, repo="org/gte"):
    """Catalog with policy-selected safetensors (+ aux) and excluded ONNX."""
    f.seed_plan_selection(con, repos=(repo,))
    # seed_plan_selection already inserts model.safetensors; add aux + onnx.
    con.execute(
        "INSERT OR IGNORE INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES(?,?,10,'aux',NULL,?)",
        [repo, "config.json", "b" * 64])
    con.execute(
        "INSERT OR IGNORE INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES(?,?,5000,'onnx',NULL,?)",
        [repo, "onnx/model.onnx", "c" * 64])


def test_non_approval_characterization_uses_canonical_manifest_for_repo():
    """Non-approval path: call manifest_for_repo; safetensors in, ONNX out."""
    con = f.mem_con()
    _seed_safetensors_and_onnx(con)
    repo = "org/gte"
    projection = _proj_fetch(repo=repo)

    real_mfr = archive_manifest.manifest_for_repo
    calls: list = []

    def spy_mfr(con_arg, repo_id, policy=None):
        calls.append({"repo_id": repo_id, "policy": policy})
        if policy is None:
            return real_mfr(con_arg, repo_id)
        return real_mfr(con_arg, repo_id, policy)

    with mock.patch.object(archive_manifest, "manifest_for_repo", side_effect=spy_mfr):
        # Also patch the name as resolved inside fill if it imports the function later.
        with mock.patch(
                "modelark.archive_manifest.manifest_for_repo", side_effect=spy_mfr):
            units = fill_mod._projection_work_units(
                con, projection, proposal_files=None, require_proposal_files=False)

    fetch_units = [u for u in units if getattr(u, "kind", None) == TaskKind.FETCH]
    assert fetch_units, "expected a FETCH characterization unit"
    assert calls, (
        "INC-025 Gate-2 remediation: non-approval characterization must call "
        "archive_manifest.manifest_for_repo (not raw catalog + FLOAT_QUANTS)")
    assert any(c["repo_id"] == repo for c in calls), calls

    unit = fetch_units[0]
    by_name = {fr.rfilename: fr for fr in unit.file_rows}
    assert "model.safetensors" in by_name, by_name
    assert "onnx/model.onnx" not in by_name, (
        f"ONNX must be excluded by canonical policy, got {sorted(by_name)}")

    manifests = fill_mod._fetch_task_manifests(fetch_units)
    assert repo in manifests
    mf_by_name = {m.rfilename: m for m in manifests[repo]}
    assert "model.safetensors" in mf_by_name
    assert "onnx/model.onnx" not in mf_by_name
    for name, item in mf_by_name.items():
        assert isinstance(item, archive_manifest.ManifestFile), (
            f"{name}: expected ManifestFile, got {type(item).__name__}")
        assert item.storage_action in ("compress", "raw"), item.storage_action
    assert mf_by_name["model.safetensors"].storage_action == "compress"
    if "config.json" in mf_by_name:
        assert mf_by_name["config.json"].storage_action == "raw"


def test_approved_proposal_path_never_calls_manifest_for_repo():
    """Approved frozen proposal_files must not reread live catalog/policy."""
    con = f.mem_con()
    _seed_safetensors_and_onnx(con)
    rid = "primary:org/gte"
    pfiles = [
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

    with mock.patch.object(
            archive_manifest, "manifest_for_repo",
            side_effect=AssertionError(
                "approved path must not call manifest_for_repo")):
        with mock.patch(
                "modelark.archive_manifest.manifest_for_repo",
                side_effect=AssertionError(
                    "approved path must not call manifest_for_repo")):
            units = fill_mod._projection_work_units(
                con, _proj_fetch(), proposal_files=pfiles,
                require_proposal_files=True)
            fetch_units = [
                u for u in units if getattr(u, "kind", None) == TaskKind.FETCH]
            assert fetch_units
            manifests = fill_mod._fetch_task_manifests(fetch_units)

    names = {m.rfilename for m in manifests["org/gte"]}
    assert names == {"model.safetensors", "config.json"}
    assert all(
        isinstance(m, archive_manifest.ManifestFile) for m in manifests["org/gte"])
    # Catalog ONNX must not appear from frozen authority.
    assert "onnx/model.onnx" not in names
