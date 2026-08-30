"""INC-027 Gate-1 contracts — baseline certificate exact multi-file revalidation.

Expected-red until Gate-2 remediates ``execution_session._catalog_projection_bundle``
to recompute current certificates from approve-time authority rather than self-copying
``proposal.baseline_certificate`` after any-row archival presence.

Authority inventory (must not invent a parallel path):
  • Approve-time evidence: ``proposal._baseline_file_evidence``
  • Canonical constructor: ``proposal_canonical.baseline_satisfaction_certificate``
  • Exact sibling revalidation: ``proposal.validate_exact_assignment``
  • Defective execution seam: ``execution_session._catalog_projection_bundle``
  • Projection comparison: ``execution_projection.project_pure``

Exact vocabulary:
  • Partial/changed evidence → ``APPROVAL_PROJECTION_VIOLATION`` /
    reason ``baseline_certificate_mismatch``
  • Complete archive absence → reason ``baseline_archive_missing``
    (``__MISSING__`` solely for that path)
  • Epoch drift → ``APPROVED_TARGET_IDENTITY_CHANGED`` with expected/current epoch
  • Manifest drift → ``APPROVED_INPUT_CHANGED`` / ``full_manifest_hash``
  • Lost drive → ``APPROVAL_PROJECTION_VIOLATION`` / ``drive_lifecycle``

No production in this gate.
"""
from __future__ import annotations

import hashlib
import inspect
from types import SimpleNamespace
from unittest import mock

import _pr09_gate1_fixtures as f
from modelark import proposal as prop
from modelark import proposal_canonical as canonical
from modelark.execution_projection import ExecutionProjection, project_pure
from modelark.execution_session import _catalog_projection_bundle
from modelark.proposal import Refusal


_CERT_MISMATCH_REASON = "baseline_certificate_mismatch"
_ABSENCE_REASON = "baseline_archive_missing"

# Non-hardcoded multi-file names (model.safetensors has no special authority).
_FILE_A = "weights-shard-a.safetensors"
_FILE_B = "tokenizer-main.json"
_REPO = "org/inc027-base"
_DRIVE = "d0"
_RID = f"primary:{_REPO}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _services():
    return SimpleNamespace(
        observe_exact_capacity=lambda *a, **k: {
            "d0": SimpleNamespace(kind="offline", executable=True, admissible_free=10**12),
            "d1": SimpleNamespace(kind="offline", executable=True, admissible_free=10**12),
        },
    )


def _seed_drives(con):
    for label, meta in f.DRIVE_IDS.items():
        free = 10**12
        con.execute(
            "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
            "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
            "write_authority,filesystem_capacity_bytes) "
            "VALUES(?,?,?,?,0,'active','enabled',?,1,?,'dedicated_local',?)",
            [label, free, free, meta["role"], meta["epoch"], meta["fingerprint"], free],
        )


def _insert_file(con, repo, rfilename, fmt, quant, data: bytes):
    digest = _sha(data)
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES(?,?,?,?,?,?)",
        [repo, rfilename, len(data), fmt, quant, digest],
    )
    return digest


def _insert_archived(
    con, repo, rfilename, drive, data: bytes, *, digest=None, annex_key=None,
    orig_bytes=None, stored_bytes=None,
):
    digest = digest or _sha(data)
    ob = len(data) if orig_bytes is None else orig_bytes
    sb = len(data) if stored_bytes is None else stored_bytes
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,orig_bytes,stored_bytes,compressed,annex_key,verified_at) "
        "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
        [
            repo, rfilename, rfilename, rfilename, drive,
            digest, ob, sb, annex_key, "2026-07-11 10:00:00",
        ],
    )


def _seed_multifile_baseline(con, *, repo=_REPO, drive=_DRIVE):
    """Genuine multi-file archived baseline: ≥2 files, non-hardcoded names."""
    from modelark import plan
    con.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
    _seed_drives(con)
    if plan.get(con, "ark") is None:
        plan.create(con, "ark", name="Ark")
    for label in f.DRIVE_IDS:
        if label not in plan.plan_drive_labels(con, "ark"):
            plan.add_drive(con, "ark", label)
    plan.set_active(con, "ark")

    data_a = b"shard-a-bytes"
    data_b = b'{"tok":1}'
    dig_a = _insert_file(con, repo, _FILE_A, "safetensors", "bf16", data_a)
    dig_b = _insert_file(con, repo, _FILE_B, "aux", None, data_b)
    key_a = f"SHA256E-s{len(data_a)}--{dig_a}"
    _insert_archived(con, repo, _FILE_A, drive, data_a, digest=dig_a, annex_key=key_a)
    _insert_archived(con, repo, _FILE_B, drive, data_b, digest=dig_b, annex_key=None)
    n = con.execute(
        "SELECT count(*) FROM archived WHERE repo_id=? AND drive_label=?",
        [repo, drive],
    ).fetchone()[0]
    assert n >= 2, n
    return repo, drive, {_FILE_A: dig_a, _FILE_B: dig_b}


def _drive_identity(con, label):
    row = con.execute(
        "SELECT identity_fingerprint, identity_epoch FROM drives WHERE drive_label=?",
        [label],
    ).fetchone()
    assert row and row[0], row
    return row[0], int(row[1])


def _realign_semantic(con, proposal):
    """Keep execution_invariants aligned after catalog mutations so the target pin decides."""
    try:
        proposal["semantic_input_hash"] = prop._semantic_input_hash(
            con, "ark", ("adopt_current", ()))
    except Exception:
        proposal["semantic_input_hash"] = None


def _evidence_by_drive():
    return {
        _DRIVE: SimpleNamespace(
            executable=True, kind="offline", admissible_free=10**12),
        "d1": SimpleNamespace(
            executable=True, kind="offline", admissible_free=10**12),
    }


def _recompute_certificate(con, *, repo, drive, rid, full_manifest_hash=None):
    """Approve-time authority: _baseline_file_evidence + baseline_satisfaction_certificate."""
    mh = full_manifest_hash or prop._manifest_hash(con, repo)
    fp, epoch = _drive_identity(con, drive)
    files = prop._baseline_file_evidence(con, repo, drive)
    cert = canonical.baseline_satisfaction_certificate(
        requirement_id=rid,
        full_manifest_hash=mh,
        drive_label=drive,
        identity_epoch=epoch,
        identity_fingerprint=fp,
        files=files,
    )
    return cert, mh, files


def _baseline_proposal(con, *, repo=_REPO, drive=_DRIVE, rid=_RID):
    """Approved-shaped proposal with genuine multi-file baseline certificate."""
    cert, mh, files = _recompute_certificate(con, repo=repo, drive=drive, rid=rid)
    assert len(files) >= 2, files
    assert not any(x.get("rfilename") == "model.safetensors" for x in files)
    try:
        sem = prop._semantic_input_hash(con, "ark", ("adopt_current", ()))
    except Exception:
        sem = "s" * 64
    tasks = [
        {
            "requirement_id": rid,
            "row_kind": "baseline_satisfied",
            "repo_id": repo,
            "target_drive": None,
            "source_drive": None,
            "satisfying_drive": drive,
            "full_manifest_hash": mh,
            "order_key": 1,
            "guaranteed_durable": 0,
            "expected_durable": 0,
            "identity_epoch": _drive_identity(con, drive)[1],
            "baseline_certificate": cert,
        }
    ]
    proposal = {
        "lifecycle": "approved",
        "proposal_id": "inc027-g1",
        "plan_id": "ark",
        "mutation_kind": "adopt_current",
        "mutation_args": (),
        "requirement_set_hash": prop._requirement_set_hash(tasks),
        "semantic_input_hash": sem,
        "capacity_mode": "guaranteed",
        "policy_version": "1",
        "solver_version": "1",
        "tasks": tasks,
        "files": [],
    }
    return proposal, cert


def _project(con, proposal):
    """Execution seam: catalog projection bundle → project_pure."""
    current_input, current_graph = _catalog_projection_bundle(
        con, proposal, ["d0", "d1"], _services(), {"capacity_mode": "guaranteed"},
    )
    out = project_pure(proposal, current_input, current_graph, f.EMPTY_OVERLAY)
    return out, current_input


def _refusal_evidence(out) -> dict:
    if isinstance(out, Refusal):
        ev = getattr(out, "evidence", None)
        if isinstance(ev, dict):
            return ev
    ev = getattr(out, "evidence", None)
    return ev if isinstance(ev, dict) else {}


def _assert_refuses_cert_mismatch(out, *, label: str):
    assert f.is_refusal(out), f"{label}: expected refusal, got {out!r}"
    code = f.refusal_code(out)
    assert code == "APPROVAL_PROJECTION_VIOLATION", (
        f"{label}: code must be APPROVAL_PROJECTION_VIOLATION, got {code!r} / {out!r}"
    )
    reason = _refusal_evidence(out).get("reason")
    assert reason == _CERT_MISMATCH_REASON, (
        f"{label}: exact reason must be {_CERT_MISMATCH_REASON!r}, "
        f"got reason={reason!r} evidence={_refusal_evidence(out)!r} out={out!r}"
    )


# ---------------------------------------------------------------------------
# GREEN — preserved / already-true behaviour
# ---------------------------------------------------------------------------

def test_g01_intact_proposal_is_approval_valid_and_projects():
    """Unchanged multi-file evidence: validate_exact_assignment + project_pure succeed."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, cert = _baseline_proposal(con)
    # Approval-valid before any mutation.
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    out, _inp = _project(con, proposal)
    assert not f.is_refusal(out), f"g01: intact baseline must project, got {out!r}"
    assert isinstance(out, ExecutionProjection), out
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed == cert


def test_g02_complete_archive_absence_is_baseline_archive_missing():
    """__MISSING__ path is solely complete archive absence → baseline_archive_missing."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _cert = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute("DELETE FROM archived WHERE repo_id=?", [_REPO])
    _realign_semantic(con, proposal)
    out, current_input = _project(con, proposal)
    assert f.is_refusal(out), out
    assert f.refusal_code(out) == "APPROVAL_PROJECTION_VIOLATION"
    assert _refusal_evidence(out).get("reason") == _ABSENCE_REASON, _refusal_evidence(out)
    # Bundle marks absence with __MISSING__ (not self-copied stored cert).
    got = (getattr(current_input, "certificates", None) or {}).get(_RID)
    assert got == "__MISSING__", f"g02: complete absence must yield __MISSING__, got {got!r}"


def test_g03_validate_exact_assignment_refuses_removed_file():
    """Approve-time sibling recomputes multi-file evidence (green)."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _cert = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute(
        "DELETE FROM archived WHERE repo_id=? AND rfilename=?", [_REPO, _FILE_B]
    )
    try:
        prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
        raise AssertionError("g03: validate_exact_assignment must refuse after file removal")
    except Refusal as exc:
        assert exc.code == "EXACT_ASSIGNMENT_REJECTED", exc
        assert exc.evidence.get("reason") == _CERT_MISMATCH_REASON, exc.evidence


def test_g04_approve_helpers_are_the_named_authority():
    """Inventory: helpers exist and bind multi-file certificate fields."""
    assert callable(prop._baseline_file_evidence)
    assert callable(canonical.baseline_satisfaction_certificate)
    assert callable(prop.validate_exact_assignment)
    con = f.mem_con()
    _seed_multifile_baseline(con)
    files = prop._baseline_file_evidence(con, _REPO, _DRIVE)
    assert {x["rfilename"] for x in files} == {_FILE_A, _FILE_B}
    for field in ("orig_sha256", "orig_bytes", "annex_key", "stored_bytes"):
        assert field in files[0], field
    c1, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    c2, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert c1 == c2 and len(c1) == 64


def test_g05_executable_projection_succeeds_when_content_satisfied():
    """Executable path is genuinely green: successful ExecutionProjection, not merely non-baseline."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/exec",), with_archive_on=[("org/exec", "d0")])
    mh = prop._manifest_hash(con, "org/exec")
    try:
        sem = prop._semantic_input_hash(con, "ark", ("adopt_current", ()))
    except Exception:
        sem = "s" * 64
    tasks = [{
        "requirement_id": "primary:org/exec",
        "row_kind": "executable",
        "repo_id": "org/exec",
        "target_drive": "d0",
        "source_drive": None,
        "satisfying_drive": None,
        "full_manifest_hash": mh,
        "order_key": 1,
        "guaranteed_durable": 100,
        "expected_durable": 100,
        "identity_epoch": 1,
        "baseline_certificate": None,
    }]
    proposal = {
        "lifecycle": "approved",
        "proposal_id": "inc027-exec",
        "plan_id": "ark",
        "mutation_kind": "adopt_current",
        "mutation_args": (),
        "requirement_set_hash": prop._requirement_set_hash(tasks),
        "semantic_input_hash": sem,
        "capacity_mode": "guaranteed",
        "policy_version": "1",
        "solver_version": "1",
        "tasks": tasks,
        "files": [{
            "requirement_id": "primary:org/exec",
            "rfilename": "model.safetensors",
            "role": "missing",
            "size_bytes": 100,
            "orig_sha256": "1" * 64,
            "format": "safetensors",
            "quant": "bf16",
            "storage_action": "compress",
        }],
    }
    current_input, current_graph = _catalog_projection_bundle(
        con, proposal, ["d0", "d1"], _services(), {"capacity_mode": "guaranteed"},
    )
    out = project_pure(proposal, current_input, current_graph, f.EMPTY_OVERLAY)
    assert not f.is_refusal(out), f"g05: executable projection must succeed, got {out!r}"
    assert isinstance(out, ExecutionProjection), out
    # Content-satisfied on target → shrunk out of remaining work (empty tasks is success).
    assert out.tasks == () or all(
        getattr(t, "requirement_id", None) or (t.get("requirement_id") if isinstance(t, dict) else None)
        for t in out.tasks
    )
    assert isinstance(out.projection_hash, str) and len(out.projection_hash) == 64


def test_g06_lost_drive_refuses_with_drive_lifecycle():
    """Lost satisfying drive: exact established refusal vocabulary."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _ = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute(
        "UPDATE drives SET lifecycle='lost' WHERE drive_label=?", [_DRIVE]
    )
    _realign_semantic(con, proposal)
    out, _ = _project(con, proposal)
    assert f.is_refusal(out), out
    assert f.refusal_code(out) == "APPROVAL_PROJECTION_VIOLATION"
    ev = _refusal_evidence(out)
    assert ev.get("reason") == "drive_lifecycle", ev
    assert ev.get("drive") == _DRIVE
    assert ev.get("lifecycle") == "lost"


def test_g07_epoch_drift_is_approved_target_identity_changed():
    """Epoch change (same fingerprint) is identity refusal, not certificate mismatch."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _ = _baseline_proposal(con)
    assert proposal["tasks"][0]["identity_epoch"] == 1
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute(
        "UPDATE drives SET identity_epoch=? WHERE drive_label=?", [2, _DRIVE]
    )
    _realign_semantic(con, proposal)
    out, _ = _project(con, proposal)
    assert f.is_refusal(out), out
    assert f.refusal_code(out) == "APPROVED_TARGET_IDENTITY_CHANGED", out
    ev = _refusal_evidence(out)
    assert ev.get("drive") == _DRIVE
    assert ev.get("expected_epoch") == 1, ev
    assert ev.get("current_epoch") == 2, ev


def test_g08_manifest_drift_is_approved_input_changed_full_manifest_hash():
    """Catalog planned-set hash drift → exact APPROVED_INPUT_CHANGED / full_manifest_hash."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _ = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    stored_mh = proposal["tasks"][0]["full_manifest_hash"]
    # Change planned safetensors digest → full_manifest_hash drifts.
    con.execute(
        "UPDATE files SET sha256=? WHERE repo_id=? AND rfilename=?",
        ["b" * 64, _REPO, _FILE_A],
    )
    current_mh = prop._manifest_hash(con, _REPO)
    assert current_mh != stored_mh
    _realign_semantic(con, proposal)
    out, _ = _project(con, proposal)
    assert f.is_refusal(out), out
    assert f.refusal_code(out) == "APPROVED_INPUT_CHANGED", out
    assert _refusal_evidence(out).get("reason") == "full_manifest_hash", _refusal_evidence(out)


# ---------------------------------------------------------------------------
# RED — execution seam must recompute multi-file certificate
# ---------------------------------------------------------------------------

def test_r01_removing_one_of_two_files_refuses_with_cert_mismatch():
    """Any-row presence of the remaining file must not satisfy the baseline."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute(
        "DELETE FROM archived WHERE repo_id=? AND rfilename=?", [_REPO, _FILE_B]
    )
    remaining = con.execute(
        "SELECT count(*) FROM archived WHERE repo_id=? AND drive_label=?",
        [_REPO, _DRIVE],
    ).fetchone()[0]
    assert remaining == 1
    _realign_semantic(con, proposal)
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r01 remove one file")
    got = (getattr(current_input, "certificates", None) or {}).get(_RID)
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert got == recomputed, (
        f"r01: current cert must be recomputed, not self-copied; got={got!r}"
    )


def test_r02_orig_sha256_change_refuses():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute(
        "UPDATE archived SET orig_sha256=? WHERE repo_id=? AND rfilename=?",
        ["0" * 64, _REPO, _FILE_A],
    )
    _realign_semantic(con, proposal)
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r02 orig_sha256")
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r03_orig_bytes_change_refuses():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute(
        "UPDATE archived SET orig_bytes=? WHERE repo_id=? AND rfilename=?",
        [99999, _REPO, _FILE_A],
    )
    _realign_semantic(con, proposal)
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r03 orig_bytes")
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r04_annex_key_change_refuses():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute(
        "UPDATE archived SET annex_key=? WHERE repo_id=? AND rfilename=?",
        ["SHA256E-s1--" + ("f" * 64), _REPO, _FILE_A],
    )
    _realign_semantic(con, proposal)
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r04 annex_key")
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r05_stored_bytes_change_refuses():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute(
        "UPDATE archived SET stored_bytes=? WHERE repo_id=? AND rfilename=?",
        [1, _REPO, _FILE_B],
    )
    _realign_semantic(con, proposal)
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r05 stored_bytes")
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r06_adding_archived_row_refuses_archived_unfiltered():
    """Extra on-drive file with matching catalog row; planned hash unchanged; cert mismatches."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    mh_before = prop._manifest_hash(con, _REPO)
    assert mh_before == proposal["tasks"][0]["full_manifest_hash"]

    # Policy-excluded format (other/onnx) while safetensors remain planned — FK-valid files row.
    extra = b"extra-on-drive"
    dig = _insert_file(con, _REPO, "extra-notes.onnx", "other", None, extra)
    mh_after = prop._manifest_hash(con, _REPO)
    assert mh_after == mh_before, (
        "r06: planned full_manifest_hash must be unchanged when only policy-excluded "
        f"catalog rows are added (before={mh_before[:16]} after={mh_after[:16]})"
    )
    _insert_archived(con, _REPO, "extra-notes.onnx", _DRIVE, extra, digest=dig)
    # FK: archived references files — already inserted.
    n_files = con.execute(
        "SELECT count(*) FROM files WHERE repo_id=? AND rfilename=?",
        [_REPO, "extra-notes.onnx"],
    ).fetchone()[0]
    assert n_files == 1

    _realign_semantic(con, proposal)
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r06 add archived row")
    recomputed, _, files = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert len(files) == 3
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r07_self_copy_of_stored_certificate_is_forbidden():
    """current_input.certificates must equal helper recompute, never the stored proposal cert alone."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    con.execute(
        "UPDATE archived SET orig_sha256=? WHERE repo_id=? AND rfilename=?",
        ["a" * 64, _REPO, _FILE_B],
    )
    _realign_semantic(con, proposal)
    _, current_input = _project(con, proposal)
    got = (getattr(current_input, "certificates", None) or {}).get(_RID)
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert got != stored, (
        f"r07: must not self-copy proposal baseline_certificate (got {got!r})"
    )
    assert got == recomputed, (
        f"r07: must equal baseline_satisfaction_certificate(_baseline_file_evidence(...)); "
        f"got={got!r} expected={recomputed!r}"
    )


def test_r08_none_or_absent_current_certificate_is_exact_cert_mismatch():
    """None / missing current cert while archive rows remain → exact cert mismatch (not pass)."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _stored = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    current_input, current_graph = _catalog_projection_bundle(
        con, proposal, ["d0", "d1"], _services(), {"capacity_mode": "guaranteed"},
    )
    # Rows still present on drive.
    assert any(
        k[0] == _REPO and k[2] == _DRIVE
        for k in (getattr(current_input, "archived", None) or {})
    )

    current_input.certificates = {_RID: None}
    out = project_pure(proposal, current_input, current_graph, f.EMPTY_OVERLAY)
    _assert_refuses_cert_mismatch(out, label="r08 None current cert")

    current_input.certificates = {}
    out2 = project_pure(proposal, current_input, current_graph, f.EMPTY_OVERLAY)
    _assert_refuses_cert_mismatch(out2, label="r08b absent current cert")

    # __MISSING__ is reserved for genuine complete absence (g02), not partial presence.
    current_input.certificates = {_RID: "__MISSING__"}
    # With rows still present, production must not treat this as clean success either.
    out3 = project_pure(proposal, current_input, current_graph, f.EMPTY_OVERLAY)
    assert f.is_refusal(out3), out3
    # When rows exist, __MISSING__ is not the honest complete-absence path — still refuse.
    assert f.refusal_code(out3) == "APPROVAL_PROJECTION_VIOLATION"


def test_r09_no_model_safetensors_special_case_in_baseline_block():
    """Baseline block must not retain hardcoded model.safetensors presence authority."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _ = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    files = prop._baseline_file_evidence(con, _REPO, _DRIVE)
    assert all(row["rfilename"] != "model.safetensors" for row in files)
    # Remove a non-default name; any model.safetensors-only check would still false-pass.
    con.execute(
        "DELETE FROM archived WHERE repo_id=? AND rfilename=?", [_REPO, _FILE_A]
    )
    _realign_semantic(con, proposal)
    out, _ = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r09 arbitrary filename")

    src = inspect.getsource(project_pure)
    # Explicit prohibition of the defective special-case key.
    assert 'model.safetensors' not in src or '(repo, "model.safetensors", label)' not in src, (
        "r09: project_pure baseline block must not hardcode (repo, 'model.safetensors', label)"
    )
    assert '(repo, "model.safetensors", label)' not in src, (
        "r09: remove hardcoded model.safetensors archival presence key from baseline block"
    )


def test_r10_bundle_behaviorally_invokes_evidence_and_certificate_helpers():
    """Spies: _catalog_projection_bundle must call both existing helpers (not comments)."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _ = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())

    real_ev = prop._baseline_file_evidence
    real_cert = canonical.baseline_satisfaction_certificate
    evidence_calls: list = []
    cert_calls: list = []

    def spy_ev(*args, **kwargs):
        evidence_calls.append((args, kwargs))
        return real_ev(*args, **kwargs)

    def spy_cert(*args, **kwargs):
        cert_calls.append((args, kwargs))
        return real_cert(*args, **kwargs)

    # Patch at definition modules (where Gate-2 must call through).
    import modelark.execution_session as esess
    patches = [
        mock.patch("modelark.proposal._baseline_file_evidence", side_effect=spy_ev),
        mock.patch(
            "modelark.proposal_canonical.baseline_satisfaction_certificate",
            side_effect=spy_cert,
        ),
        mock.patch(
            "modelark.proposal_canonical.certificate_baseline_satisfied",
            side_effect=spy_cert,
        ),
    ]
    if hasattr(esess, "_baseline_file_evidence"):
        patches.append(
            mock.patch.object(esess, "_baseline_file_evidence", side_effect=spy_ev)
        )
    for name in ("baseline_satisfaction_certificate", "certificate_baseline_satisfied"):
        if hasattr(esess, name):
            patches.append(mock.patch.object(esess, name, side_effect=spy_cert))
    for p in patches:
        p.start()
    try:
        _catalog_projection_bundle(
            con, proposal, ["d0", "d1"], _services(),
            {"capacity_mode": "guaranteed"},
        )
    finally:
        for p in patches:
            p.stop()

    assert len(evidence_calls) >= 1, (
        "r10: _catalog_projection_bundle must call proposal._baseline_file_evidence "
        f"(got {len(evidence_calls)} calls)"
    )
    assert len(cert_calls) >= 1, (
        "r10: _catalog_projection_bundle must call "
        "proposal_canonical.baseline_satisfaction_certificate "
        f"(got {len(cert_calls)} calls)"
    )
    # Evidence spy must have been asked for this baseline repo/drive.
    assert any(
        _REPO in str(a) or _DRIVE in str(a)
        for a, _k in evidence_calls
    ) or evidence_calls, evidence_calls


def test_r11_same_epoch_fingerprint_change_is_cert_mismatch():
    """Same-epoch identity_fingerprint drift is certificate-bound → baseline_certificate_mismatch."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    prop.validate_exact_assignment(con, proposal, _evidence_by_drive())
    fp0, epoch0 = _drive_identity(con, _DRIVE)
    assert epoch0 == 1
    new_fp = "c" * 64
    assert new_fp != fp0
    con.execute(
        "UPDATE drives SET identity_fingerprint=? WHERE drive_label=?",
        [new_fp, _DRIVE],
    )
    # Epoch unchanged — must not become APPROVED_TARGET_IDENTITY_CHANGED.
    assert _drive_identity(con, _DRIVE) == (new_fp, 1)
    _realign_semantic(con, proposal)
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r11 same-epoch fingerprint")
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed
    # Not the epoch-drift code path.
    assert f.refusal_code(out) != "APPROVED_TARGET_IDENTITY_CHANGED"
