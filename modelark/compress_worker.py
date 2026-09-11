"""Isolated compress+canary child process (DEC-023 stage 3 / INC-005).

The compressor's native core (ZipNN) can double-free and SIGABRT on certain shards' data — INC-005:
DeepSeek-R1-Distill-Qwen-32B shard 3, deterministic and threads-independent. A native abort cannot
be caught in-process; it takes the whole interpreter (and, in-process, the portal) down. So the
fetch pipeline runs compression HERE, in a short-lived child: if this process aborts, only the child
dies and the parent (`fetch._compress_isolated`) falls back to storing that shard raw and moves on.

Protocol — one-shot, no persistent state:
    argv[1] = a JSON request {src, dst, dtype, codec, threads, expected_sha256,
                             expected_bytes, decode_policy, parent_pid, result}
On a clean run this writes a JSON result to the `result` path and exits 0:
    {"ok": true,  "znn_path": ..., "znn_sha256": ..., "stored_bytes": ...}   canary passed
    {"ok": false}                                                            canary FAILED (.znn removed)
    {"ok": false, "over_cap": true, "detail": ...}                          safe raw fallback
    {"ok": false, "resource_refused"|"decode_refused": true, "detail": ...}   safe raw fallback
A native crash exits via a signal WITHOUT writing the result; the parent reads the negative return
code and treats it as a crash. The result travels by file, never by stdout — the compressor
libraries may write to stdout and would corrupt a stdout-based channel.
Fresh isolated no-site startup installs the shared memory/parent-death guards
before dependency hooks and native imports. Canary reuses the shared decoder
inside this same guarded process; standalone/restore callers are unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

def run(request: dict, policy) -> dict:
    """Compress src -> dst and canary-verify, entirely in this process. A native abort in either the
    compress or the canary decompress kills the process before this returns."""
    from modelark import compress
    from modelark.artifact_io import DecodeError
    from modelark.codec_inprocess import require_guard, verify_original
    from modelark.codec_resources import CodecResourceRefusal

    require_guard(policy)
    src = Path(request["src"])
    dst = Path(request["dst"])
    dtype = request["dtype"]
    codec = request["codec"]
    threads = int(request["threads"])
    expected_sha256 = request["expected_sha256"]

    try:
        znn = compress.compress_file(src, dst, dtype=dtype, codec=codec, threads=threads)
        if verify_original(znn, expected_sha256, request["expected_bytes"], policy):
            return {"ok": True, "znn_path": str(znn),
                    "znn_sha256": compress.sha256_file(znn), "stored_bytes": znn.stat().st_size}
    except compress.OutputCapExceeded as exc:
        dst.unlink(missing_ok=True)
        return {"ok": False, "over_cap": True, "detail": str(exc)}
    except (CodecResourceRefusal, MemoryError) as exc:
        dst.unlink(missing_ok=True)
        return {"ok": False, "resource_refused": True, "detail": str(exc) or "worker allocation refused"}
    except DecodeError as exc:
        dst.unlink(missing_ok=True)
        if exc.kind in {"RESOURCE", "LIMIT", "UNSUPPORTED"}:
            return {"ok": False, "decode_refused": True, "detail": str(exc)}
        return {"ok": False, "detail": str(exc)}
    dst.unlink(missing_ok=True)                 # round-trip did not certify → never keep the compressed file
    return {"ok": False}


def main(argv: list[str]) -> int:
    from modelark.artifact_policy import DecodePolicy
    from modelark.codec_process import initialize_worker, runtime_record, validate_runtime
    from modelark.codec_resources import CodecResourceRefusal

    request = json.loads(argv[1])
    policy = DecodePolicy.from_record(request["decode_policy"])
    try:
        initialize_worker(policy.memory, request["parent_pid"])
        result = run(request, policy)           # native abort: no result, parent's raw fallback
        if result["ok"]:
            result["worker"] = validate_runtime(runtime_record(), policy.memory)
    except (CodecResourceRefusal, MemoryError) as exc:
        Path(request["dst"]).unlink(missing_ok=True)
        result = {"ok": False, "resource_refused": True,
                  "detail": str(exc) or "worker allocation refused"}
    Path(request["result"]).write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
