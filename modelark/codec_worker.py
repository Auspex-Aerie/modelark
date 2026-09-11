"""One-shot, read-only ZipNN child; no archive paths or publication authority.

Internal protocol v1: stdin is exactly one frame followed by EOF; a dedicated
FD carries a one-byte status + uint64 length + payload. stdout/stderr are only
diagnostics. Limits and parent-death handling precede every native import.
"""
from __future__ import annotations

import ctypes
import json
import os
import signal
import sys

from modelark.codec_resources import CodecMemoryPolicy, CodecResourceRefusal
from modelark import streamznn

IO_BYTES = 64 << 10
ERROR_BYTES = 4096


class WorkerInitializationError(RuntimeError):
    """Parent-lifetime protection failed, distinct from memory admission."""


def bind_parent(parent_pid):
    """Linux kills the worker if its spawning thread dies, including mid-codec.

    Check after prctl to cover death between spawn and installing the signal.
    Linux PDEATHSIG follows the parent *thread*, not just the process; a caller
    must keep that thread alive for the entire context-managed decode.
    """
    if sys.platform != "linux":
        raise WorkerInitializationError("codec parent-death guard requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise WorkerInitializationError("could not install codec parent-death guard")
    if os.getppid() != parent_pid:
        raise WorkerInitializationError("codec parent exited before worker initialization")


def _send(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view[:IO_BYTES])
        if count <= 0:
            raise BrokenPipeError("codec output closed")
        view = view[count:]


def run(request, source):
    """The process entrypoint is the only production caller of this function."""
    if type(request) is not dict or set(request) != {
            "version", "policy", "stored_bytes", "original_bytes"}:
        raise ValueError("invalid worker request fields")
    if request["version"] != "modelark.codec-worker.v1":
        raise ValueError("unknown worker protocol")
    for key in ("stored_bytes", "original_bytes"):
        if type(request[key]) is not int or request[key] <= 0:
            raise ValueError("invalid worker frame size")
    CodecMemoryPolicy.from_record(request["policy"]).install_in_worker()

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


def main(argv):
    output, parent_pid = int(argv[1]), int(argv[2])
    try:
        bind_parent(parent_pid)
        if len(argv[3]) > ERROR_BYTES:
            raise ValueError("oversize worker request")
        request = json.loads(argv[3])
        restored = run(request, sys.stdin.buffer)
    except WorkerInitializationError:
        status, detail = b"I", b"codec worker initialization failed"
    except (CodecResourceRefusal, MemoryError):
        status, detail = b"R", b"codec memory guard refused work"
    except streamznn.DecodeLimitExceeded:
        status, detail = b"L", b"codec frame exceeds bound"
    except streamznn.UnsupportedFrame:
        status, detail = b"U", b"unsupported codec frame"
    except Exception:
        # No paths or arbitrary exception contents are sent across this boundary.
        status, detail = b"I", b"codec worker refused frame"
    else:
        # A response is exactly one status frame. Once success emission begins,
        # any output failure is represented only by an unsuccessful process exit;
        # never append an error frame where the parent expects original bytes.
        try:
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
