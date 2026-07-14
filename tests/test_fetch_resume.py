"""Crash resume is durable per archived file and per destination drive (DEC-019)."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

from modelark import archive_manifest, fetch


def _catalog():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE models(repo_id TEXT PRIMARY KEY, params_b REAL, status TEXT)")
    con.execute("CREATE TABLE files(repo_id TEXT, rfilename TEXT, size_bytes INT, sha256 TEXT, "
                "format TEXT, quant TEXT)")
    con.execute("CREATE TABLE archived(repo_id TEXT, rfilename TEXT, drive_label TEXT)")
    con.execute("INSERT INTO models VALUES ('org/model', 1.0, 'fetching')")
    con.execute("INSERT INTO files VALUES "
                "('org/model', 'weights/model.safetensors', 10, NULL, 'safetensors', 'bf16')")
    return con


def test_completed_file_is_not_downloaded_again(tmp_path):
    con = _catalog()
    con.execute("INSERT INTO archived VALUES ('org/model', 'weights/model.safetensors', 'drive-01')")
    ctx = fetch.RunCtx(con=con)
    with mock.patch.object(fetch, "_download_shard") as download:
        result = fetch.fetch_model(ctx, "org/model", tmp_path, "drive-01", False,
                                   {"max_compress_ram_gb": 4, "threads": 1})
    download.assert_not_called()
    assert result == {"repo_id": "org/model", "files": 0, "skipped": 1, "bytes": 0}
    assert con.execute("SELECT status FROM models").fetchone()[0] == "archived"
    con.close()


def test_completion_on_one_drive_does_not_skip_another_drive(tmp_path):
    con = _catalog()
    con.execute("INSERT INTO archived VALUES ('org/model', 'weights/model.safetensors', 'drive-01')")
    ctx = fetch.RunCtx(con=con)
    marker = RuntimeError("download attempted for new destination")
    with mock.patch.object(fetch, "_download_shard", side_effect=marker) as download:
        try:
            fetch.fetch_model(ctx, "org/model", Path(tmp_path), "drive-02", False,
                              {"max_compress_ram_gb": 4, "threads": 1})
            raise AssertionError("drive-02 must still receive its own copy")
        except RuntimeError as exc:
            assert exc is marker
    download.assert_called_once()
    con.close()


def test_explicit_task_manifest_never_broadens_after_restart(tmp_path):
    con = _catalog()
    con.execute(
        "INSERT INTO files VALUES "
        "('org/model', 'weights/second.safetensors', 20, NULL, 'safetensors', 'bf16')"
    )
    con.execute("INSERT INTO archived VALUES ('org/model', 'weights/model.safetensors', 'drive-01')")
    exact = (archive_manifest.ManifestFile(
        rfilename="weights/second.safetensors",
        size_bytes=20,
        sha256=None,
        format="safetensors",
        quant="bf16",
        storage_action="compress",
    ),)
    marker = RuntimeError("only the durable graph's missing file was attempted")
    ctx = fetch.RunCtx(con=con)
    with mock.patch.object(fetch, "_download_shard", side_effect=marker) as download:
        try:
            fetch.fetch_model(
                ctx, "org/model", Path(tmp_path), "drive-01", False,
                {"max_compress_ram_gb": 4, "threads": 1}, manifest=exact,
            )
            raise AssertionError("the exact missing file should be attempted")
        except RuntimeError as exc:
            assert exc is marker
    assert download.call_count == 1
    assert download.call_args.args[2] == "weights/second.safetensors"
    con.close()


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as td:
                fn(Path(td))
            print(f"ok  {name}")
    print("all passed")
