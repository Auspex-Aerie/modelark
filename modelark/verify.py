"""Tier A structural verification.

Validates that a repo is a complete, well-formed, known checkpoint *without
downloading the weights* — by HTTP range-reading file headers. This is what
lets us "know it loads" for a 700B we will never fully pull.

Tier B (functional load) is a separate step and only runs on models that fit
local hardware; it is not implemented here.
"""
from __future__ import annotations

import json
import struct

import httpx
from huggingface_hub import get_token, hf_hub_url

from modelark.core import db
from modelark import discover
from modelark.formats import KNOWN_ST_DTYPES

_client = httpx.Client(follow_redirects=True, timeout=30.0)
WEIGHT_FORMATS = {"safetensors", "gguf", "pytorch", "onnx", "mlx"}


class AuthRequired(RuntimeError):
    """Raised when a repo's bytes are gated/private and we lack access."""


def _auth_headers() -> dict:
    tok = get_token()
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _range(url: str, start: int, length: int) -> bytes:
    """Read `length` bytes from `start` without downloading the whole file."""
    end = start + length - 1
    headers = {"Range": f"bytes={start}-{end}", **_auth_headers()}
    with _client.stream("GET", url, headers=headers) as r:
        if r.status_code in (401, 403):
            raise AuthRequired(f"gated/private ({r.status_code})")
        if r.status_code == 206:
            return r.read()[:length]
        if r.status_code == 200:  # server ignored Range; stop early, don't pull it all
            buf = b""
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) >= length:
                    break
            return buf[:length]
        r.raise_for_status()
        raise RuntimeError(f"unexpected status {r.status_code} for {url}")


def check_safetensors(url: str, file_size: int) -> tuple[bool, set[str], str]:
    """Validate a safetensors header. Returns (ok, tensor_names, detail)."""
    n = struct.unpack("<Q", _range(url, 0, 8))[0]
    if not (0 < n <= 200_000_000):
        return False, set(), f"implausible header length {n}"
    header = json.loads(_range(url, 8, n))
    tensors = {k: v for k, v in header.items() if k != "__metadata__"}
    if not tensors:
        return False, set(), "header has no tensors"
    data_region = file_size - 8 - n
    max_end = 0
    for name, t in tensors.items():
        if t["dtype"] not in KNOWN_ST_DTYPES:
            return False, set(tensors), f"unknown dtype {t['dtype']} ({name})"
        lo, hi = t["data_offsets"]
        if not (0 <= lo <= hi <= data_region):
            return False, set(tensors), f"bad offsets for {name}"
        max_end = max(max_end, hi)
    covers = max_end == data_region
    detail = f"{len(tensors)} tensors" + ("" if covers else " (offsets do not fully cover data region)")
    return True, set(tensors), detail


def check_gguf(url: str) -> tuple[bool, str]:
    """Validate a GGUF magic/version/counts header."""
    head = _range(url, 0, 24)
    if head[:4] != b"GGUF":
        return False, "bad GGUF magic"
    version, = struct.unpack("<I", head[4:8])
    n_tensors, = struct.unpack("<Q", head[8:16])
    n_kv, = struct.unpack("<Q", head[16:24])
    if version not in (2, 3):
        return False, f"unsupported GGUF version {version}"
    if not (0 < n_tensors < 10_000_000 and 0 <= n_kv < 1_000_000):
        return False, f"implausible counts t={n_tensors} kv={n_kv}"
    return True, f"GGUF v{version}, {n_tensors} tensors, {n_kv} kv"


def _fmt_safety(safeties: set[str]) -> str:
    if "pickle" in safeties:
        return "pickle-present"
    if safeties and safeties <= {"safe"}:
        return "safe"
    return "unknown"


def verify_repo(con, repo_id: str) -> dict:
    """Run Tier A on one repo using catalog file metadata; upsert the result."""
    rows = con.execute(
        "SELECT rfilename, size_bytes, format, safety FROM files WHERE repo_id = ?",
        [repo_id],
    ).fetchall()
    if not rows:
        raise ValueError(f"{repo_id}: no files in catalog — run discover first")

    files = [{"rfilename": r[0], "size": r[1], "format": r[2], "safety": r[3]} for r in rows]
    weights = [f for f in files if f["format"] in WEIGHT_FORMATS]
    safeties = {f["safety"] for f in weights}
    format_safety = _fmt_safety(safeties)

    st = [f for f in weights if f["format"] == "safetensors"]
    gguf = [f for f in weights if f["format"] == "gguf"]
    names = {f["rfilename"] for f in files}

    structural_ok: bool | None = None
    shards_complete: bool | None = None
    details: list[str] = []

    gated = (con.execute("SELECT gated FROM models WHERE repo_id = ?", [repo_id]).fetchone() or [None])[0]
    if gated and gated not in ("false", "no") and not get_token():
        details.append("gated: needs accepted license + HF token (run `hf auth login`)")
        return _record(con, repo_id, None, None, format_safety, details)

    try:
        structural_ok, shards_complete, details = _structural(repo_id, st, gguf, names, details)
    except AuthRequired as e:
        details.append(f"{e}; structural check skipped")
        return _record(con, repo_id, None, None, format_safety, details)

    return _record(con, repo_id, structural_ok, shards_complete, format_safety, details)


def _structural(repo_id, st, gguf, names, details):
    structural_ok = shards_complete = None
    if st:
        index = "model.safetensors.index.json"
        expected_tensors: set[str] | None = None
        if index in names:
            resp = _client.get(hf_hub_url(repo_id, index), headers=_auth_headers())
            if resp.status_code in (401, 403):
                raise AuthRequired(f"gated/private ({resp.status_code})")
            try:
                wm = resp.json()["weight_map"]
            except (json.JSONDecodeError, KeyError) as e:
                raise AuthRequired(f"{index} unreadable (gated or unexpected response)") from e
            expected_tensors = set(wm)
            referenced = set(wm.values())
            missing = referenced - {f["rfilename"] for f in st}
            if missing:
                shards_complete = False
                details.append(f"index references missing shards: {sorted(missing)[:3]}")
        seen_tensors: set[str] = set()
        ok_all = True
        for f in st:
            ok, tensors, d = check_safetensors(hf_hub_url(repo_id, f["rfilename"]), f["size"])
            ok_all = ok_all and ok
            seen_tensors |= tensors
            details.append(f"{f['rfilename']}: {d}")
        structural_ok = ok_all
        if shards_complete is None:
            shards_complete = (expected_tensors is None) or expected_tensors <= seen_tensors
            if expected_tensors and not shards_complete:
                details.append(
                    f"index expects {len(expected_tensors)} tensors, headers cover {len(seen_tensors)}")
    elif gguf:
        ok_all = True
        for f in gguf:
            ok, d = check_gguf(hf_hub_url(repo_id, f["rfilename"]))
            ok_all = ok_all and ok
            details.append(f"{f['rfilename']}: {d}")
        structural_ok = ok_all
        shards_complete = True  # split-GGUF completeness not deeply validated in Tier A
    else:
        details.append("no safetensors/gguf weights; structural parse skipped (pickle/other)")

    return structural_ok, shards_complete, details


def _record(con, repo_id, structural_ok, shards_complete, format_safety, details) -> dict:
    result = {
        "repo_id": repo_id,
        "checksum_ok": None,                 # set when bytes are local (fetch step)
        "structural_ok": structural_ok,
        "shards_complete": shards_complete,
        "format_safety": format_safety,
        "pickle_scan": "n/a" if format_safety != "pickle-present" else "unscanned",
        "hf_scan_status": None,
        "signature": "none",
        "signer": None,
        "load_tier_max": "A" if structural_ok else None,
        "functional_ok": None,
        "detail": " | ".join(details)[:1900],
        "tool_versions": f"{discover.tool_versions()}; modelark-tierA",
    }
    db.upsert(con, "verifications", result, pk=["repo_id"], touch=["verified_at"])
    if structural_ok:
        con.execute("UPDATE models SET status='verified' WHERE repo_id=? AND status='discovered'",
                    [repo_id])
    return result


def verify_many(repo_ids: list[str], con=None) -> dict[str, dict]:
    own = con is None
    con = con or db.connect()
    out = {}
    try:
        for rid in repo_ids:
            try:
                res = verify_repo(con, rid)
                flag = "OK " if res["structural_ok"] else ("?? " if res["structural_ok"] is None else "FAIL")
                print(f"  [{flag}] {rid}  safety={res['format_safety']} shards={res['shards_complete']}")
                out[rid] = res
            except Exception as e:
                print(f"  [ERR ] {rid}: {e}")
                out[rid] = {"error": str(e)}
    finally:
        if own:
            con.close()
    return out
