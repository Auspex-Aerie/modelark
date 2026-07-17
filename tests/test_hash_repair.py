"""Legacy restore-hash repair and universal ingestion evidence (INC-017)."""
from __future__ import annotations

import hashlib
import io
import sqlite3
import subprocess
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from modelark import archive_manifest, cli, compress, fetch, hash_repair, restore
from modelark.core import db


def _catalog(path: Path | None = None):
    con = sqlite3.connect(str(path) if path else ":memory:", isolation_level=None)
    con.execute("PRAGMA foreign_keys=ON")
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute("PRAGMA user_version=2")
    return con


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(*args: str):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def _legacy_git_file(
    tmp_path: Path,
    *,
    name: str = ".gitattributes",
    data: bytes = b"*.bin filter=lfs diff=lfs\n",
):
    repo, drive = "org/model", "drive-00"
    archive = tmp_path / "archive"
    stored = archive / repo / name
    stored.parent.mkdir(parents=True)
    _run("git", "-C", str(archive), "init", "-q")
    _run("git", "-C", str(archive), "config", "user.name", "ModelArk Test")
    _run("git", "-C", str(archive), "config", "user.email", "test@modelark.invalid")
    stored.write_bytes(data)
    _run("git", "-C", str(archive), "add", "--", f"{repo}/{name}")
    _run("git", "-C", str(archive), "commit", "-qm", "archive fixture")
    return repo, name, drive, archive, stored, data


def _record_legacy_file(con, repo: str, name: str, drive: str, data: bytes, *, compressed=0):
    con.execute("INSERT INTO models(repo_id) VALUES(?)", [repo])
    con.execute("INSERT INTO drives(drive_label) VALUES(?)", [drive])
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) VALUES(?,?,?,?,?)",
        [repo, name, len(data), None, "aux"],
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,orig_bytes,stored_bytes,compressed,annex_key) VALUES(?,?,?,?,?,?,?,?,?,?)",
        [repo, name, Path(name).name, name, drive, None, len(data), len(data), compressed, None],
    )


def test_repair_validates_git_blob_backs_up_and_unblocks_restore(tmp_path):
    repo, name, drive, archive, _, data = _legacy_git_file(tmp_path)
    catalog = tmp_path / "catalog.sqlite"
    con = _catalog(catalog)
    _record_legacy_file(con, repo, name, drive, data)
    resolver = lambda _con, _label: archive

    dry = hash_repair.repair_hashes(con, [repo], archive_resolver=resolver)
    assert dry["mode"] == "dry-run" and dry["errors"] == []
    assert dry["repairs"] == [{
        "repo_id": repo, "rfilename": name, "drive_label": drive,
        "sha256": _sha(data), "bytes": len(data), "evidence": "archive-head-blob",
    }]
    assert con.execute("SELECT orig_sha256 FROM archived").fetchone()[0] is None
    assert not list(tmp_path.glob("catalog.sqlite.pre-hash-repair-*.bak"))

    result = hash_repair.repair_hashes(
        con, [repo], apply=True, archive_resolver=resolver
    )
    assert result["applied"] == 1
    backup = Path(result["backup"])
    assert backup.exists() and backup.parent == tmp_path
    assert backup.stat().st_mode & 0o777 == 0o600
    assert con.execute("SELECT orig_sha256 FROM archived").fetchone()[0] == _sha(data)
    backup_con = sqlite3.connect(backup)
    try:
        assert backup_con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup_con.execute("SELECT orig_sha256 FROM archived").fetchone()[0] is None
    finally:
        backup_con.close()

    with mock.patch.object(restore.register, "archive_path", return_value=archive):
        restored = restore.restore_repo(con, repo, tmp_path / "recovered")
    assert restored["n_files"] == 1
    assert (tmp_path / "recovered" / repo / name).read_bytes() == data

    repeated = hash_repair.repair_hashes(
        con, [repo], apply=True, archive_resolver=resolver
    )
    assert repeated["applied"] == 0 and repeated["backup"] is None
    assert len(list(tmp_path.glob("catalog.sqlite.pre-hash-repair-*.bak"))) == 1
    con.close()


def test_repair_preserves_nested_original_paths(tmp_path):
    nested = ".eval_results/gpqa.yaml"
    repo, name, drive, archive, _, data = _legacy_git_file(
        tmp_path, name=nested, data=b"score: 0.42\n"
    )
    con = _catalog(tmp_path / "catalog.sqlite")
    _record_legacy_file(con, repo, name, drive, data)
    result = hash_repair.repair_hashes(
        con, [repo], apply=True, archive_resolver=lambda _c, _d: archive
    )
    assert result["applied"] == 1
    with mock.patch.object(restore.register, "archive_path", return_value=archive):
        restore.restore_repo(con, repo, tmp_path / "recovered")
    assert (tmp_path / "recovered" / repo / nested).read_bytes() == data
    con.close()


def test_repair_refuses_modified_bytes_without_backup(tmp_path):
    repo, name, drive, archive, stored, data = _legacy_git_file(tmp_path)
    catalog = tmp_path / "catalog.sqlite"
    con = _catalog(catalog)
    _record_legacy_file(con, repo, name, drive, data)
    stored.write_bytes(b"tampered archive bytes\n")

    report = hash_repair.audit_hashes(con, [repo], archive_resolver=lambda _c, _d: archive)
    assert report["repairs"] == []
    assert report["errors"][0]["code"] == "UNPROVEN_BYTES"
    assert "do not match archive HEAD" in report["errors"][0]["detail"]
    try:
        hash_repair.repair_hashes(
            con, [repo], apply=True, archive_resolver=lambda _c, _d: archive
        )
        raise AssertionError("unproven bytes must refuse the entire repair")
    except hash_repair.HashRepairError as exc:
        assert "refusing hash repair" in str(exc)
    assert con.execute("SELECT orig_sha256 FROM archived").fetchone()[0] is None
    assert not list(tmp_path.glob("catalog.sqlite.pre-hash-repair-*.bak"))
    con.close()


def test_repair_reports_offline_and_compressed_candidates(tmp_path):
    repo, name, drive, archive, stored, data = _legacy_git_file(tmp_path)
    con = _catalog()
    _record_legacy_file(con, repo, name, drive, data)
    offline = hash_repair.audit_hashes(con, [repo], archive_resolver=lambda _c, _d: None)
    assert offline["errors"][0]["code"] == "DRIVE_UNAVAILABLE"

    con.execute("UPDATE archived SET compressed=1,stored_name=?,stored_relpath=?", [
        name + ".znn", name + ".znn",
    ])
    compressed = archive / repo / (name + ".znn")
    stored.rename(compressed)
    _run("git", "-C", str(archive), "add", "-A")
    _run("git", "-C", str(archive), "commit", "-qm", "compressed fixture")
    report = hash_repair.audit_hashes(con, [repo], archive_resolver=lambda _c, _d: archive)
    assert report["errors"][0]["code"] == "UNPROVEN_BYTES"
    assert "compressed" in report["errors"][0]["detail"]
    con.close()


def test_cli_repair_hashes_is_read_only_by_default(tmp_path):
    repo, name, drive, archive, _, data = _legacy_git_file(tmp_path)
    catalog = tmp_path / "catalog.sqlite"
    con = _catalog(catalog)
    _record_legacy_file(con, repo, name, drive, data)
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.object(cli.db, "connect", return_value=con) as connect, \
         mock.patch.object(hash_repair.register, "archive_path", return_value=archive), \
         redirect_stdout(stdout), redirect_stderr(stderr):
        cli.main(["repair-hashes", "--repo", repo])
    connect.assert_called_once_with(read_only=True)
    output = stdout.getvalue()
    assert "would repair" in output and "1 provable, 0 blocked" in output
    check = sqlite3.connect(catalog)
    try:
        assert check.execute("SELECT orig_sha256 FROM archived").fetchone()[0] is None
    finally:
        check.close()
    assert not list(tmp_path.glob("catalog.sqlite.pre-hash-repair-*.bak"))


def test_fetch_records_hash_for_hub_file_without_canonical_sha(tmp_path):
    con = _catalog()
    repo, name, drive, data = "org/model", ".gitattributes", "drive-00", b"*.bin annex.largefiles=anything\n"
    con.execute("INSERT INTO models(repo_id,status) VALUES(?,?)", [repo, "fetching"])
    con.execute("INSERT INTO drives(drive_label) VALUES(?)", [drive])
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format) VALUES(?,?,?,?,?)",
        [repo, name, len(data), None, "aux"],
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) VALUES(?,?,?,?,?,?)",
        [repo, "model.safetensors", 10, "0" * 64, "safetensors", "gptq"],
    )
    manifest = (archive_manifest.ManifestFile(
        rfilename=name, size_bytes=len(data), sha256=None, format="aux", quant=None,
        storage_action="raw",
    ),)

    def download(_ctx, _repo, rfilename, model_dir, _base):
        path = model_dir / rfilename
        path.write_bytes(data)
        return path

    with mock.patch.object(fetch, "_download_shard", side_effect=download):
        fetch.fetch_model(
            fetch.RunCtx(con=con), repo, tmp_path / "archive", drive, False,
            {"max_compress_ram_gb": 4, "threads": 1}, manifest=manifest,
        )
    assert con.execute("SELECT orig_sha256 FROM archived").fetchone()[0] == _sha(data)
    con.close()


def test_fetch_uses_computed_hash_for_canary_when_hub_sha_is_missing(tmp_path):
    con = _catalog()
    repo, name, drive, data = "org/model", "model.safetensors", "drive-00", b"weight bytes"
    con.execute("INSERT INTO models(repo_id,status) VALUES(?,?)", [repo, "fetching"])
    con.execute("INSERT INTO drives(drive_label) VALUES(?)", [drive])
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) VALUES(?,?,?,?,?,?)",
        [repo, name, len(data), None, "safetensors", "bf16"],
    )
    manifest = (archive_manifest.ManifestFile(
        rfilename=name, size_bytes=len(data), sha256=None, format="safetensors", quant="bf16",
        storage_action="compress",
    ),)

    def download(_ctx, _repo, rfilename, model_dir, _base):
        path = model_dir / rfilename
        path.write_bytes(data)
        return path

    failed_compressor = {"status": "crash", "signal": 6, "stderr": "fixture"}
    with mock.patch.object(fetch, "_download_shard", side_effect=download), \
         mock.patch.object(fetch.compress, "plan_codec", return_value=compress.CODEC_STREAM), \
         mock.patch.object(fetch, "_compress_isolated", return_value=failed_compressor) as isolated:
        fetch.fetch_model(
            fetch.RunCtx(con=con), repo, tmp_path / "archive", drive, False,
            {"max_compress_ram_gb": 4, "threads": 1}, manifest=manifest,
        )
    assert isolated.call_args.args[4] == _sha(data)
    assert con.execute("SELECT orig_sha256 FROM archived").fetchone()[0] == _sha(data)
    con.close()


def test_fetch_rejects_hub_hash_mismatch_before_archiving(tmp_path):
    con = _catalog()
    repo, name, drive, data = "org/model", "model.safetensors", "drive-00", b"corrupt transfer"
    con.execute("INSERT INTO models(repo_id,status) VALUES(?,?)", [repo, "fetching"])
    con.execute("INSERT INTO drives(drive_label) VALUES(?)", [drive])
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) VALUES(?,?,?,?,?,?)",
        [repo, name, len(data), "0" * 64, "safetensors", "gptq"],
    )
    manifest = (archive_manifest.ManifestFile(
        rfilename=name, size_bytes=len(data), sha256="0" * 64,
        format="safetensors", quant="gptq", storage_action="raw",
    ),)

    def download(_ctx, _repo, rfilename, model_dir, _base):
        path = model_dir / rfilename
        path.write_bytes(data)
        return path

    with mock.patch.object(fetch, "_download_shard", side_effect=download):
        try:
            fetch.fetch_model(
                fetch.RunCtx(con=con), repo, tmp_path / "archive", drive, False,
                {"max_compress_ram_gb": 4, "threads": 1}, manifest=manifest,
            )
            raise AssertionError("a Hub digest mismatch must fail before archive recording")
        except RuntimeError as exc:
            assert "sha256 mismatch" in str(exc)
    assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 0
    con.close()


if __name__ == "__main__":
    for test_name, function in sorted(globals().items()):
        if test_name.startswith("test_") and callable(function):
            with tempfile.TemporaryDirectory() as directory:
                function(Path(directory))
            print(f"ok  {test_name}")
    print("all passed")
