"""Tier A remote-header evidence.

Safetensors receives a strict layout check (dtype, shape byte length, non-overlapping
contiguous ranges, and shard-index agreement) through HTTP range reads. GGUF currently
receives only a magic/version/count sanity check; it is reported as header evidence, not
as a tensor-layout or loadability proof.

Tier B functional loading is planned but not implemented.
"""
from __future__ import annotations

import json
import math
import re
import struct

import httpx
from huggingface_hub import get_token, hf_hub_url

from modelark.core import db
from modelark import discover
from modelark.formats import DTYPE_BITS, KNOWN_ST_DTYPES

_client = httpx.Client(follow_redirects=True, timeout=30.0)
WEIGHT_FORMATS = {"safetensors", "gguf", "pytorch", "onnx", "mlx"}
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(?:\d+|\*)", re.IGNORECASE)


class AuthRequired(RuntimeError):
    """Raised when a repo's bytes are gated/private and we lack access."""


class _DuplicateJsonKey(ValueError):
    """A JSON object repeated a key that Python's default decoder would silently overwrite."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


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
            content_range = r.headers.get("Content-Range", "")
            match = _CONTENT_RANGE.fullmatch(content_range.strip())
            expected_end = start + length - 1
            if (match is None or int(match.group(1)) != start
                    or int(match.group(2)) != expected_end):
                raise RuntimeError(
                    f"unexpected Content-Range {content_range!r}; "
                    f"expected bytes {start}-{expected_end}"
                )
            return r.read()[:length]
        if r.status_code == 200:  # server ignored Range; stop early, don't pull it all
            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) >= start + length:
                    break
            return bytes(buf[start:start + length])
        r.raise_for_status()
        raise RuntimeError(f"unexpected status {r.status_code} for {url}")


def check_safetensors(url: str, file_size: int) -> tuple[bool, set[str], str]:
    """Validate a safetensors header. Returns (ok, tensor_names, detail)."""
    if not isinstance(file_size, int) or file_size < 9:
        return False, set(), f"invalid file size {file_size!r}"
    prefix = _range(url, 0, 8)
    if len(prefix) != 8:
        return False, set(), f"truncated length prefix ({len(prefix)}/8 bytes)"
    n = struct.unpack("<Q", prefix)[0]
    if not (0 < n <= 200_000_000):
        return False, set(), f"implausible header length {n}"
    if 8 + n > file_size:
        return False, set(), f"header length {n} exceeds file size {file_size}"
    encoded = _range(url, 8, n)
    if len(encoded) != n:
        return False, set(), f"truncated header ({len(encoded)}/{n} bytes)"
    try:
        header = json.loads(encoded, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeDecodeError, _DuplicateJsonKey) as exc:
        return False, set(), f"invalid header JSON: {exc}"
    if not isinstance(header, dict):
        return False, set(), "header JSON is not an object"
    metadata = header.get("__metadata__")
    if metadata is not None and (
            not isinstance(metadata, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in metadata.items())):
        return False, set(), "__metadata__ must contain only string keys and values"
    tensors = {k: v for k, v in header.items() if k != "__metadata__"}
    if not tensors:
        return False, set(), "header has no tensors"
    data_region = file_size - 8 - n
    ranges: list[tuple[int, int, str]] = []
    for name, t in tensors.items():
        if not isinstance(name, str) or not name or not isinstance(t, dict):
            return False, set(tensors), f"invalid tensor entry {name!r}"
        dtype = t.get("dtype")
        shape = t.get("shape")
        offsets = t.get("data_offsets")
        if dtype not in KNOWN_ST_DTYPES:
            return False, set(tensors), f"unknown dtype {dtype!r} ({name})"
        if (not isinstance(shape, list) or len(shape) > 64
                or any(type(dim) is not int or dim < 0 for dim in shape)):
            return False, set(tensors), f"invalid shape for {name}"
        if (not isinstance(offsets, list) or len(offsets) != 2
                or any(type(value) is not int for value in offsets)):
            return False, set(tensors), f"invalid data_offsets for {name}"
        lo, hi = offsets
        if not (0 <= lo <= hi <= data_region):
            return False, set(tensors), f"bad offsets for {name}"
        elements = math.prod(shape)
        expected_bytes = (elements * DTYPE_BITS[dtype] + 7) // 8
        if hi - lo != expected_bytes:
            return False, set(tensors), (
                f"shape/offset size mismatch for {name}: expected {expected_bytes}, got {hi - lo}"
            )
        ranges.append((lo, hi, name))

    cursor = 0
    for lo, hi, name in sorted(ranges):
        if lo != cursor:
            kind = "overlap" if lo < cursor else "gap"
            return False, set(tensors), f"tensor data {kind} before {name} at byte {lo}"
        cursor = hi
    if cursor != data_region:
        return False, set(tensors), (
            f"tensor ranges cover {cursor} of {data_region} data bytes"
        )
    detail = f"{len(tensors)} tensors; shapes and {data_region} data bytes fully accounted for"
    return True, set(tensors), detail


def check_gguf(url: str, file_size: int | None = None) -> tuple[bool, str]:
    """Validate only the fixed GGUF magic/version/count header (not its tensor table)."""
    if file_size is not None and (not isinstance(file_size, int) or file_size < 24):
        return False, f"invalid file size {file_size!r}"
    head = _range(url, 0, 24)
    if len(head) != 24:
        return False, f"truncated GGUF header ({len(head)}/24 bytes)"
    if head[:4] != b"GGUF":
        return False, "bad GGUF magic"
    version, = struct.unpack("<I", head[4:8])
    n_tensors, = struct.unpack("<Q", head[8:16])
    n_kv, = struct.unpack("<Q", head[16:24])
    if version not in (2, 3):
        return False, f"unsupported GGUF version {version}"
    if not (0 < n_tensors < 10_000_000 and 0 <= n_kv < 1_000_000):
        return False, f"implausible counts t={n_tensors} kv={n_kv}"
    return True, f"GGUF v{version}, {n_tensors} tensors, {n_kv} kv (fixed header only)"


_SPLIT_WEIGHT = re.compile(
    r"^(?P<stem>.+)-(?P<part>\d{5})-of-(?P<total>\d{5})\.(?P<ext>safetensors|gguf)$",
    re.IGNORECASE,
)


def _split_sequence_complete(filenames: set[str]) -> bool | None:
    """Prove standard split filenames are complete; return None for an ambiguous multi-file set."""
    if not filenames:
        return False
    matches = [_SPLIT_WEIGHT.fullmatch(filename) for filename in filenames]
    if any(match is None for match in matches):
        # One ordinary weight file is complete; mixed/non-standard multi-file layouts
        # carry no machine-readable completeness claim.
        return True if len(filenames) == 1 else None

    groups: dict[tuple[str, str, int], set[int]] = {}
    for match in matches:
        assert match is not None  # narrowed by the ambiguity check above
        total = int(match.group("total"))
        if total < 1:
            return False
        key = (match.group("stem"), match.group("ext").lower(), total)
        groups.setdefault(key, set()).add(int(match.group("part")))
    return all(parts == set(range(1, total + 1)) for (_, _, total), parts in groups.items())


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
        expected_map: dict[str, str] | None = None
        if index in names:
            resp = _client.get(hf_hub_url(repo_id, index), headers=_auth_headers())
            if resp.status_code in (401, 403):
                raise AuthRequired(f"gated/private ({resp.status_code})")
            try:
                resp.raise_for_status()
                payload = json.loads(resp.content, object_pairs_hook=_unique_object)
                expected_map = payload["weight_map"]
                if (not isinstance(expected_map, dict) or not expected_map
                        or any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
                               for k, v in expected_map.items())):
                    raise ValueError("weight_map must be a non-empty string map")
            except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError,
                    KeyError, TypeError, ValueError, _DuplicateJsonKey) as exc:
                details.append(f"{index}: invalid shard index ({exc})")
                expected_map = {}
            referenced = set(expected_map.values())
            missing = referenced - {f["rfilename"] for f in st}
            if missing:
                shards_complete = False
                details.append(f"index references missing shards: {sorted(missing)[:3]}")
        seen_locations: dict[str, str] = {}
        duplicate_tensors: set[str] = set()
        ok_all = True
        for f in st:
            ok, tensors, d = check_safetensors(hf_hub_url(repo_id, f["rfilename"]), f["size"])
            ok_all = ok_all and ok
            for tensor in tensors:
                if tensor in seen_locations:
                    duplicate_tensors.add(tensor)
                else:
                    seen_locations[tensor] = f["rfilename"]
            details.append(f"{f['rfilename']}: {d}")
        if duplicate_tensors:
            ok_all = False
            details.append(f"duplicate tensors across shards: {sorted(duplicate_tensors)[:3]}")
        structural_ok = ok_all
        if expected_map is not None:
            expected_tensors = set(expected_map)
            seen_tensors = set(seen_locations)
            wrong_locations = [
                tensor for tensor in expected_tensors & seen_tensors
                if expected_map[tensor] != seen_locations[tensor]
            ]
            mapping_complete = (
                bool(expected_map)
                and expected_tensors == seen_tensors
                and not wrong_locations
                and not duplicate_tensors
            )
            shards_complete = bool(shards_complete is not False and mapping_complete)
            if expected_tensors != seen_tensors:
                details.append(
                    f"index expects {len(expected_tensors)} tensors, headers cover {len(seen_tensors)}")
            if wrong_locations:
                details.append(f"index maps tensors to wrong shards: {sorted(wrong_locations)[:3]}")
        else:
            shards_complete = _split_sequence_complete({f["rfilename"] for f in st})
            if shards_complete is None:
                details.append("multiple safetensors files without an index; shard completeness unknown")
            elif shards_complete is False:
                details.append("split safetensors filename sequence is incomplete")
    elif gguf:
        ok_all = True
        for f in gguf:
            ok, d = check_gguf(hf_hub_url(repo_id, f["rfilename"]), f["size"])
            ok_all = ok_all and ok
            details.append(f"{f['rfilename']}: {d}")
        structural_ok = ok_all
        shards_complete = _split_sequence_complete({f["rfilename"] for f in gguf})
        if shards_complete is None:
            details.append("multiple GGUF files without a standard split sequence; completeness unknown")
        elif shards_complete is False:
            details.append("split GGUF filename sequence is incomplete")
        details.append("GGUF tensor metadata, offsets, shapes, and data bytes were not validated")
    else:
        details.append("no safetensors/gguf weights; structural parse skipped (pickle/other)")

    return structural_ok, shards_complete, details


def _record(con, repo_id, structural_ok, shards_complete, format_safety, details) -> dict:
    tier_a_passed = structural_ok is True and shards_complete is True
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
        "load_tier_max": "A" if tier_a_passed else None,
        "functional_ok": None,
        "detail": " | ".join(details)[:1900],
        "tool_versions": f"{discover.tool_versions()}; modelark-tierA",
    }
    db.upsert(con, "verifications", result, pk=["repo_id"], touch=["verified_at"])
    if tier_a_passed:
        con.execute("UPDATE models SET status='inspected' WHERE repo_id=? AND status='discovered'",
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
                if res["load_tier_max"] == "A":
                    flag = "OK "
                elif res["structural_ok"] is False or res["shards_complete"] is False:
                    flag = "FAIL"
                else:
                    flag = "?? "
                print(f"  [{flag}] {rid}  safety={res['format_safety']} shards={res['shards_complete']}")
                out[rid] = res
            except Exception as e:
                print(f"  [ERR ] {rid}: {e}")
                out[rid] = {"error": str(e)}
    finally:
        if own:
            con.close()
    return out
