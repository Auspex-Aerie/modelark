"""D1 writer integration; synthetic files only, never a live archive or catalog."""
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys

import pytest

from modelark import archive_manifest, artifact_io, codec_inprocess, compress, compress_worker, fetch, streamznn
from modelark.core import db
from modelark.artifact_policy import qualified_policy
from modelark.codec_resources import CodecResourceRefusal


def source(tmp_path):
    path = tmp_path / "model.safetensors"
    path.write_bytes(b"\0\x3f" * 4096)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_parent_refuses_before_files_or_spawn(tmp_path, monkeypatch):
    path, digest = source(tmp_path)
    before = set(tmp_path.iterdir())
    limits = resource.getrlimit(resource.RLIMIT_AS)
    monkeypatch.setattr(fetch, "available_memory", lambda: {"available_bytes": 1})
    monkeypatch.setattr(fetch, "_run_monitored", lambda *a, **kw: pytest.fail("spawned"))
    result = fetch._compress_isolated(path, "bfloat16", compress.CODEC_WHOLE, 1, digest, lambda: False)
    assert result["status"] == "resource"
    assert set(tmp_path.iterdir()) == before
    assert compress.sha256_file(path) == digest
    assert resource.getrlimit(resource.RLIMIT_AS) == limits


def test_stop_precedes_resource_refusal(tmp_path, monkeypatch):
    path, digest = source(tmp_path)
    answers = iter([False, True])
    monkeypatch.setattr(fetch, "available_memory", lambda: {"available_bytes": 1})
    with pytest.raises(fetch._StopRequested):
        fetch._compress_isolated(path, "bfloat16", compress.CODEC_WHOLE, 1, digest, lambda: next(answers))


def test_unguarded_inline_adapter_never_decodes(monkeypatch):
    monkeypatch.setattr(streamznn, "read_zipnn_frame", lambda *a, **kw: pytest.fail("native reached"))
    with pytest.raises(CodecResourceRefusal, match="installed worker guard"):
        codec_inprocess.frame_adapter(qualified_policy())


@pytest.mark.parametrize("codec", [compress.CODEC_WHOLE, compress.CODEC_STREAM, compress.CODEC_ZSTD])
@pytest.mark.parametrize("dtype", ["bfloat16", "float16", "float32"])
def test_real_production_child_and_slice_agree(tmp_path, codec, dtype):
    if codec == compress.CODEC_ZSTD:
        pytest.importorskip("zstandard")
    path, digest = source(tmp_path)
    limits_before = resource.getrlimit(resource.RLIMIT_AS)
    result = fetch._compress_isolated(path, dtype, codec, 1, digest, lambda: False)
    assert result["status"] == "ok", result
    assert result["worker"]["address_space_bytes"] == [8 << 30] * 2
    assert result["worker"]["startup"] == "isolated-no-site-guard-then-site.v1"
    assert result["admission"]["available_bytes"] >= 10 << 30
    policy = qualified_policy()
    got = hashlib.sha256()
    with Path(result["znn_path"]).open("rb") as encoded, closing(artifact_io.original_stream(
            encoded, compressed=True, expected_bytes=path.stat().st_size,
            limits=policy.limits, policy=policy)) as reader:
        while chunk := reader.read(policy.limits.read_bytes):
            got.update(chunk)
    assert got.hexdigest() == digest == compress.sha256_file(path)
    assert resource.getrlimit(resource.RLIMIT_AS) == limits_before


@pytest.mark.parametrize("failure,status", [
    (CodecResourceRefusal("memory sample refused"), "resource_refused"),
    (MemoryError(), "resource_refused"),
    (artifact_io.DecodeError("LIMIT", "frame"), "decode_refused"),
    (artifact_io.DecodeError("UNSUPPORTED", "format"), "decode_refused"),
    (artifact_io.DecodeError("INVALID", "bad bytes"), None),
])
def test_worker_refusal_preserves_original_and_removes_uncertified_output(
        tmp_path, monkeypatch, failure, status):
    path, digest = source(tmp_path)
    dest = tmp_path / "encoded"
    monkeypatch.setattr(codec_inprocess, "require_guard", lambda p: None)
    def fake_compress(*a, **kw):
        dest.write_bytes(b"uncertified")
        return dest
    monkeypatch.setattr(compress, "compress_file", fake_compress)
    def fail(*a, **kw):
        raise failure
    monkeypatch.setattr(codec_inprocess, "verify_original", fail)
    result = compress_worker.run({"src": str(path), "dst": str(dest), "dtype": "bfloat16",
                                  "codec": compress.CODEC_WHOLE, "threads": 1,
                                  "expected_sha256": digest, "expected_bytes": path.stat().st_size},
                                 qualified_policy())
    assert result["ok"] is False
    if status:
        assert result[status] is True
    assert not dest.exists()
    assert compress.sha256_file(path) == digest


@pytest.mark.parametrize("flag,status", [("resource_refused", "resource"), ("decode_refused", "decode-refused")])
def test_parent_preserves_typed_worker_refusal(tmp_path, monkeypatch, flag, status):
    path, digest = source(tmp_path)
    def monitor(cmd, *a, **kw):
        assert cmd[1:3] == ["-I", "-S"]
        request = json.loads(cmd[-1])
        assert request["expected_bytes"] == path.stat().st_size
        assert request["decode_policy"] == qualified_policy().to_record()
        Path(request["result"]).write_text(json.dumps({"ok": False, flag: True, "detail": "refused"}))
        return {"outcome": "exited", "rc": 0, "stderr": ""}
    monkeypatch.setattr(fetch, "_run_monitored", monitor)
    result = fetch._compress_isolated(path, "bfloat16", compress.CODEC_WHOLE, 1, digest, lambda: False)
    assert result["status"] == status and result["stderr"] == ""
    assert not list(tmp_path.glob("*.result"))


def test_spawn_failure_cleans_result_file(tmp_path, monkeypatch):
    path, digest = source(tmp_path)
    def fail(*a, **kw):
        raise OSError("spawn")
    monkeypatch.setattr(fetch, "_run_monitored", fail)
    with pytest.raises(OSError, match="spawn"):
        fetch._compress_isolated(path, "bfloat16", compress.CODEC_WHOLE, 1, digest, lambda: False)
    assert not list(tmp_path.glob("*.result"))


def test_inline_uses_same_parser_without_nested_child(tmp_path, monkeypatch):
    path, digest = source(tmp_path)
    encoded = compress.compress_file(path, tmp_path / "encoded", threads=1)
    monkeypatch.setattr(codec_inprocess, "require_guard", lambda p: None)  # unit seam, no native guard claim
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("nested child"))
    assert codec_inprocess.verify_original(encoded, digest, path.stat().st_size, qualified_policy())
    assert not codec_inprocess.verify_original(encoded, "0" * 64, path.stat().st_size, qualified_policy())
    with pytest.raises(artifact_io.DecodeError):
        codec_inprocess.verify_original(encoded, digest, path.stat().st_size - 1, qualified_policy())


def test_frame_override_requires_explicit_policy():
    with pytest.raises(ValueError, match="explicit policy"):
        artifact_io.original_stream(io.BytesIO(), compressed=True, expected_bytes=0,
                                    limits=qualified_policy().limits, frame_stream=lambda: None)


@pytest.mark.parametrize("where", ["stop", "progress"])
def test_monitor_callback_failure_reaps_before_return(tmp_path, monkeypatch, where):
    real_popen = subprocess.Popen
    children = []
    def spawn(*a, **kw):
        proc = real_popen(*a, **kw)
        children.append(proc)
        return proc
    def fail():
        raise RuntimeError("callback failed")
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(fetch, "_MONITOR_POLL", .01)
    try:
        with pytest.raises(RuntimeError, match="callback failed"):
            fetch._run_monitored([sys.executable, "-c", "import signal; signal.pause()"],
                                 fail if where == "progress" else lambda: 0, 300,
                                 fail if where == "stop" else lambda: False)
        assert len(children) == 1 and children[0].poll() is not None
    finally:
        for proc in children:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


@pytest.mark.parametrize("status", ["resource", "decode-refused"])
def test_ingestion_publishes_verified_raw_after_refusal(tmp_path, monkeypatch, status):
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute("PRAGMA foreign_keys=ON")
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute(f"PRAGMA user_version={db._SCHEMA_VERSION}")
    repo, name, drive = "org/model", "model.safetensors", "drive-00"
    data = b"downloaded original"
    digest = hashlib.sha256(data).hexdigest()
    con.execute("INSERT INTO models(repo_id,status) VALUES(?,?)", [repo, "fetching"])
    con.execute("INSERT INTO drives(drive_label) VALUES(?)", [drive])
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) VALUES(?,?,?,?,?,?)",
                [repo, name, len(data), digest, "safetensors", "bf16"])
    manifest = (archive_manifest.ManifestFile(
        rfilename=name, size_bytes=len(data), sha256=digest, format="safetensors",
        quant="bf16", storage_action="compress"),)
    def download(_ctx, _repo, filename, directory, _base, **kw):
        path = directory / filename
        path.write_bytes(data)
        return path
    monkeypatch.setattr(fetch, "_download_shard", download)
    monkeypatch.setattr(compress, "plan_codec", lambda *a: compress.CODEC_WHOLE)
    monkeypatch.setattr(fetch, "_compress_isolated",
                        lambda *a, **kw: {"status": status, "detail": "refused", "stderr": ""})
    archive = tmp_path / "archive"
    try:
        fetch.fetch_model(fetch.RunCtx(con=con), repo, archive, drive, False,
                          {"max_compress_ram_gb": 4, "threads": 1}, manifest=manifest)
        row = con.execute("SELECT compressed,orig_sha256,stored_relpath FROM archived").fetchone()
        assert row[:2] == (0, digest)
        assert (archive / repo / row[2]).read_bytes() == data
        assert not list(archive.rglob("*.znn"))
    finally:
        con.close()
