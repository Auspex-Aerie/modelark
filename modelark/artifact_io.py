"""Retrieval-free original-byte streams with bounded codec allocations.

The bound limits individual stored/decoded ZipNN frames and zstd windows, not
the native codecs' total working set. No tensor input, whole-file scratch, or
unbounded ``read()`` is accepted. Without an explicit policy, large legacy
whole-ZipNN files fail closed. A policy opts into isolated bounded frame output.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import streamznn
from .codec_resources import CodecResourceRefusal
from .codec_supervisor import WorkerRefusal


class DecodeError(Exception):
    """Neutral decode refusal; caller maps kind to its own error contract."""

    def __init__(self, kind, detail=""):
        self.kind, self.detail = kind, detail
        super().__init__(f"{kind}: {detail}")


@dataclass(frozen=True)
class DecodeLimits:
    """Explicit, distinct buffer bounds, not a native RAM admission policy."""

    stored_frame_bytes: int
    decoded_frame_bytes: int
    read_bytes: int
    zstd_window_bytes: int

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in (
                self.stored_frame_bytes, self.decoded_frame_bytes,
                self.read_bytes, self.zstd_window_bytes)):
            raise ValueError("positive integer decoding bounds required")


def _refuse(code="INVALID", detail=""):
    raise DecodeError(code, detail)


def _exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            _refuse(detail="truncated container")
        data.extend(chunk)
    return bytes(data)


def _prefix(stream, size):
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _zstd_header(stream, prefix, expected, limits, *, qualified=False):
    try:
        import zstandard as zstd
    except ImportError as exc:
        raise DecodeError("UNSUPPORTED", "zstandard is not installed") from exc
    if qualified and (getattr(zstd, "__version__", None), getattr(zstd, "backend", None)) != ("0.25.0", "cext"):
        _refuse("UNSUPPORTED", "byte-window policy is qualified for zstandard 0.25.0 cext only")
    if limits.decoded_frame_bytes < 128 << 10:
        _refuse("LIMIT", "zstd requires a 128 KiB decoding bound")
    header = prefix
    while True:
        try:
            params = zstd.get_frame_parameters(header)
            break
        except zstd.ZstdError:
            if len(header) >= 18:
                _refuse(detail="invalid zstd header")
            header += _exact(stream, 1)
    if (params.window_size > limits.zstd_window_bytes
            or params.content_size not in (zstd.CONTENTSIZE_UNKNOWN, zstd.CONTENTSIZE_ERROR)
            and (params.content_size > expected or qualified and params.content_size != expected)):
        _refuse("LIMIT", "zstd window/content exceeds bound")
    return zstd, header


def _chunks(stream, compressed, expected, limits, policy, check, on_frame_complete, frame_stream):
    try:
        yield from _decoded_chunks(stream, compressed, expected, limits, policy, check,
                                   on_frame_complete, frame_stream)
    except streamznn.DecodeLimitExceeded as exc:
        raise DecodeError("LIMIT", str(exc)) from exc
    except streamznn.UnsupportedFrame as exc:
        raise DecodeError("UNSUPPORTED", str(exc)) from exc
    except streamznn.StreamZnnError as exc:
        raise DecodeError("INVALID", str(exc)) from exc
    except WorkerRefusal as exc:
        raise DecodeError(exc.kind, exc.detail) from exc
    except CodecResourceRefusal as exc:
        raise DecodeError("RESOURCE", str(exc)) from exc


def _decoded_chunks(stream, compressed, expected, limits, policy, check, on_frame_complete,
                    frame_stream):
    if not compressed:
        while chunk := stream.read(min(limits.read_bytes, 1 << 20)):
            yield chunk
        return
    prefix = _prefix(stream, 5)

    def frame(source, stored_length, remaining, prefix=b""):
        return streamznn.read_zipnn_frame(
            source, max_stored_bytes=limits.stored_frame_bytes,
            max_decoded_bytes=limits.decoded_frame_bytes, remaining_bytes=remaining,
            stored_length=stored_length, prefix=prefix,
            read_size=min(limits.read_bytes, 1 << 20))

    def guarded(source, stored_length, remaining, prefix=b""):
        if frame_stream is not None:
            return frame_stream(source, stored_length, remaining, prefix=prefix)
        from .codec_supervisor import guarded_zipnn_frame
        return guarded_zipnn_frame(
            source, policy=policy.memory, max_stored_bytes=limits.stored_frame_bytes,
            max_decoded_bytes=limits.decoded_frame_bytes, remaining_bytes=remaining,
            stored_length=stored_length, prefix=prefix, check=check, on_complete=on_frame_complete)

    if prefix == streamznn.MAGIC:
        yield from streamznn.iter_decompress(
            stream, prefix=prefix, max_stored_frame_bytes=limits.stored_frame_bytes,
            max_decoded_frame_bytes=limits.decoded_frame_bytes, max_total_bytes=expected,
            read_size=min(limits.read_bytes, 1 << 20),
            **({"frame_stream": guarded} if policy is not None else {"frame_reader": frame}))
    elif prefix.startswith(b"ZN"):
        if policy is None:
            yield frame(stream, None, expected, prefix)
        else:
            with guarded(stream, None, expected, prefix) as chunks:
                yield from chunks
        if stream.read(1):
            _refuse(detail="trailing whole-ZipNN data")
    elif prefix.startswith(b"\x28\xb5\x2f\xfd"):
        # zstd's maximum regenerated block is 128 KiB. Feeding no more
        # than limit/128KiB input bytes bounds even maximally dense RLE output
        # before Python gets the opportunity to check its length.
        zstd, header = _zstd_header(stream, prefix, expected, limits, qualified=policy is not None)
        # Preserve legacy Slice's native setting in this no-widening extraction.
        # zstandard 0.25.0 actually treats this setting as bytes, despite its
        # documentation's KiB wording. Stage C must qualify the units correction;
        # this conversion currently refuses some frames below our header ceiling.
        window = (limits.zstd_window_bytes if policy is not None
                  else max(1, limits.zstd_window_bytes // 1024))
        decoder = zstd.ZstdDecompressor(max_window_size=window).decompressobj()
        output_bound = min(limits.decoded_frame_bytes, 1 << 20) if policy else limits.decoded_frame_bytes
        pending = header
        try:
            while pending:
                data = decoder.decompress(pending)
                if len(data) > output_bound:
                    _refuse("LIMIT", "zstd output block exceeds bound")
                if data:
                    yield data
                if decoder.eof:
                    if decoder.unused_data or stream.read(1):
                        _refuse(detail="trailing/concatenated zstd data")
                    break
                pending = stream.read(max(1, output_bound // (128 << 10)))
            if not decoder.eof:
                _refuse(detail="truncated zstd frame")
        except zstd.ZstdError as exc:
            raise DecodeError("INVALID", "zstd decoder refused frame") from exc
    else:
        _refuse("UNSUPPORTED", "unknown compressed container")


class _CheckedInput:
    """Check every underlying read; callbacks and IO errors keep their identity."""

    def __init__(self, stream, read_bytes, check):
        self.stream, self.read_bytes, self.check = stream, read_bytes, check

    def read(self, size):
        self.check()
        size = min(size, self.read_bytes, 1 << 20)
        chunk = self.stream.read(size)
        if not isinstance(chunk, bytes) or len(chunk) > size:
            _refuse(detail="source did not honor bounded binary read")
        self.check()
        return chunk


class _OriginalStream:
    def __init__(self, stream, compressed, expected, limits, check, policy, on_frame_complete,
                 frame_stream):
        self._chunks = iter(_chunks(_CheckedInput(stream, limits.read_bytes, check),
                                    compressed, expected, limits, policy, check, on_frame_complete,
                                    frame_stream))
        self._pending = b""
        self._offset = 0
        self._total = 0
        self._expected, self._limit, self._check = expected, limits.read_bytes, check
        self._done = False

    def close(self):
        self._done = True
        self._pending = b""
        self._chunks.close()

    def read(self, size=-1):
        if type(size) is not int or size < 0 or size > self._limit:
            _refuse("LIMIT", "explicit bounded read size required")
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
                _refuse("LIMIT", "decoded total exceeds sealed original size")
        end = min(self._offset + size, len(self._pending))
        data = self._pending[self._offset:end]
        self._offset = end
        self._check()
        return data


def original_stream(stream, *, compressed, expected_bytes, limits: DecodeLimits,
                    check=lambda: None, policy=None, on_frame_complete=lambda evidence: None,
                    frame_stream=None):
    """Decode caller-owned bytes; no paths, source authority or output publication.

    Original length is enforced here; the caller must verify the original digest.
    Buffer limits alone do not add resource isolation. An explicit DecodePolicy
    selects the guarded ZipNN helper and qualified byte-valued zstd window.
    Close this reader on early termination before releasing source ownership.
    on_frame_complete is per-child evidence, not artifact/destination success.
    An explicit frame_stream may supply a non-spawning frame context for an
    already-guarded caller. It owns enforcement of the same policy; it is never
    selected implicitly and is not exposed by Slice's approval/reader adapters.
    """
    if type(expected_bytes) is not int or expected_bytes < 0:
        raise ValueError("nonnegative original size required")
    if not isinstance(limits, DecodeLimits):
        raise ValueError("explicit DecodeLimits required")
    if frame_stream is not None and (policy is None or not callable(frame_stream)):
        raise ValueError("a frame execution adapter requires an explicit policy")
    if policy is not None:
        from .artifact_policy import DecodePolicy
        if not isinstance(policy, DecodePolicy) or policy.limits != limits:
            raise ValueError("decode policy differs from supplied limits")
    return _OriginalStream(stream, compressed, expected_bytes, limits, check, policy,
                           on_frame_complete, frame_stream)
