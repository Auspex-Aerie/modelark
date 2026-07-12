"""Read GGUF header metadata (e.g. general.architecture) via an HTTP range read.

GGUF repos have no config.json, but the GGUF file embeds its architecture in the
key-value header at the very start — so a tiny range read classifies them.
"""
from __future__ import annotations

import struct

import httpx
from huggingface_hub import hf_hub_url

_client = httpx.Client(follow_redirects=True, timeout=30.0)
_HEADER_BYTES = 1 << 18  # 256 KiB — general.* KVs live at the very start
# GGUF value-type -> fixed byte size (u8 i8 u16 i16 u32 i32 f32 bool u64 i64 f64)
_FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def _read_head(url: str, n: int) -> bytes:
    with _client.stream("GET", url, headers={"Range": f"bytes=0-{n - 1}"}) as r:
        if r.status_code in (200, 206):
            return r.read()[:n]
        r.raise_for_status()
        return b""


def architecture(repo_id: str, filename: str) -> str | None:
    """Return `general.architecture` from a GGUF file's header, or None."""
    try:
        buf = _read_head(hf_hub_url(repo_id, filename), _HEADER_BYTES)
    except Exception:
        return None
    if buf[:4] != b"GGUF":
        return None
    try:
        off = 8  # magic(4) + version(4)
        struct.unpack_from("<Q", buf, off)  # n_tensors
        off += 8
        n_kv, = struct.unpack_from("<Q", buf, off)
        off += 8

        def rstr() -> str:
            nonlocal off
            ln, = struct.unpack_from("<Q", buf, off)
            off += 8
            s = buf[off:off + ln].decode("utf-8", "replace")
            off += ln
            return s

        for _ in range(n_kv):
            key = rstr()
            vtype, = struct.unpack_from("<I", buf, off)
            off += 4
            if vtype == 8:  # string value
                val = rstr()
                if key == "general.architecture":
                    return val
            elif vtype == 9:  # array
                atype, = struct.unpack_from("<I", buf, off)
                off += 4
                alen, = struct.unpack_from("<Q", buf, off)
                off += 8
                if atype == 8:
                    for _ in range(alen):
                        sl, = struct.unpack_from("<Q", buf, off)
                        off += 8 + sl
                else:
                    off += alen * _FIXED.get(atype, 4)
            else:
                off += _FIXED.get(vtype, 4)
    except (struct.error, IndexError):
        return None
    return None
