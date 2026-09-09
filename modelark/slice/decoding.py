"""Retrieval-free original-byte streams with bounded codec allocations.

The bound limits individual stored/decoded ZipNN frames and zstd windows, not
the native codecs' total working set. No tensor input, whole-file scratch, or
unbounded ``read()`` is accepted. Large legacy whole-ZipNN files fail closed.
"""
from __future__ import annotations

import struct

from .transaction import TransferRefusal


def _refuse(code="SOURCE_DECODE_INVALID", detail=""):
    raise TransferRefusal(code, detail)


def _exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            _refuse(detail="truncated container")
        data.extend(chunk)
    return bytes(data)


def _zipnn_frame(stream, prefix, limit, remaining, stored_length=None):
    header = prefix + _exact(stream, 32 - len(prefix))
    original = int.from_bytes(header[16:24], "little")
    stored = int.from_bytes(header[24:32], "little")
    if original > limit or stored > limit or original > remaining:
        _refuse("SOURCE_DECODE_LIMIT", "ZipNN frame exceeds configured or artifact bound")
    # Only the byte-lossless, non-streaming 0.5 header layout used by this
    # archive writer is supported. Tensor/shape, delta and lossy modes cannot
    # be safely inferred from catalog SourceEvidence and must never reach C.
    if (header[:4] != b"ZN\x00\x05" or header[8] != 1
            or any(header[9:14]) or header[15] not in (1, 2, 4, 5, 6)
            or header[7] not in (0, 1) or header[6] not in (0, 1)
            or header[5] not in (10, 220)):
        _refuse("SOURCE_DECODE_UNSUPPORTED", "unsupported ZipNN header")
    if (stored < 32 or original == 0 or (stored_length is not None and stored != stored_length)
            or header[14] > 26):
        _refuse(detail="inconsistent ZipNN frame header")
    from zipnn import ZipNN
    blob = header + _exact(stream, stored - 32)
    try:
        decoded = ZipNN(input_format="byte", threads=1).decompress(blob)
    except Exception as exc:
        raise TransferRefusal("SOURCE_DECODE_INVALID", "ZipNN decoder refused frame") from exc
    if not isinstance(decoded, (bytes, bytearray, memoryview)) or len(decoded) != original:
        _refuse(detail="ZipNN decoded size differs from header")
    return bytes(decoded)


def _chunks(stream, compressed, expected, limit):
    if not compressed:
        while chunk := stream.read(min(limit, 1 << 20)):
            yield chunk
        return
    prefix = stream.read(5)
    if prefix == b"SZNN\x01":
        remaining = expected
        while length := stream.read(4):
            if len(length) != 4:
                _refuse(detail="truncated StreamZNN frame length")
            size = struct.unpack("<I", length)[0]
            if size > limit:
                _refuse("SOURCE_DECODE_LIMIT", "stored StreamZNN frame exceeds bound")
            if size < 32:
                _refuse(detail="short StreamZNN frame")
            data = _zipnn_frame(stream, b"", limit, remaining, size)
            remaining -= len(data)
            yield data
    elif prefix.startswith(b"ZN"):
        yield _zipnn_frame(stream, prefix, limit, expected)
        if stream.read(1):
            _refuse(detail="trailing whole-ZipNN data")
    elif prefix.startswith(b"\x28\xb5\x2f\xfd"):
        try:
            import zstandard as zstd
        except ImportError as exc:
            raise TransferRefusal("SOURCE_DECODE_UNSUPPORTED", "zstandard is not installed") from exc
        # zstd's maximum regenerated block is 128 KiB. Feeding no more
        # than limit/128KiB input bytes bounds even maximally dense RLE output
        # before Python gets the opportunity to check its length.
        if limit < 128 << 10:
            _refuse("SOURCE_DECODE_LIMIT", "zstd requires a 128 KiB decoding bound")
        header = prefix
        while True:
            try:
                params = zstd.get_frame_parameters(header)
                break
            except zstd.ZstdError:
                if len(header) >= 18:
                    _refuse(detail="invalid zstd header")
                header += _exact(stream, 1)
        if (params.window_size > limit
                or params.content_size not in (zstd.CONTENTSIZE_UNKNOWN, zstd.CONTENTSIZE_ERROR)
                and params.content_size > expected):
            _refuse("SOURCE_DECODE_LIMIT", "zstd window/content exceeds bound")
        decoder = zstd.ZstdDecompressor(max_window_size=max(1, limit // 1024)).decompressobj()
        pending = header
        try:
            while pending:
                data = decoder.decompress(pending)
                if len(data) > limit:
                    _refuse("SOURCE_DECODE_LIMIT", "zstd output block exceeds bound")
                if data:
                    yield data
                if decoder.eof:
                    if decoder.unused_data or stream.read(1):
                        _refuse(detail="trailing/concatenated zstd data")
                    break
                pending = stream.read(max(1, limit // (128 << 10)))
            if not decoder.eof:
                _refuse(detail="truncated zstd frame")
        except zstd.ZstdError as exc:
            raise TransferRefusal("SOURCE_DECODE_INVALID", "zstd decoder refused frame") from exc
    else:
        _refuse("SOURCE_DECODE_UNSUPPORTED", "unknown compressed container")


class _OriginalStream:
    def __init__(self, stream, compressed, expected, limit, check):
        self._chunks = iter(_chunks(stream, compressed, expected, limit))
        self._pending = b""
        self._offset = 0
        self._total = 0
        self._expected, self._limit, self._check = expected, limit, check
        self._done = False

    def read(self, size=-1):
        if type(size) is not int or size < 0 or size > self._limit:
            _refuse("SOURCE_DECODE_LIMIT", "explicit bounded read size required")
        self._check()
        if not size or self._done:
            return b""
        if self._offset == len(self._pending):
            try:
                self._pending = next(self._chunks)
                self._offset = 0
            except StopIteration:
                self._done = True
                if self._total != self._expected:
                    _refuse(detail="decoded size differs from sealed original size")
                return b""
            self._total += len(self._pending)
            if self._total > self._expected:
                _refuse("SOURCE_DECODE_LIMIT", "decoded total exceeds sealed original size")
        end = min(self._offset + size, len(self._pending))
        data = self._pending[self._offset:end]
        self._offset = end
        self._check()
        return data


def original_stream(stream, *, compressed, expected_bytes, max_decode_bytes=64 << 20,
                    check=lambda: None):
    """Wrap an already confined binary stream; caller retains descriptor ownership."""
    if (type(expected_bytes) is not int or expected_bytes < 0
            or type(max_decode_bytes) is not int or max_decode_bytes <= 0):
        raise ValueError("nonnegative original size and positive decoding bound required")
    return _OriginalStream(stream, compressed, expected_bytes, max_decode_bytes, check)
