"""DEF-021 Verifier — re-verify ARCHIVED copies + auto-surface disruption 'suspects'.

Distinct from `verify.py` (Tier-A SOURCE checks against HF headers). This verifies what actually landed
ON THE DRIVE:
  • suspects(con)          — models whose archiving overlapped a disruption boundary, auto-surfaced as
                             re-verify candidates: a compressor raw-fallback (INC-005), an interrupted
                             partial copy, or an archive that landed within a window of a recorded
                             disruption event (fetch_events: awaiting-drive / compress-fallback / error).
  • reverify(con, repo)    — RECORD consistency always (offline-safe: all planned files archived and
                             Hub hashes agree when supplied) + a decompress-CANARY spot-check of
                             each stored blob when its drive is mounted (the DIS-002 proof, on demand).

The write-time canary (DEC-003/019) proves each shard at store time; this is the cheaper, on-demand
re-check for exactly the shards a disruption (restart / crash / RO flip / raw-fallback) put in doubt.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from modelark import archive_hash, archive_manifest, compress, register

_DISRUPTION_OUTCOMES = ("awaiting-drive", "compress-fallback", "error")
_WINDOW_MIN = 15                    # an archive within ±this many minutes of a disruption is a suspect
_FLOAT_SQL = ("bf16", "bfloat16", "fp16", "f16", "float16", "fp32", "f32", "float32")


def _stored_relpath(rfilename: str, stored_name: str | None, stored_relpath: str | None) -> PurePosixPath:
    """Canonical path below <archive>/<repo>; infer it for a pre-migration record when needed."""
    if stored_relpath:
        value = stored_relpath
    elif stored_name:
        value = str(PurePosixPath(rfilename).parent / stored_name)
    else:
        raise ValueError("unsafe stored path: both stored_relpath and stored_name are empty")
    rel = PurePosixPath(value)
    if not rel.parts or value != rel.as_posix() or rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe stored path {value!r}")
    return rel


def suspects(con) -> list[dict]:
    """Typed operator follow-ups: archive-integrity suspects and unresolved gated access."""
    acc: dict[str, dict] = {}

    def add(repo, drive, reason, va=None, *, kind="integrity", url=None, followup_at=None):
        r = acc.setdefault(repo, {
            "drives": set(), "reasons": set(), "types": set(), "verified_at": None,
            "followup_at": None, "access_url": None, "sort_at": None,
        })
        if drive:
            r["drives"].add(drive)
        r["reasons"].add(reason)
        r["types"].add(kind)
        if va and (r["verified_at"] is None or va > r["verified_at"]):
            r["verified_at"] = va
        if followup_at and (r["followup_at"] is None or followup_at > r["followup_at"]):
            r["followup_at"] = followup_at
        if url:
            r["access_url"] = url
        stamp = followup_at or va
        if stamp and (r["sort_at"] is None or stamp > r["sort_at"]):
            r["sort_at"] = stamp

    floats = ",".join(f"'{q}'" for q in _FLOAT_SQL)
    # 1. a float safetensors stored UNCOMPRESSED — a compressor crash/hang (INC-005) OR the DEC-022
    #    over-budget CODEC_RAW path. Either way it never got the round-trip canary a compress would run,
    #    so it's a re-verify candidate. (Slightly broad: a tiny float shard skipped by should_compress
    #    would also match, but re-verifying it just passes — a harmless extra check.)
    for repo, drive, va in con.execute(
        "SELECT DISTINCT a.repo_id, a.drive_label, a.verified_at FROM archived a "
        "JOIN files f ON a.repo_id=f.repo_id AND a.rfilename=f.rfilename "
        f"WHERE a.compressed=0 AND f.format='safetensors' AND (f.quant IS NULL OR lower(f.quant) IN ({floats}))"
    ).fetchall():
        add(repo, drive, "float weights stored raw (compress fallback / over-budget)", va)

    # 2. partial copy: compare exact canonical filename sets. Counts are unsafe because an extra
    # archived filename can otherwise hide a missing required one, and old SQL omitted pickle.
    archived_rows = con.execute(
        "SELECT repo_id,drive_label,rfilename FROM archived ORDER BY repo_id,drive_label,rfilename"
    ).fetchall()
    repos = sorted({row[0] for row in archived_rows})
    manifest_batch = archive_manifest.inspect_manifests_for_repos(
        con, repos, archive_manifest.recovery_policy()
    )
    present: dict[tuple[str, str], set[str]] = {}
    archived_names: dict[str, set[str]] = {}
    for repo, drive, rfilename in archived_rows:
        present.setdefault((repo, drive), set()).add(rfilename)
        archived_names.setdefault(repo, set()).add(rfilename)
    for (repo, drive), names in present.items():
        manifest = manifest_batch.manifests.get(repo)
        required = ({item.rfilename for item in manifest} if manifest is not None
                    else archived_names.get(repo, set()))
        if not required <= names:
            add(repo, drive, "partial copy (interrupted)")

    # 3. disruption-window: an archive that landed within ±window of the repo's own disruption event
    q = ",".join(f"'{o}'" for o in _DISRUPTION_OUTCOMES)
    for repo, drive, va in con.execute(
        "SELECT DISTINCT a.repo_id, a.drive_label, a.verified_at FROM archived a JOIN fetch_events e "
        f"ON a.repo_id=e.repo_id WHERE e.outcome IN ({q}) "
        "AND abs(julianday(a.verified_at)-julianday(e.event_at))*1440 <= ?", [_WINDOW_MIN]
    ).fetchall():
        add(repo, drive, "archived near a disruption event", va)

    # 4. A gated repository explicitly skipped (or left unanswered for five minutes) is not an
    # archive-integrity concern: no bytes landed. It still belongs in the operator follow-up queue,
    # typed separately, until a later successful archive event proves access was resolved.
    latest_access: dict[str, tuple[int, str, dict]] = {}
    for rowid, repo, event_at, detail in con.execute(
        "SELECT rowid,repo_id,event_at,detail FROM fetch_events "
        "WHERE outcome='auth' AND repo_id IS NOT NULL ORDER BY rowid"
    ).fetchall():
        try:
            payload = json.loads(detail or "")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("type") == "access-gated":
            latest_access[repo] = (rowid, event_at, payload)
    for repo, (rowid, event_at, payload) in latest_access.items():
        resolved = con.execute(
            "SELECT 1 FROM fetch_events WHERE repo_id=? AND outcome='archived' AND rowid>? LIMIT 1",
            [repo, rowid],
        ).fetchone()
        if resolved:
            continue
        resolution = payload.get("resolution") or "deferred"
        add(
            repo, None, f"Hugging Face access required ({resolution})",
            kind="access-gated", url=payload.get("url"), followup_at=event_at,
        )

    out = [{
        "repo": r, "drives": sorted(v["drives"]), "reasons": sorted(v["reasons"]),
        "types": sorted(v["types"]), "verified_at": v["verified_at"],
        "followup_at": v["followup_at"], "access_url": v["access_url"],
        "_sort_at": v["sort_at"],
    } for r, v in acc.items()]
    out.sort(key=lambda x: (x["_sort_at"] or ""), reverse=True)
    for item in out:
        item.pop("_sort_at", None)
    return out


def reverify(con, repo_id: str, deep: bool = True) -> dict:
    """Re-verify one archived model. RECORD consistency is offline-safe; physical status remains
    unknown for stored blobs whose drive is shelved, and missing bytes on a mounted drive fail."""
    catalog_sha = dict(con.execute("SELECT rfilename, sha256 FROM files WHERE repo_id=?", [repo_id]).fetchall())
    by_name: dict[str, list[dict]] = {}
    for rf, sn, sr, dl, osha, comp, key in con.execute(
            "SELECT rfilename, stored_name, stored_relpath, drive_label, orig_sha256, compressed, annex_key "
            "FROM archived WHERE repo_id=?", [repo_id]).fetchall():
        by_name.setdefault(rf, []).append({"stored_name": sn, "stored_relpath": sr, "drive": dl,
                                           "orig": osha, "compressed": bool(comp), "annex_key": key})

    if not by_name:
        return {"repo": repo_id, "archived": False, "status": "not-archived", "ok": False,
                "detail": "not archived — nothing to re-verify"}

    # Verify bytes already accepted into the archive regardless of today's acquisition
    # policy. For legacy/foreign unsupported formats, the durable records are the manifest.
    try:
        planned_names = {
            item.rfilename
            for item in archive_manifest.manifest_for_repo(
                con, repo_id, archive_manifest.recovery_policy()
            )
        }
    except archive_manifest.ArchivePolicyError:
        planned_names = set(by_name)

    missing = sorted(planned_names - set(by_name))                    # planned but never archived
    sha_mismatch = [rf for rf, cs in by_name.items()                 # any stored HF-hash disagrees with catalog
                    if catalog_sha.get(rf) and any(c["orig"] and catalog_sha[rf] != c["orig"] for c in cs)]
    record_ok = not missing and not sha_mismatch

    required = max(1, int((con.execute("SELECT coalesce(numcopies,1) FROM models WHERE repo_id=?",
                                       [repo_id]).fetchone() or [1])[0]))
    deep_checks, deep_ran, offline = [], False, set()
    if deep:
        for rf, copies in by_name.items():
            for c in copies:
                path = register.archive_path(con, c["drive"])         # None → drive shelved → skip (not fail)
                if path is None:
                    offline.add(c["drive"])
                    continue
                deep_ran = True
                try:
                    rel = _stored_relpath(rf, c["stored_name"], c["stored_relpath"])
                except ValueError as e:
                    deep_checks.append({"file": rf, "drive": c["drive"], "ok": False, "err": str(e)[:120]})
                    continue
                stored = path / repo_id / Path(*rel.parts)
                if not stored.exists():
                    deep_checks.append({"file": rf, "drive": c["drive"], "ok": False,
                                        "err": "recorded blob is missing on mounted drive"})
                    continue
                try:
                    expected = archive_hash.expected_sha256(
                        catalog_sha=catalog_sha.get(rf), orig_sha256=c["orig"],
                        compressed=c["compressed"], annex_key=c["annex_key"],
                    )
                    if expected is None:
                        deep_checks.append({"file": rf, "drive": c["drive"], "ok": None,
                                            "err": "no original-byte or annex sha256 available"})
                    else:
                        ok = (compress.canary_ok(stored, expected) if c["compressed"]
                              else compress.sha256_file(stored) == expected)
                        deep_checks.append({"file": rf, "drive": c["drive"], "ok": bool(ok)})
                except Exception as e:                                # a decompress error IS a failed check
                    deep_checks.append({"file": rf, "drive": c["drive"], "ok": False, "err": str(e)[:80]})
    checked_fail = any(d["ok"] is False for d in deep_checks)
    unverifiable = any(d["ok"] is None for d in deep_checks)
    insufficient = []
    if deep:
        for rf in planned_names:
            healthy = sum(d["ok"] is True for d in deep_checks if d["file"] == rf)
            unknown_checked = sum(d["ok"] is None for d in deep_checks if d["file"] == rf)
            possible = healthy + unknown_checked + sum(c["drive"] in offline for c in by_name.get(rf, []))
            if possible < required:
                insufficient.append(rf)
    if not record_ok or checked_fail or insufficient:
        status = "failed"
    elif not deep or unverifiable or offline or any(
            sum(d["ok"] is True for d in deep_checks if d["file"] == rf) < required for rf in planned_names):
        status = "unknown"
    else:
        status = "verified"
    deep_ok = status == "verified" if deep else None

    parts = [f"{len(by_name)} file(s) archived"]
    if missing:
        parts.append(f"{len(missing)} planned file(s) MISSING")
    if sha_mismatch:
        parts.append(f"{len(sha_mismatch)} sha256 MISMATCH")
    if insufficient:
        parts.append(f"{len(insufficient)} file(s) below required {required} checked/possible copies")
    parts.append("physical " + ("PASS" if status == "verified" else "FAIL" if status == "failed" else "UNKNOWN"))
    return {"repo": repo_id, "archived": True, "record_ok": record_ok,
            "missing": missing, "sha_mismatch": sha_mismatch,
            "n_files": len(by_name), "required_copies": required, "status": status,
            "offline_drives": sorted(offline), "insufficient": sorted(insufficient),
            "deep_ran": deep_ran, "deep_ok": deep_ok,
            "deep_checks": deep_checks[:24], "ok": status == "verified",
            "detail": " · ".join(parts)}


def reverify_many(con, repo_ids: list[str], deep: bool = True) -> list[dict]:
    return [reverify(con, r, deep) for r in repo_ids]
