"""DEC-023 stage 3 + stage-2 watchdog: compress runs in a MONITORED child; crash/hang → raw fallback.

Real subprocess round-trips prove the plumbing (whole + stream codecs, canary pass/fail); the
crash/stall/error/stop branches are driven by mocking `_run_monitored` (a real SIGABRT / hang can't
be summoned deterministically — the ZipNN double-free is data-specific).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from unittest import mock

from modelark import compress, fetch

NEVER = (lambda: False)


def _shard(dirpath: Path, name: str, n: int = 2_000_000) -> tuple[Path, str]:
    # bf16-like: noisy mantissa byte plus a compressible exponent byte. Fully random bytes are
    # intentionally routed to raw by the DEC-045 no-expansion cap.
    data = bytearray(n)
    data[0::2] = os.urandom(n // 2)
    data[1::2] = b"\x3f" * (n // 2)
    data = bytes(data)
    p = dirpath / name
    p.write_bytes(data)
    return p, hashlib.sha256(data).hexdigest()


def test_ok_whole(tmp_path):
    local, sha = _shard(tmp_path, "model-00001-of-00001.safetensors")
    res = fetch._compress_isolated(local, "bfloat16", compress.CODEC_WHOLE, 1, sha, NEVER)
    assert res["status"] == "ok", res
    assert Path(res["znn_path"]).exists() and res["znn_sha256"] and res["stored_bytes"] > 0
    back = compress.decompress_file(res["znn_path"], tmp_path / "back.bin")
    assert hashlib.sha256(back.read_bytes()).hexdigest() == sha    # child's canary certified this


def test_ok_stream(tmp_path):
    local, sha = _shard(tmp_path, "model-00001-of-00002.safetensors")
    res = fetch._compress_isolated(local, "bfloat16", compress.CODEC_STREAM, 1, sha, NEVER)
    assert res["status"] == "ok", res
    assert Path(res["znn_path"]).exists()


def test_canary_fail_is_reported(tmp_path):
    local, _ = _shard(tmp_path, "m.safetensors")
    res = fetch._compress_isolated(local, "bfloat16", compress.CODEC_WHOLE, 1, "0" * 64, NEVER)
    assert res["status"] == "canary", res
    assert not (tmp_path / "m.safetensors.znn").exists()          # child removed the uncertified .znn


def test_incompressible_output_cap_falls_back_before_publish(tmp_path):
    local = tmp_path / "dense.safetensors"
    local.write_bytes(os.urandom(2_000_000))
    sha = hashlib.sha256(local.read_bytes()).hexdigest()
    res = fetch._compress_isolated(
        local, "bfloat16", compress.CODEC_WHOLE, 1, sha, NEVER
    )
    assert res["status"] == "over-cap", res
    assert local.exists(), "raw source must survive output-cap fallback"
    assert not (tmp_path / "dense.safetensors.znn").exists()


def test_whole_cap_is_checked_before_first_write(tmp_path):
    src = tmp_path / "whole.bin"
    dst = tmp_path / "whole.znn"
    src.write_bytes(b"1234")
    fake_zipnn = mock.Mock()
    fake_zipnn.compress.return_value = b"12345"
    with mock.patch("modelark.compress._zipnn", return_value=fake_zipnn), \
         mock.patch("modelark.compress._atomic_write_bytes") as write:
        try:
            compress.compress_file(src, dst, codec=compress.CODEC_WHOLE)
            raise AssertionError("expanded whole output must hit the cap")
        except compress.OutputCapExceeded:
            pass
    write.assert_not_called()
    assert not dst.exists()


def test_zstd_cap_is_checked_before_next_write(tmp_path):
    src = tmp_path / "zstd.bin"
    dst = tmp_path / "zstd.znn"
    src.write_bytes(b"1234")
    compressor = mock.Mock()
    compressor.compress.return_value = b"x" * 100
    factory = mock.Mock()
    factory.compressobj.return_value = compressor
    zstd = mock.Mock()
    zstd.ZstdCompressor.return_value = factory
    with mock.patch("modelark.compress._zstd", return_value=zstd):
        try:
            compress._compress_zstd(src, dst, threads=1, output_cap=4)
            raise AssertionError("expanded zstd output must hit the cap")
        except compress.OutputCapExceeded:
            pass
    assert not dst.exists()
    assert not list(tmp_path.glob("zstd.znn.*.tmp"))


def test_crash_falls_back(tmp_path):
    local, sha = _shard(tmp_path, "bad.safetensors")
    dst = tmp_path / "bad.safetensors.znn"
    orphan = tmp_path / "bad.safetensors.znn.abcd1234.sznn.tmp"   # a half-written temp the crashed child leaves
    orphan.write_bytes(b"partial")
    dst.write_bytes(b"partial-znn")
    mon = {"outcome": "exited", "rc": -6, "stderr": "double free or corruption (!prev)"}   # -6 = SIGABRT
    with mock.patch("modelark.fetch._run_monitored", return_value=mon):
        res = fetch._compress_isolated(local, "bfloat16", compress.CODEC_STREAM, 1, sha, NEVER)
    assert res["status"] == "crash" and res["signal"] == 6, res
    assert not orphan.exists() and not dst.exists()               # swept the half-written temp + partial dst


def test_hang_falls_back(tmp_path):
    local, sha = _shard(tmp_path, "hung.safetensors")
    (tmp_path / "hung.safetensors.znn.zz.sznn.tmp").write_bytes(b"partial")
    mon = {"outcome": "stalled", "rc": None, "stderr": ""}
    with mock.patch("modelark.fetch._run_monitored", return_value=mon):
        res = fetch._compress_isolated(local, "bfloat16", compress.CODEC_STREAM, 1, sha, NEVER)
    assert res["status"] == "stalled", res
    assert not (tmp_path / "hung.safetensors.znn.zz.sznn.tmp").exists()


def test_error_surfaces(tmp_path):
    local, sha = _shard(tmp_path, "e.safetensors")
    mon = {"outcome": "exited", "rc": 1, "stderr": "ImportError: boom"}
    with mock.patch("modelark.fetch._run_monitored", return_value=mon):
        res = fetch._compress_isolated(local, "bfloat16", compress.CODEC_WHOLE, 1, sha, NEVER)
    assert res["status"] == "error" and res["returncode"] == 1, res


def test_stall_window_scales_with_shard_size(tmp_path):
    # INC-011: the child's canary (decompress+hash the whole shard to certify restore) READS the .znn,
    # so its temp stops growing and the watchdog goes blind — a fixed 300 s false-killed a 29 GB canary
    # (~13 min over iSCSI) → stored raw. The window must scale to the shard size.
    local = tmp_path / "giant-00001-of-00031.safetensors"
    with open(local, "wb") as f:
        f.truncate(30_000_000_000)                             # 30 GB sparse — logical size only, no disk cost
    captured = {}
    def capture(cmd, progress, stall_secs, should_stop, **_kw):
        captured["stall"] = stall_secs
        return {"outcome": "stalled", "rc": None, "stderr": ""}  # short-circuit; we only want the passed window
    with mock.patch("modelark.fetch._run_monitored", side_effect=capture):
        fetch._compress_isolated(local, "bfloat16", compress.CODEC_STREAM, 1, "0" * 64, NEVER)
    assert captured["stall"] == int(30 * fetch._COMPRESS_STALL_PER_GB), captured   # 30 GB → 30×60 = 1800 s
    assert captured["stall"] > fetch._COMPRESS_STALL_SECS, "big shard must exceed the small-shard floor"


def test_stall_window_floor_for_small_shard(tmp_path):
    local, sha = _shard(tmp_path, "small.safetensors")         # ~2 MB → scaled term ≈ 0, so the floor wins
    captured = {}
    def capture(cmd, progress, stall_secs, should_stop, **_kw):
        captured["stall"] = stall_secs
        return {"outcome": "stalled", "rc": None, "stderr": ""}
    with mock.patch("modelark.fetch._run_monitored", side_effect=capture):
        fetch._compress_isolated(local, "bfloat16", compress.CODEC_STREAM, 1, sha, NEVER)
    assert captured["stall"] == fetch._COMPRESS_STALL_SECS, captured


def test_stop_raises(tmp_path):
    local, sha = _shard(tmp_path, "s.safetensors")
    mon = {"outcome": "stopped", "rc": None, "stderr": ""}
    with mock.patch("modelark.fetch._run_monitored", return_value=mon):
        try:
            fetch._compress_isolated(local, "bfloat16", compress.CODEC_WHOLE, 1, sha, NEVER)
        except fetch._StopRequested:
            return
    raise AssertionError("expected _StopRequested on a stopped compress")


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(Path(tempfile.mkdtemp()))
            print(f"ok  {name}")
    print("all passed")
