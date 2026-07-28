"""INC-024 Gate-1 contracts — expected red until production remediation.

Locked decisions: Q1 inspect batch + _manifest_hash narrows internally; Q2a INFEASIBLE
with named repos; DEC-056 planned-set drift; Q4 no migration (wide hash refuses).
No production code in this gate.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import _pr09_gate1_fixtures as f
from modelark import archive_manifest, proposal as prop
from modelark.proposal import Refusal


def _seed_drives_and_plan(con):
    f.seed_plan_selection(con, repos=())
    con.execute("DELETE FROM selection")
    con.execute("DELETE FROM files")
    con.execute("DELETE FROM models")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")


def _add_repo(con, repo, files, *, numcopies=1):
    con.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES(?,?)", [repo, numcopies])
    con.execute(
        "INSERT OR IGNORE INTO selection(repo_id,finalized_at) VALUES(?, '2026-01-01')",
        [repo])
    for rfilename, size, fmt, quant, sha in files:
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?,?,?,?,?,?)",
            [repo, rfilename, size, fmt, quant, sha])


def _gte_shape(con, repo="org/gte"):
    """18-like: safetensors + aux + onnx (planned 2, catalog 3)."""
    _add_repo(con, repo, [
        ("model.safetensors", 1000, "safetensors", "bf16", "a" * 64),
        ("config.json", 10, "aux", None, "b" * 64),
        ("onnx/model.onnx", 5000, "onnx", None, "c" * 64),
    ])


def _planned_names(con, repo):
    return {m.rfilename for m in archive_manifest.manifest_for_repo(con, repo)}


def _planned_size(con, repo):
    return sum(m.size_bytes for m in archive_manifest.manifest_for_repo(con, repo))


def _wide_hash(con, repo):
    """Catalog-wide hash matching today's defective _manifest_hash shape."""
    files = con.execute(
        "SELECT rfilename, size_bytes, sha256, format, quant FROM files "
        "WHERE repo_id=? ORDER BY rfilename", [repo]).fetchall()
    payload = json.dumps(
        [list(r) for r in files], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _planned_hash(con, repo):
    files = [
        (m.rfilename, m.size_bytes, m.sha256, m.format, m.quant)
        for m in archive_manifest.manifest_for_repo(con, repo)
    ]
    payload = json.dumps(
        [list(r) for r in files], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _draft(con):
    return prop.preview_and_draft(con, plan_id="ark", mutation=("adopt_current", ()))


# ---------------------------------------------------------------------------
# File authority
# ---------------------------------------------------------------------------


def test_c01_proposal_files_are_acquisition_planned_not_catalog():
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    names = {ff["rfilename"] for ff in payload["files"] if ff["requirement_id"].endswith("org/gte")}
    planned = _planned_names(con, "org/gte")
    catalog = {r[0] for r in con.execute(
        "SELECT rfilename FROM files WHERE repo_id=?", ["org/gte"])}
    assert names == planned, (
        f"INC-024: proposal_files must be planned set {planned}, not catalog {catalog}; got {names}")
    assert "onnx/model.onnx" not in names
    assert len(names) == 2


def test_c02_storage_action_from_manifest_not_hardcoded_compress():
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    by_name = {
        ff["rfilename"]: ff["storage_action"]
        for ff in payload["files"] if "org/gte" in ff["requirement_id"]
    }
    planned = {
        m.rfilename: m.storage_action
        for m in archive_manifest.manifest_for_repo(con, "org/gte")
    }
    assert by_name == planned, (
        f"storage_action must match ManifestFile; planned={planned} got={by_name}")
    assert by_name.get("config.json") == "raw"
    assert by_name.get("model.safetensors") == "compress"


# ---------------------------------------------------------------------------
# Drift (DEC-056)
# ---------------------------------------------------------------------------


def test_c03_manifest_hash_equals_planned_not_wide():
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    got = prop._manifest_hash(con, "org/gte")
    assert got == _planned_hash(con, "org/gte"), "DEC-056: _manifest_hash must hash planned set"
    assert got != _wide_hash(con, "org/gte"), "DEC-056: must differ from wide catalog hash"


def test_c04_fresh_draft_full_manifest_hash_is_planned():
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    task = next(t for t in payload["tasks"] if t["repo_id"] == "org/gte")
    assert task["full_manifest_hash"] == _planned_hash(con, "org/gte")
    assert task["full_manifest_hash"] != _wide_hash(con, "org/gte")


def test_c05_wide_hash_approval_refused_and_reapprove_is_narrow():
    """Q4 pin: wide stored hash fails project_pure; fresh approve regenerates narrow."""
    from modelark.execution_projection import project_pure

    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    # Archive planned set so placement can baseline or execute cleanly after re-approve
    for m in archive_manifest.manifest_for_repo(con, "org/gte"):
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
            "stored_bytes,orig_sha256) VALUES(?,?,?,?,?,?,?)",
            ["org/gte", m.rfilename, "d0", 0, m.size_bytes, m.size_bytes, m.sha256 or "d" * 64])

    # Simulate a legacy wide-hash approval without going through current draft helpers
    # by drafting then overwriting full_manifest_hash to the wide value.
    draft = _draft(con)
    pid = draft["proposal_id"] if isinstance(draft, dict) else draft
    wide = _wide_hash(con, "org/gte")
    con.execute(
        "UPDATE proposal_tasks SET full_manifest_hash=? WHERE proposal_id=? AND repo_id=?",
        [wide, pid, "org/gte"])
    prop.approve(con, pid, mutation=("adopt_current", ()), services=f.default_services())
    loaded = prop.load_proposal(con, pid)
    assert loaded["lifecycle"] == "approved"
    # Comparison input as execution_session builds it (always via _manifest_hash)
    manifests = {"org/gte": prop._manifest_hash(con, "org/gte")}
    proposal = {
        "lifecycle": "approved",
        "proposal_id": pid,
        "tasks": loaded["tasks"],
        "files": loaded["files"],
        "requirement_set_hash": loaded.get("requirement_set_hash") or loaded["tasks"][0].get("full_manifest_hash"),
        "semantic_input_hash": loaded.get("semantic_input_hash") or "e" * 64,
    }
    current_input = SimpleNamespace(
        manifests=manifests,
        drives={
            "d0": SimpleNamespace(lifecycle="active", identity_epoch=1),
            "d1": SimpleNamespace(lifecycle="active", identity_epoch=1),
        },
        archived={},
        evidence={},
        observed_ratio={},
    )
    current_graph = SimpleNamespace(
        requirement_ids=[t["requirement_id"] for t in loaded["tasks"]],
        requirement_set_hash=proposal["requirement_set_hash"],
    )
    out = project_pure(proposal, current_input, current_graph, SimpleNamespace(parked_gated_repos=frozenset()))
    assert isinstance(out, Refusal), (
        "DEC-056/Q4: wide-hash approval must be refused by project_pure, got success")
    assert out.code == "APPROVED_INPUT_CHANGED"
    assert (out.evidence or {}).get("reason") == "full_manifest_hash"

    # Fresh preview+approve under (future) narrow code regenerates and projects cleanly
    con.execute("UPDATE planner_state SET planner_revision=planner_revision")  # no-op keep open
    draft2 = _draft(con)
    pid2 = draft2["proposal_id"] if isinstance(draft2, dict) else draft2
    prop.approve(con, pid2, mutation=("adopt_current", ()), services=f.default_services())
    loaded2 = prop.load_proposal(con, pid2)
    for t in loaded2["tasks"]:
        if t.get("repo_id") == "org/gte":
            assert t["full_manifest_hash"] == _planned_hash(con, "org/gte")
    manifests2 = {"org/gte": prop._manifest_hash(con, "org/gte")}
    proposal2 = {
        "lifecycle": "approved",
        "proposal_id": pid2,
        "tasks": loaded2["tasks"],
        "files": loaded2["files"],
        "requirement_set_hash": loaded2.get("requirement_set_hash") or "f" * 64,
        "semantic_input_hash": loaded2.get("semantic_input_hash") or "f" * 64,
    }
    out2 = project_pure(
        proposal2,
        SimpleNamespace(
            manifests=manifests2,
            drives=current_input.drives,
            archived={},
            evidence={},
            observed_ratio={},
        ),
        SimpleNamespace(
            requirement_ids=[t["requirement_id"] for t in loaded2["tasks"]],
            requirement_set_hash=proposal2["requirement_set_hash"],
        ),
        SimpleNamespace(parked_gated_repos=frozenset()),
    )
    assert not isinstance(out2, Refusal), f"fresh narrow approval must project cleanly: {out2!r}"


def test_c06_excluded_catalog_file_does_not_invalidate_approval():
    """DEC-056 insensitivity: new policy-excluded catalog row must not trip drift."""
    from modelark.execution_projection import project_pure

    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    for m in archive_manifest.manifest_for_repo(con, "org/gte"):
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
            "stored_bytes,orig_sha256) VALUES(?,?,?,?,?,?,?)",
            ["org/gte", m.rfilename, "d0", 0, m.size_bytes, m.size_bytes, m.sha256 or "d" * 64])
    prop_, pid, loaded = f.create_and_approve(con)
    # Add excluded format after approval
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/gte','onnx/new.onnx',999,'onnx',NULL,?)", ["e" * 64])
    manifests = {"org/gte": prop._manifest_hash(con, "org/gte")}
    proposal = {
        "lifecycle": "approved",
        "proposal_id": pid,
        "tasks": loaded["tasks"],
        "files": loaded["files"],
        "requirement_set_hash": loaded.get("requirement_set_hash") or "1" * 64,
        "semantic_input_hash": loaded.get("semantic_input_hash") or "1" * 64,
    }
    out = project_pure(
        proposal,
        SimpleNamespace(
            manifests=manifests,
            drives={
                "d0": SimpleNamespace(lifecycle="active", identity_epoch=1),
                "d1": SimpleNamespace(lifecycle="active", identity_epoch=1),
            },
            archived={}, evidence={}, observed_ratio={},
        ),
        SimpleNamespace(
            requirement_ids=[t["requirement_id"] for t in loaded["tasks"]],
            requirement_set_hash=proposal["requirement_set_hash"],
        ),
        SimpleNamespace(parked_gated_repos=frozenset()),
    )
    assert not isinstance(out, Refusal), (
        f"excluded catalog add must not invalidate approval; got {out!r}")


# ---------------------------------------------------------------------------
# Capacity and durability class
# ---------------------------------------------------------------------------


def test_c07_durable_charges_equal_planned_size():
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    task = next(t for t in payload["tasks"] if t["repo_id"] == "org/gte")
    planned = _planned_size(con, "org/gte")
    catalog = int(con.execute(
        "SELECT coalesce(sum(size_bytes),0) FROM files WHERE repo_id=?",
        ["org/gte"]).fetchone()[0])
    assert task["guaranteed_durable"] == planned
    assert task["expected_durable"] == planned
    assert planned < catalog
    assert task["guaranteed_durable"] != catalog


def test_c08_planned_complete_drive_is_baseline_satisfied():
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    # Planned set present; excluded onnx absent
    for m in archive_manifest.manifest_for_repo(con, "org/gte"):
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
            "stored_bytes,orig_sha256) VALUES(?,?,?,?,?,?,?)",
            ["org/gte", m.rfilename, "d0", 0, m.size_bytes, m.size_bytes, m.sha256 or "d" * 64])
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    tasks = [t for t in payload["tasks"] if t["repo_id"] == "org/gte"]
    assert tasks, "expected tasks for org/gte"
    assert all(t["row_kind"] == "baseline_satisfied" for t in tasks), (
        f"planned-complete must be baseline_satisfied, got {tasks!r}")
    assert all(int(t.get("guaranteed_durable") or 0) == 0 or t["row_kind"] == "baseline_satisfied"
               for t in tasks)
    # No executable capacity charge for already-durable planned set
    exec_charge = sum(
        int(t.get("guaranteed_durable") or 0)
        for t in tasks if t["row_kind"] == "executable")
    assert exec_charge == 0


def test_c09_joint_feasibility_uses_planned_not_wide_charge():
    """Fleet where wide charge is INFEASIBLE but planned charge is FEASIBLE."""
    from modelark import plan as plan_mod

    con = f.mem_con()
    free = 8_000  # planned ~5k fits; wide ~45k does not under safety-adjusted free
    for label, meta in f.DRIVE_IDS.items():
        con.execute(
            "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
            "write_authority,filesystem_capacity_bytes) "
            "VALUES(?,?,?,?,0,'active','enabled',?,1,?,'dedicated_local',?)",
            [label, free, free, meta["role"], meta["epoch"], meta["fingerprint"], free])
        con.execute(
            "INSERT INTO drive_dirty_generations"
            "(drive_label,identity_epoch,generation,operation_code) VALUES(?,?,1,'seed')",
            [label, meta["epoch"]])
        con.execute(
            "INSERT INTO drive_clean_anchors"
            "(drive_label,identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
            "identity_fingerprint,write_authority,identity_proof,fence_proof,observed_at) "
            "VALUES(?,?,1,?,?,?,'dedicated_local','seed','seed','2026-01-01T00:00:00Z')",
            [label, meta["epoch"], free, free, meta["fingerprint"]])
    if plan_mod.get(con, "ark") is None:
        plan_mod.create(con, "ark", name="Ark")
    for label in f.DRIVE_IDS:
        if label not in plan_mod.plan_drive_labels(con, "ark"):
            plan_mod.add_drive(con, "ark", label)
    plan_mod.set_active(con, "ark")
    con.execute("UPDATE planner_state SET planner_revision=0 WHERE singleton_id=1")
    _add_repo(con, "org/fat", [
        ("model.safetensors", 5_000, "safetensors", "bf16", "1" * 64),
        ("onnx/big.onnx", 40_000, "onnx", None, "2" * 64),
    ])
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    gate = payload["header"]["gate_b_code"]
    # After fix: FEASIBLE (planned 5k). Today: typically INFEASIBLE on wide 45k.
    assert gate == "FEASIBLE", (
        f"joint feasibility must use planned charge; gate={gate} "
        f"(wide catalog charge must not starve the fleet)")
# ---------------------------------------------------------------------------
# Policy error (Q2a)
# ---------------------------------------------------------------------------


def test_c10_multi_repo_policy_errors_gate_infeasible_with_named_evidence():
    con = f.mem_con()
    _seed_drives_and_plan(con)
    # One healthy repo so assignment is otherwise possible
    _gte_shape(con, "org/ok")
    # Pickle-only blocked under exclude.pickle_only
    _add_repo(con, "org/pickle", [
        ("weights.bin", 100, "pytorch", None, "3" * 64),
    ])
    # No supported weights
    _add_repo(con, "org/noweights", [
        ("tokenizer.json", 10, "aux", None, "4" * 64),
        ("weird.dat", 10, "other", None, "5" * 64),
    ])
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    header = payload["header"]
    assert header["gate_b_code"] == "INFEASIBLE"
    # Evidence must name each blocked repo — surface may be header, payload top-level,
    # or task annotations. Prefer explicit gate evidence once production lands.
    blob = json.dumps(payload, default=str)
    assert "org/pickle" in blob and "org/noweights" in blob, (
        "Q2a: evidence must name every blocked repository")
    # Established shape fields (DEC-050 observability)
    evidence = (
        payload.get("evidence")
        or header.get("evidence")
        or payload.get("gate_b_evidence")
        or {}
    )
    actions = (
        payload.get("actions")
        or header.get("actions")
        or (evidence.get("actions") if isinstance(evidence, dict) else None)
        or []
    )
    assert "review_manifest_policy" in list(actions) or "review_manifest_policy" in blob
    assert "trim_selection" in list(actions) or "trim_selection" in blob
    # Count-only is insufficient — reasons present
    assert "pickle" in blob.lower() or "blocked" in blob.lower() or "ArchivePolicy" in blob or "no supported" in blob.lower()


def test_c11_approve_refuses_policy_infeasible_draft():
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _add_repo(con, "org/pickle", [("w.bin", 100, "pytorch", None, "3" * 64)])
    _add_repo(con, "org/noweights", [("t.json", 10, "aux", None, "4" * 64)])
    draft = _draft(con)
    pid = draft["proposal_id"] if isinstance(draft, dict) else draft
    f.assert_refuses(
        lambda: prop.approve(con, pid, mutation=("adopt_current", ()), services=f.default_services()),
        code="PROPOSAL_NOT_FEASIBLE",
        label="approve of multi-repo policy INFEASIBLE draft",
    )


def test_c12_blocked_repos_not_silently_omitted():
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con, "org/ok")
    _add_repo(con, "org/pickle", [("w.bin", 100, "pytorch", None, "3" * 64)])
    _add_repo(con, "org/noweights", [("t.json", 10, "aux", None, "4" * 64)])
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    selected = {r[0] for r in con.execute("SELECT repo_id FROM selection")}
    assert {"org/pickle", "org/noweights"} <= selected
    # DEC-050 shape: blocked set must be derivable from typed gate evidence, not only
    # by still appearing as executable work (which would mean silent inclusion).
    evidence = (
        payload.get("evidence")
        or payload.get("header", {}).get("evidence")
        or payload.get("gate_b_evidence")
    )
    assert isinstance(evidence, dict), (
        "Q2a: preview must emit structured gate evidence for blocked repos")
    blocked = (
        evidence.get("blocked_repositories")
        or evidence.get("manifest_policy_errors")
        or evidence.get("repos")
    )
    assert blocked, f"evidence must list blocked repos, got {evidence!r}"
    blocked_ids = {
        (b.get("repo_id") if isinstance(b, dict) else b) for b in blocked
    }
    assert "org/pickle" in blocked_ids and "org/noweights" in blocked_ids


# ---------------------------------------------------------------------------
# Consequential (no fill/certificate production edits)
# ---------------------------------------------------------------------------


def test_c13_source_ready_with_only_planned_files_on_source():
    """35-stall class resolves once proposal_files narrow — no fill.py edit."""
    from modelark import fill as fill_mod

    con = f.mem_con()
    _seed_drives_and_plan(con)
    _add_repo(con, "org/gte", [
        ("model.safetensors", 1000, "safetensors", "bf16", "a" * 64),
        ("config.json", 10, "aux", None, "b" * 64),
        ("onnx/model.onnx", 5000, "onnx", None, "c" * 64),
    ], numcopies=2)
    for m in archive_manifest.manifest_for_repo(con, "org/gte"):
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
            "stored_bytes,orig_sha256) VALUES(?,?,?,?,?,?,?)",
            ["org/gte", m.rfilename, "d0", 0, m.size_bytes, m.size_bytes, m.sha256 or "d" * 64])
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    replica = next(
        (t for t in payload["tasks"]
         if t["repo_id"] == "org/gte" and t.get("source_drive")),
        None)
    assert replica is not None, "numcopies=2 should produce a replica task"
    pfiles = [
        {
            "requirement_id": ff["requirement_id"],
            "rfilename": ff["rfilename"],
            "size_bytes": ff["size_bytes"],
            "orig_sha256": ff["orig_sha256"],
            "format": ff.get("format"),
            "quant": ff.get("quant"),
        }
        for ff in payload["files"]
        if ff["requirement_id"] == replica["requirement_id"]
    ]
    # After INC-024: pfiles is planned-only → ready True without fill edits.
    # Today: pfiles includes onnx → ready False (expected red).
    assert "onnx/model.onnx" not in {p["rfilename"] for p in pfiles}
    assert fill_mod._source_files_content_ready(
        con, "org/gte", replica["source_drive"], pfiles) is True
def test_c14_baseline_certificate_payload_stays_archived_unfiltered():
    """Pin current certificate behaviour (Q6) — archived-derived, not catalog-filtered."""
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    # Archive planned + also put an excluded file on drive as if from older era
    for m in archive_manifest.manifest_for_repo(con, "org/gte"):
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
            "stored_bytes,orig_sha256) VALUES(?,?,?,?,?,?,?)",
            ["org/gte", m.rfilename, "d0", 0, m.size_bytes, m.size_bytes, m.sha256 or "d" * 64])
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
        "stored_bytes,orig_sha256) VALUES('org/gte','onnx/model.onnx','d0',0,5000,5000,?)",
        ["c" * 64])
    evidence = prop._baseline_file_evidence(con, "org/gte", "d0")
    names = {e["rfilename"] for e in evidence}
    assert "onnx/model.onnx" in names, (
        "certificate evidence stays archived-unfiltered; onnx present on drive must appear")
    assert "model.safetensors" in names
