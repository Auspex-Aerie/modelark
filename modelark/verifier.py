"""DEF-021 Verifier — re-verify ARCHIVED copies + auto-surface disruption 'suspects'.

Distinct from `verify.py` (Tier-A SOURCE checks against HF headers). This verifies what actually landed
ON THE DRIVE:
  • suspects(con)          — models whose archiving overlapped a disruption boundary, auto-surfaced as
                             re-verify candidates: a compressor raw-fallback (INC-005), an interrupted
                             partial copy, or an archive that landed within a window of a recorded
                             disruption event (fetch_events: awaiting-drive / compress-fallback / error).
  • reverify(con, repo)    — RECORD consistency always (offline-safe: all planned files archived, each
                             stored orig_sha256 matches the catalog) + a decompress-CANARY spot-check of
                             each stored blob when its drive is mounted (the DIS-002 proof, on demand).

The write-time canary (DEC-003/019) proves each shard at store time; this is the cheaper, on-demand
re-check for exactly the shards a disruption (restart / crash / RO flip / raw-fallback) put in doubt.
"""
from __future__ import annotations

from modelark import compress, fetch, register

_DISRUPTION_OUTCOMES = ("awaiting-drive", "compress-fallback", "error")
_WINDOW_MIN = 15                    # an archive within ±this many minutes of a disruption is a suspect
_FLOAT_SQL = ("bf16", "bfloat16", "fp16", "f16", "float16", "fp32", "f32", "float32")


def suspects(con) -> list[dict]:
    """Re-verify candidates, newest archive first. Each: {repo, drives, reasons, verified_at}."""
    acc: dict[str, dict] = {}

    def add(repo, drive, reason, va=None):
        r = acc.setdefault(repo, {"drives": set(), "reasons": set(), "verified_at": None})
        if drive:
            r["drives"].add(drive)
        r["reasons"].add(reason)
        if va and (r["verified_at"] is None or va > r["verified_at"]):
            r["verified_at"] = va

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

    # 2. partial copy: a drive holds FEWER than the repo's planned files (interrupted mid-copy)
    for repo, drive in con.execute(
        "WITH hasst AS (SELECT repo_id, max(CASE WHEN format='safetensors' THEN 1 ELSE 0 END) s "
        "               FROM files GROUP BY repo_id), "
        "planned AS (SELECT f.repo_id, count(*) n FROM files f JOIN hasst h USING(repo_id) "
        "            WHERE f.format IN ('safetensors','aux') OR (f.format='gguf' AND h.s=0) GROUP BY f.repo_id), "
        "perdrive AS (SELECT repo_id, drive_label, count(*) n FROM archived GROUP BY repo_id, drive_label) "
        "SELECT pd.repo_id, pd.drive_label FROM perdrive pd JOIN planned pl "
        "  ON pd.repo_id=pl.repo_id WHERE pd.n < pl.n"
    ).fetchall():
        add(repo, drive, "partial copy (interrupted)")

    # 3. disruption-window: an archive that landed within ±window of the repo's own disruption event
    q = ",".join(f"'{o}'" for o in _DISRUPTION_OUTCOMES)
    for repo, drive, va in con.execute(
        "SELECT DISTINCT a.repo_id, a.drive_label, a.verified_at FROM archived a JOIN fetch_events e "
        f"ON a.repo_id=e.repo_id WHERE e.outcome IN ({q}) "
        "AND abs(julianday(a.verified_at)-julianday(e.event_at))*1440 <= ?", [_WINDOW_MIN]
    ).fetchall():
        add(repo, drive, "archived near a disruption event", va)

    out = [{"repo": r, "drives": sorted(v["drives"]), "reasons": sorted(v["reasons"]),
            "verified_at": v["verified_at"]} for r, v in acc.items()]
    out.sort(key=lambda x: (x["verified_at"] or ""), reverse=True)
    return out


def reverify(con, repo_id: str, deep: bool = True) -> dict:
    """Re-verify one archived model. RECORD consistency is offline-safe; the decompress-canary runs only
    for stored blobs whose drive is mounted (skipped, not failed, when a drive is shelved)."""
    planned_names = {f["rfilename"] for f in fetch.plan(con, repo_id)}
    catalog_sha = dict(con.execute("SELECT rfilename, sha256 FROM files WHERE repo_id=?", [repo_id]).fetchall())
    by_name: dict[str, list[dict]] = {}
    for rf, sn, dl, osha, comp in con.execute(
            "SELECT rfilename, stored_name, drive_label, orig_sha256, compressed "
            "FROM archived WHERE repo_id=?", [repo_id]).fetchall():
        by_name.setdefault(rf, []).append({"stored_name": sn, "drive": dl, "orig": osha, "compressed": bool(comp)})

    if not by_name:
        return {"repo": repo_id, "archived": False, "ok": False, "detail": "not archived — nothing to re-verify"}

    missing = sorted(planned_names - set(by_name))                    # planned but never archived
    sha_mismatch = [rf for rf, cs in by_name.items()                 # stored HF-hash disagrees with the catalog
                    if catalog_sha.get(rf) and cs[0]["orig"] and catalog_sha[rf] != cs[0]["orig"]]
    record_ok = not missing and not sha_mismatch

    deep_checks, deep_ran = [], False
    if deep:
        for rf, copies in by_name.items():
            for c in copies:
                path = register.archive_path(con, c["drive"])         # None → drive shelved → skip (not fail)
                if path is None:
                    continue
                stored = path / repo_id / c["stored_name"]
                if not stored.exists():
                    continue
                deep_ran = True
                try:
                    ok = (compress.canary_ok(stored, c["orig"]) if c["compressed"]
                          else compress.sha256_file(stored) == c["orig"])
                    deep_checks.append({"file": rf, "drive": c["drive"], "ok": bool(ok)})
                except Exception as e:                                # a decompress error IS a failed check
                    deep_checks.append({"file": rf, "drive": c["drive"], "ok": False, "err": str(e)[:80]})
                break                                                 # one healthy copy per file is enough
    deep_ok = all(d["ok"] for d in deep_checks) if deep_checks else None

    parts = [f"{len(by_name)} file(s) archived"]
    if missing:
        parts.append(f"{len(missing)} planned file(s) MISSING")
    if sha_mismatch:
        parts.append(f"{len(sha_mismatch)} sha256 MISMATCH")
    parts.append("canary " + ("PASS" if deep_ok else "FAIL" if deep_ok is False else "skipped (drive not mounted)"))
    return {"repo": repo_id, "archived": True, "record_ok": record_ok,
            "missing": missing, "sha_mismatch": sha_mismatch,
            "n_files": len(by_name), "deep_ran": deep_ran, "deep_ok": deep_ok,
            "deep_checks": deep_checks[:24], "ok": record_ok and deep_ok is not False,
            "detail": " · ".join(parts)}


def reverify_many(con, repo_ids: list[str], deep: bool = True) -> list[dict]:
    return [reverify(con, r, deep) for r in repo_ids]
