"""Compressed-at-rest archive layer (DEC-003) + the compression gate (DEC-022).

Weights are stored compressed and decompressed on use. Every compression is gated by a mandatory
round-trip *canary*: decompress, hash the result, require it equal HF's canonical sha256 — before
the uncompressed original is ever deleted.

Codec is chosen PER SHARD by `plan_codec` from the `compression` config (wishlist.compression()):
  • whole-file ZipNN  — shard fits the RAM budget: fastest, best ratio, in-memory (ZipNN blob, "ZN").
  • StreamZNN         — over budget + stream_compress: O(chunk) float-aware (SZNN container).
  • zstd-stream       — over budget + stream off + `zstandard` installed: O(chunk), plain zstd (zstd frame).
  • raw               — over budget + stream off + no zstd: store uncompressed (still sha-verified/annexed).
Whole-file peak ≈ ~4× the shard in RAM (measured ~3.67× + baseline); that factor drives the gate.

Decompress/canary route by the stored file's MAGIC, so restores work across every codec + legacy
pre-StreamZNN `.znn`. Streaming (StreamZNN + zstd) is O(chunk); whole/legacy load the blob in RAM.
"""
from __future__ import annotations

import hashlib
import math
import os
import tempfile
from pathlib import Path
from typing import Final, Union

from zipnn import ZipNN

from modelark import streamznn

StrPath = Union[str, os.PathLike[str]]

COMPRESSIBLE_SUFFIXES: Final[set[str]] = {".safetensors"}   # GGUF/ONNX already dense → store raw
ZNN_SUFFIX: Final[str] = ".znn"

# Codec labels (also what fetch records as the storage method for logging/audit).
CODEC_WHOLE: Final[str] = "zipnn-whole"
CODEC_STREAM: Final[str] = "streamznn"
CODEC_ZSTD: Final[str] = "zstd-stream"
CODEC_RAW: Final[str] = "raw"

# Whole-file compress peak ≈ this × shard size in RAM (measured 3.67× + a fixed baseline; 4× w/ margin).
_PEAK_FACTOR: Final[float] = 4.0
_ZSTD_MAGIC: Final[bytes] = b"\x28\xb5\x2f\xfd"

# catalog quant label -> ZipNN bytearray_dtype
_DTYPE_MAP: Final[dict[str, str]] = {
    "bf16": "bfloat16", "bfloat16": "bfloat16",
    "fp16": "float16", "f16": "float16", "float16": "float16",
    "fp32": "float32", "f32": "float32", "float32": "float32",
}


class OutputCapExceeded(RuntimeError):
    """A codec would cross the guaranteed raw-plus-framing output ceiling."""


def zipnn_dtype(quant: str | None) -> str:
    """Map a catalog quant/dtype label to ZipNN's bytearray_dtype (default bf16)."""
    return _DTYPE_MAP.get((quant or "").lower(), "bfloat16")


def should_compress(filename: str) -> bool:
    return Path(filename).suffix.lower() in COMPRESSIBLE_SUFFIXES


def sha256_file(path: StrPath) -> str:
    """Streaming sha256 of a file on disk (O(1) memory)."""
    return streamznn.sha256_file(path)


def _zstd():
    """The `zstandard` module if importable, else None — the zstd codec is dormant until installed."""
    try:
        import zstandard
        return zstandard
    except ImportError:
        return None


def plan_codec(shard_bytes: int, cfg: dict) -> str:
    """Pick the codec for a shard (DEC-022 gate). `cfg` = wishlist.compression()."""
    budget = float(cfg["max_compress_ram_gb"]) * 1e9
    if _PEAK_FACTOR * shard_bytes <= budget:
        return CODEC_WHOLE                              # fits RAM → fast in-memory ZipNN, best ratio
    if cfg["stream_compress"]:
        return CODEC_STREAM                             # over budget → O(chunk) StreamZNN
    if _zstd() is not None:
        return CODEC_ZSTD                               # stream off, zstd present → streaming zstd
    return CODEC_RAW                                    # stream off, no zstd → store raw


def zstd_output_cap(raw_size: int) -> int:
    """ZSTD_compressBound's documented single-frame upper bound."""
    low_size_overhead = ((128 * 1024 - raw_size) >> 11) if raw_size < 128 * 1024 else 0
    return raw_size + (raw_size >> 8) + low_size_overhead


def codec_output_cap(
    raw_size: int,
    codec: str,
    *,
    stream_chunk_bytes: int = streamznn.DEFAULT_CHUNK,
) -> int:
    if codec == CODEC_RAW:
        return 0
    if codec == CODEC_STREAM:
        chunks = math.ceil(raw_size / stream_chunk_bytes) if raw_size else 0
        return raw_size + len(streamznn.MAGIC) + 4 * chunks
    if codec == CODEC_ZSTD:
        return zstd_output_cap(raw_size)
    if codec == CODEC_WHOLE:
        return raw_size
    raise ValueError(f"unsupported codec {codec!r}")


def _zipnn(dtype: str = "bfloat16", threads: int = 0) -> ZipNN:
    return ZipNN(input_format="byte", bytearray_dtype=dtype, threads=threads)


def _atomic_write_bytes(dst: Path, data: bytes) -> None:
    """Write `data` to `dst` via temp + os.replace so a crash never leaves a half-written file."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=dst.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fo:
            fo.write(data)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _compress_whole(src: Path, dst: Path, dtype: str, threads: int, output_cap: int) -> Path:
    """In-memory ZipNN of the whole shard (O(shard) RAM) → a self-describing ZipNN blob."""
    blob = bytes(_zipnn(dtype, threads).compress(src.read_bytes()))   # ZipNN mutates its input; read is single-use
    if len(blob) > output_cap:
        raise OutputCapExceeded(
            f"whole ZipNN output {len(blob)} exceeds {output_cap}-byte cap"
        )
    _atomic_write_bytes(dst, blob)
    return dst


def _write_capped(fo, data: bytes, written: int, output_cap: int) -> int:
    if written + len(data) > output_cap:
        raise OutputCapExceeded(
            f"zstd output would exceed {output_cap}-byte cap before next write"
        )
    fo.write(data)
    return written + len(data)


def _compress_zstd(src: Path, dst: Path, threads: int, output_cap: int) -> Path:
    """Streaming plain zstd (O(chunk) RAM) — the stream-off fallback, only when `zstandard` exists."""
    zstd = _zstd()
    if zstd is None:
        raise RuntimeError("zstd codec requested but `zstandard` is not installed")
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=dst.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fo, open(src, "rb") as fi:
            compressor = zstd.ZstdCompressor(level=3, threads=threads).compressobj()
            written = 0
            for chunk in iter(lambda: fi.read(1 << 20), b""):
                written = _write_capped(fo, compressor.compress(chunk), written, output_cap)
            written = _write_capped(fo, compressor.flush(), written, output_cap)
            fo.flush()
            os.fsync(fo.fileno())
        os.replace(tmp, dst)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dst


def compress_file(src: StrPath, dst: StrPath | None = None, dtype: str = "bfloat16",
                  codec: str = CODEC_STREAM, threads: int = 0) -> Path:
    """Compress `src` → `dst` (default `src` + .znn) with the given codec. RAW is handled by the
    caller (nothing to compress), so it is not a valid codec here."""
    src_path = Path(src)
    dst_path = Path(dst) if dst is not None else src_path.with_name(src_path.name + ZNN_SUFFIX)
    output_cap = codec_output_cap(src_path.stat().st_size, codec)
    if codec == CODEC_WHOLE:
        return _compress_whole(src_path, dst_path, dtype, threads, output_cap)
    if codec == CODEC_STREAM:
        try:
            return streamznn.compress_file(
                src_path, dst_path, dtype=dtype, threads=threads,
                max_output_bytes=output_cap,
            )
        except streamznn.OutputCapExceeded as exc:
            raise OutputCapExceeded(str(exc)) from exc
    if codec == CODEC_ZSTD:
        return _compress_zstd(src_path, dst_path, threads, output_cap)
    raise ValueError(f"compress_file: not a compressing codec: {codec!r}")


def _head(path: StrPath, n: int = 8) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def canary_ok(znn_path: StrPath, expected_sha256: str, dtype: str = "bfloat16") -> bool:
    """Decompress + hash; must equal HF's canonical sha256. Routes by the stored file's magic:
    StreamZNN + zstd stream (O(chunk) memory); whole/legacy ZipNN load the blob in RAM."""
    if not expected_sha256:
        return False        # no canonical hash to certify against → canary cannot pass (keep the original)
    head = _head(znn_path)
    if head.startswith(streamznn.MAGIC):
        return streamznn.verify_sha256(znn_path, expected_sha256)
    if head.startswith(_ZSTD_MAGIC):
        return _zstd_verify_sha256(znn_path, expected_sha256)
    out = _zipnn(dtype).decompress(Path(znn_path).read_bytes())       # whole / legacy ZipNN blob (self-describing)
    return hashlib.sha256(bytes(out)).hexdigest() == expected_sha256


def _zstd_verify_sha256(znn_path: StrPath, expected_sha256: str) -> bool:
    zstd = _zstd()
    if zstd is None:
        raise RuntimeError("cannot verify a zstd file — `zstandard` is not installed")
    hasher = hashlib.sha256()
    with open(znn_path, "rb") as fi, zstd.ZstdDecompressor().stream_reader(fi) as reader:
        for chunk in iter(lambda: reader.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest() == expected_sha256


def decompress_file(znn_path: StrPath, dst: StrPath, dtype: str = "bfloat16") -> Path:
    """Materialize a stored file back to original bytes (for loading). Byte-identical to what the
    canary verified. Routes by magic; StreamZNN/zstd stream, whole/legacy load in RAM."""
    head = _head(znn_path)
    if head.startswith(streamznn.MAGIC):
        return streamznn.decompress_file(znn_path, dst)
    dst_path = Path(dst)
    if head.startswith(_ZSTD_MAGIC):
        zstd = _zstd()
        if zstd is None:
            raise RuntimeError("cannot decompress a zstd file — `zstandard` is not installed")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        with open(znn_path, "rb") as fi, open(dst_path, "wb") as fo:
            zstd.ZstdDecompressor().copy_stream(fi, fo)
        return dst_path
    _atomic_write_bytes(dst_path, bytes(_zipnn(dtype).decompress(Path(znn_path).read_bytes())))
    return dst_path
