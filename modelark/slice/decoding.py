"""Slice original-output adapter; archive authority stays in LocalArchiveReader."""
from __future__ import annotations

from modelark import artifact_io

from .transaction import TransferRefusal


class _SliceOriginalStream:
    def __init__(self, reader):
        self._reader = reader

    def read(self, size=-1):
        try:
            return self._reader.read(size)
        except artifact_io.DecodeError as exc:
            raise TransferRefusal(f"SOURCE_DECODE_{exc.kind}", exc.detail) from exc


def original_stream(stream, *, compressed, expected_bytes, max_decode_bytes=64 << 20,
                    check=lambda: None):
    """Preserve legacy Slice limits; caller retains source descriptor ownership.

    No new seal/resource policy or large-frame admission is enabled in Stage B.
    """
    limits = artifact_io.DecodeLimits(max_decode_bytes, max_decode_bytes,
                                      max_decode_bytes, max_decode_bytes)
    return _SliceOriginalStream(artifact_io.original_stream(
        stream, compressed=compressed, expected_bytes=expected_bytes,
        limits=limits, check=check))
