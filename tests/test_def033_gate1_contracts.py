"""DEF-033 Gate-1 contracts — verifier policy-error disposition (DEC-060 §6).

Expected-red until Gate-2 production remediates ``modelark/verifier.py``
(``reverify`` false-clean at the ``ArchivePolicyError`` fallback; ``suspects``
false-suspect at the union-of-archived-names fallback) and the Verify operator
surface treats policy unknowns as neutral follow-ups.

Disposition pinned by DEC-060 / DEF-033:
  • ArchivePolicyError ⇒ operator-visible **unknown** (not clean/verified).
  • ``ok`` must be false; record completeness must not be true when the required
    manifest is unknowable.
  • Typed policy-error evidence in the result; do not manufacture a planned set
    from archived rows.
  • Remains unknown even if every available physical byte check passes.
  • Independent known failures (digest disagreement, missing mounted bytes,
    insufficient copies) remain **failed**, not downgraded to unknown.
  • ManifestBatch.errors ⇒ distinct neutral unknown follow-up — not integrity /
    "partial copy" solely because policy evaluation failed.
  • Legitimate multi-drive layout under policy-error must not generate the
    union-of-filenames false suspect.
  • Independent real suspect reasons (disruption, raw-float fallback) stay visible.
  • Operator surface: unknowns counted/rendered neutrally; excluded from
    "re-verify all integrity suspects"; no automatic mutation; manual re-verify
    may report the typed unknown.

No production or UI changes in this gate. Green pins preserve existing
supported-manifest, offline, physical-failure, access-gated, and ordinary
integrity-suspect behaviour.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from modelark.core import db
from modelark import archive_manifest, verifier


# ---------------------------------------------------------------------------
# Fixtures — genuine unsupported-policy catalogs (no mocked ArchivePolicyError)
# ---------------------------------------------------------------------------

def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _require_policy_error(con, repo_id: str) -> archive_manifest.ArchivePolicyError:
    """Prove recovery_policy() genuinely cannot build a manifest (not a mock)."""
    try:
        archive_manifest.manifest_for_repo(
            con, repo_id, archive_manifest.recovery_policy()
        )
    except archive_manifest.ArchivePolicyError as exc:
        return exc
    raise AssertionError(
        f"{repo_id}: expected genuine ArchivePolicyError under recovery_policy(); "
        "fixture is not unsupported-policy"
    )


def _seed_unsupported(
    con,
    repo: str = "nvidia/parakeet-tdt-0.6b-v2",
    *,
    numcopies: int = 1,
    files: list[tuple[str, str, bytes]] | None = None,
    drive: str = "d0",
):
    """Catalog + archived rows with no supported weights → ArchivePolicyError.

    Default shape mirrors the live DEF-033 exposure: aux + foreign ``other``
    weights only (no safetensors/GGUF/pickle).
    """
    if files is None:
        files = [
            ("config.json", "aux", b'{"arch":"parakeet"}'),
            ("tokenizer.json", "aux", b'{"tok":1}'),
            ("model.onnx", "other", b"onnx-weight-bytes"),
        ]
    con.execute(
        "INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES(?,?)",
        [repo, numcopies],
    )
    for rfilename, fmt, data in files:
        digest = _sha(data)
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?,?,?,?,NULL,?)",
            [repo, rfilename, len(data), fmt, digest],
        )
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?)",
            [
                repo, rfilename, Path(rfilename).name, rfilename, drive,
                digest, len(data), len(data), "2026-07-11 10:00:00",
            ],
        )
    return repo, files


def _seed_supported_complete(con, repo: str = "org/supported"):
    """One planned safetensors + aux, fully archived on one drive — clean."""
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
    weights = b"safetensors-bytes"
    cfg = b'{"ok":true}'
    for rfilename, fmt, quant, data in (
        ("m.safetensors", "safetensors", "bf16", weights),
        ("config.json", "aux", None, cfg),
    ):
        digest = _sha(data)
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?,?,?,?,?,?)",
            [repo, rfilename, len(data), fmt, quant, digest],
        )
        stored = rfilename + (".znn" if fmt == "safetensors" else "")
        # compressed flag only for float safetensors path; record path for reverify deep=False
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                repo, rfilename, Path(stored).name, stored, "d0",
                digest, len(data), len(data),
                1 if fmt == "safetensors" else 0, "2026-07-11 12:00:00",
            ],
        )
    return repo


def _write_archived_tree(root: Path, repo: str, files: list[tuple[str, str, bytes]], drive_label: str = "d0"):
    """Materialize archived blobs under root/<repo>/<rfilename> for deep reverify."""
    base = root / repo
    for rfilename, _fmt, data in files:
        path = base / rfilename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _policy_error_fields(result: dict) -> object:
    """Locate typed policy-error evidence on a reverify result (Gate-2 shape)."""
    for key in (
        "policy_error",
        "archive_policy_error",
        "manifest_policy_error",
        "policy_errors",
    ):
        if key in result and result[key]:
            return result[key]
    detail = result.get("detail") or ""
    # Accept a structured sub-object under detail only if explicitly typed.
    return None if not any(
        token in detail.lower()
        for token in (
            "archivepolicyerror",
            "policy error",
            "manifest policy",
            "no supported archive weights",
            "policy cannot",
            "policy evaluation",
        )
    ) else detail


# ---------------------------------------------------------------------------
# GREEN — preserve existing correct behaviour
# ---------------------------------------------------------------------------

def test_g01_supported_manifest_record_ok_offline_is_unknown_not_verified():
    """Supported complete archive with shelved drive: record_ok, status unknown, ok false."""
    con = _mem()
    repo = _seed_supported_complete(con)
    # Prove policy succeeds (supported).
    planned = {
        item.rfilename
        for item in archive_manifest.manifest_for_repo(
            con, repo, archive_manifest.recovery_policy()
        )
    }
    assert planned == {"m.safetensors", "config.json"}
    r = verifier.reverify(con, repo, deep=True)
    assert r["archived"] is True
    assert r["record_ok"] is True
    assert r["status"] == "unknown"
    assert r["ok"] is False
    assert r["deep_ran"] is False


def test_g02_digest_disagreement_is_failed():
    con = _mem()
    repo = _seed_supported_complete(con)
    con.execute(
        "UPDATE archived SET orig_sha256='WRONG' WHERE repo_id=? AND rfilename='m.safetensors'",
        [repo],
    )
    r = verifier.reverify(con, repo, deep=False)
    assert r["record_ok"] is False
    assert "m.safetensors" in r["sha_mismatch"]
    assert r["status"] == "failed"
    assert r["ok"] is False


def test_g03_mounted_missing_bytes_are_failed():
    con = _mem()
    repo = _seed_supported_complete(con)
    # Re-archive as raw so deep path reads real files without ZipNN.
    con.execute("DELETE FROM archived WHERE repo_id=?", [repo])
    weights = b"safetensors-bytes"
    cfg = b'{"ok":true}'
    for rfilename, data in (("m.safetensors", weights), ("config.json", cfg)):
        digest = _sha(data)
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?)",
            [
                repo, rfilename, rfilename, rfilename, "d0",
                digest, len(data), len(data), "2026-07-11 12:00:00",
            ],
        )
    with tempfile.TemporaryDirectory() as td:
        # Mount present but blobs absent → hard fail.
        with mock.patch.object(verifier.register, "archive_path", return_value=Path(td)):
            r = verifier.reverify(con, repo, deep=True)
    assert r["status"] == "failed"
    assert r["ok"] is False
    assert r["deep_ran"] is True
    assert any(c.get("ok") is False for c in r["deep_checks"])


def test_g04_ordinary_integrity_suspects_remain():
    con = _mem()
    # Float raw fallback
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('A','m.safetensors',100,'safetensors','bf16','sA')"
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
        "VALUES('A','m.safetensors','m.safetensors','m.safetensors','drive-00',"
        "'sA',100,100,0,'2026-07-11 10:00:00')"
    )
    # Partial copy under a *supported* plan (one of two planned files missing)
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('B','m.safetensors',100,'safetensors','bf16','sB')"
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('B','config.json',10,'aux',NULL,NULL)"
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
        "VALUES('B','m.safetensors','m.safetensors.znn','m.safetensors.znn','drive-01',"
        "'sB',100,80,1,'2026-07-11 10:00:00')"
    )
    # Disruption window
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('C','m.safetensors',100,'safetensors','bf16','sC')"
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
        "VALUES('C','m.safetensors','m.safetensors.znn','m.safetensors.znn','drive-02',"
        "'sC',100,80,1,'2026-07-11 12:00:00')"
    )
    con.execute(
        "INSERT INTO fetch_events(repo_id,event_at,outcome,detail) "
        "VALUES('C','2026-07-11 12:05:00','awaiting-drive','drive drop')"
    )
    reps = {s["repo"]: s for s in verifier.suspects(con)}
    assert set(reps) >= {"A", "B", "C"}
    assert any("float" in reason for reason in reps["A"]["reasons"])
    assert any("partial" in reason for reason in reps["B"]["reasons"])
    assert any("disruption" in reason for reason in reps["C"]["reasons"])
    assert "integrity" in reps["A"]["types"]
    assert "integrity" in reps["B"]["types"]
    assert "integrity" in reps["C"]["types"]


def test_g05_access_gated_is_typed_not_integrity():
    con = _mem()
    con.execute(
        "INSERT INTO fetch_events(repo_id,event_at,outcome,detail) VALUES(?,?,?,?)",
        [
            "org/gated",
            "2026-07-17 21:00:00",
            "auth",
            '{"resolution":"timeout","type":"access-gated",'
            '"url":"https://huggingface.co/org/gated"}',
        ],
    )
    one = verifier.suspects(con)
    assert len(one) == 1 and one[0]["repo"] == "org/gated"
    assert one[0]["types"] == ["access-gated"]
    assert "integrity" not in one[0]["types"]


def test_g06_reverify_all_integrity_filter_excludes_access_gated():
    """Operator contract already true for access-gated; pin so unknowns can share it."""
    followups = [
        {"repo": "a", "types": ["integrity"], "reasons": ["partial copy (interrupted)"]},
        {"repo": "b", "types": ["access-gated"], "reasons": ["Hugging Face access required"]},
        {"repo": "c", "types": ["integrity", "access-gated"], "reasons": ["x"]},
    ]
    integrity_only = [
        s["repo"]
        for s in followups
        if "integrity" in (s.get("types") or ["integrity"])
    ]
    assert integrity_only == ["a", "c"]

    js = Path("modelark/web/static/verify.js").read_text()
    assert 'includes("integrity")' in js
    assert "vfReverifyAll" in js
    # Bulk re-verify is scoped to integrity suspects.
    assert re.search(
        r"filter\(s\s*=>\s*\(s\.types\s*\|\|\s*\[\"integrity\"\]\)\.includes\(\"integrity\"\)\)",
        js,
    )


# ---------------------------------------------------------------------------
# RED — reverify policy-error disposition
# ---------------------------------------------------------------------------

def test_r01_policy_error_is_unknown_not_verified_even_when_physical_passes():
    """ArchivePolicyError must not green-verify when every available byte check passes."""
    con = _mem()
    repo, files = _seed_unsupported(con)
    err = _require_policy_error(con, repo)
    assert "no supported archive weights" in str(err).lower() or "supported" in str(err).lower()

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_archived_tree(root, repo, files)
        with mock.patch.object(verifier.register, "archive_path", return_value=root):
            r = verifier.reverify(con, repo, deep=True)

    assert r["archived"] is True
    assert r["ok"] is False, (
        f"policy-unknowable archive must not report ok=true (got status={r.get('status')!r})"
    )
    assert r["status"] == "unknown", (
        f"ArchivePolicyError disposition is operator-visible unknown, got {r.get('status')!r}"
    )
    # Must not claim verified / deep_ok when the planned set is unknowable.
    assert r.get("deep_ok") is not True


def test_r02_record_completeness_false_when_manifest_unknowable():
    """Do not manufacture planned_names from archived rows → record_ok must be false."""
    con = _mem()
    repo, _files = _seed_unsupported(con)
    _require_policy_error(con, repo)
    r = verifier.reverify(con, repo, deep=False)
    assert r["record_ok"] is False, (
        "record_ok must not be true when recovery_policy cannot evaluate the required "
        f"manifest (got record_ok={r.get('record_ok')!r}, missing={r.get('missing')!r})"
    )


def test_r03_typed_policy_error_evidence_in_result():
    con = _mem()
    repo, _files = _seed_unsupported(con)
    err = _require_policy_error(con, repo)
    r = verifier.reverify(con, repo, deep=False)
    evidence = _policy_error_fields(r)
    assert evidence is not None, (
        "reverify result must carry typed policy-error evidence "
        f"(keys={sorted(r)}; detail={r.get('detail')!r})"
    )
    # Evidence must identify the failure class, not invent a planned file list.
    blob = str(evidence).lower()
    assert any(
        token in blob
        for token in (
            "policy",
            "archivepolicy",
            "no supported",
            "manifest",
        )
    ), f"evidence does not look policy-typed: {evidence!r}"
    # Prefer structured field over detail-only when present.
    structured = any(
        k in r and r[k]
        for k in ("policy_error", "archive_policy_error", "manifest_policy_error", "policy_errors")
    )
    assert structured or "policy" in (r.get("detail") or "").lower() or str(err).split(":")[0] in str(evidence)
    # Must not pretend missing is a known empty list derived from archived rows without flagging policy.
    if r.get("missing") == [] and r.get("record_ok") is True:
        raise AssertionError(
            "manufactured planned set from archived rows (missing=[] with record_ok) "
            "without honest policy-unknown disposition"
        )


def test_r04_sha_mismatch_under_policy_error_remains_failed():
    """Independent digest disagreement is failed, not soft-downgraded to unknown."""
    con = _mem()
    repo, _files = _seed_unsupported(con)
    _require_policy_error(con, repo)
    con.execute(
        "UPDATE archived SET orig_sha256=? WHERE repo_id=? AND rfilename='model.onnx'",
        ["0" * 64, repo],
    )
    r = verifier.reverify(con, repo, deep=False)
    assert "model.onnx" in r["sha_mismatch"]
    assert r["status"] == "failed"
    assert r["ok"] is False
    assert r["record_ok"] is False


def test_r05_missing_mounted_bytes_under_policy_error_remains_failed():
    con = _mem()
    repo, files = _seed_unsupported(con)
    _require_policy_error(con, repo)
    with tempfile.TemporaryDirectory() as td:
        # Drive mounted; no blobs written.
        with mock.patch.object(verifier.register, "archive_path", return_value=Path(td)):
            r = verifier.reverify(con, repo, deep=True)
    assert r["status"] == "failed", (
        f"missing mounted bytes must fail even when policy is unknowable, got {r.get('status')!r}"
    )
    assert r["ok"] is False
    assert any(c.get("ok") is False for c in r.get("deep_checks") or [])


def test_r06_insufficient_copies_under_policy_error_remains_failed():
    """numcopies=2 with one healthy mounted copy must still fail on insufficient."""
    con = _mem()
    repo, files = _seed_unsupported(con, numcopies=2)
    _require_policy_error(con, repo)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_archived_tree(root, repo, files)
        with mock.patch.object(verifier.register, "archive_path", return_value=root):
            r = verifier.reverify(con, repo, deep=True)
    # Current code may still green-verify (false clean) or unknown; pin: failed for insufficient.
    assert r["status"] == "failed", (
        f"insufficient copies must remain failed under policy-error, got {r.get('status')!r} "
        f"insufficient={r.get('insufficient')!r}"
    )
    assert r["ok"] is False
    assert r.get("insufficient"), "expected non-empty insufficient list for numcopies=2 / 1 copy"


# ---------------------------------------------------------------------------
# RED — suspects() policy-error disposition
# ---------------------------------------------------------------------------

def test_r07_manifest_batch_error_is_neutral_unknown_not_integrity_partial():
    """Repos in ManifestBatch.errors → distinct unknown follow-up, not partial integrity."""
    con = _mem()
    repo, _files = _seed_unsupported(con, repo="org/aux-only")
    batch = archive_manifest.inspect_manifests_for_repos(
        con, [repo], archive_manifest.recovery_policy()
    )
    assert repo in batch.errors and repo not in batch.manifests

    reps = {s["repo"]: s for s in verifier.suspects(con)}
    assert repo in reps, (
        f"policy-error repository must surface as a follow-up, got {list(reps)}"
    )
    entry = reps[repo]
    types = set(entry.get("types") or [])
    reasons = " ".join(entry.get("reasons") or []).lower()
    assert "integrity" not in types, (
        f"policy evaluation failure must not be labelled integrity: {entry}"
    )
    assert "partial copy" not in reasons, (
        f"must not invent partial-copy solely from policy failure: {entry}"
    )
    # Neutral unknown type (name closed by Gate 2; accept common vocabulary).
    assert types & {"unknown", "policy-unknown", "manifest-policy", "policy"}, (
        f"expected a neutral unknown-type tag, got types={types}"
    )


def test_r08_multi_drive_policy_error_not_union_filename_false_suspect():
    """Split archived names across drives under policy-error must not false-flag partial."""
    con = _mem()
    repo = "org/split-foreign"
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
    parts = [
        ("a.onnx", "other", b"aaa", "d0"),
        ("b.onnx", "other", b"bbb", "d1"),
    ]
    for rfilename, fmt, data, drive in parts:
        digest = _sha(data)
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?,?,?,?,NULL,?)",
            [repo, rfilename, len(data), fmt, digest],
        )
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?)",
            [
                repo, rfilename, rfilename, rfilename, drive,
                digest, len(data), len(data), "2026-07-11 10:00:00",
            ],
        )
    batch = archive_manifest.inspect_manifests_for_repos(
        con, [repo], archive_manifest.recovery_policy()
    )
    assert repo in batch.errors

    reps = {s["repo"]: s for s in verifier.suspects(con)}
    if repo in reps:
        entry = reps[repo]
        reasons = " ".join(entry.get("reasons") or []).lower()
        types = set(entry.get("types") or [])
        assert "partial copy" not in reasons, (
            "union-of-filenames fallback must not flag legitimate multi-drive "
            f"policy-error layout as partial copy: {entry}"
        )
        assert "integrity" not in types or types & {"unknown", "policy-unknown", "manifest-policy", "policy"}, (
            f"must not be integrity-only partial: {entry}"
        )
    else:
        # Accept missing entry only if Gate 2 chooses pure reverify-path surfacing;
        # DEF-033 pins a distinct follow-up, so absence is still red.
        raise AssertionError(
            "policy-error multi-drive repository must appear as a neutral unknown follow-up"
        )


def test_r09_real_suspect_reasons_remain_visible_alongside_policy_unknown():
    """Disruption / raw-float stay integrity; policy-unknown is a separate type."""
    con = _mem()
    # Policy-error repo
    policy_repo, _ = _seed_unsupported(con, repo="org/policy")
    _require_policy_error(con, policy_repo)
    # Float raw integrity
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/float','m.safetensors',100,'safetensors','bf16','sF')"
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
        "VALUES('org/float','m.safetensors','m.safetensors','m.safetensors','d0',"
        "'sF',100,100,0,'2026-07-11 10:00:00')"
    )
    # Disruption
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
        "VALUES('org/disrupt','m.safetensors',100,'safetensors','bf16','sD')"
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
        "VALUES('org/disrupt','m.safetensors','m.safetensors.znn','m.safetensors.znn','d1',"
        "'sD',100,80,1,'2026-07-11 12:00:00')"
    )
    con.execute(
        "INSERT INTO fetch_events(repo_id,event_at,outcome,detail) "
        "VALUES('org/disrupt','2026-07-11 12:05:00','compress-fallback','raw')"
    )

    reps = {s["repo"]: s for s in verifier.suspects(con)}
    assert "org/float" in reps and any("float" in r for r in reps["org/float"]["reasons"])
    assert "integrity" in reps["org/float"]["types"]
    assert "org/disrupt" in reps and any("disruption" in r for r in reps["org/disrupt"]["reasons"])
    assert "integrity" in reps["org/disrupt"]["types"]
    assert policy_repo in reps, f"policy unknown missing from follow-ups: {list(reps)}"
    ptypes = set(reps[policy_repo].get("types") or [])
    assert "integrity" not in ptypes or ptypes & {"unknown", "policy-unknown", "manifest-policy", "policy"}
    assert not any("partial" in r for r in reps[policy_repo].get("reasons") or [])


# ---------------------------------------------------------------------------
# RED — operator surface (static Verify UI + bulk filter contract)
# ---------------------------------------------------------------------------

def test_r10_operator_counts_unknowns_separately_from_integrity():
    """verify.js must count/render neutral unknowns; not fold them into integrity."""
    js = Path("modelark/web/static/verify.js").read_text()
    html = Path("modelark/web/static/index.html").read_text()
    # Must acknowledge an unknown / policy follow-up class in the note or badges.
    has_unknown_vocab = bool(
        re.search(r"unknown|policy-unknown|manifest-policy|policy", js, re.I)
    ) and (
        "unknown" in js
        or "policy-unknown" in js
        or "manifest-policy" in js
    )
    # Today only integrity + access-gated are counted.
    note_line = next(
        (line for line in js.splitlines() if "integrity suspect" in line or "access follow-up" in line),
        "",
    )
    asserts_unknown_count = bool(
        re.search(r"unknown", note_line, re.I)
        or re.search(r"policy", note_line, re.I)
    )
    assert has_unknown_vocab and asserts_unknown_count, (
        "Verify UI must visibly count neutral unknown follow-ups separately from "
        f"integrity suspects (note line={note_line!r})"
    )
    # Badge styling for unknown must not reuse integrity "bad" alone without a neutral path.
    assert "access-gated" in js  # existing neutral-ish mut path
    # Unknown type should map to neutral (mut) rather than integrity bad-only.
    assert re.search(r"unknown|policy-unknown", js), (
        "verify.js must name the unknown follow-up type for rendering"
    )
    assert "Verify" in html or "vfSuspects" in html


def test_r11_unknown_excluded_from_reverify_all_integrity_suspects():
    """Bulk re-verify must not include pure unknown follow-ups."""
    followups = [
        {"repo": "integrity-one", "types": ["integrity"], "reasons": ["partial copy (interrupted)"]},
        {"repo": "policy-u", "types": ["unknown"], "reasons": ["archive policy cannot be evaluated"]},
        {"repo": "policy-alt", "types": ["policy-unknown"], "reasons": ["no supported archive weights"]},
        {"repo": "access", "types": ["access-gated"], "reasons": ["Hugging Face access required"]},
        {
            "repo": "both",
            "types": ["integrity", "unknown"],
            "reasons": ["float weights stored raw", "policy unknown"],
        },
    ]
    # Contract: bulk set is integrity-tagged only (unknown-only repos excluded).
    bulk = [
        s["repo"]
        for s in followups
        if "integrity" in (s.get("types") or [])
    ]
    assert bulk == ["integrity-one", "both"]
    assert "policy-u" not in bulk and "policy-alt" not in bulk

    js = Path("modelark/web/static/verify.js").read_text()
    # Source of truth for bulk selection remains the integrity filter.
    assert re.search(
        r"filter\(s\s*=>\s*\(s\.types\s*\|\|\s*\[\"integrity\"\]\)\.includes\(\"integrity\"\)\)",
        js,
    )
    # Pure-unknown rows must not get a one-click integrity re-verify action solely from defaulting types.
    # After Gate 2, types default must not coerce unknown → integrity.
    # Pin: suspectRow only offers re-verify when integrity is present — already true —
    # and types default ["integrity"] must not apply when the server sends types:["unknown"].
    # The red pin is server-side types; UI default only applies when types missing.
    assert 's.types || ["integrity"]' in js or "s.types || ['integrity']" in js


def test_r12_no_automatic_mutation_on_policy_unknown_and_manual_reports_unknown():
    """Policy unknown authorises no automatic repair/restore/delete; manual reverify may report it."""
    con = _mem()
    repo, files = _seed_unsupported(con)
    _require_policy_error(con, repo)
    before = con.execute(
        "SELECT count(*) FROM archived WHERE repo_id=?", [repo]
    ).fetchone()[0]
    before_events = con.execute("SELECT count(*) FROM fetch_events").fetchone()[0]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_archived_tree(root, repo, files)
        with mock.patch.object(verifier.register, "archive_path", return_value=root):
            r = verifier.reverify(con, repo, deep=True)

    after = con.execute(
        "SELECT count(*) FROM archived WHERE repo_id=?", [repo]
    ).fetchone()[0]
    after_events = con.execute("SELECT count(*) FROM fetch_events").fetchone()[0]
    assert after == before, "reverify must not mutate archived rows"
    assert after_events == before_events, "reverify must not write fetch_events"
    # Manual path may report typed unknown (this is the desired disposition).
    assert r["ok"] is False
    assert r["status"] == "unknown"
    assert _policy_error_fields(r) is not None

    # suspects() must not trigger repair/hash_repair imports as a side effect of listing.
    import sys
    pre = {name for name in sys.modules if "hash_repair" in name or name.endswith("restore")}
    _ = verifier.suspects(con)
    post = {name for name in sys.modules if "hash_repair" in name or name.endswith("restore")}
    assert post == pre or not (post - pre), (
        f"suspects() must not load mutation modules as a side effect: new={post - pre}"
    )


def test_r13_fixture_is_genuine_policy_error_not_import_gap():
    """Meta-pin: contracts fail on semantics, not missing symbols."""
    assert hasattr(verifier, "reverify") and callable(verifier.reverify)
    assert hasattr(verifier, "suspects") and callable(verifier.suspects)
    assert issubclass(archive_manifest.ArchivePolicyError, Exception)
    con = _mem()
    repo, _ = _seed_unsupported(con)
    err = _require_policy_error(con, repo)
    assert isinstance(err, archive_manifest.ArchivePolicyError)
    batch = archive_manifest.inspect_manifests_for_repos(
        con, [repo], archive_manifest.recovery_policy()
    )
    assert repo in batch.errors
    # Current false-clean still observable (documents why Gate 2 is owed).
    r = verifier.reverify(con, repo, deep=False)
    # If production already fixed record_ok, this meta-pin still only checks callability.
    assert "record_ok" in r and "status" in r and "ok" in r
