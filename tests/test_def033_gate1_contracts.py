"""DEF-033 Gate-1 contracts — verifier policy-error disposition (DEC-060 §6).

Expected-red until Gate-2 production remediates ``modelark/verifier.py``
(``reverify`` false-clean at the ``ArchivePolicyError`` fallback; ``suspects``
false-suspect at the union-of-archived-names fallback) and the Verify operator
surface treats policy unknowns as neutral follow-ups.

Pinned Gate-1 interface (sole vocabulary for Gate 2):
  • Policy-unknown reverify result:
      status == "unknown"
      ok is False
      record_ok is None          # tri-state: True/False/None — not False-for-unknown
      missing is None            # not [] manufactured from archived rows
      policy_error == {
          "code": "ARCHIVE_POLICY_UNKNOWN",
          "detail": <non-empty original ArchivePolicyError text>,
      }
  • Known evidence retains precedence: digest disagreement / missing mounted
    bytes / insufficient copies → status == "failed"; physical PASS cannot
    upgrade policy-unknown to verified.
  • Follow-up type is exactly ``"unknown"`` (no aliases).
      Policy-only: types == ["unknown"]
      Policy + independent integrity: sorted(types) == ["integrity", "unknown"]
  • Operator surface: count/render ``unknown`` neutrally; no unknown-only
    re-verify action; exclude from bulk integrity re-verify; no mutation.

Live-shaped fixture: four catalog files (3 aux + 1 other), three archived rows
— matches the recorded DEF-033 exposure (e.g. nvidia/parakeet-tdt-0.6b-v2).

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


# Sole follow-up type vocabulary for policy-unknown (Gate-1 closed interface).
_UNKNOWN_TYPE = "unknown"
_POLICY_ERROR_CODE = "ARCHIVE_POLICY_UNKNOWN"


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


def _live_shaped_catalog() -> list[tuple[str, str, bytes]]:
    """Four catalog files: 3 aux + 1 other (recorded live exposure shape)."""
    return [
        ("config.json", "aux", b'{"arch":"parakeet"}'),
        ("tokenizer.json", "aux", b'{"tok":1}'),
        ("preprocessor_config.json", "aux", b'{"pre":1}'),
        ("model.onnx", "other", b"onnx-weight-bytes"),
    ]


def _seed_unsupported(
    con,
    repo: str = "nvidia/parakeet-tdt-0.6b-v2",
    *,
    numcopies: int = 1,
    drive: str = "d0",
    archive_rfilenames: frozenset[str] | None = None,
):
    """Unsupported-policy catalog matching the recorded live DEF-033 exposure.

    Four catalog files (3 aux + 1 foreign ``other``); three archived rows.
    Default archives config.json, tokenizer.json, and model.onnx — leaves
    preprocessor_config.json catalog-only (4 files / 3 archived).
    """
    catalog = _live_shaped_catalog()
    if archive_rfilenames is None:
        archive_rfilenames = frozenset(
            {"config.json", "tokenizer.json", "model.onnx"}
        )
    assert len(catalog) == 4, "live-shaped catalog must have four files"
    assert len(archive_rfilenames) == 3, "live-shaped archive must have three rows"

    con.execute(
        "INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES(?,?)",
        [repo, numcopies],
    )
    archived: list[tuple[str, str, bytes]] = []
    for rfilename, fmt, data in catalog:
        digest = _sha(data)
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?,?,?,?,NULL,?)",
            [repo, rfilename, len(data), fmt, digest],
        )
        if rfilename not in archive_rfilenames:
            continue
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?)",
            [
                repo, rfilename, Path(rfilename).name, rfilename, drive,
                digest, len(data), len(data), "2026-07-11 10:00:00",
            ],
        )
        archived.append((rfilename, fmt, data))
    assert len(archived) == 3, archived
    n_files = con.execute(
        "SELECT count(*) FROM files WHERE repo_id=?", [repo]
    ).fetchone()[0]
    n_arch = con.execute(
        "SELECT count(*) FROM archived WHERE repo_id=?", [repo]
    ).fetchone()[0]
    assert (n_files, n_arch) == (4, 3), (n_files, n_arch)
    return repo, archived


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


def _write_archived_tree(root: Path, repo: str, archived: list[tuple[str, str, bytes]]):
    """Materialize only archived blobs under root/<repo>/<rfilename>."""
    base = root / repo
    for rfilename, _fmt, data in archived:
        path = base / rfilename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _tree_inventory(root: Path) -> list[tuple[str, str]]:
    """Ordered (relpath, sha256) inventory of every file under root."""
    out: list[tuple[str, str]] = []
    if not root.exists():
        return out
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        out.append((str(path.relative_to(root)), _sha(path.read_bytes())))
    return out


def _db_content_identity(con) -> str:
    """Database identity: user_version, ordered schema defs, ordered table contents."""
    h = hashlib.sha256()
    user_version = con.execute("PRAGMA user_version").fetchone()[0]
    h.update(f"user_version={user_version}".encode())
    # Ordered sqlite_master definitions for tables, indexes, triggers, and views.
    for row in con.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' "
        "ORDER BY type, name, tbl_name, ifnull(sql, '')"
    ).fetchall():
        h.update(repr(tuple(row)).encode())
    tables = [
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    ]
    for table in tables:
        cols = [
            row[1]
            for row in con.execute(f"PRAGMA table_info({table})").fetchall()
        ]
        h.update(table.encode())
        h.update(repr(cols).encode())
        if not cols:
            continue
        order = ", ".join(f'"{c}"' for c in cols)
        rows = con.execute(
            f'SELECT * FROM "{table}" ORDER BY {order}'
        ).fetchall()
        for row in rows:
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


def _assert_policy_error_retained(r: dict, err: archive_manifest.ArchivePolicyError):
    """Structured policy evidence survives even when status precedence is failed."""
    pe = r.get("policy_error")
    assert pe == {
        "code": _POLICY_ERROR_CODE,
        "detail": str(err),
    }, (
        f"policy_error must remain exactly "
        f"{{'code': {_POLICY_ERROR_CODE!r}, 'detail': <original ArchivePolicyError text>}}; "
        f"got {pe!r} (original err={str(err)!r}); status={r.get('status')!r}"
    )
    assert r.get("missing") is None, (
        f"missing must be None when the required manifest is unknowable, "
        f"not {r.get('missing')!r}"
    )
    for alt in ("archive_policy_error", "manifest_policy_error", "policy_errors"):
        assert alt not in r or r[alt] is None, f"alternate key {alt} must not be used: {r.get(alt)!r}"


def _assert_exact_policy_unknown(r: dict, err: archive_manifest.ArchivePolicyError):
    """Sole policy-unknown reverify shape for Gate 2."""
    assert r["status"] == "unknown", r
    assert r["ok"] is False, r
    assert r["record_ok"] is None, (
        f"record_ok must be None (unknowable), not {r.get('record_ok')!r} "
        f"(False would collapse unknown into known inconsistency)"
    )
    _assert_policy_error_retained(r, err)


def _assert_policy_reason_carries_batch_error(entry: dict, batch_err: Exception):
    """suspects() reason list must include the original ManifestBatch.errors detail."""
    reasons = entry.get("reasons") or []
    assert reasons, f"policy unknown follow-up must have a non-empty reasons list: {entry}"
    original = str(batch_err)
    assert any(original in reason or reason == original for reason in reasons), (
        f"reasons must carry original ManifestBatch.errors detail\n"
        f"  expected to include: {original!r}\n  got reasons: {reasons!r}"
    )


# ---------------------------------------------------------------------------
# GREEN — preserve existing correct behaviour
# ---------------------------------------------------------------------------

def test_g01_supported_manifest_record_ok_offline_is_unknown_not_verified():
    """Supported complete archive with shelved drive: record_ok, status unknown, ok false."""
    con = _mem()
    repo = _seed_supported_complete(con)
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
        with mock.patch.object(verifier.register, "archive_path", return_value=Path(td)):
            r = verifier.reverify(con, repo, deep=True)
    assert r["status"] == "failed"
    assert r["ok"] is False
    assert r["deep_ran"] is True
    assert any(c.get("ok") is False for c in r["deep_checks"])


def test_g04_ordinary_integrity_suspects_remain():
    con = _mem()
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
    """Bulk re-verify is integrity-tagged only (access-gated already excluded)."""
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
    assert re.search(
        r"filter\(s\s*=>\s*\(s\.types\s*\|\|\s*\[\"integrity\"\]\)\.includes\(\"integrity\"\)\)",
        js,
    )


def test_g07_live_shaped_fixture_is_four_catalog_three_archived():
    """Meta: default fixture matches recorded live shape (4 files / 3 archived)."""
    con = _mem()
    repo, archived = _seed_unsupported(con)
    n_files = con.execute(
        "SELECT count(*) FROM files WHERE repo_id=?", [repo]
    ).fetchone()[0]
    n_arch = con.execute(
        "SELECT count(*) FROM archived WHERE repo_id=?", [repo]
    ).fetchone()[0]
    assert (n_files, n_arch) == (4, 3)
    assert len(archived) == 3
    formats = {
        row[0]
        for row in con.execute(
            "SELECT format FROM files WHERE repo_id=?", [repo]
        ).fetchall()
    }
    assert "aux" in formats and "other" in formats
    _require_policy_error(con, repo)


# ---------------------------------------------------------------------------
# RED — reverify policy-error disposition (exact interface)
# ---------------------------------------------------------------------------

def test_r01_policy_error_exact_unknown_even_when_physical_passes():
    """All three archived blobs must physically PASS; policy-unknown still dominates."""
    con = _mem()
    repo, archived = _seed_unsupported(con)
    err = _require_policy_error(con, repo)
    archived_names = {rf for rf, _fmt, _data in archived}
    assert len(archived_names) == 3

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_archived_tree(root, repo, archived)
        assert len(_tree_inventory(root)) == 3
        with mock.patch.object(verifier.register, "archive_path", return_value=root):
            r = verifier.reverify(con, repo, deep=True)

    # Prove physical PASS first (not tautological with status==unknown).
    assert r.get("deep_ran") is True, r
    checks = r.get("deep_checks") or []
    checked_files = {c.get("file") for c in checks}
    assert checked_files == archived_names, (
        f"exactly the three archived blobs must be deep-checked; "
        f"expected {sorted(archived_names)}, got {sorted(checked_files)}"
    )
    assert len(checks) == 3, f"expected 3 deep_checks entries, got {len(checks)}: {checks}"
    assert all(c.get("ok") is True for c in checks), (
        f"every archived blob deep check must pass before asserting policy dominance: {checks}"
    )
    # Then exact policy-unknown (physical PASS cannot upgrade).
    _assert_exact_policy_unknown(r, err)
    assert r.get("deep_ok") is not True


def test_r02_record_ok_none_and_missing_none_when_manifest_unknowable():
    """Tri-state: unknowable is None, not False (failed) or True (complete)."""
    con = _mem()
    repo, _archived = _seed_unsupported(con)
    err = _require_policy_error(con, repo)
    r = verifier.reverify(con, repo, deep=False)
    _assert_exact_policy_unknown(r, err)


def test_r03_policy_error_field_exact_code_and_original_detail():
    """Sole evidence key: policy_error with ARCHIVE_POLICY_UNKNOWN + original text."""
    con = _mem()
    repo, _archived = _seed_unsupported(con)
    err = _require_policy_error(con, repo)
    r = verifier.reverify(con, repo, deep=False)
    _assert_policy_error_retained(r, err)
    assert r.get("status") == "unknown"
    assert r.get("ok") is False
    assert r.get("record_ok") is None


def test_r04_sha_mismatch_under_policy_error_remains_failed():
    """Digest disagreement → failed; policy_error retained; record_ok False."""
    con = _mem()
    repo, _archived = _seed_unsupported(con)
    err = _require_policy_error(con, repo)
    con.execute(
        "UPDATE archived SET orig_sha256=? WHERE repo_id=? AND rfilename='model.onnx'",
        ["0" * 64, repo],
    )
    r = verifier.reverify(con, repo, deep=False)
    assert "model.onnx" in r["sha_mismatch"]
    assert r["status"] == "failed"
    assert r["ok"] is False
    assert r["record_ok"] is False  # known digest inconsistency
    _assert_policy_error_retained(r, err)


def test_r05_missing_mounted_bytes_under_policy_error_remains_failed():
    """Missing mounted bytes → failed; policy_error retained; record_ok None."""
    con = _mem()
    repo, _archived = _seed_unsupported(con)
    err = _require_policy_error(con, repo)
    with tempfile.TemporaryDirectory() as td:
        with mock.patch.object(verifier.register, "archive_path", return_value=Path(td)):
            r = verifier.reverify(con, repo, deep=True)
    assert r["status"] == "failed", (
        f"missing mounted bytes must fail even when policy is unknowable, got {r.get('status')!r}"
    )
    assert r["ok"] is False
    assert any(c.get("ok") is False for c in r.get("deep_checks") or [])
    # No digest disagreement: record completeness remains unknowable (None).
    assert r["record_ok"] is None, (
        f"without digest disagreement, record_ok stays None (got {r.get('record_ok')!r})"
    )
    _assert_policy_error_retained(r, err)


def test_r06_insufficient_copies_under_policy_error_remains_failed():
    """numcopies=2 / one healthy copy → failed; policy_error retained; record_ok None."""
    con = _mem()
    repo, archived = _seed_unsupported(con, numcopies=2)
    err = _require_policy_error(con, repo)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_archived_tree(root, repo, archived)
        with mock.patch.object(verifier.register, "archive_path", return_value=root):
            r = verifier.reverify(con, repo, deep=True)
    assert r["status"] == "failed", (
        f"insufficient copies must remain failed under policy-error, got {r.get('status')!r} "
        f"insufficient={r.get('insufficient')!r}"
    )
    assert r["ok"] is False
    assert r.get("insufficient"), "expected non-empty insufficient list for numcopies=2 / 1 copy"
    assert r["record_ok"] is None, (
        f"without digest disagreement, record_ok stays None (got {r.get('record_ok')!r})"
    )
    _assert_policy_error_retained(r, err)


# ---------------------------------------------------------------------------
# RED — suspects() policy-error disposition (type exactly "unknown")
# ---------------------------------------------------------------------------

def test_r07_policy_only_followup_types_exactly_unknown():
    """ManifestBatch.errors → types == ['unknown'] + original error detail in reasons."""
    con = _mem()
    repo, _archived = _seed_unsupported(con, repo="org/aux-only")
    batch = archive_manifest.inspect_manifests_for_repos(
        con, [repo], archive_manifest.recovery_policy()
    )
    assert repo in batch.errors and repo not in batch.manifests
    batch_err = batch.errors[repo]

    reps = {s["repo"]: s for s in verifier.suspects(con)}
    assert repo in reps, (
        f"policy-error repository must surface as a follow-up, got {list(reps)}"
    )
    entry = reps[repo]
    assert entry["types"] == [_UNKNOWN_TYPE], (
        f"policy-only follow-up types must be exactly ['unknown'], got {entry.get('types')!r}"
    )
    _assert_policy_reason_carries_batch_error(entry, batch_err)
    reasons = " ".join(entry.get("reasons") or []).lower()
    assert "partial copy" not in reasons, (
        f"must not invent partial-copy solely from policy failure: {entry}"
    )


def test_r08_multi_drive_policy_error_not_union_filename_false_suspect():
    """Split archived names across drives under policy-error → types ['unknown'] + detail."""
    con = _mem()
    repo = "org/split-foreign"
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
    # Four catalog / three archived multi-drive: two on d0, one on d1, one catalog-only.
    catalog = [
        ("a.onnx", "other", b"aaa"),
        ("b.onnx", "other", b"bbb"),
        ("c.onnx", "other", b"ccc"),
        ("readme.md", "aux", b"readme"),
    ]
    archive_plan = [
        ("a.onnx", "d0"),
        ("b.onnx", "d1"),
        ("c.onnx", "d0"),
    ]
    for rfilename, fmt, data in catalog:
        digest = _sha(data)
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?,?,?,?,NULL,?)",
            [repo, rfilename, len(data), fmt, digest],
        )
    for rfilename, drive in archive_plan:
        data = next(d for n, _f, d in catalog if n == rfilename)
        digest = _sha(data)
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,verified_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?)",
            [
                repo, rfilename, rfilename, rfilename, drive,
                digest, len(data), len(data), "2026-07-11 10:00:00",
            ],
        )
    n_files = con.execute(
        "SELECT count(*) FROM files WHERE repo_id=?", [repo]
    ).fetchone()[0]
    n_arch = con.execute(
        "SELECT count(*) FROM archived WHERE repo_id=?", [repo]
    ).fetchone()[0]
    assert (n_files, n_arch) == (4, 3)
    batch = archive_manifest.inspect_manifests_for_repos(
        con, [repo], archive_manifest.recovery_policy()
    )
    assert repo in batch.errors
    batch_err = batch.errors[repo]

    reps = {s["repo"]: s for s in verifier.suspects(con)}
    assert repo in reps, "policy-error multi-drive repo must appear as unknown follow-up"
    entry = reps[repo]
    assert entry["types"] == [_UNKNOWN_TYPE], entry
    _assert_policy_reason_carries_batch_error(entry, batch_err)
    reasons = " ".join(entry.get("reasons") or []).lower()
    assert "partial copy" not in reasons, (
        "union-of-filenames fallback must not flag multi-drive policy-error "
        f"layout as partial copy: {entry}"
    )


def test_r09_independent_integrity_suspects_remain_on_other_repos():
    """Disruption / raw-float stay integrity on their own repos."""
    con = _mem()
    policy_repo, _ = _seed_unsupported(con, repo="org/policy")
    batch = archive_manifest.inspect_manifests_for_repos(
        con, [policy_repo], archive_manifest.recovery_policy()
    )
    assert policy_repo in batch.errors
    batch_err = batch.errors[policy_repo]

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
    assert reps["org/float"]["types"] == ["integrity"] or "integrity" in reps["org/float"]["types"]
    assert "org/disrupt" in reps and any("disruption" in r for r in reps["org/disrupt"]["reasons"])
    assert "integrity" in reps["org/disrupt"]["types"]
    assert policy_repo in reps, f"policy unknown missing: {list(reps)}"
    assert reps[policy_repo]["types"] == [_UNKNOWN_TYPE], reps[policy_repo]
    _assert_policy_reason_carries_batch_error(reps[policy_repo], batch_err)


def test_r09b_same_repo_policy_plus_disruption_types_integrity_and_unknown():
    """Same repository: policy + disruption → integrity+unknown; both reasons retained."""
    con = _mem()
    repo, _archived = _seed_unsupported(con, repo="org/mixed")
    batch = archive_manifest.inspect_manifests_for_repos(
        con, [repo], archive_manifest.recovery_policy()
    )
    assert repo in batch.errors
    batch_err = batch.errors[repo]
    # Disruption within ±15 min of archived.verified_at (2026-07-11 10:00:00).
    con.execute(
        "INSERT INTO fetch_events(repo_id,event_at,outcome,detail) "
        "VALUES(?,?,?,?)",
        [repo, "2026-07-11 10:05:00", "awaiting-drive", "drive drop near archive"],
    )
    reps = {s["repo"]: s for s in verifier.suspects(con)}
    assert repo in reps, list(reps)
    entry = reps[repo]
    assert sorted(entry["types"]) == ["integrity", _UNKNOWN_TYPE], (
        f"mixed policy+disruption must be exactly integrity+unknown, got {entry.get('types')!r}"
    )
    _assert_policy_reason_carries_batch_error(entry, batch_err)
    reasons_joined = " ".join(entry.get("reasons") or []).lower()
    assert "disruption" in reasons_joined, (
        f"mixed entry must retain independent integrity reason: {entry.get('reasons')!r}"
    )
    assert "partial copy" not in reasons_joined


# ---------------------------------------------------------------------------
# RED — operator surface (exact "unknown" vocabulary)
# ---------------------------------------------------------------------------

def test_r10_operator_counts_and_renders_unknown_neutrally():
    """verify.js: exact type 'unknown' counted via ${unknown.length}; badge mut only for unknown."""
    js = Path("modelark/web/static/verify.js").read_text()
    html = Path("modelark/web/static/index.html").read_text()

    # Collect unknown by exact type name.
    assert re.search(
        r'includes\(\s*["\']unknown["\']\s*\)',
        js,
    ), "verify.js must filter follow-ups by exact type 'unknown'"

    # Displayed note must include the actual unknown collection's count.
    assert "${unknown.length}" in js, (
        "vfNote must include ${unknown.length} for the unknown follow-up count "
        "(not a free-form 'unknown' substring elsewhere)"
    )

    # Neutral badge: only an explicit "unknown" → "mut" branch satisfies.
    # access-gated-only mut styling (current code) does NOT satisfy this pin.
    # Accept: t === "unknown" ? "mut"  or  (… || t === "unknown") ? "mut"
    has_explicit_unknown_mut = bool(
        re.search(
            r"""t\s*===\s*["']unknown["']\s*\?\s*["']mut["']"""
            r"""|"""
            r"""["']unknown["']\s*===\s*t\s*\?\s*["']mut["']"""
            r"""|"""
            r"""\|\|\s*t\s*===\s*["']unknown["']\s*\)\s*\?\s*["']mut["']"""
            r"""|"""
            r"""t\s*===\s*["']access-gated["']\s*\|\|\s*t\s*===\s*["']unknown["']\s*\?\s*["']mut["']"""
            r"""|"""
            r"""t\s*===\s*["']unknown["']\s*\|\|\s*t\s*===\s*["']access-gated["']\s*\?\s*["']mut["']""",
            js,
        )
    )
    assert has_explicit_unknown_mut, (
        "unknown type must map to mut via an explicit branch naming \"unknown\" "
        "(access-gated-only mut styling does not satisfy this pin)"
    )
    # Current integrity-default badge must not be the only path: unknown must not fall through to "bad".
    access_only_mut = bool(
        re.search(
            r"""t\s*===\s*["']access-gated["']\s*\?\s*["']mut["']\s*:\s*["']bad["']""",
            js,
        )
    )
    assert not access_only_mut or has_explicit_unknown_mut, (
        "access-gated ? mut : bad without naming unknown leaves unknown as bad"
    )

    assert "vfSuspects" in html or "Verify" in html


def test_r11_unknown_only_excluded_from_bulk_and_row_reverify_action():
    """Unknown-only: no row re-verify button; excluded from bulk integrity re-verify."""
    followups = [
        {"repo": "integrity-one", "types": ["integrity"], "reasons": ["partial copy (interrupted)"]},
        {"repo": "policy-u", "types": ["unknown"], "reasons": ["archive policy cannot be evaluated"]},
        {"repo": "access", "types": ["access-gated"], "reasons": ["Hugging Face access required"]},
        {
            "repo": "both",
            "types": ["integrity", "unknown"],
            "reasons": ["archived near a disruption event", "archive policy cannot be evaluated"],
        },
    ]
    bulk = [
        s["repo"]
        for s in followups
        if "integrity" in (s.get("types") or [])
    ]
    assert bulk == ["integrity-one", "both"]
    assert "policy-u" not in bulk

    # Row action: re-verify only when integrity present (unknown-only → no button).
    row_reverify = [
        s["repo"]
        for s in followups
        if "integrity" in (s.get("types") or [])
    ]
    assert "policy-u" not in row_reverify
    assert "both" in row_reverify  # mixed may still offer re-verify for the integrity reason

    js = Path("modelark/web/static/verify.js").read_text()
    assert re.search(
        r"filter\(s\s*=>\s*\(s\.types\s*\|\|\s*\[\"integrity\"\]\)\.includes\(\"integrity\"\)\)",
        js,
    )
    # Row re-verify gated on integrity (not offered for unknown-only).
    assert re.search(
        r"""integrity\s*\?\s*`?<button[^`]*re-verify|integrity\s*\?[^;]*vfone""",
        js,
    ) or ('integrity ?' in js and "re-verify" in js)
    # Must recognize exact "unknown" type (not only generic non-integrity).
    assert re.search(r'["\']unknown["\']', js), (
        "verify.js must name the exact follow-up type 'unknown'"
    )


def test_r12_no_mutation_and_manual_reports_exact_policy_unknown():
    """reverify/suspects: full DB identity + tree + total_changes unchanged; exact unknown."""
    con = _mem()
    repo, archived = _seed_unsupported(con)
    err = _require_policy_error(con, repo)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_archived_tree(root, repo, archived)
        tree_before = _tree_inventory(root)
        db_before = _db_content_identity(con)
        changes_before = con.total_changes

        with mock.patch.object(verifier.register, "archive_path", return_value=root):
            r = verifier.reverify(con, repo, deep=True)

        assert con.total_changes == changes_before, (
            f"reverify must not mutate the connection (total_changes "
            f"{changes_before} → {con.total_changes})"
        )
        assert _db_content_identity(con) == db_before, (
            "reverify must leave complete ordered database content identity unchanged"
        )
        assert _tree_inventory(root) == tree_before, (
            "reverify must not alter the test archive tree"
        )

        _assert_exact_policy_unknown(r, err)

        # suspects() likewise: no DB mutation.
        db_mid = _db_content_identity(con)
        changes_mid = con.total_changes
        tree_mid = _tree_inventory(root)
        _ = verifier.suspects(con)
        assert con.total_changes == changes_mid, (
            f"suspects must not mutate the connection (total_changes "
            f"{changes_mid} → {con.total_changes})"
        )
        assert _db_content_identity(con) == db_mid, (
            "suspects must leave complete ordered database content identity unchanged"
        )
        assert _tree_inventory(root) == tree_mid, (
            "suspects must not alter the test archive tree"
        )


def test_r13_fixture_is_genuine_policy_error_not_import_gap():
    """Meta-pin: contracts fail on semantics, not missing symbols."""
    assert hasattr(verifier, "reverify") and callable(verifier.reverify)
    assert hasattr(verifier, "suspects") and callable(verifier.suspects)
    assert issubclass(archive_manifest.ArchivePolicyError, Exception)
    con = _mem()
    repo, archived = _seed_unsupported(con)
    assert len(archived) == 3
    err = _require_policy_error(con, repo)
    assert isinstance(err, archive_manifest.ArchivePolicyError)
    batch = archive_manifest.inspect_manifests_for_repos(
        con, [repo], archive_manifest.recovery_policy()
    )
    assert repo in batch.errors
    r = verifier.reverify(con, repo, deep=False)
    assert "record_ok" in r and "status" in r and "ok" in r
