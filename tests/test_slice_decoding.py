"""Bounded original-byte decoding; no whole-file scratch or unsafe header allocation."""
import io
import struct

import pytest

from modelark.slice.transaction import TransferRefusal


def decode(blob, *, expected, compressed=True, limit=64 << 20):
    from modelark.slice.decoding import original_stream
    stream = original_stream(io.BytesIO(blob), compressed=compressed,
                             expected_bytes=expected, max_decode_bytes=limit)
    pieces = []
    while piece := stream.read(17):
        pieces.append(piece)
    return b"".join(pieces)


def znn(data):
    from modelark.streamznn import _zipnn
    return bytes(_zipnn(dtype="bfloat16", threads=1).compress(bytearray(data)))


@pytest.mark.parametrize("kind", ["raw", "whole", "stream", "zstd"])
def test_actual_original_bytes(kind):
    data = bytes(range(128)) * 16
    if kind == "raw":
        blob = data
    elif kind == "whole":
        blob = znn(data)
    elif kind == "stream":
        chunks = [znn(data[:1024]), znn(data[1024:])]
        blob = b"SZNN\x01" + b"".join(struct.pack("<I", len(c)) + c for c in chunks)
    else:
        zstandard = pytest.importorskip("zstandard")
        blob = zstandard.ZstdCompressor().compress(data)
    assert decode(blob, expected=len(data), compressed=kind != "raw") == data


@pytest.mark.parametrize("field,value", [(16, 1 << 45), (24, 1 << 45)])
def test_huge_zipnn_header_refused_before_native_decoder(field, value, monkeypatch):
    blob = bytearray(znn(bytes(128)))
    blob[field:field + 8] = value.to_bytes(8, "little")
    monkeypatch.setattr("zipnn.ZipNN.decompress", lambda *a, **k: pytest.fail("unsafe decode"))
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_LIMIT"):
        decode(blob, expected=128)


def test_huge_stream_frame_refused_without_reading_payload():
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_LIMIT"):
        decode(b"SZNN\x01" + struct.pack("<I", 0xffffffff), expected=128)


@pytest.mark.parametrize("kind", ["whole", "stream", "zstd"])
def test_truncation_is_not_silent(kind):
    if kind == "zstd":
        zstandard = pytest.importorskip("zstandard")
        blob = zstandard.ZstdCompressor().compress(bytes(1024))
    else:
        blob = znn(bytes(1024))
        if kind == "stream":
            blob = b"SZNN\x01" + struct.pack("<I", len(blob)) + blob
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE"):
        decode(blob[:-1], expected=1024)


def test_unknown_codec_and_nonbyte_zipnn_refused():
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_UNSUPPORTED"):
        decode(b"not compressed", expected=14)
    blob = bytearray(znn(bytes(128)))
    blob[8] = 2
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_UNSUPPORTED"):
        decode(blob, expected=128)


def test_decoded_total_cannot_exceed_expected():
    zstandard = pytest.importorskip("zstandard")
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_LIMIT"):
        decode(zstandard.ZstdCompressor().compress(bytes(1024)), expected=12)


def test_read_requires_explicit_bounded_request():
    from modelark.slice.decoding import original_stream
    stream = original_stream(io.BytesIO(b"abc"), compressed=False, expected_bytes=3)
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_LIMIT"):
        stream.read()


@pytest.mark.parametrize("dtype", ["float32", "float16", "bfloat16"])
def test_byte_dtype_is_read_from_validated_header(dtype):
    from modelark.streamznn import _zipnn
    data = bytes(range(128)) * 8
    blob = bytes(_zipnn(dtype=dtype, threads=1).compress(bytearray(data)))
    assert decode(blob, expected=len(data)) == data


def test_zstd_unknown_content_size_and_trailing_data():
    zstandard = pytest.importorskip("zstandard")
    data = bytes(10000)
    blob = zstandard.ZstdCompressor(write_content_size=False).compress(data)
    assert decode(blob, expected=len(data)) == data
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_INVALID"):
        decode(blob + b"trailing", expected=len(data))


def test_zstd_window_bound_precedes_decoder_allocation(monkeypatch):
    zstandard = pytest.importorskip("zstandard")
    blob = zstandard.ZstdCompressor().compress(bytes(1 << 20))
    monkeypatch.setattr(zstandard, "ZstdDecompressor", lambda **k: pytest.fail("oversize window"))
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_LIMIT"):
        decode(blob, expected=1 << 20, limit=128 << 10)
