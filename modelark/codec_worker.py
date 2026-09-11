"""One-shot, read-only ZipNN child; no archive paths or publication authority.

Internal protocol v2: success is bounded runtime metadata followed by original
bytes, each framed by status + uint64 length. Failure is one error frame.
stdout/stderr are only diagnostics; dependency hooks run after the guards.
"""
from __future__ import annotations

import json
import os
import sys

from modelark.codec_resources import CodecMemoryPolicy, CodecResourceRefusal
from modelark.codec_process import (
    WorkerInitializationError, initialize_worker, runtime_record, validate_runtime,
)
from modelark import streamznn

IO_BYTES = 64 << 10
ERROR_BYTES = 4096
METADATA_BYTES = 8192


class _OutputFailed(Exception):
    """A packet may be partial; no subsequent error frame can be sent safely."""


def _packet(fd, data):
    try:
        _send(fd, data)
    except Exception as exc:
        raise _OutputFailed from exc


def _send(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view[:IO_BYTES])
        if count <= 0:
            raise BrokenPipeError("codec output closed")
        view = view[count:]


def _policy(request):
    if type(request) is dict and request.get("version") == "modelark.codec-worker.v3-legacy":
        if (set(request) != {"version", "policy", "stored_bytes", "dtype"}
                or type(request["stored_bytes"]) is not int or request["stored_bytes"] < 0
                or type(request["dtype"]) is not str or not 0 < len(request["dtype"]) <= 128):
            raise ValueError("invalid legacy worker request")
        return CodecMemoryPolicy.from_record(request["policy"])
    if type(request) is not dict or set(request) != {
            "version", "policy", "stored_bytes", "original_bytes"}:
        raise ValueError("invalid worker request fields")
    if request["version"] != "modelark.codec-worker.v2":
        raise ValueError("unknown worker protocol")
    for key in ("stored_bytes", "original_bytes"):
        if type(request[key]) is not int or request[key] <= 0:
            raise ValueError("invalid worker frame size")
    return CodecMemoryPolicy.from_record(request["policy"])


def run(request, source):
    """Called only after guarded initialization by the process entrypoint."""
    _policy(request)

    def decode(blob):
        # Do not start native work until the parent has closed the input pipe.
        if source.read(1):
            raise streamznn.StreamZnnError("trailing worker input")
        from zipnn import ZipNN
        return ZipNN(input_format="byte", threads=1).decompress(blob)

    restored = streamznn.read_zipnn_frame(
        source, max_stored_bytes=request["stored_bytes"],
        max_decoded_bytes=request["original_bytes"],
        remaining_bytes=request["original_bytes"], stored_length=request["stored_bytes"],
        read_size=IO_BYTES, decode_frame=decode)
    if len(restored) != request["original_bytes"]:
        raise streamznn.StreamZnnError("worker decoded length differs from request")
    return restored


def _legacy(request, source, output, policy):
    from modelark.codec_legacy import chunks
    from modelark.artifact_policy import qualified_policy
    decode_policy = qualified_policy()
    if policy != decode_policy.memory:
        raise CodecResourceRefusal("legacy worker requires the shared qualified policy")
    metadata = json.dumps(validate_runtime(runtime_record(), policy), separators=(",", ":")).encode()
    if not 0 < len(metadata) <= METADATA_BYTES:
        raise ValueError("oversize worker runtime evidence")
    _packet(output, b"M" + len(metadata).to_bytes(8, "little") + metadata)
    total = 0

    class Counted:
        remaining = request["stored_bytes"]

        def read(self, size):
            data = source.read(min(size, IO_BYTES, self.remaining))
            if not data and self.remaining:
                raise streamznn.StreamZnnError("truncated legacy worker input")
            self.remaining -= len(data)
            return data

    counted = Counted()
    for data in chunks(counted, request["dtype"], decode_policy):
        for offset in range(0, len(data), IO_BYTES):
            piece = data[offset:offset + IO_BYTES]
            _packet(output, b"D" + len(piece).to_bytes(8, "little") + piece)
            total += len(piece)
    if counted.remaining or source.read(1):
        raise streamznn.StreamZnnError("legacy worker did not consume exact input")
    _packet(output, b"E" + total.to_bytes(8, "little"))


def main(argv):
    output, parent_pid = int(argv[1]), int(argv[2])
    try:
        if len(argv[3]) > ERROR_BYTES:
            raise ValueError("oversize worker request")
        request = json.loads(argv[3])
        policy = _policy(request)
        initialize_worker(policy, parent_pid)
        if request["version"] == "modelark.codec-worker.v3-legacy":
            _legacy(request, sys.stdin.buffer, output, policy)
            return 0
        restored = run(request, sys.stdin.buffer)
        metadata = json.dumps(validate_runtime(runtime_record(), policy),
                              separators=(",", ":")).encode("utf-8")
        if not 0 < len(metadata) <= METADATA_BYTES:
            raise ValueError("oversize worker runtime evidence")
    except _OutputFailed:
        return 1
    except WorkerInitializationError:
        status = b"U" if request.get("version") == "modelark.codec-worker.v3-legacy" else b"I"
        detail = b"codec worker initialization failed"
    except (CodecResourceRefusal, MemoryError):
        status, detail = b"R", b"codec memory guard refused work"
    except ImportError:
        status, detail = b"U", b"required codec dependency is unavailable"
    except streamznn.DecodeLimitExceeded:
        status, detail = b"L", b"codec frame exceeds bound"
    except streamznn.UnsupportedFrame:
        status, detail = b"U", b"unsupported codec frame"
    except Exception:
        # No paths or arbitrary exception contents are sent across this boundary.
        status, detail = b"I", b"codec worker refused frame"
    else:
        # Success is metadata then bytes. Once either emission begins,
        # any output failure is represented only by an unsuccessful process exit;
        # never append an error frame where the parent expects original bytes.
        try:
            _send(output, b"M" + len(metadata).to_bytes(8, "little"))
            _send(output, metadata)
            _send(output, b"O" + len(restored).to_bytes(8, "little"))
            _send(output, restored)
            return 0
        except Exception:
            return 1
    try:
        _send(output, status + len(detail).to_bytes(8, "little") + detail)
    except OSError:
        pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
