"""Stage B: real writer compatibility, neutral boundaries, and legacy Slice policy."""
import ast
import hashlib
import io
from pathlib import Path
import struct

import pytest

from modelark import artifact_io as aio, compress, streamznn as sz
from modelark.slice.decoding import original_stream as slice_stream
from modelark.slice.transaction import TransferRefusal


class ShortSource:
    """No seek/fileno/path; check every bounded input request."""

    def __init__(self, data, *, piece=3, ceiling=4096):
        self.buffer = io.BytesIO(data)
        self.piece, self.ceiling = piece, ceiling
        self.requests = []

    def read(self, size):
        assert 0 < size <= self.ceiling
        self.requests.append(size)
        return self.buffer.read(min(size, self.piece))


def drain(reader, size=17):
    return b"".join(iter(lambda: reader.read(size), b""))


def frame(data):
    return bytes(sz._zipnn(dtype="bfloat16", threads=1).compress(bytearray(data)))


def container(*frames):
    return sz.MAGIC + b"".join(struct.pack("<I", len(blob)) + blob for blob in frames)


def limits(stored=4096, decoded=4096, read=17, window=1 << 20):
    return aio.DecodeLimits(stored, decoded, read, window)


@pytest.mark.parametrize("codec", [compress.CODEC_RAW, compress.CODEC_WHOLE, compress.CODEC_STREAM])
@pytest.mark.parametrize("dtype", ["bfloat16", "float16", "float32"])
def test_real_writer_short_nonseekable_roundtrip(codec, dtype, tmp_path):
    original = b"\0\x3f" * 512
    src, stored, restored = (tmp_path / name for name in ("in", "misleading.blob", "out"))
    src.write_bytes(original)
    if codec == compress.CODEC_RAW:
        stored.write_bytes(original)
    else:
        if codec == compress.CODEC_STREAM:
            sz.compress_file(src, stored, dtype=dtype, threads=1, chunk_bytes=257)
        else:
            compress.compress_file(src, stored, codec=codec, dtype=dtype, threads=1)
        assert compress.canary_ok(stored, hashlib.sha256(original).hexdigest())
        compress.decompress_file(stored, restored)
        assert restored.read_bytes() == original
    source = ShortSource(stored.read_bytes())
    reader = aio.original_stream(source, compressed=codec != compress.CODEC_RAW,
                                 expected_bytes=len(original), limits=limits())
    assert drain(reader) == original
    assert max(source.requests) <= 17
    assert not source.buffer.closed
    assert src.read_bytes() == original
    # The Slice adapter must use the same mechanism without new policy/seal behavior.
    assert drain(slice_stream(ShortSource(stored.read_bytes()),
                              compressed=codec != compress.CODEC_RAW,
                              expected_bytes=len(original), max_decode_bytes=4096)) == original


def test_separate_encoded_decoded_and_consumer_bounds():
    original = bytes(range(16))
    blob = frame(original)
    assert len(blob) > len(original)  # framing overhead is distinct from original length
    def reader(bound):
        return aio.original_stream(ShortSource(blob, ceiling=2), compressed=True,
                                   expected_bytes=len(original), limits=bound)
    assert drain(reader(limits(len(blob), len(original), 2)), 2) == original
    for bound in (limits(len(blob) - 1, len(original), 2),
                  limits(len(blob), len(original) - 1, 2)):
        with pytest.raises(aio.DecodeError, match="LIMIT"):
            drain(reader(bound), 2)
    with pytest.raises(aio.DecodeError, match="LIMIT"):
        reader(limits(len(blob), len(original), 2)).read(3)


@pytest.mark.parametrize("field,value,error", [(16, 4097, "LIMIT"), (24, 4097, "LIMIT"),
                                               (16, 0, "INVALID"), (24, 31, "INVALID")])
def test_frame_size_checks_precede_native(field, value, error, monkeypatch):
    blob = bytearray(frame(bytes(128)))
    blob[field:field + 8] = value.to_bytes(8, "little")
    monkeypatch.setattr("zipnn.ZipNN.decompress", lambda *a, **k: pytest.fail("native reached"))
    with pytest.raises(aio.DecodeError, match=error):
        drain(aio.original_stream(ShortSource(blob), compressed=True,
                                  expected_bytes=128, limits=limits()))


@pytest.mark.parametrize("field,value", [(3, 6), (5, 0), (6, 2), (7, 2), (8, 2),
                                        (9, 1), (10, 1), (11, 1), (12, 1), (13, 1), (15, 3)])
def test_unsupported_byte_modes_never_reach_native(field, value, monkeypatch):
    blob = bytearray(frame(bytes(128)))
    blob[field] = value
    monkeypatch.setattr("zipnn.ZipNN.decompress", lambda *a, **k: pytest.fail("native reached"))
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_UNSUPPORTED"):
        drain(slice_stream(ShortSource(blob), compressed=True, expected_bytes=128,
                           max_decode_bytes=4096))


@pytest.mark.parametrize("transform", [lambda b: b[:-1], lambda b: b + b"x",
                                       lambda b: container(b) + b"xx"])
def test_corrupt_frame_and_trailing_content(transform):
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_INVALID"):
        drain(slice_stream(ShortSource(transform(frame(bytes(128)))), compressed=True,
                           expected_bytes=128, max_decode_bytes=4096))


def test_stream_later_frame_over_total_refuses_before_second_native(monkeypatch):
    blob = frame(bytes(128))
    real = sz.read_zipnn_frame
    decoded = []
    def counted(*a, **kw):
        value = real(*a, **kw)
        decoded.append(value)
        return value
    monkeypatch.setattr(sz, "read_zipnn_frame", counted)
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_LIMIT"):
        drain(slice_stream(ShortSource(container(blob, blob)), compressed=True,
                           expected_bytes=128, max_decode_bytes=4096))
    assert len(decoded) == 1


@pytest.mark.parametrize("compressed,data,expected", [(False, b"abc", 4),
                                                     (False, b"abc", 2),
                                                     (True, sz.MAGIC, 1)])
def test_total_length_mismatch(compressed, data, expected):
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE"):
        drain(slice_stream(ShortSource(data), compressed=compressed, expected_bytes=expected,
                           max_decode_bytes=4096))


@pytest.mark.parametrize("where", ["source", "check"])
def test_source_and_cancellation_exceptions_keep_identity(where):
    problem = OSError("source changed") if where == "source" else TransferRefusal("STOP_REQUESTED")
    events = []
    class Source:
        def read(self, size):
            events.append("read")
            if where == "source":
                raise problem
            return b"abc"
    def check():
        events.append("check")
        if where == "check" and "read" in events:
            raise problem
    reader = slice_stream(Source(), compressed=False, expected_bytes=3, check=check)
    with pytest.raises(type(problem)) as caught:
        reader.read(3)
    assert caught.value is problem


def test_buffered_output_is_rechecked_without_new_source_read():
    source = ShortSource(frame(bytes(128)))
    stop = False
    def check():
        if stop:
            raise TransferRefusal("STOP_REQUESTED")
    reader = slice_stream(source, compressed=True, expected_bytes=128,
                          max_decode_bytes=4096, check=check)
    assert reader.read(1) == b"\0"
    reads = len(source.requests)
    stop = True
    with pytest.raises(TransferRefusal, match="STOP_REQUESTED"):
        reader.read(1)
    assert len(source.requests) == reads


def test_modules_keep_authority_and_standalone_boundaries():
    for module in (aio, sz):
        tree = ast.parse(Path(module.__file__).read_text())
        imports = [name.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                   for name in node.names]
        imports += [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any("slice" in name or "catalog" in name or "sqlite" in name
                       or "subprocess" in name for name in imports)
    assert not any(isinstance(node, ast.ImportFrom) and node.level
                   for node in ast.walk(ast.parse(Path(sz.__file__).read_text())))


def test_optional_zstd_short_reads_and_distinct_window_limit():
    zstd = pytest.importorskip("zstandard")
    original = bytes(10000)
    blob = zstd.ZstdCompressor(write_content_size=False).compress(original)
    def reader(window):
        return aio.original_stream(ShortSource(blob), compressed=True,
                                   expected_bytes=len(original),
                                   limits=limits(decoded=512 << 10, window=window))
    assert drain(reader(64 << 20)) == original
    with pytest.raises(aio.DecodeError, match="LIMIT"):
        drain(reader(1024))


def test_legacy_zstd_native_window_conversion_remains_a_known_limit():
    """Stage B preserves legacy admission; Stage C must qualify the units repair."""
    zstd = pytest.importorskip("zstandard")
    original = bytes(256 << 10)
    blob = zstd.ZstdCompressor(write_content_size=False).compress(original)
    assert zstd.get_frame_parameters(blob).window_size == len(original)
    # The dependency's native API accepts this bytes-valued ceiling. The existing
    # Slice /1024 conversion is narrower than its 64 MiB header check suggests.
    assert zstd.ZstdDecompressor(max_window_size=64 << 20).decompressobj().decompress(blob) == original
    with pytest.raises(TransferRefusal, match="SOURCE_DECODE_INVALID"):
        drain(slice_stream(ShortSource(blob), compressed=True, expected_bytes=len(original)))
