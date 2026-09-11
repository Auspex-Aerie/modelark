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

from .codec_resources import CodecMemoryPolicy, available_memory
from .codec_process import isolated_command, validate_runtime
from . import streamznn

IO_BYTES = 64 << 10
DIAGNOSTIC_BYTES = 8192
ERROR_BYTES = 4096
METADATA_BYTES = 8192
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
        isolated_command("modelark.codec_worker", output, os.getpid(),
                         json.dumps(request, separators=(",", ":"))),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        close_fds=True, pass_fds=(output,), bufsize=0)


def _transfer(proc, output, source, header, check, policy, admission, on_complete,
              *, stored_bytes=None):
    """Nonblocking full-duplex supervision, bounded even for noisy native code."""
    streaming = stored_bytes is not None
    original = int.from_bytes(header[16:24], "little")
    remaining = (stored_bytes if streaming else int.from_bytes(header[24:32], "little")) - len(header)
    pending = header
    response = bytearray()
    detail = bytearray()
    diagnostics = bytearray()
    metadata = bytearray()
    runtime = None
    status = None
    left = None
    output_eof = False
    terminal = False
    total = 0
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
                        if streaming and source.read(1):
                            raise streamznn.StreamZnnError("stored source grew during decode")
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
                        if terminal:
                            raise WorkerRefusal("INVALID", "trailing worker output")
                        response.extend(data)
                        if len(response) != 9:
                            continue
                        status = bytes(response[:1])
                        left = int.from_bytes(response[1:], "little")
                        if status == b"M":
                            if (runtime is not None or not 0 < left <= METADATA_BYTES
                                    or not streaming and not proc.stdin.closed):
                                raise WorkerRefusal("INVALID", "invalid worker metadata header")
                        elif streaming and status == b"D":
                            if runtime is None or not 0 < left <= IO_BYTES:
                                raise WorkerRefusal("INVALID", "invalid worker data header")
                        elif streaming and status == b"E":
                            if runtime is None or left != total or not proc.stdin.closed:
                                raise WorkerRefusal("INVALID", "invalid worker end header")
                            left = 0
                            terminal = True
                        elif status == b"O":
                            if streaming or runtime is None or left != original or not proc.stdin.closed:
                                raise WorkerRefusal("INVALID", "invalid worker success header")
                        elif (not streaming and runtime is not None
                              or status not in (b"R", b"I", b"U", b"L") or left > ERROR_BYTES):
                            raise WorkerRefusal("INVALID", "invalid worker error header")
                    else:
                        if len(data) > left:
                            raise WorkerRefusal("INVALID", "trailing worker output")
                        left -= len(data)
                        if status == b"M":
                            metadata.extend(data)
                            if left == 0:
                                try:
                                    runtime = validate_runtime(json.loads(metadata), policy)
                                except (ValueError, UnicodeError, RecursionError) as exc:
                                    raise WorkerRefusal("INVALID", "invalid worker runtime evidence") from exc
                                status = None
                                response.clear()
                        elif status in (b"O", b"D"):
                            total += len(data)
                            check()
                            yield data
                            check()
                            if status == b"D" and left == 0:
                                status = None
                                response.clear()
                        else:
                            detail.extend(data)
            code = proc.poll()
            if code is not None and output_eof:
                if status is None or left != 0:
                    raise WorkerRefusal("WORKER_FAILED", f"incomplete worker result (exit {code})")
                if status not in (b"O", b"E"):
                    kind = {b"R": "RESOURCE", b"I": "INVALID", b"U": "UNSUPPORTED", b"L": "LIMIT"}[status]
                    if streaming and kind == "INVALID":
                        kind = "CODEC_INVALID"
                    raise WorkerRefusal(kind, detail.decode("utf-8", errors="replace"))
                if code != 0:
                    raise WorkerRefusal("WORKER_FAILED", f"worker failed after output (exit {code})")
                check()
                on_complete({"admission": admission, "worker": runtime})
                return


@contextmanager
def guarded_zipnn_frame(source, *, policy: CodecMemoryPolicy, max_stored_bytes,
                        max_decoded_bytes, remaining_bytes, stored_length=None,
                        prefix=b"", check=lambda: None, on_complete=lambda evidence: None):
    """Yield a bounded iterator of original-byte chunks for exactly one frame.

    Consume to EOF for worker certification; context exit alone never certifies
    success. Always nest this context inside source/fence ownership. Closing on a
    destination error preserves that error and reaps before the caller can release
    its fence. Input IO/check exceptions retain identity. No source is closed here.
    Memory observation/admission is identical to the writer qualification; a sample
    is not a reservation against unrelated processes. Only Linux is qualified.
    on_complete receives the exact admission sample and validated child runtime
    only after exact output, protocol EOF and zero child exit; never on early close.
    """
    if not isinstance(policy, CodecMemoryPolicy):
        raise ValueError("explicit codec memory policy required")
    checked = _Input(source, check)
    header = streamznn.read_zipnn_header(
        checked, max_stored_bytes=max_stored_bytes, max_decoded_bytes=max_decoded_bytes,
        remaining_bytes=remaining_bytes, stored_length=stored_length, prefix=prefix,
        read_size=IO_BYTES)
    admission = available_memory()
    policy.admit(admission["available_bytes"])
    check()
    request = {"version": "modelark.codec-worker.v2", "policy": policy.to_record(),
               "stored_bytes": int.from_bytes(header[24:32], "little"),
               "original_bytes": int.from_bytes(header[16:24], "little")}
    with _session(request, checked, header, check, policy, admission, on_complete) as chunks:
        yield chunks


@contextmanager
def guarded_legacy_stream(source, *, stored_bytes, dtype="bfloat16", check=lambda: None,
                          on_complete=lambda evidence: None):
    """Legacy archive formats, unknown original size, same guarded byte-only child.

    No Slice allowlist/window cap is imposed. Native legacy acceptance executes
    under the shared AS ceiling. Completion still requires all stored input,
    framed EOF, validated runtime and zero exit; callers own original hashes.
    """
    from .artifact_policy import qualified_policy
    policy = qualified_policy().memory
    if type(stored_bytes) is not int or stored_bytes < 0:
        raise ValueError("nonnegative stored size required")
    if type(dtype) is not str or not 0 < len(dtype) <= 128:
        raise ValueError("unsupported dtype hint")
    check()
    admission = available_memory()
    policy.admit(admission["available_bytes"])
    check()
    request = {"version": "modelark.codec-worker.v3-legacy", "policy": policy.to_record(),
               "stored_bytes": stored_bytes, "dtype": dtype}
    with _session(request, _Input(source, check), b"", check, policy, admission,
                  on_complete, stored_bytes=stored_bytes) as chunks:
        yield chunks


@contextmanager
def _session(request, source, header, check, policy, admission, on_complete, *, stored_bytes=None):
    """One owner for launch, pipe ownership, cancellation and reaping in both modes."""
    try:
        output, writer = os.pipe()
    except OSError as exc:
        if stored_bytes is not None:
            raise WorkerRefusal("WORKER_FAILED", "could not allocate codec pipes") from exc
        raise
    proc = chunks = None
    try:
        try:
            # Popen replaces child stdio. A pipe allocated into a closed stdio
            # slot must not be the protocol writer passed through that setup.
            if writer < 3:
                import fcntl
                moved = fcntl.fcntl(writer, fcntl.F_DUPFD_CLOEXEC, 3)
                os.close(writer)
                writer = moved
            try:
                proc = _launch(request, writer)
            except OSError as exc:
                if stored_bytes is not None:
                    raise WorkerRefusal("WORKER_FAILED", "could not launch codec worker") from exc
                raise
        finally:
            os.close(writer)
        chunks = _transfer(proc, output, source, header, check, policy, admission, on_complete,
                           stored_bytes=stored_bytes)
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
