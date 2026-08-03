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

Pin (behavioral + helper authority):
  • Unchanged multi-file evidence passes.
  • Removing any one of ≥2 archived files refuses (any-row presence is not enough).
  • Changing any certificate-bound field (orig_sha256, orig_bytes, annex_key,
    stored_bytes) refuses.
  • Adding an archived row refuses (archived-unfiltered certificate semantics).
  • Current certificate is canonically recomputed; copying the proposal's stored
    certificate into current_input.certificates is forbidden.
  • missing/None current certificate cannot pass merely because an archive row exists.
  • Arbitrary filenames work; model.safetensors has no special authority.
  • Complete archive absence → typed ``baseline_archive_missing``.
  • Partial/changed evidence → exact reason ``baseline_certificate_mismatch``
    (one vocabulary; no aliases).
  • Drive lifecycle, identity/epoch, manifest-drift, and executable projection stay green.

No production in this gate.
"""
from __future__ import annotations

import hashlib
import inspect
from types import SimpleNamespace

import _pr09_gate1_fixtures as f
from modelark import proposal as prop
from modelark import proposal_canonical as canonical
from modelark.execution_session import _catalog_projection_bundle
from modelark.proposal import Refusal


# Exact refusal reason for partial/changed baseline evidence (Gate-2 sole vocabulary).
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


def _insert_file(con, repo, rfilename, fmt, quant, data: bytes, *, annex_key=None):
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
    # Align semantic invariants with what _catalog_projection_bundle recomputes.
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
    rsh = prop._requirement_set_hash(tasks)
    proposal = {
        "lifecycle": "approved",
        "proposal_id": "inc027-g1",
        "plan_id": "ark",
        "mutation_kind": "adopt_current",
        "mutation_args": (),
        "requirement_set_hash": rsh,
        "semantic_input_hash": sem,
        "capacity_mode": "guaranteed",
        "policy_version": "1",
        "solver_version": "1",
        "tasks": tasks,
        "files": [],  # baseline_satisfied carries no proposal_files
    }
    return proposal, cert


def _project(con, proposal):
    """Execution seam: catalog projection bundle → project_pure."""
    _, project_pure = f.project_pure_fn()
    current_input, current_graph = _catalog_projection_bundle(
        con, proposal, ["d0", "d1"], _services(), {"capacity_mode": "guaranteed"},
    )
    out = project_pure(proposal, current_input, current_graph, f.EMPTY_OVERLAY)
    return out, current_input


def _assert_refuses_cert_mismatch(out, *, label: str):
    assert f.is_refusal(out), f"{label}: expected refusal, got {out!r}"
    code = f.refusal_code(out)
    assert code == "APPROVAL_PROJECTION_VIOLATION", (
        f"{label}: code must be APPROVAL_PROJECTION_VIOLATION, got {code!r} / {out!r}"
    )
    evidence = getattr(out, "evidence", None) or {}
    if not isinstance(evidence, dict) and hasattr(out, "evidence"):
        evidence = out.evidence if isinstance(out.evidence, dict) else {}
    # Refusal may store evidence as mapping attribute or positional.
    if not evidence and hasattr(out, "args") and out.args:
        for a in out.args:
            if isinstance(a, dict) and "reason" in a:
                evidence = a
                break
    reason = evidence.get("reason") if isinstance(evidence, dict) else None
    if reason is None and isinstance(out, Refusal):
        # modelark.proposal.Refusal often: Refusal(code, evidence, actions)
        try:
            reason = out.evidence.get("reason")  # type: ignore[union-attr]
            evidence = out.evidence  # type: ignore[assignment]
        except Exception:
            pass
    assert reason == _CERT_MISMATCH_REASON, (
        f"{label}: exact reason must be {_CERT_MISMATCH_REASON!r}, "
        f"got reason={reason!r} evidence={evidence!r} out={out!r}"
    )


def _refusal_evidence(out) -> dict:
    if isinstance(out, Refusal):
        ev = getattr(out, "evidence", None)
        if isinstance(ev, dict):
            return ev
    ev = getattr(out, "evidence", None)
    if isinstance(ev, dict):
        return ev
    return {}


# ---------------------------------------------------------------------------
# GREEN — existing correct / already-true behaviour
# ---------------------------------------------------------------------------

def test_g01_unchanged_multifile_evidence_passes():
    """Complete multi-file evidence on satisfying drive still projects cleanly."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, cert = _baseline_proposal(con)
    out, current_input = _project(con, proposal)
    assert not f.is_refusal(out), f"g01: intact baseline must pass, got {out!r}"
    # Stored certificate still matches recompute under unchanged evidence.
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed == cert


def test_g02_complete_archive_absence_is_baseline_archive_missing():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _cert = _baseline_proposal(con)
    con.execute("DELETE FROM archived WHERE repo_id=?", [_REPO])
    out, _inp = _project(con, proposal)
    assert f.is_refusal(out), out
    assert f.refusal_code(out) == "APPROVAL_PROJECTION_VIOLATION"
    assert _refusal_evidence(out).get("reason") == _ABSENCE_REASON, (
        f"g02: complete absence must be {_ABSENCE_REASON!r}, got {_refusal_evidence(out)!r}"
    )


def test_g03_validate_exact_assignment_recomputes_and_refuses_removed_file():
    """Approve-time sibling already uses multi-file authority (green)."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _cert = _baseline_proposal(con)
    con.execute(
        "DELETE FROM archived WHERE repo_id=? AND rfilename=?", [_REPO, _FILE_B]
    )
    evidence_by_drive = {
        _DRIVE: SimpleNamespace(executable=True, kind="offline", admissible_free=10**12),
    }
    try:
        prop.validate_exact_assignment(con, proposal, evidence_by_drive)
        raise AssertionError("g03: validate_exact_assignment must refuse after file removal")
    except Refusal as exc:
        assert exc.code == "EXACT_ASSIGNMENT_REJECTED", exc
        assert exc.evidence.get("reason") == "baseline_certificate_mismatch", exc.evidence


def test_g04_approve_helpers_are_the_named_authority():
    """Inventory pin: helpers exist and bind multi-file fields (no second constructor)."""
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


def test_g05_executable_projection_still_works():
    """Executable multi-file shrink/satisfaction path remains independent of baseline fix."""
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/exec",), with_archive_on=[("org/exec", "d0")])
    # Use project_pure with a simple executable task — should not refuse baseline paths.
    _, project_pure = f.project_pure_fn()
    proposal = {
        "lifecycle": "approved",
        "proposal_id": "inc027-exec",
        "plan_id": "ark",
        "requirement_set_hash": "r" * 64,
        "semantic_input_hash": "s" * 64,
        "capacity_mode": "guaranteed",
        "policy_version": "1",
        "solver_version": "1",
        "tasks": [{
            "requirement_id": "primary:org/exec",
            "row_kind": "executable",
            "repo_id": "org/exec",
            "target_drive": "d0",
            "source_drive": None,
            "satisfying_drive": None,
            "full_manifest_hash": "a" * 64,
            "order_key": 1,
            "guaranteed_durable": 100,
            "expected_durable": 100,
            "identity_epoch": 1,
            "baseline_certificate": None,
        }],
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
    services = _services()
    current_input, current_graph = _catalog_projection_bundle(
        con, proposal, ["d0", "d1"], services, {"capacity_mode": "guaranteed"},
    )
    out = project_pure(proposal, current_input, current_graph, f.EMPTY_OVERLAY)
    # Content-satisfied executable shrinks or remains; must not be a baseline cert refusal.
    if f.is_refusal(out):
        ev = _refusal_evidence(out)
        assert ev.get("reason") not in (_CERT_MISMATCH_REASON, "baseline_certificate"), out


def test_g06_baseline_drive_lost_still_refuses():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _ = _baseline_proposal(con)
    con.execute(
        "UPDATE drives SET lifecycle='lost' WHERE drive_label=?", [_DRIVE]
    )
    # Re-align semantic hash after catalog mutation so the refuse is the lifecycle pin
    # (not an earlier execution_invariants drift).
    try:
        proposal["semantic_input_hash"] = prop._semantic_input_hash(
            con, "ark", ("adopt_current", ()))
    except Exception:
        proposal["semantic_input_hash"] = None
    out, _ = _project(con, proposal)
    assert f.is_refusal(out), out
    assert f.refusal_code(out) == "APPROVAL_PROJECTION_VIOLATION"
    reason = _refusal_evidence(out).get("reason")
    # Generic assigned-drive lifecycle check or baseline-specific invalid drive.
    assert reason in ("drive_lifecycle", "baseline_drive_invalid"), reason


# ---------------------------------------------------------------------------
# RED — execution seam must recompute multi-file certificate
# ---------------------------------------------------------------------------

def test_r01_removing_one_of_two_files_refuses_with_cert_mismatch():
    """Any-row presence of the remaining file must not satisfy the baseline."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    con.execute(
        "DELETE FROM archived WHERE repo_id=? AND rfilename=?", [_REPO, _FILE_B]
    )
    # One file remains — defective path self-copies stored cert and falsely passes.
    remaining = con.execute(
        "SELECT count(*) FROM archived WHERE repo_id=? AND drive_label=?",
        [_REPO, _DRIVE],
    ).fetchone()[0]
    assert remaining == 1
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r01 remove one file")
    # Bundle must not leave the stored certificate as current when evidence diverged.
    got = (getattr(current_input, "certificates", None) or {}).get(_RID)
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored, "fixture: removal must change certificate"
    assert got == recomputed, (
        f"r01: current certificate must be recomputed ({recomputed[:16]}…), "
        f"not self-copied stored ({stored[:16]}…); got={got!r}"
    )


def test_r02_orig_sha256_change_refuses():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    con.execute(
        "UPDATE archived SET orig_sha256=? WHERE repo_id=? AND rfilename=?",
        ["0" * 64, _REPO, _FILE_A],
    )
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r02 orig_sha256")
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r03_orig_bytes_change_refuses():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    con.execute(
        "UPDATE archived SET orig_bytes=? WHERE repo_id=? AND rfilename=?",
        [99999, _REPO, _FILE_A],
    )
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r03 orig_bytes")
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r04_annex_key_change_refuses():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    con.execute(
        "UPDATE archived SET annex_key=? WHERE repo_id=? AND rfilename=?",
        ["SHA256E-s1--" + ("f" * 64), _REPO, _FILE_A],
    )
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r04 annex_key")
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r05_stored_bytes_change_refuses():
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    con.execute(
        "UPDATE archived SET stored_bytes=? WHERE repo_id=? AND rfilename=?",
        [1, _REPO, _FILE_B],
    )
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r05 stored_bytes")
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r06_adding_archived_row_refuses():
    """Archived-unfiltered certificate: extra on-drive file changes the bound set."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    extra = b"extra-on-drive"
    dig = _sha(extra)
    # Catalog row optional for archived-unfiltered evidence (evidence reads archived only).
    _insert_archived(con, _REPO, "extra-notes.txt", _DRIVE, extra, digest=dig)
    out, current_input = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r06 add archived row")
    recomputed, _, files = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert len(files) == 3
    assert recomputed != stored
    assert (getattr(current_input, "certificates", None) or {}).get(_RID) == recomputed


def test_r07_self_copy_of_stored_certificate_is_forbidden():
    """Even when evidence is intact, current cert must equal helper recompute (not only stored)."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    # Mutate evidence so stored no longer matches, then require bundle recomputes.
    con.execute(
        "UPDATE archived SET orig_sha256=? WHERE repo_id=? AND rfilename=?",
        ["a" * 64, _REPO, _FILE_B],
    )
    _, current_input = _project(con, proposal)
    got = (getattr(current_input, "certificates", None) or {}).get(_RID)
    recomputed, _, _ = _recompute_certificate(con, repo=_REPO, drive=_DRIVE, rid=_RID)
    assert got != stored, (
        f"r07: must not self-copy proposal baseline_certificate into current_input "
        f"(got stored self-copy {got!r})"
    )
    assert got == recomputed, (
        f"r07: current certificate must equal "
        f"baseline_satisfaction_certificate(_baseline_file_evidence(...)); "
        f"got={got!r} expected={recomputed!r}"
    )


def test_r08_missing_current_certificate_cannot_pass_on_any_row_presence():
    """If certificates[rid] is missing/None/__MISSING__, any-row presence must not green-pass."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, stored = _baseline_proposal(con)
    # Force empty certificate map path: strip certificate from proposal after building bundle manually.
    _, project_pure = f.project_pure_fn()
    current_input, current_graph = _catalog_projection_bundle(
        con, proposal, ["d0", "d1"], _services(), {"capacity_mode": "guaranteed"},
    )
    # Simulate production emitting no usable current cert while rows remain.
    current_input.certificates = {_RID: None}
    out = project_pure(proposal, current_input, current_graph, f.EMPTY_OVERLAY)
    # Must refuse — cannot accept solely because archived rows exist on the drive.
    assert f.is_refusal(out), (
        f"r08: missing/None current certificate must not pass via any-row presence; got {out!r}"
    )
    # Also: blank certificates dict
    current_input.certificates = {}
    out2 = project_pure(proposal, current_input, current_graph, f.EMPTY_OVERLAY)
    assert f.is_refusal(out2), (
        f"r08b: absent certificate entry must not pass; got {out2!r}"
    )


def test_r09_arbitrary_filenames_not_model_safetensors_special_case():
    """Baseline evidence uses actual archived names; hardcoded model.safetensors is not authority."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _ = _baseline_proposal(con)
    files = prop._baseline_file_evidence(con, _REPO, _DRIVE)
    assert all(f["rfilename"] != "model.safetensors" for f in files)
    # Remove non-default name; defective path that only checks model.safetensors would still pass.
    con.execute(
        "DELETE FROM archived WHERE repo_id=? AND rfilename=?", [_REPO, _FILE_A]
    )
    out, _ = _project(con, proposal)
    _assert_refuses_cert_mismatch(out, label="r09 arbitrary filename")
    # Source of project_pure must not special-case model.safetensors for baseline pass.
    src = inspect.getsource(
        __import__("modelark.execution_projection", fromlist=["project_pure"]).project_pure
    )
    # After Gate-2, baseline path must not treat model.safetensors as the sole presence key.
    # Pin: with only tokenizer left (no model.safetensors ever), still refuses — covered above.
    assert _FILE_A not in src  # production must not hardcode our fixture name either


def test_r10_bundle_source_uses_baseline_file_evidence_and_canonical_certificate():
    """Helper-authority: _catalog_projection_bundle must call the existing constructors."""
    src = inspect.getsource(_catalog_projection_bundle)
    assert "_baseline_file_evidence" in src or "baseline_file_evidence" in src, (
        "r10: _catalog_projection_bundle must use proposal._baseline_file_evidence "
        "(no second evidence path)"
    )
    assert "baseline_satisfaction_certificate" in src, (
        "r10: _catalog_projection_bundle must call "
        "proposal_canonical.baseline_satisfaction_certificate"
    )
    # Defective any-row LIMIT 1 self-copy must not remain the sole certificate path.
    assert "LIMIT 1" not in src or "_baseline_file_evidence" in src, (
        "r10: any-row LIMIT 1 presence check is not multi-file evidence authority"
    )


def test_r11_manifest_drift_still_refuses_independently():
    """full_manifest_hash drift remains APPROVED_INPUT_CHANGED (not cert path confusion)."""
    con = f.mem_con()
    _seed_multifile_baseline(con)
    proposal, _ = _baseline_proposal(con)
    # Change catalog content identity so full_manifest_hash drifts.
    con.execute(
        "UPDATE files SET sha256=? WHERE repo_id=? AND rfilename=?",
        ["b" * 64, _REPO, _FILE_A],
    )
    out, _ = _project(con, proposal)
    assert f.is_refusal(out), out
    # Either APPROVED_INPUT_CHANGED (manifest) or cert mismatch if hash is in cert —
    # both are fail-closed; pin that it does not silently pass.
    code = f.refusal_code(out)
    assert code in ("APPROVED_INPUT_CHANGED", "APPROVAL_PROJECTION_VIOLATION"), code
