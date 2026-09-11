"""Caller-owned StreamZNN primitives preserve standalone wrappers and failures."""
import hashlib
import importlib.util
import io
import struct
from array import array

import pytest

from modelark import streamznn as sz


class ShortReader:
    def __init__(self, data, piece=1):
        self.inner = io.BytesIO(data)
        self.piece = piece
        self.requests = []

    def read(self, size):
        assert 0 < size <= 7
        self.requests.append(size)
        return self.inner.read(min(size, self.piece))


def fake_container(*frames):
    return sz.MAGIC + b"".join(struct.pack("<I", len(frame)) + frame for frame in frames)


def test_default_iterator_and_all_path_wrappers_share_loop(tmp_path, monkeypatch):
    blob = fake_container(b"abc", b"def")
    native_calls = []
    class Codec:
        def decompress(self, blob):
            native_calls.append(blob)
            return blob.upper()
    monkeypatch.setattr(sz, "_zipnn", lambda: Codec())
    source = ShortReader(blob)
    assert b"".join(sz.iter_decompress(source, read_size=7)) == b"ABCDEF"
    assert native_calls == [b"abc", b"def"]
    assert not source.inner.closed
    # Permissive legacy wrappers must NOT acquire Slice's strict frame parser.
    monkeypatch.setattr(sz, "read_zipnn_frame", lambda *a, **kw: pytest.fail("narrowed wrapper"))
    real_iterator = sz.iter_decompress
    calls = []
    def iterator(*a, **kw):
        calls.append(a[0])
        yield from real_iterator(*a, **kw)
    monkeypatch.setattr(sz, "iter_decompress", iterator)
    src, out = tmp_path / "stored", tmp_path / "restored"
    src.write_bytes(blob)
    parts = []
    sz.decompress_to(src, parts.append)
    assert b"".join(parts) == b"ABCDEF"
    assert sz.verify_sha256(src, hashlib.sha256(b"ABCDEF").hexdigest())
    sz.decompress_file(src, out)
    assert out.read_bytes() == b"ABCDEF"
    assert len(calls) == 3
    assert all(stream.closed for stream in calls)


def test_iterator_close_does_not_read_next_frame_or_close_input():
    source = ShortReader(fake_container(b"abc", b"def"))
    seen = []
    def reader(stream, length, remaining):
        seen.append((length, remaining))
        return sz._read_exact(stream, length, read_size=7)
    iterator = sz.iter_decompress(source, max_total_bytes=6, read_size=7, frame_reader=reader)
    assert next(iterator) == b"abc"
    consumed = source.inner.tell()
    iterator.close()
    assert source.inner.tell() == consumed
    assert seen == [(3, 6)]
    assert not source.inner.closed


@pytest.mark.parametrize("bound", [{"max_stored_frame_bytes": 2},
                                  {"max_decoded_frame_bytes": 2}, {"max_total_bytes": 2}])
def test_explicit_iterator_bounds(bound):
    source = ShortReader(fake_container(b"abc"))
    def reader(stream, length, remaining):
        return sz._read_exact(stream, length, read_size=7)
    with pytest.raises(sz.DecodeLimitExceeded):
        list(sz.iter_decompress(source, read_size=7, frame_reader=reader, **bound))
    assert not source.inner.closed


@pytest.mark.parametrize("data", [b"", b"S", b"XXXXX", sz.MAGIC + b"\0",
                                 sz.MAGIC + bytes(4), fake_container(b"abc")[:-1]])
def test_short_or_invalid_container(data, monkeypatch):
    monkeypatch.setattr(sz, "_zipnn", lambda: pytest.fail("truncation reached native"))
    with pytest.raises(sz.StreamZnnError):
        list(sz.iter_decompress(ShortReader(data), read_size=7))


@pytest.mark.parametrize("value", [False, -1, 0, 1.5, "7"])
def test_input_read_bound_is_strict(value):
    with pytest.raises(ValueError):
        list(sz.iter_decompress(io.BytesIO(sz.MAGIC), read_size=value))


def test_callback_and_source_failures_keep_identity(tmp_path, monkeypatch):
    problem = RuntimeError("codec callback failed")
    def fail(*args):
        raise problem
    source = ShortReader(fake_container(b"abc"))
    with pytest.raises(RuntimeError) as caught:
        list(sz.iter_decompress(source, read_size=7, frame_reader=fail))
    assert caught.value is problem
    assert not source.inner.closed
    class Broken:
        def read(self, size):
            raise problem
    with pytest.raises(RuntimeError) as caught:
        list(sz.iter_decompress(Broken()))
    assert caught.value is problem
    class Codec:
        decompress = staticmethod(fail)
    monkeypatch.setattr(sz, "_zipnn", lambda: Codec())
    src, dst = tmp_path / "stored", tmp_path / "out"
    src.write_bytes(fake_container(b"abc"))
    dst.write_bytes(b"keep")
    with pytest.raises(RuntimeError) as caught:
        sz.decompress_file(src, dst)
    assert caught.value is problem
    assert dst.read_bytes() == b"keep"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out", "stored"]


def test_sink_failure_closes_owned_path_handle(tmp_path, monkeypatch):
    src = tmp_path / "stored"
    src.write_bytes(sz.MAGIC)
    opened = []
    real_open = open
    def tracked_open(*a, **kw):
        stream = real_open(*a, **kw)
        opened.append(stream)
        return stream
    monkeypatch.setattr(sz, "open", tracked_open, raising=False)
    monkeypatch.setattr(sz, "iter_decompress", lambda stream: iter([b"data"]))
    problem = OSError("destination full")
    def sink(data):
        raise problem
    with pytest.raises(OSError) as caught:
        sz.decompress_to(src, sink)
    assert caught.value is problem
    assert opened[0].closed


def header(original=4, stored=36):
    value = bytearray(32)
    value[:4] = b"ZN\x00\x05"
    value[5], value[8], value[15] = 10, 1, 1
    value[16:24] = original.to_bytes(8, "little")
    value[24:32] = stored.to_bytes(8, "little")
    return bytes(value)


@pytest.mark.parametrize("dtype", [1, 2, 4, 5, 6])
def test_validated_frame_supported_byte_dtype_headers(dtype):
    data = bytearray(header() + b"abcd")
    data[15] = dtype
    source = ShortReader(data)
    assert sz.read_zipnn_frame(source, max_stored_bytes=36, max_decoded_bytes=4,
                               remaining_bytes=4, read_size=7,
                               decode_frame=lambda blob: blob[-4:]) == b"abcd"
    assert not source.inner.closed


def test_validated_frame_injection_and_actual_byte_count():
    # A typed memoryview's len counts elements, not bytes.
    data = header(original=1, stored=33) + b"a"
    with pytest.raises(sz.StreamZnnError, match="decoded size"):
        sz.read_zipnn_frame(io.BytesIO(data), max_stored_bytes=33, max_decoded_bytes=1,
                            remaining_bytes=1, decode_frame=lambda b: memoryview(array("I", [1])))
    problem = RuntimeError("cancel decoder")
    def fail(blob):
        raise problem
    with pytest.raises(RuntimeError) as caught:
        sz.read_zipnn_frame(io.BytesIO(data), max_stored_bytes=33, max_decoded_bytes=1,
                            remaining_bytes=1, decode_frame=fail)
    assert caught.value is problem


def test_standalone_import_by_file_without_modelark_package():
    spec = importlib.util.spec_from_file_location("standalone_streamznn", sz.__file__)
    standalone = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(standalone)
    assert list(standalone.iter_decompress(ShortReader(sz.MAGIC), read_size=7)) == []
    assert standalone.MAGIC == sz.MAGIC
