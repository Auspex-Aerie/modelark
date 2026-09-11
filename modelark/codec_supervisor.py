"""Bounded parent-side transport for a single guarded ZipNN frame.

Not yet wired to Slice approvals. Caller owns source acquisition, fences and
original digest verification. Mandatory context exit kills/reaps the child before
returning, including consumer failure; no whole-frame buffers exist in this parent.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import selectors
import subprocess
import sys

from .codec_resources import CodecMemoryPolicy, available_memory
from . import streamznn

IO_BYTES = 64 << 10
DIAGNOSTIC_BYTES = 8192
ERROR_BYTES = 4096
POLL_SECONDS = 0.05  # Cancellation responsiveness, not a decode deadline.


class WorkerRefusal(Exception):
    """Neutral worker failure, distinct from caller IO/cancellation exceptions."""

    def __init__(self, kind, detail):
        self.kind, self.detail = kind, detail
        super().__init__(f"{kind}: {detail}")


class _Input:
    def __init__(self, source, check):
        self.source, self.check = source, check

    def read(self, size):
        self.check()
        size = min(size, IO_BYTES)
        data = self.source.read(size)
        if not isinstance(data, bytes) or len(data) > size:
            raise streamznn.StreamZnnError("source did not honor bounded binary read")
        self.check()
        return data


def _launch(request, output):
    return subprocess.Popen(
        [sys.executable, "-m", "modelark.codec_worker", str(output), str(os.getpid()),
         json.dumps(request, separators=(",", ":"))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        close_fds=True, pass_fds=(output,), bufsize=0)


def _transfer(proc, output, source, header, check):
    """Nonblocking full-duplex supervision, bounded even for noisy native code."""
    original = int.from_bytes(header[16:24], "little")
    remaining = int.from_bytes(header[24:32], "little") - len(header)
    pending = header
    response = bytearray()
    detail = bytearray()
    diagnostics = bytearray()
    status = None
    left = None
    output_eof = False
    with selectors.DefaultSelector() as poll:
        for handle, events, tag in ((proc.stdin, selectors.EVENT_WRITE, "input"),
                                    (proc.stdout, selectors.EVENT_READ, "diagnostic"),
                                    (output, selectors.EVENT_READ, "output")):
            os.set_blocking(handle if isinstance(handle, int) else handle.fileno(), False)
            poll.register(handle, events, tag)
        while True:
            check()
            for key, _ in poll.select(POLL_SECONDS):
                check()
                if key.data == "input":
                    if not pending and remaining:
                        pending = source.read(min(IO_BYTES, remaining))
                        if not pending:
                            raise streamznn.StreamZnnError("truncated worker frame input")
                        remaining -= len(pending)
                    if pending:
                        try:
                            count = os.write(key.fd, pending)
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            # Drain the dedicated result to preserve a typed early
                            # guard refusal instead of reporting generic pipe failure.
                            poll.unregister(key.fileobj)
                            proc.stdin.close()
                            continue
                        pending = pending[count:]
                    if not pending and not remaining:
                        poll.unregister(key.fileobj)
                        proc.stdin.close()
                elif key.data == "diagnostic":
                    try:
                        data = os.read(key.fd, IO_BYTES)
                    except BlockingIOError:
                        continue
                    if not data:
                        poll.unregister(key.fileobj)
                        continue
                    diagnostics.extend(data)
                    del diagnostics[:-DIAGNOSTIC_BYTES]
                else:
                    # Never read across response phases or allocate from an
                    # unchecked worker-declared length.
                    size = 9 - len(response) if status is None else min(IO_BYTES, left or 1)
                    try:
                        data = os.read(key.fd, size)
                    except BlockingIOError:
                        continue
                    if not data:
                        output_eof = True
                        poll.unregister(key.fileobj)
                        continue
                    if status is None:
                        response.extend(data)
                        if len(response) != 9:
                            continue
                        status = bytes(response[:1])
                        left = int.from_bytes(response[1:], "little")
                        if status == b"O":
                            if left != original or not proc.stdin.closed:
                                raise WorkerRefusal("INVALID", "invalid worker success header")
                        elif status not in (b"R", b"I", b"U", b"L") or left > ERROR_BYTES:
                            raise WorkerRefusal("INVALID", "invalid worker error header")
                    else:
                        if len(data) > left:
                            raise WorkerRefusal("INVALID", "trailing worker output")
                        left -= len(data)
                        if status == b"O":
                            check()
                            yield data
                            check()
                        else:
                            detail.extend(data)
            code = proc.poll()
            if code is not None and output_eof:
                if status is None or left != 0:
                    raise WorkerRefusal("WORKER_FAILED", f"incomplete worker result (exit {code})")
                if status != b"O":
                    kind = {b"R": "RESOURCE", b"I": "INVALID", b"U": "UNSUPPORTED", b"L": "LIMIT"}[status]
                    raise WorkerRefusal(kind, detail.decode("utf-8", errors="replace"))
                if code != 0:
                    raise WorkerRefusal("WORKER_FAILED", f"worker failed after output (exit {code})")
                return


@contextmanager
def guarded_zipnn_frame(source, *, policy: CodecMemoryPolicy, max_stored_bytes,
                        max_decoded_bytes, remaining_bytes, stored_length=None,
                        prefix=b"", check=lambda: None):
    """Yield a bounded iterator of original-byte chunks for exactly one frame.

    Consume to EOF for worker certification; context exit alone never certifies
    success. Always nest this context inside source/fence ownership. Closing on a
    destination error preserves that error and reaps before the caller can release
    its fence. Input IO/check exceptions retain identity. No source is closed here.
    Memory observation/admission is identical to the writer qualification; a sample
    is not a reservation against unrelated processes. Only Linux is qualified.
    """
    if not isinstance(policy, CodecMemoryPolicy):
        raise ValueError("explicit codec memory policy required")
    checked = _Input(source, check)
    header = streamznn.read_zipnn_header(
        checked, max_stored_bytes=max_stored_bytes, max_decoded_bytes=max_decoded_bytes,
        remaining_bytes=remaining_bytes, stored_length=stored_length, prefix=prefix,
        read_size=IO_BYTES)
    policy.admit(available_memory()["available_bytes"])
    check()
    request = {"version": "modelark.codec-worker.v1", "policy": policy.to_record(),
               "stored_bytes": int.from_bytes(header[24:32], "little"),
               "original_bytes": int.from_bytes(header[16:24], "little")}
    output, writer = os.pipe()
    proc = chunks = None
    try:
        try:
            proc = _launch(request, writer)
        finally:
            os.close(writer)
        chunks = _transfer(proc, output, checked, header, check)
        yield chunks
    finally:
        # Kill first: no new source reads/output are attempted during unwinding.
        # SIGKILL avoids waiting for native code to cooperate with cancellation.
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            finally:
                proc.wait()
                proc.stdin.close()
                proc.stdout.close()
        if chunks is not None:
            chunks.close()
        os.close(output)
