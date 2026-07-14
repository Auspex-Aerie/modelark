"""Byte-identity + safety proof for StreamZNN (modelark/streamznn.py) and the compress.py adapter.

Runs standalone (`python tests/test_streamznn.py`) or under pytest. The core promise this guards:
a restore is byte-for-byte identical to the original, the canary proves it, and corruption never
passes silently. Uses a real bf16 weight sample (tests/fixtures/streamznn_golden.bin) plus
synthetic edge sizes. No third-party test deps.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from unittest import mock

from zipnn import ZipNN

from modelark import streamznn
from modelark import compress

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "streamznn_golden.bin"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _synth_golden(n: int = 4_000_000) -> bytes:
    """Deterministic bf16-like bytes (compressible high-byte plane + noisy low-byte plane), used
    when the real sampled fixture is absent (fresh clone / CI) so the suite runs without committing
    anyone's tensors. DEF: replace with a golden sampled from our own ParameterGolfLLM."""
    import random
    rnd = random.Random(0xA55E)
    out = bytearray(n)
    out[0::2] = rnd.randbytes(n // 2)                                  # mantissa-ish low bytes: incompressible
    out[1::2] = bytes((i // 4096) & 0xFF for i in range(n // 2))       # exponent-ish high bytes: compressible
    return bytes(out)


def _golden() -> bytes:
    """The real sampled fixture if present locally; otherwise a deterministic synthetic one (cached,
    gitignored) so the suite is self-contained without shipping model weights."""
    if _FIXTURE.exists():
        return _FIXTURE.read_bytes()
    _FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    data = _synth_golden()
    _FIXTURE.write_bytes(data)
    return data


def _roundtrip(data: bytes, chunk_bytes: int, tmp: Path) -> bytes:
    """compress → decompress_file, returning the restored bytes read back off disk."""
    src = tmp / "in.bin"
    znn = tmp / "in.znn"
    out = tmp / "out.bin"
    src.write_bytes(data)
    streamznn.compress_file(src, znn, dtype="bfloat16", chunk_bytes=chunk_bytes)
    assert streamznn.is_container(znn), "compress_file must produce a StreamZNN container"
    streamznn.decompress_file(znn, out)
    return out.read_bytes()


def test_roundtrip_byte_identical_across_chunk_sizes() -> None:
    """Real 4 MB sample must round-trip byte-identical at chunk sizes that force 1 slice, many
    slices, and non-aligned boundaries."""
    data = _golden()
    want = _sha(data)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        for chunk in (64 * 1024, 1_000_003, len(data), len(data) + 1, streamznn.DEFAULT_CHUNK):
            restored = _roundtrip(data, chunk, tmp)
            assert restored == data, f"restore differs at chunk={chunk}"
            assert _sha(restored) == want, f"sha differs at chunk={chunk}"


def test_edge_sizes() -> None:
    """Empty, 1-byte, and exactly-at/around the chunk boundary must all round-trip identically."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        chunk = 4096
        for n in (0, 1, chunk - 1, chunk, chunk + 1, 3 * chunk, 3 * chunk + 7):
            data = os.urandom(n)
            restored = _roundtrip(data, chunk, tmp)
            assert restored == data, f"edge size {n} differs"


def test_canary_matches_and_rejects() -> None:
    """verify_sha256 is True for the real hash, False for a wrong one — and its streamed hash
    equals the hash of the actually-restored file (canary path == restore path)."""
    data = _golden()
    want = _sha(data)
    wrong = _sha(data + b"\x00")
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src, znn, out = tmp / "i", tmp / "i.znn", tmp / "o"
        src.write_bytes(data)
        streamznn.compress_file(src, znn, chunk_bytes=64 * 1024)
        assert streamznn.verify_sha256(znn, want) is True
        assert streamznn.verify_sha256(znn, wrong) is False
        streamznn.decompress_file(znn, out)
        assert _sha(out.read_bytes()) == want            # the file the canary "approved" restores to `want`


def test_corruption_fails_loud() -> None:
    """Truncation, a bad magic, and a flipped payload byte must each fail loudly or as a clean
    mismatch — never a silent wrong-bytes 'pass'."""
    data = _golden()
    want = _sha(data)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src, znn = tmp / "i", tmp / "i.znn"
        src.write_bytes(data)
        streamznn.compress_file(src, znn, chunk_bytes=64 * 1024)
        blob = znn.read_bytes()

        # (a) truncated container → StreamZnnError
        trunc = tmp / "trunc.znn"
        trunc.write_bytes(blob[:-64])
        try:
            streamznn.verify_sha256(trunc, want)
            raised = False
        except streamznn.StreamZnnError:
            raised = True
        assert raised, "truncated container must raise StreamZnnError"

        # (b) bad magic → StreamZnnError
        badmagic = tmp / "bad.znn"
        badmagic.write_bytes(b"XXXX\x00" + blob[5:])
        try:
            streamznn.verify_sha256(badmagic, want)
            raised = False
        except streamznn.StreamZnnError:
            raised = True
        assert raised, "bad magic must raise StreamZnnError"

        # (c) a flipped payload byte → NOT a silent identical pass (raises OR verifies False)
        ba = bytearray(blob)
        ba[len(ba) // 2] ^= 0xFF
        flipped = tmp / "flip.znn"
        flipped.write_bytes(bytes(ba))
        silently_passed = False
        try:
            silently_passed = streamznn.verify_sha256(flipped, want) is True
        except Exception:
            silently_passed = False                      # any raise is an acceptable loud failure
        assert not silently_passed, "a flipped byte must never verify as the original"


def test_is_container_discriminates() -> None:
    """is_container True for a StreamZNN file, False for a bare ZipNN blob and for random bytes."""
    data = _golden()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src, znn = tmp / "i", tmp / "i.znn"
        src.write_bytes(data)
        streamznn.compress_file(src, znn, chunk_bytes=64 * 1024)
        assert streamznn.is_container(znn) is True

        legacy = tmp / "legacy.znn"
        legacy.write_bytes(bytes(ZipNN(input_format="byte", bytearray_dtype="bfloat16").compress(bytearray(data))))
        assert streamznn.is_container(legacy) is False

        rnd = tmp / "rand.bin"
        rnd.write_bytes(os.urandom(1024))
        assert streamznn.is_container(rnd) is False


def test_legacy_whole_blob_still_restores() -> None:
    """Already-archived .znn (pre-StreamZNN whole-blob ZipNN) must still canary + restore
    byte-identical through the compress.py adapter's fallback path."""
    data = _golden()
    want = _sha(data)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        legacy = tmp / "legacy.znn"
        legacy.write_bytes(bytes(ZipNN(input_format="byte", bytearray_dtype="bfloat16").compress(bytearray(data))))
        assert compress.canary_ok(legacy, want, dtype="bfloat16") is True
        out = tmp / "restored.bin"
        compress.decompress_file(legacy, out, dtype="bfloat16")
        assert out.read_bytes() == data
        assert _sha(out.read_bytes()) == want


def test_dtype_hint_cannot_corrupt_restore() -> None:
    """Self-describing: compress bf16, restore through compress.py with a WRONG dtype hint —
    still byte-identical (StreamZNN path ignores the hint entirely)."""
    data = _golden()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src, znn, out = tmp / "i", tmp / "i.znn", tmp / "o"
        src.write_bytes(data)
        compress.compress_file(src, znn, dtype="bfloat16")
        compress.decompress_file(znn, out, dtype="float32")   # deliberately wrong hint
        assert out.read_bytes() == data


def test_atomic_leaves_no_tmp() -> None:
    """A successful compress publishes only the destination — no leftover temp files."""
    data = _golden()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src, znn = tmp / "i", tmp / "i.znn"
        src.write_bytes(data)
        streamznn.compress_file(src, znn, chunk_bytes=64 * 1024)
        leftovers = [p.name for p in tmp.iterdir() if p.name not in {"i", "i.znn"}]
        assert leftovers == [], f"unexpected leftover files: {leftovers}"


def test_source_file_untouched() -> None:
    """compress_file must leave the SOURCE file byte-identical. ZipNN reorders its input buffer
    in place, so this guards that compress_file only ever feeds it single-use chunks — the
    downloaded shard survives intact until the canary lets fetch drop it."""
    data = _golden()
    before = _sha(data)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src, znn = tmp / "i", tmp / "i.znn"
        src.write_bytes(data)
        streamznn.compress_file(src, znn, chunk_bytes=64 * 1024)
        assert _sha(src.read_bytes()) == before, "compress_file must not mutate the source file"


def test_stream_output_cap_rejects_expansion_before_frame_write() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src, dst = tmp / "i", tmp / "i.znn"
        src.write_bytes(b"1234")
        compressor = mock.Mock()
        compressor.compress.return_value = b"12345"
        with mock.patch("modelark.streamznn.ZipNN", return_value=compressor):
            try:
                streamznn.compress_file(
                    src, dst, chunk_bytes=4,
                    max_output_bytes=4 + len(streamznn.MAGIC) + 4,
                )
                raise AssertionError("expanded stream chunk must hit the cap")
            except streamznn.OutputCapExceeded:
                pass
        assert not dst.exists()
        assert not list(tmp.glob("i.znn.*.tmp"))


def test_plan_codec_gate() -> None:
    """The DEC-022 gate: under budget → whole-file; over budget → stream (or zstd/raw when stream off)."""
    cfg = {"max_compress_ram_gb": 4.0, "stream_compress": True, "threads": 4}
    assert compress.plan_codec(500 * 1024**2, cfg) == compress.CODEC_WHOLE    # 0.5GB × 4 = 2GB ≤ 4GB
    assert compress.plan_codec(2 * 1024**3, cfg) == compress.CODEC_STREAM     # 2GB × 4 = 8GB > 4GB, stream on
    off = dict(cfg, stream_compress=False)
    want_off = compress.CODEC_ZSTD if compress._zstd() is not None else compress.CODEC_RAW
    assert compress.plan_codec(2 * 1024**3, off) == want_off                  # stream off → zstd if installed, else raw


def test_whole_codec_roundtrip() -> None:
    """The in-memory whole-file codec round-trips byte-identical, canaries, and leaves the source
    untouched (fresh-read + atomic write) — and it is NOT a StreamZNN container."""
    data = _golden()
    want = _sha(data)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src, znn, out = tmp / "i", tmp / "i.znn", tmp / "o"
        src.write_bytes(data)
        compress.compress_file(src, znn, dtype="bfloat16", codec=compress.CODEC_WHOLE, threads=4)
        assert not streamznn.is_container(znn)              # whole = a bare ZipNN blob, not a StreamZNN container
        assert compress.canary_ok(znn, want, "bfloat16") is True
        compress.decompress_file(znn, out, "bfloat16")
        assert out.read_bytes() == data
        assert _sha(src.read_bytes()) == want               # source shard untouched


def _report_ratio() -> None:
    data = _golden()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        src = tmp / "i"
        src.write_bytes(data)
        for chunk in (256 * 1024, 4 * 1024 * 1024, streamznn.DEFAULT_CHUNK):
            znn = tmp / f"i_{chunk}.znn"
            streamznn.compress_file(src, znn, dtype="bfloat16", chunk_bytes=chunk)
            sz = znn.stat().st_size
            print(f"    chunk={chunk:>10}  ->  {sz:>9} bytes  ({100 * sz / len(data):.1f}% of {len(data)})")


def main() -> None:
    tests = [
        test_roundtrip_byte_identical_across_chunk_sizes,
        test_edge_sizes,
        test_canary_matches_and_rejects,
        test_corruption_fails_loud,
        test_is_container_discriminates,
        test_legacy_whole_blob_still_restores,
        test_dtype_hint_cannot_corrupt_restore,
        test_atomic_leaves_no_tmp,
        test_source_file_untouched,
        test_plan_codec_gate,
        test_whole_codec_roundtrip,
    ]
    for t in tests:
        t()
        print(f"  PASS  {t.__name__}")
    print("\n  ratio (StreamZNN framing overhead is negligible vs whole-file):")
    _report_ratio()
    print("\nALL STREAMZNN TESTS PASSED — restore is byte-identical, canary proves it, corruption fails loud.")


if __name__ == "__main__":
    main()
