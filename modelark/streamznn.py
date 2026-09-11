"""StreamZNN — constant-memory streaming file compression around ZipNN.

ZipNN (https://github.com/zipnn/zipnn) losslessly compresses floating-point model
weights very well, but its Python API compresses/decompresses a whole buffer in RAM:
a 10 GB shard needs ~25 GB and OOM-kills long archival runs. StreamZNN wraps ZipNN so a
file of ANY size round-trips in O(chunk) memory (a few hundred MB): it splits the input
into fixed-size slices, compresses each as an independent, self-describing ZipNN blob,
and frames the blobs in a tiny container.

Container layout (one file):
    MAGIC = b"SZNN\\x01"            # 5 bytes; lets a reader tell a StreamZNN container
                                    # apart from a bare ZipNN blob (which begins b"ZN")
    then, repeated until EOF:
        [uint32 little-endian: blob_len][blob_len bytes: one ZipNN blob]

Why this is safe to trust with an irreplaceable archive:
  * Each ZipNN blob is FULLY SELF-DESCRIBING — its header records dtype, byte-reorder and
    original length, so decompression reconstructs the exact bytes regardless of any dtype
    hint. (Verified: compress bf16, decompress "as" float32 → still byte-identical.)
  * decompress_file() and verify_sha256() share ONE decompression path (decompress_to);
    "the canary passed" therefore provably means "a restore yields these same bytes".
  * verify_sha256() streams — decompress a slice, feed sha256, discard — so integrity is
    checked without ever materializing the whole file in RAM or on disk.
  * Outputs are written atomically (temp + os.replace); a crash mid-run never leaves a
    truncated file that looks complete.
  * Corruption fails LOUD (StreamZnnError): a short read trips the exact-read guard and a
    damaged blob trips ZipNN's own "header must start with ZN" check. A bad archive can
    never pass silently as good.

Standalone + MIT: depends only on `zipnn` + the Python stdlib and imports nothing project-
specific, so it lifts into its own repository unchanged. Offered to the community as
"StreamZNN" (verify the name/prior-art before publishing; ZipNN's own `is_streaming` chunks
only the in-memory computation — the file-level O(chunk) framing here is the added piece).

------------------------------------------------------------------------------------------
MIT License · Copyright (c) 2026 Auspex Labs

Permission is hereby granted, free of charge, to any person obtaining a copy of this
software and associated documentation files (the "Software"), to deal in the Software
without restriction, including without limitation the rights to use, copy, modify, merge,
publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or
substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
------------------------------------------------------------------------------------------
"""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
import os
import re
import struct
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable, Final, Union

StrPath = Union[str, "os.PathLike[str]"]
Sink = Callable[[bytes], object]

MAGIC: Final[bytes] = b"SZNN\x01"
DEFAULT_CHUNK: Final[int] = 64 * 1024 * 1024        # 64 MiB in → ~sub-GB peak, size-independent
_LEN: Final[struct.Struct] = struct.Struct("<I")    # per-slice compressed-length frame (uint32 LE)
_MAX_BLOB: Final[int] = 1 << 34                      # 16 GiB sanity ceiling on a single framed blob
_HASH_READ: Final[int] = 1 << 20
_SHA256_RE: Final["re.Pattern[str]"] = re.compile(r"\A[0-9a-f]{64}\Z")


class StreamZnnError(Exception):
    """Raised on a malformed / truncated / corrupt StreamZNN container. Never swallowed."""


class OutputCapExceeded(StreamZnnError):
    """Compression would exceed the caller's guaranteed on-disk output ceiling."""


class DecodeLimitExceeded(StreamZnnError):
    """A frame or decoded total exceeds an explicit reader bound."""


class UnsupportedFrame(StreamZnnError):
    """A frame is outside the supported lossless byte-header contract."""


def _zipnn(*, dtype: str | None = None, threads: int = 0):
    # Import only on an actual codec operation. Planning imports this module for
    # constants and must not initialize Torch or surface its dependency warnings.
    from zipnn import ZipNN

    kwargs = {"input_format": "byte"}
    if dtype is not None:
        kwargs.update(bytearray_dtype=dtype, threads=threads)
    return ZipNN(**kwargs)


def _read_exact(fh: BinaryIO, n: int, *, read_size: int = _HASH_READ) -> bytes:
    """Read exactly `n` bytes from `fh` or raise StreamZnnError. A short read means a
    truncated/corrupt container — it is never returned as a silently-short slice."""
    if n < 0:
        raise StreamZnnError(f"negative read length {n}")
    buf = bytearray()
    while len(buf) < n:
        piece = fh.read(min(read_size, n - len(buf)))
        if not piece:
            raise StreamZnnError(f"truncated container: wanted {n} bytes, got {len(buf)}")
        if len(piece) > min(read_size, n - len(buf)):
            raise StreamZnnError("source returned more bytes than requested")
        buf.extend(piece)
    return bytes(buf)


def _positive_bound(value, name, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise ValueError(f"invalid {name}")


def read_zipnn_header(stream: BinaryIO, *, max_stored_bytes: int, max_decoded_bytes: int,
                      remaining_bytes: int, stored_length: int | None = None,
                      prefix: bytes = b"", read_size: int = _HASH_READ) -> bytes:
    """Read/validate only the fixed header, leaving payload in the caller's stream.

    Shared interpretation for in-process readers, guarded workers and preflight.
    No native import, payload allocation, process management or source ownership.
    The returned header carries original/stored sizes at offsets 16/24 (uint64 LE).
    """
    for value, name in ((max_stored_bytes, "stored bound"),
                        (max_decoded_bytes, "decoded bound"), (read_size, "read size")):
        _positive_bound(value, name)
    _positive_bound(remaining_bytes, "remaining bytes", zero=True)
    if not isinstance(prefix, bytes) or len(prefix) > 32:
        raise ValueError("invalid frame prefix")
    if stored_length is not None:
        _positive_bound(stored_length, "stored length")
        if stored_length > max_stored_bytes:
            raise DecodeLimitExceeded("stored ZipNN frame exceeds bound")
        if stored_length < 32:
            raise StreamZnnError("short ZipNN frame")
    header = prefix + _read_exact(stream, 32 - len(prefix), read_size=read_size)
    original = int.from_bytes(header[16:24], "little")
    stored = int.from_bytes(header[24:32], "little")
    if original > max_decoded_bytes or stored > max_stored_bytes or original > remaining_bytes:
        raise DecodeLimitExceeded("ZipNN frame exceeds configured or artifact bound")
    if (header[:4] != b"ZN\x00\x05" or header[8] != 1
            or any(header[9:14]) or header[15] not in (1, 2, 4, 5, 6)
            or header[7] not in (0, 1) or header[6] not in (0, 1)
            or header[5] not in (10, 220)):
        raise UnsupportedFrame("unsupported ZipNN header")
    if (stored < 32 or original == 0 or (stored_length is not None and stored != stored_length)
            or header[14] > 26):
        raise StreamZnnError("inconsistent ZipNN frame header")
    return header


def read_zipnn_frame(stream: BinaryIO, *, max_stored_bytes: int, max_decoded_bytes: int,
                     remaining_bytes: int, stored_length: int | None = None,
                     prefix: bytes = b"", read_size: int = _HASH_READ,
                     decode_frame: Callable[[bytes], object] | None = None) -> bytes:
    """Read one validated, non-streaming ZipNN 0.5 lossless byte frame.

    The optional decoder receives the complete validated blob; its exceptions
    propagate unchanged. Bounds buffers, NOT native working memory. Caller owns
    the stream. Legacy path-wrapper format support is deliberately unchanged.
    """
    header = read_zipnn_header(
        stream, max_stored_bytes=max_stored_bytes, max_decoded_bytes=max_decoded_bytes,
        remaining_bytes=remaining_bytes, stored_length=stored_length,
        prefix=prefix, read_size=read_size)
    original = int.from_bytes(header[16:24], "little")
    stored = int.from_bytes(header[24:32], "little")
    blob = header + _read_exact(stream, stored - 32, read_size=read_size)
    if decode_frame is None:
        from zipnn import ZipNN
        try:
            decoded = ZipNN(input_format="byte", threads=1).decompress(blob)
        except Exception as exc:
            raise StreamZnnError("ZipNN decoder refused frame") from exc
    else:
        decoded = decode_frame(blob)
    if (not isinstance(decoded, (bytes, bytearray, memoryview))
            or (decoded.nbytes if isinstance(decoded, memoryview) else len(decoded)) != original):
        raise StreamZnnError("ZipNN decoded size differs from header")
    return bytes(decoded)


def iter_frame_lengths(stream: BinaryIO, *, max_stored_frame_bytes: int = _MAX_BLOB,
                       read_size: int = _HASH_READ, prefix: bytes = b""):
    """Shared container framing. Caller consumes each yielded frame before next()."""
    _positive_bound(max_stored_frame_bytes, "stored frame bound")
    _positive_bound(read_size, "read size")
    if not isinstance(prefix, bytes) or len(prefix) > len(MAGIC):
        raise ValueError("invalid container prefix")
    magic = prefix + _read_exact(stream, len(MAGIC) - len(prefix), read_size=read_size)
    if magic != MAGIC:
        raise StreamZnnError(f"bad magic {magic!r}: not a StreamZNN container")
    while True:
        head = stream.read(1)
        if head == b"":
            return
        if len(head) != 1:
            raise StreamZnnError("source returned more bytes than requested")
        head += _read_exact(stream, _LEN.size - 1, read_size=read_size)
        (blob_len,) = _LEN.unpack(head)
        if blob_len == 0:
            raise StreamZnnError("implausible frame length 0")
        if blob_len > max_stored_frame_bytes:
            raise DecodeLimitExceeded("stored StreamZNN frame exceeds bound")
        yield blob_len


def iter_decompress(stream: BinaryIO, *, max_stored_frame_bytes: int = _MAX_BLOB,
                    max_decoded_frame_bytes: int | None = None,
                    max_total_bytes: int | None = None, read_size: int = _HASH_READ,
                    prefix: bytes = b"",
                    frame_reader: Callable[[BinaryIO, int, int | None], bytes] | None = None,
                    frame_stream=None):
    """Yield restored frames from a caller-owned, possibly non-seekable stream.

    Handles short reads and bounds each input request by `read_size`. `prefix`
    is container magic already consumed by a dispatcher. Closing the iterator
    never closes the supplied stream. IO and frame-reader exceptions propagate.

    Default decoding preserves the historical standalone ZipNN acceptance and
    exception behavior. Optional decoded/total limits are checked AFTER default
    native decoding, not a native RAM guard. For untrusted byte frames, inject a
    reader using `read_zipnn_frame` to validate headers before native allocation.
    The callback receives (stream, stored_length, remaining_total_or_None), must
    consume exactly that frame, and retains its own input-read/resource policy.
    Alternatively, frame_stream returns a context manager yielding bounded byte
    pieces for one frame. Its context closes before advancing to the next frame,
    including on iterator close/failure. The two hooks are mutually exclusive.
    """
    _positive_bound(max_stored_frame_bytes, "stored frame bound")
    if frame_reader is not None and frame_stream is not None:
        raise ValueError("choose one frame reader")
    _positive_bound(read_size, "read size")
    for value, name in ((max_decoded_frame_bytes, "decoded frame bound"),
                        (max_total_bytes, "total bound")):
        if value is not None:
            _positive_bound(value, name, zero=True)
    total = 0
    for blob_len in iter_frame_lengths(stream, max_stored_frame_bytes=max_stored_frame_bytes,
                                      read_size=read_size, prefix=prefix):
        remaining = None if max_total_bytes is None else max_total_bytes - total
        if frame_stream is not None:
            context = frame_stream(stream, blob_len, remaining)
        elif frame_reader is None:
            blob = _read_exact(stream, blob_len, read_size=read_size)
            restored = bytes(_zipnn().decompress(blob))
            context = nullcontext((restored,))
        else:
            restored = frame_reader(stream, blob_len, remaining)
            context = nullcontext((restored,))
        frame_total = 0
        with context as pieces:
            for restored in pieces:
                if not isinstance(restored, bytes):
                    raise StreamZnnError("frame reader must return bytes")
                frame_total += len(restored)
                if max_decoded_frame_bytes is not None and frame_total > max_decoded_frame_bytes:
                    raise DecodeLimitExceeded("decoded StreamZNN frame exceeds bound")
                total += len(restored)
                if max_total_bytes is not None and total > max_total_bytes:
                    raise DecodeLimitExceeded("decoded total exceeds bound")
                yield restored


def compress_file(
    src: StrPath,
    dst: StrPath,
    *,
    dtype: str = "bfloat16",
    chunk_bytes: int = DEFAULT_CHUNK,
    threads: int = 0,
    max_output_bytes: int | None = None,
) -> Path:
    """Compress `src` → `dst` as a StreamZNN container, written atomically.

    Peak memory ≈ one `chunk_bytes` slice + ZipNN's per-slice working set, regardless of the
    size of `src`. `dtype` is the ZipNN bytearray_dtype used to GROUP bytes for the float-aware
    compressor; it affects ratio only — the exact value is recorded per-blob, so decompression
    never needs it. `threads` is ZipNN's internal thread count (0 = its default); threads work on
    the same in-memory slice, so they add CPU parallelism at no extra peak RSS. Returns dst.
    """
    src_path = Path(src)
    dst_path = Path(dst)
    if not isinstance(chunk_bytes, int) or chunk_bytes <= 0:
        raise ValueError(f"chunk_bytes must be a positive int, got {chunk_bytes!r}")
    if not src_path.is_file():
        raise FileNotFoundError(f"source is not a file: {src_path}")

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(dst_path.parent), prefix=dst_path.name + ".", suffix=".sznn.tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as fo, open(src_path, "rb") as fi:
            if max_output_bytes is not None and len(MAGIC) > max_output_bytes:
                raise OutputCapExceeded(
                    f"StreamZNN magic exceeds {max_output_bytes}-byte output cap"
                )
            fo.write(MAGIC)
            written = len(MAGIC)
            while True:
                chunk = fi.read(chunk_bytes)
                if not chunk:
                    break
                # NOTE: ZipNN.compress() REORDERS ITS INPUT BUFFER IN PLACE (its native core writes
                # through even a `bytes` object). Safe here only because `chunk` is a fresh, single-use
                # read we never touch again, and the source file was opened read-only — so the file on
                # disk is untouched. Never hand ZipNN.compress a buffer the caller still needs.
                blob: bytes = bytes(_zipnn(dtype=dtype, threads=threads).compress(chunk))
                if len(blob) > _MAX_BLOB:
                    raise StreamZnnError(f"compressed slice {len(blob)} exceeds {_MAX_BLOB}-byte frame ceiling")
                # Check expansion while the blob is still in memory. A post-write check could
                # transiently cross the ledger's raw-plus-framing guarantee by one whole chunk.
                if max_output_bytes is not None and len(blob) > len(chunk):
                    raise OutputCapExceeded(
                        f"compressed slice {len(blob)} exceeds raw slice {len(chunk)}"
                    )
                next_size = _LEN.size + len(blob)
                if max_output_bytes is not None and written + next_size > max_output_bytes:
                    raise OutputCapExceeded(
                        f"StreamZNN output would exceed {max_output_bytes}-byte cap"
                    )
                fo.write(_LEN.pack(len(blob)))
                fo.write(blob)
                written += next_size
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp_path, dst_path)          # atomic publish; a crash before this leaves only the tmp
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return dst_path


def decompress_to(src: StrPath, sink: Sink) -> None:
    """THE decompression path — shared by decompress_file() and verify_sha256().

    Streams `src`, calling `sink(bytes)` with each restored slice in order; O(chunk) memory.
    Decompression is self-describing (no dtype needed). Raises StreamZnnError on any framing
    problem; propagates ZipNN's own error on a corrupt blob.
    """
    with open(src, "rb") as fi:
        for restored in iter_decompress(fi):
            sink(restored)


def decompress_file(src: StrPath, dst: StrPath) -> Path:
    """Restore a StreamZNN container to the original bytes on disk (O(chunk) memory), written
    atomically so a partial restore is never published under `dst`. Returns the destination path."""
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(dst_path.parent), prefix=dst_path.name + ".", suffix=".out.tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as fo:
            decompress_to(src, fo.write)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp_path, dst_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    return dst_path


def verify_sha256(src: StrPath, expected_sha256: str) -> bool:
    """Canary: stream-decompress `src` and return whether the result's sha256 equals
    `expected_sha256`. No scratch file, O(chunk) memory, and the SAME decompression path a
    restore uses — so True guarantees a faithful restore. Raises on a malformed hash string or
    a corrupt container (a corrupt container is a hard error, distinct from a clean mismatch)."""
    if not _SHA256_RE.match(expected_sha256):
        raise ValueError(f"expected_sha256 must be 64 lowercase hex chars, got {expected_sha256!r}")
    hasher = hashlib.sha256()
    decompress_to(src, hasher.update)
    return hasher.hexdigest() == expected_sha256


def is_container(src: StrPath) -> bool:
    """True iff `src` begins with the StreamZNN magic (vs a bare ZipNN blob / other data)."""
    with open(src, "rb") as fi:
        return fi.read(len(MAGIC)) == MAGIC


def sha256_file(src: StrPath) -> str:
    """Streaming sha256 of a file on disk (utility; O(1) memory)."""
    hasher = hashlib.sha256()
    with open(src, "rb") as fi:
        for chunk in iter(lambda: fi.read(_HASH_READ), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
