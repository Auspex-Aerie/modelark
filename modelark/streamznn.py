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
import os
import re
import struct
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable, Final, Union

from zipnn import ZipNN

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


def _read_exact(fh: BinaryIO, n: int) -> bytes:
    """Read exactly `n` bytes from `fh` or raise StreamZnnError. A short read means a
    truncated/corrupt container — it is never returned as a silently-short slice."""
    if n < 0:
        raise StreamZnnError(f"negative read length {n}")
    buf = bytearray()
    while len(buf) < n:
        piece = fh.read(n - len(buf))
        if not piece:
            raise StreamZnnError(f"truncated container: wanted {n} bytes, got {len(buf)}")
        buf.extend(piece)
    return bytes(buf)


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
                blob: bytes = bytes(ZipNN(input_format="byte", bytearray_dtype=dtype, threads=threads).compress(chunk))
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
        magic = _read_exact(fi, len(MAGIC))
        if magic != MAGIC:
            raise StreamZnnError(f"bad magic {magic!r}: not a StreamZNN container")
        while True:
            head = fi.read(_LEN.size)
            if head == b"":
                return                          # clean EOF exactly at a frame boundary
            if len(head) != _LEN.size:
                raise StreamZnnError(f"truncated frame length: got {len(head)} of {_LEN.size} bytes")
            (blob_len,) = _LEN.unpack(head)
            if blob_len == 0 or blob_len > _MAX_BLOB:
                raise StreamZnnError(f"implausible frame length {blob_len}")
            blob = _read_exact(fi, blob_len)
            restored: bytes = bytes(ZipNN(input_format="byte").decompress(blob))
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
