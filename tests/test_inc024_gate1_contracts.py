"""INC-024 Gate-1 contracts — expected red until production remediation.

Locked decisions: Q1 inspect batch + _manifest_hash narrows internally; Q2a INFEASIBLE
with named repos; DEC-056 planned-set drift; Q4 no migration (wide hash refuses).
No production code in this gate.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

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


def _project_approved(con, pid, loaded, drives=None):
    """Build project_pure inputs the way execution_session does (via _manifest_hash)."""
    from modelark.execution_projection import project_pure

    drives = drives or {
        "d0": SimpleNamespace(lifecycle="active", identity_epoch=1),
        "d1": SimpleNamespace(lifecycle="active", identity_epoch=1),
    }
    manifests = {
        t["repo_id"]: prop._manifest_hash(con, t["repo_id"])
        for t in loaded["tasks"] if t.get("repo_id")
    }
    proposal = {
        "lifecycle": "approved",
        "proposal_id": pid,
        "tasks": loaded["tasks"],
        "files": loaded["files"],
        "requirement_set_hash": (
            loaded.get("requirement_set_hash")
            or loaded["tasks"][0].get("full_manifest_hash")
        ),
        "semantic_input_hash": loaded.get("semantic_input_hash") or "e" * 64,
    }
    current_input = SimpleNamespace(
        manifests=manifests,
        drives=drives,
        archived={},
        evidence={},
        observed_ratio={},
    )
    current_graph = SimpleNamespace(
        requirement_ids=[t["requirement_id"] for t in loaded["tasks"]],
        requirement_set_hash=proposal["requirement_set_hash"],
    )
    return project_pure(
        proposal, current_input, current_graph,
        SimpleNamespace(parked_gated_repos=frozenset()),
    )


def test_c05_wide_hash_approval_refused_and_reapprove_is_narrow():
    """Q4/DEC-056: independently pin three states (do not corrupt-before-approve alone).

    1. Draft corrupted to legacy wide hash is refused by approve() as
       APPROVED_INPUT_CHANGED / full_manifest_hash.
    2. Already-approved historical-wide proposal (approve first, then seed wide
       on the persisted task) is refused by project_pure with the same code/reason.
    3. Fresh narrow preview/approval projects cleanly.
    """
    con = f.mem_con()
    _seed_drives_and_plan(con)
    _gte_shape(con)
    for m in archive_manifest.manifest_for_repo(con, "org/gte"):
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,compressed,orig_bytes,"
            "stored_bytes,orig_sha256) VALUES(?,?,?,?,?,?,?)",
            ["org/gte", m.rfilename, "d0", 0, m.size_bytes, m.size_bytes, m.sha256 or "d" * 64])
    wide = _wide_hash(con, "org/gte")
    services = f.default_services()

    # --- 1. Corrupted draft refused at approve (validation, not project_pure) ---
    draft1 = _draft(con)
    pid1 = draft1["proposal_id"] if isinstance(draft1, dict) else draft1
    con.execute(
        "UPDATE proposal_tasks SET full_manifest_hash=? WHERE proposal_id=? AND repo_id=?",
        [wide, pid1, "org/gte"])
    refuse1 = f.assert_refuses(
        lambda: prop.approve(con, pid1, mutation=("adopt_current", ()), services=services),
        code="APPROVED_INPUT_CHANGED",
        label="approve of draft corrupted to legacy wide full_manifest_hash",
    )
    ev1 = getattr(refuse1, "evidence", None) or {}
    if not isinstance(ev1, dict):
        ev1 = {}
    assert ev1.get("reason") == "full_manifest_hash", (
        f"approve refusal reason must be full_manifest_hash, got {ev1!r}")

    # --- 2. Historical wide: approve first, then seed wide on persisted task ---
    draft2 = _draft(con)
    pid2 = draft2["proposal_id"] if isinstance(draft2, dict) else draft2
    prop.approve(con, pid2, mutation=("adopt_current", ()), services=services)
    con.execute(
        "UPDATE proposal_tasks SET full_manifest_hash=? WHERE proposal_id=? AND repo_id=?",
        [wide, pid2, "org/gte"])
    loaded2 = prop.load_proposal(con, pid2)
    assert loaded2["lifecycle"] == "approved"
    out2 = _project_approved(con, pid2, loaded2)
    assert isinstance(out2, Refusal), (
        "DEC-056/Q4: historical wide-hash approval must be refused by project_pure, "
        f"got success {out2!r}")
    assert out2.code == "APPROVED_INPUT_CHANGED"
    assert (out2.evidence or {}).get("reason") == "full_manifest_hash"

    # --- 3. Fresh narrow preview/approval projects cleanly ---
    draft3 = _draft(con)
    pid3 = draft3["proposal_id"] if isinstance(draft3, dict) else draft3
    prop.approve(con, pid3, mutation=("adopt_current", ()), services=services)
    loaded3 = prop.load_proposal(con, pid3)
    for t in loaded3["tasks"]:
        if t.get("repo_id") == "org/gte":
            assert t["full_manifest_hash"] == _planned_hash(con, "org/gte"), (
                "fresh approval must store the planned-set full_manifest_hash")
    out3 = _project_approved(con, pid3, loaded3)
    assert not isinstance(out3, Refusal), (
        f"fresh narrow approval must project cleanly: {out3!r}")


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
# Policy error (Q2a) — exact structured observability, not substring presence
# ---------------------------------------------------------------------------

_Q2_EXPECTED_ACTIONS = ["review_manifest_policy", "trim_selection", "replan"]
_Q2_BLOCKED_REPOS = ("org/pickle", "org/noweights")


def _seed_mixed_policy_selection(con):
    """Healthy repo + pickle-only + unsupported-weights (both raise ArchivePolicyError)."""
    _seed_drives_and_plan(con)
    _gte_shape(con, "org/ok")
    _add_repo(con, "org/pickle", [
        ("weights.bin", 100, "pytorch", None, "3" * 64),
    ])
    _add_repo(con, "org/noweights", [
        ("tokenizer.json", 10, "aux", None, "4" * 64),
        ("weird.dat", 10, "other", None, "5" * 64),
    ])


def _q2_select_container(payload):
    """Select exactly one structured Q2 observability container (no mixing).

    Named gate blocks are preferred over payload root / header. A container
    qualifies only if it itself carries code, gate, or evidence.blocked_repositories.
    ``header.gate_b_code`` is never used to select a container.
    """
    if not isinstance(payload, dict):
        return None
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    candidates = (
        payload.get("gate_b_refusal"),
        payload.get("manifest_policy_gate"),
        payload.get("gate_b_evidence"),
        header.get("gate_b_refusal"),
        header.get("manifest_policy_gate"),
        # Payload root (DEC-050 / admission-terminal shape) — only if the root
        # itself holds the observability fields, not merely nested tasks/files.
        payload,
        # Header only if it itself holds the observability fields (gate_b_code alone
        # is insufficient and remains a separate Gate-B status field).
        header,
    )
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        evidence = cand.get("evidence")
        if (
            cand.get("code") is not None
            or cand.get("gate") is not None
            or (isinstance(evidence, dict) and "blocked_repositories" in evidence)
        ):
            return cand
    return None


def _q2_gate_observability(payload):
    """Extract Q2 fields from one container only — no cross-container fallbacks.

    ``header.gate_b_code`` is the separate Gate-B status field. ``code``, ``gate``,
    ``evidence``, and ``actions`` all come from the single selected container.
    A block missing ``actions`` is not rescued by root/header actions; evidence is
    not borrowed from another container.
    """
    header = payload.get("header") if isinstance(payload, dict) else None
    if not isinstance(header, dict):
        header = {}
    block = _q2_select_container(payload)
    if block is None:
        return {
            "gate_b_code": header.get("gate_b_code"),
            "code": None,
            "gate": None,
            "evidence": {},
            "actions": None,
        }
    evidence = block.get("evidence")
    if not isinstance(evidence, dict):
        evidence = {}
    actions = block.get("actions")  # no cross-container rescue
    return {
        "gate_b_code": header.get("gate_b_code"),
        "code": block.get("code"),
        "gate": block.get("gate"),
        "evidence": evidence,
        "actions": list(actions) if actions is not None else None,
    }


def _assert_q2_exact_shape(payload, *, label: str):
    """Pin one exact structured shape for multi-repo policy INFEASIBLE.

    Required:
      - header.gate_b_code == "INFEASIBLE" (separate Gate-B status field)
      - one container's typed code MANIFEST_POLICY
      - same container's gate == "B"
      - same container's evidence["blocked_repositories"]: exactly one record each
        for org/pickle and org/noweights — no duplicates, no extras, no org/ok
      - every record has its own repo_id and non-empty reason (semantically distinct)
      - same container's exact ordered actions
        ["review_manifest_policy", "trim_selection", "replan"]

    A global reason, count-only evidence, advisory text, action strings outside the
    selected container, or a healthy repo listed as blocked must not satisfy this.
    """
    obs = _q2_gate_observability(payload)
    assert obs["gate_b_code"] == "INFEASIBLE", (
        f"{label}: gate_b_code must be INFEASIBLE, got {obs['gate_b_code']!r}")
    assert obs["code"] == "MANIFEST_POLICY", (
        f"{label}: typed code must be MANIFEST_POLICY, got {obs['code']!r} "
        f"(substring presence elsewhere is insufficient)")
    assert obs["gate"] == "B", (
        f"{label}: gate must be 'B', got {obs['gate']!r}")

    evidence = obs["evidence"]
    assert isinstance(evidence, dict) and evidence, (
        f"{label}: structured evidence dict required on the selected container, "
        f"got {evidence!r}")
    blocked = evidence.get("blocked_repositories")
    assert isinstance(blocked, list), (
        f"{label}: evidence['blocked_repositories'] must be a list of records, "
        f"got {blocked!r} (count-only / global reason / missing key do not satisfy)")
    assert len(blocked) == len(_Q2_BLOCKED_REPOS), (
        f"{label}: blocked_repositories must contain exactly {len(_Q2_BLOCKED_REPOS)} "
        f"records {_Q2_BLOCKED_REPOS}, got {len(blocked)}: {blocked!r}")

    by_id = {}
    for rec in blocked:
        assert isinstance(rec, dict), (
            f"{label}: each blocked record must be a dict with repo_id+reason, "
            f"got {rec!r}")
        rid = rec.get("repo_id")
        reason = rec.get("reason")
        assert rid, f"{label}: blocked record missing repo_id: {rec!r}"
        assert rid != "org/ok", (
            f"{label}: healthy repository org/ok must not appear in "
            f"blocked_repositories, got {blocked!r}")
        assert rid not in by_id, (
            f"{label}: duplicate blocked repository {rid!r}; exactly one record "
            f"per blocked id required, got {blocked!r}")
        assert isinstance(reason, str) and reason.strip(), (
            f"{label}: blocked record for {rid!r} must carry a non-empty reason, "
            f"got {reason!r}")
        by_id[rid] = reason

    assert set(by_id) == set(_Q2_BLOCKED_REPOS), (
        f"{label}: blocked set must be exactly {set(_Q2_BLOCKED_REPOS)}, "
        f"got {set(by_id)} (no extras, no omissions)")

    # Each blocked id maps to its own appropriate reason (not one global string).
    pickle_reason = by_id["org/pickle"].lower()
    noweights_reason = by_id["org/noweights"].lower()
    assert "pickle" in pickle_reason, (
        f"{label}: org/pickle reason must describe pickle policy, "
        f"got {by_id['org/pickle']!r}")
    assert (
        "supported" in noweights_reason
        or "no supported" in noweights_reason
        or "weight" in noweights_reason
    ), (
        f"{label}: org/noweights reason must describe unsupported/missing weights, "
        f"got {by_id['org/noweights']!r}")
    assert by_id["org/pickle"] != by_id["org/noweights"], (
        f"{label}: each blocked repo must carry its own reason, not a shared global "
        f"string (both were {by_id['org/pickle']!r})")

    assert obs["actions"] == _Q2_EXPECTED_ACTIONS, (
        f"{label}: actions must be exactly {_Q2_EXPECTED_ACTIONS} on the selected "
        f"container, got {obs['actions']!r} (actions outside that container do not count)")


def _q2_well_formed_block(*, blocked=None, actions=None, omit_actions=False):
    """Build a single well-formed observability container for negative probes."""
    if blocked is None:
        blocked = [
            {"repo_id": "org/pickle", "reason": "pickle-only weights blocked by policy"},
            {"repo_id": "org/noweights", "reason": "no supported archive weights"},
        ]
    block = {
        "code": "MANIFEST_POLICY",
        "gate": "B",
        "evidence": {"blocked_repositories": list(blocked)},
    }
    if not omit_actions:
        block["actions"] = list(actions if actions is not None else _Q2_EXPECTED_ACTIONS)
    return block


def test_c10_multi_repo_policy_errors_gate_infeasible_with_named_evidence():
    con = f.mem_con()
    _seed_mixed_policy_selection(con)

    # --- Current-green negative probes: malformed shapes must not satisfy ---
    # 1. actions placed outside the selected block (cross-container rescue forbidden)
    malformed_actions_outside = {
        "header": {"gate_b_code": "INFEASIBLE"},
        "gate_b_refusal": _q2_well_formed_block(omit_actions=True),
        "actions": list(_Q2_EXPECTED_ACTIONS),
    }
    with pytest.raises(AssertionError):
        _assert_q2_exact_shape(
            malformed_actions_outside,
            label="probe: actions outside selected block")

    # 2. extra blocked healthy repository org/ok
    malformed_extra_ok = {
        "header": {"gate_b_code": "INFEASIBLE"},
        "gate_b_refusal": _q2_well_formed_block(blocked=[
            {"repo_id": "org/pickle", "reason": "pickle-only weights blocked by policy"},
            {"repo_id": "org/noweights", "reason": "no supported archive weights"},
            {"repo_id": "org/ok", "reason": "healthy repo wrongly marked blocked"},
        ]),
    }
    with pytest.raises(AssertionError):
        _assert_q2_exact_shape(
            malformed_extra_ok, label="probe: extra blocked org/ok")

    # 3. duplicate blocked repository
    malformed_duplicate = {
        "header": {"gate_b_code": "INFEASIBLE"},
        "gate_b_refusal": _q2_well_formed_block(blocked=[
            {"repo_id": "org/pickle", "reason": "pickle-only weights blocked by policy"},
            {"repo_id": "org/pickle", "reason": "pickle-only weights blocked by policy"},
            {"repo_id": "org/noweights", "reason": "no supported archive weights"},
        ]),
    }
    with pytest.raises(AssertionError):
        _assert_q2_exact_shape(
            malformed_duplicate, label="probe: duplicate blocked repository")

    # Real expected-red preview assertion (production still lacks the structured shape)
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    _assert_q2_exact_shape(
        payload, label="c10 multi-repo policy INFEASIBLE observability")


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
    _seed_mixed_policy_selection(con)
    payload = prop.preview_pure(con, "ark", ("adopt_current", ()))
    selected = {r[0] for r in con.execute("SELECT repo_id FROM selection")}
    assert {"org/pickle", "org/noweights"} <= selected, (
        "blocked repos must remain selected so the operator can act; silent "
        "omission from selection is not the contract")
    # Same exact shape as c10 — structured blocked_repositories, not omission-from-tasks alone.
    _assert_q2_exact_shape(
        payload, label="c12 blocked repos not silently omitted")


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
