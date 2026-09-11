"""Slice original-output adapter; archive authority stays in LocalArchiveReader."""
from __future__ import annotations

from modelark import artifact_io
from modelark.codec_resources import CodecResourceRefusal
from modelark.codec_supervisor import WorkerRefusal

from .transaction import TransferRefusal


class _SliceOriginalStream:
    def __init__(self, reader):
        self._reader = reader

    def read(self, size=-1):
        try:
            return self._reader.read(size)
        except artifact_io.DecodeError as exc:
            raise TransferRefusal(f"SOURCE_DECODE_{exc.kind}", exc.detail) from exc
        except WorkerRefusal as exc:
            raise TransferRefusal(f"SOURCE_DECODE_{exc.kind}", exc.detail) from exc
        except CodecResourceRefusal as exc:
            raise TransferRefusal("SOURCE_DECODE_RESOURCE", str(exc)) from exc

    def close(self):
        self._reader.close()


def original_stream(stream, *, compressed, expected_bytes, max_decode_bytes=64 << 20,
                    check=lambda: None, policy=None, on_frame_complete=lambda evidence: None):
    """Use sealed policy when provided; otherwise preserve legacy Slice limits.

    Caller owns the source and closes this reader before releasing its fences.
    """
    limits = (policy.limits if policy is not None else artifact_io.DecodeLimits(
        max_decode_bytes, max_decode_bytes, max_decode_bytes, max_decode_bytes))
    return _SliceOriginalStream(artifact_io.original_stream(
        stream, compressed=compressed, expected_bytes=expected_bytes,
        limits=limits, check=check, policy=policy, on_frame_complete=on_frame_complete))
