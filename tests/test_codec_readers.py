"""Production legacy callers: real guarded reads, compatibility and evidence."""
import hashlib
import os
import resource

import pytest

from modelark import compress, codec_supervisor as cs, streamznn
from modelark.codec_resources import CodecReadUnavailable, CodecResourceRefusal


@pytest.fixture(autouse=True)
def admit_transport(monkeypatch):
    # Synthetic parent sample only; every real child installs the actual AS guard.
    monkeypatch.setattr(cs, "available_memory", lambda: {"available_bytes": 10 << 30})


@pytest.mark.parametrize("codec", [compress.CODEC_WHOLE, compress.CODEC_STREAM, compress.CODEC_ZSTD])
@pytest.mark.parametrize("dtype", ["bfloat16", "float16", "float32"])
def test_actual_public_readers_original_identity_and_parent_limits(tmp_path, codec, dtype, monkeypatch):
    if codec == compress.CODEC_ZSTD:
        pytest.importorskip("zstandard")
    raw = b"\x00\x00\x80\x3f" * 100000
    src, encoded, dst = (tmp_path / name for name in ("source", "encoded", "restored"))
    src.write_bytes(raw)
    compress.compress_file(src, encoded, codec=codec, dtype=dtype, threads=1)
    before = resource.getrlimit(resource.RLIMIT_AS)
    # Native work must not run in the public caller (child imports are fresh).
    monkeypatch.setattr(compress, "_zipnn", lambda *a, **k: pytest.fail("native decode in parent"))
    assert compress.canary_ok(encoded, hashlib.sha256(raw).hexdigest())
    assert not compress.canary_ok(encoded, "0" * 64)
    assert compress.decompress_file(encoded, dst, dtype="float16") == dst
    assert dst.read_bytes() == raw and src.read_bytes() == raw
    assert list(tmp_path.glob("*.tmp")) == []
    assert resource.getrlimit(resource.RLIMIT_AS) == before


def test_empty_streamznn_and_malformed_hash_convention(tmp_path):
    encoded = tmp_path / "empty"
    encoded.write_bytes(streamznn.MAGIC)
    assert compress.canary_ok(encoded, hashlib.sha256(b"").hexdigest())
    assert compress.decompress_file(encoded, tmp_path / "out").read_bytes() == b""
    with pytest.raises(ValueError):
        compress.canary_ok(encoded, "bad")
    assert compress.canary_ok(encoded, "") is False


def test_legacy_zstd_concatenation_unknown_size_and_skippable_frame(tmp_path):
    zstd = pytest.importorskip("zstandard")
    compressor = zstd.ZstdCompressor(write_content_size=False)
    encoded = tmp_path / "zstd"
    # Starts with ordinary zstd magic, as required by the existing dispatcher.
    encoded.write_bytes(compressor.compress(b"abc") + bytes.fromhex("502a4d18")
                        + (3).to_bytes(4, "little") + b"skip"[:3] + compressor.compress(b"def"))
    assert compress.canary_ok(encoded, hashlib.sha256(b"abcdef").hexdigest())
    assert compress.decompress_file(encoded, tmp_path / "out").read_bytes() == b"abcdef"


def test_legacy_native_streaming_mode_not_narrowed_to_slice_allowlist(tmp_path):
    from zipnn import ZipNN
    raw = b"\x00\x3f" * 100000
    blob = bytes(ZipNN(input_format="byte", is_streaming=True, streaming_chunk=65536,
                      threads=1).compress(bytearray(raw)))
    encoded = tmp_path / "native-streaming"
    encoded.write_bytes(blob)
    assert compress.canary_ok(encoded, hashlib.sha256(raw).hexdigest())
    assert compress.decompress_file(encoded, tmp_path / "out").read_bytes() == raw
    # Same legacy frame wrapped in standalone SZNN must keep native acceptance.
    encoded.write_bytes(streamznn.MAGIC + len(blob).to_bytes(4, "little") + blob)
    assert compress.canary_ok(encoded, hashlib.sha256(raw).hexdigest())


def test_legacy_zstd_window_above_slice_limit_still_roundtrips(tmp_path):
    zstd = pytest.importorskip("zstandard")
    raw = bytes(65 << 20)
    params = zstd.ZstdCompressionParameters.from_level(3, window_log=27, write_content_size=0)
    blob = zstd.ZstdCompressor(compression_params=params).compress(raw)
    assert zstd.get_frame_parameters(blob).window_size > 64 << 20
    encoded = tmp_path / "large-window"
    encoded.write_bytes(blob)
    assert compress.canary_ok(encoded, hashlib.sha256(raw).hexdigest())
    assert compress.decompress_file(encoded, tmp_path / "out").stat().st_size == len(raw)
    assert compress.sha256_file(tmp_path / "out") == hashlib.sha256(raw).hexdigest()


def test_resource_refusal_before_output_mutation_or_child(tmp_path, monkeypatch):
    encoded, target = tmp_path / "input", tmp_path / "target"
    encoded.write_bytes(streamznn.MAGIC)
    target.write_bytes(b"existing")
    monkeypatch.setattr(cs, "available_memory", lambda: {"available_bytes": 0})
    monkeypatch.setattr(cs, "_launch", lambda *a: pytest.fail("spawned after refusal"))
    with pytest.raises(CodecResourceRefusal):
        compress.decompress_file(encoded, target)
    assert target.read_bytes() == b"existing"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input", "target"]


def test_restore_resource_refusal_keeps_original_destination_and_cleans_temps(tmp_path, monkeypatch):
    from modelark import restore
    encoded, target = tmp_path / "input", tmp_path / "target"
    encoded.write_bytes(streamznn.MAGIC)
    target.write_bytes(b"existing")
    monkeypatch.setattr(cs, "available_memory", lambda: {"available_bytes": 0})
    with pytest.raises(restore.RestoreError, match="not a corruption verdict"):
        restore._materialize(encoded, target, {"compressed": True, "quant": "bf16"}, "0" * 64)
    assert target.read_bytes() == b"existing"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["input", "target"]


def test_actual_corruption_is_not_resource_unavailability(tmp_path):
    encoded = tmp_path / "input"
    encoded.write_bytes(streamznn.MAGIC + (4096).to_bytes(4, "little") + b"truncated")
    with pytest.raises(streamznn.StreamZnnError):
        compress.canary_ok(encoded, "0" * 64)


def test_spawn_failure_is_unavailable_not_corruption(tmp_path, monkeypatch):
    encoded = tmp_path / "input"
    encoded.write_bytes(streamznn.MAGIC)
    def fail(*a):
        raise OSError("process resource unavailable")
    monkeypatch.setattr(cs, "_launch", fail)
    with pytest.raises(CodecReadUnavailable):
        compress.canary_ok(encoded, "0" * 64)


@pytest.mark.parametrize("kind,exception", [("RESOURCE", CodecResourceRefusal),
    ("WORKER_FAILED", CodecReadUnavailable), ("UNSUPPORTED", CodecReadUnavailable),
    ("INVALID", CodecReadUnavailable), ("CODEC_INVALID", streamznn.StreamZnnError)])
def test_failure_after_partial_output_keeps_destination(tmp_path, monkeypatch, kind, exception):
    from contextlib import contextmanager
    encoded, target = tmp_path / "input", tmp_path / "target"
    encoded.write_bytes(streamznn.MAGIC)
    target.write_bytes(b"existing")
    @contextmanager
    def fail(*a, **k):
        def parts():
            yield b"partial"
            raise cs.WorkerRefusal(kind, "test refusal")
        yield parts()
    monkeypatch.setattr(cs, "guarded_legacy_stream", fail)
    with pytest.raises(exception):
        compress.decompress_file(encoded, target)
    assert target.read_bytes() == b"existing"
    assert not list(tmp_path.glob("*.tmp"))


def test_actual_legacy_child_metadata_and_descriptor_boundary(tmp_path, monkeypatch):
    encoded = tmp_path / "encoded"
    encoded.write_bytes(streamznn.MAGIC)
    launched = []
    real = cs._launch
    def launch(request, output):
        assert set(request) == {"version", "policy", "stored_bytes", "dtype"}
        assert output > 2
        proc = real(request, output)
        launched.append(proc)
        return proc
    monkeypatch.setattr(cs, "_launch", launch)
    records = []
    with encoded.open("rb") as source:
        with cs.guarded_legacy_stream(source, stored_bytes=encoded.stat().st_size,
                                     on_complete=records.append) as chunks:
            assert list(chunks) == []
    assert records[0]["worker"]["address_space_bytes"] == [8 << 30] * 2
    assert records[0]["worker"]["parent_death_signal"] == 9
    assert len(launched) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(launched[0].pid, os.WNOHANG)
