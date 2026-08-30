"""First-class archive restore: layout, codecs, annex retrieval, replica fallback, and failure atomicity."""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from modelark import cli, compress, restore
from modelark.core import db


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    return con


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file(con, repo: str, name: str, data: bytes, fmt: str, quant=None, sha=True):
    digest = _sha(data) if sha else None
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format,quant) VALUES(?,?,?,?,?,?)",
        [repo, name, len(data), digest, fmt, quant],
    )
    return digest


def _archived(con, repo: str, name: str, stored: str, drive: str, digest: str | None,
              compressed: bool, key: str | None = None):
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_sha256,compressed,annex_key) VALUES(?,?,?,?,?,?,?,?)",
        [repo, name, Path(stored).name, stored, drive, digest, int(compressed), key],
    )


def _run(*args: str):
    return subprocess.run(args, check=True, capture_output=True, text=True)


def test_restore_reconstructs_nested_raw_and_compressed_layout(tmp_path):
    con = _mem()
    repo = "org/model"
    weights = b"\x00\x01" * 32_768
    config = b'{"architectures":["TestModel"]}'
    weight_sha = _file(con, repo, "weights/model.safetensors", weights, "safetensors", "bf16")
    _file(con, repo, "nested/config.json", config, "aux", sha=False)

    archive = tmp_path / "drive"
    model_dir = archive / "org" / "model"
    raw = model_dir / "nested" / "config.json"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(config)
    source = tmp_path / "source.safetensors"
    source.write_bytes(weights)
    znn = model_dir / "weights" / "model.safetensors.znn"
    compress.compress_file(source, znn, codec=compress.CODEC_STREAM, threads=1)
    _archived(con, repo, "weights/model.safetensors", "weights/model.safetensors.znn",
              "drive-00", weight_sha, True)
    annex_key = f"SHA256E-s{len(config)}--{_sha(config)}.json"
    _archived(con, repo, "nested/config.json", "nested/config.json", "drive-00", None,
              False, annex_key)

    out = tmp_path / "out"
    with mock.patch.object(restore.register, "archive_path", return_value=archive):
        result = restore.restore_repo(con, repo, out)
    final = out / "org" / "model"
    assert (final / "weights" / "model.safetensors").read_bytes() == weights
    assert (final / "nested" / "config.json").read_bytes() == config
    assert result["n_files"] == 2 and result["annex_retrievals"] == 0, result


def test_restore_asks_git_annex_for_dropped_content(tmp_path):
    con = _mem()
    repo, name, data = "org/model", "nested/config.json", b"from annex"
    digest = _file(con, repo, name, data, "aux")
    archive = tmp_path / "drive"
    (archive / ".git").mkdir(parents=True)
    stored = archive / "org" / "model" / name
    _archived(con, repo, name, name, "drive-00", digest, False,
              f"SHA256E-s{len(data)}--{digest}.json")
    calls = []

    def annex(repo_path, *args):
        calls.append(args)
        if args[:2] == ("get", "--"):
            stored.parent.mkdir(parents=True, exist_ok=True)
            stored.write_bytes(data)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")

    with mock.patch.object(restore.register, "archive_path", return_value=archive), \
         mock.patch.object(restore, "_run_annex", side_effect=annex):
        result = restore.restore_repo(con, repo, tmp_path / "out")
    assert (tmp_path / "out" / "org" / "model" / name).read_bytes() == data
    assert result["annex_retrievals"] == 1 and calls[0] == (
        "get", "--", "org/model/nested/config.json"
    ), calls


# ---- PR-03c2: restore must not MUTATE an authoritative drive to retrieve a dropped blob ---------

def _authoritative(con, label):
    con.execute("INSERT INTO drives(drive_label, write_authority) VALUES(?, 'dedicated_local')", [label])


def _unknown_drive(con, label):
    con.execute("INSERT INTO drives(drive_label, write_authority) VALUES(?, 'unknown')", [label])


def test_restore_suppresses_annex_get_on_an_authoritative_drive(tmp_path):
    """A restore must not mutate an authoritative (dedicated_local) archive to fetch a dropped blob —
    `git annex get` is suppressed even if the current generation is dirty. With no other recorded copy,
    restore fails with ONE clear typed error rather than silently invalidating the drive's anchor."""
    con = _mem()
    repo, name, data = "org/model", "nested/config.json", b"from annex"
    digest = _file(con, repo, name, data, "aux")
    _authoritative(con, "drive-00")
    archive = tmp_path / "drive"
    (archive / ".git").mkdir(parents=True)                           # git-annex checkout, content DROPPED
    _archived(con, repo, name, name, "drive-00", digest, False, f"SHA256E-s{len(data)}--{digest}.json")
    calls = []

    def annex(repo_path, *args):
        calls.append(args)
        if args[:2] == ("get", "--"):                                # today this retrieves; the guard forbids it
            (archive / "org" / "model" / "nested").mkdir(parents=True, exist_ok=True)
            (archive / "org" / "model" / name).write_bytes(data)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unexpected")

    with mock.patch.object(restore.register, "archive_path", return_value=archive), \
         mock.patch.object(restore, "_run_annex", side_effect=annex):
        try:
            restore.restore_repo(con, repo, tmp_path / "out")
            raise AssertionError("retrieval that would mutate an authoritative drive must be refused")
        except restore.RestoreError:
            pass
    assert not any(a and a[0] == "get" for a in calls), \
        f"git annex get must be suppressed on an authoritative drive: {calls}"


def test_restore_reads_local_content_on_an_authoritative_drive_unchanged(tmp_path):
    """The guard suppresses only MUTATION: content already present on an authoritative drive restores
    exactly as before, with no annex retrieval."""
    con = _mem()
    repo, name, data = "org/model", "nested/config.json", b"present locally"
    digest = _file(con, repo, name, data, "aux")
    _authoritative(con, "drive-00")
    archive = tmp_path / "drive"
    stored = archive / "org" / "model" / name
    stored.parent.mkdir(parents=True)
    stored.write_bytes(data)                                         # content LOCAL — no retrieval needed
    _archived(con, repo, name, name, "drive-00", digest, False, f"SHA256E-s{len(data)}--{digest}.json")
    calls = []
    with mock.patch.object(restore.register, "archive_path", return_value=archive), \
         mock.patch.object(restore, "_run_annex", side_effect=lambda *a: calls.append(a[1:])):
        result = restore.restore_repo(con, repo, tmp_path / "out")
    assert (tmp_path / "out" / "org" / "model" / name).read_bytes() == data
    assert calls == [] and result["annex_retrievals"] == 0, f"local content needs no annex retrieval: {calls}"


def test_restore_falls_back_from_authoritative_to_a_retrievable_copy(tmp_path):
    """The authoritative copy (tried first, ORDER BY drive_label) is skipped rather than mutated, and the
    recorded fallback copy on a non-authoritative drive — which has no anchor to invalidate — retrieves
    normally, so restore completes."""
    con = _mem()
    repo, name, data = "org/model", "nested/config.json", b"fallback bytes"
    digest = _file(con, repo, name, data, "aux")
    _authoritative(con, "drive-00")                                  # tried first
    _unknown_drive(con, "drive-01")
    arch0, arch1 = tmp_path / "d0", tmp_path / "d1"
    (arch0 / ".git").mkdir(parents=True)
    (arch1 / ".git").mkdir(parents=True)
    key = f"SHA256E-s{len(data)}--{digest}.json"
    _archived(con, repo, name, name, "drive-00", digest, False, key)
    _archived(con, repo, name, name, "drive-01", digest, False, key)
    mounts = {"drive-00": arch0, "drive-01": arch1}
    calls = []

    def annex(repo_path, *args):
        calls.append((Path(repo_path).name, args))
        if args[:2] == ("get", "--") and Path(repo_path) == arch1:   # only the non-authoritative copy retrieves
            (arch1 / "org" / "model" / "nested").mkdir(parents=True, exist_ok=True)
            (arch1 / "org" / "model" / name).write_bytes(data)
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", "unavailable")

    with mock.patch.object(restore.register, "archive_path", side_effect=lambda c, d: mounts[d]), \
         mock.patch.object(restore, "_run_annex", side_effect=annex):
        restore.restore_repo(con, repo, tmp_path / "out")
    assert (tmp_path / "out" / "org" / "model" / name).read_bytes() == data
    assert not any(where == "d0" and args[0] == "get" for where, args in calls), \
        f"the authoritative copy (drive-00 → d0) must not be retrieved by mutation: {calls}"


def test_restore_real_git_annex_roundtrip(tmp_path):
    if shutil.which("git-annex") is None:
        return
    con = _mem()
    repo, name, data = "org/model", "nested/config.json", b"real annex retrieval"
    digest = _file(con, repo, name, data, "aux")
    source, archive = tmp_path / "source", tmp_path / "drive"
    source.mkdir()
    _run("git", "-C", str(source), "init", "-q")
    _run("git", "-C", str(source), "config", "user.name", "ModelArk Test")
    _run("git", "-C", str(source), "config", "user.email", "test@modelark.invalid")
    _run("git", "-C", str(source), "annex", "init", "source", "-q")
    stored = source / "org" / "model" / name
    stored.parent.mkdir(parents=True)
    stored.write_bytes(data)
    relative = stored.relative_to(source).as_posix()
    _run("git", "-C", str(source), "annex", "add", relative)
    key = _run("git", "-C", str(source), "annex", "lookupkey", relative).stdout.strip()
    _run("git", "-C", str(source), "commit", "-qam", "archive fixture")
    _run("git", "clone", "-q", str(source), str(archive))
    _run("git", "-C", str(archive), "config", "user.name", "ModelArk Test")
    _run("git", "-C", str(archive), "config", "user.email", "test@modelark.invalid")
    _run("git", "-C", str(archive), "annex", "init", "restore-test", "-q")
    assert not (archive / relative).exists(), "clone should begin with dropped annex content"
    _archived(con, repo, name, name, "drive-00", digest, False, key)

    with mock.patch.object(restore.register, "archive_path", return_value=archive):
        result = restore.restore_repo(con, repo, tmp_path / "out")
    assert (tmp_path / "out" / "org" / "model" / name).read_bytes() == data
    assert result["annex_retrievals"] == 1, result


def test_restore_falls_back_from_corrupt_copy_to_verified_replica(tmp_path):
    con = _mem()
    repo, name, data = "org/model", "model.safetensors", b"good bytes"
    digest = _file(con, repo, name, data, "safetensors", "gptq")
    bad, good = tmp_path / "bad", tmp_path / "good"
    for root, payload in ((bad, b"corrupt"), (good, data)):
        path = root / "org" / "model" / name
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)
    _archived(con, repo, name, name, "drive-00", digest, False)
    _archived(con, repo, name, name, "drive-01", digest, False)

    mounts = {"drive-00": bad, "drive-01": good}
    with mock.patch.object(restore.register, "archive_path", side_effect=lambda c, d: mounts[d]):
        result = restore.restore_repo(con, repo, tmp_path / "out")
    assert (tmp_path / "out" / "org" / "model" / name).read_bytes() == data
    assert result["files"][0]["drive"] == "drive-01", result
    assert "sha256 mismatch" in result["warnings"][0]["detail"], result


def test_restore_falls_back_after_compressed_codec_exception(tmp_path):
    con = _mem()
    repo, name, data = "org/model", "model.safetensors", b"\x00\x01" * 32_768
    digest = _file(con, repo, name, data, "safetensors", "bf16")
    bad, good = tmp_path / "bad", tmp_path / "good"
    stored_name = name + ".znn"
    bad_stored = bad / "org" / "model" / stored_name
    bad_stored.parent.mkdir(parents=True)
    bad_stored.write_bytes(b"corrupt compressed bytes")
    source = tmp_path / "source.safetensors"
    source.write_bytes(data)
    good_stored = good / "org" / "model" / stored_name
    compress.compress_file(source, good_stored, codec=compress.CODEC_STREAM, threads=1)
    _archived(con, repo, name, stored_name, "drive-00", digest, True)
    _archived(con, repo, name, stored_name, "drive-01", digest, True)

    mounts = {"drive-00": bad, "drive-01": good}
    decompress = compress.decompress_file

    def fail_bad_copy(source_path, destination, dtype="bfloat16"):
        if Path(source_path) == bad_stored:
            raise ValueError("invalid codec fixture")
        return decompress(source_path, destination, dtype=dtype)

    with mock.patch.object(restore.register, "archive_path", side_effect=lambda c, d: mounts[d]), \
         mock.patch.object(restore.compress, "decompress_file", side_effect=fail_bad_copy):
        result = restore.restore_repo(con, repo, tmp_path / "out")
    assert (tmp_path / "out" / "org" / "model" / name).read_bytes() == data
    assert result["files"][0]["drive"] == "drive-01", result
    assert "decompression failed: invalid codec fixture" in result["warnings"][0]["detail"]


def test_restore_failure_reports_offline_missing_and_publishes_nothing(tmp_path):
    con = _mem()
    repo, name, data = "org/model", "model.safetensors", b"weights"
    digest = _file(con, repo, name, data, "safetensors", "gptq")
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    _archived(con, repo, name, name, "drive-offline", digest, False)
    _archived(con, repo, name, name, "drive-missing", digest, False)
    mounts = {"drive-offline": None, "drive-missing": mounted}

    with mock.patch.object(restore.register, "archive_path", side_effect=lambda c, d: mounts[d]):
        try:
            restore.restore_repo(con, repo, tmp_path / "out")
            raise AssertionError("restore should fail when no copy is readable")
        except restore.RestoreError as exc:
            detail = str(exc)
    assert "drive-offline: offline/not mounted" in detail, detail
    assert "drive-missing: recorded blob is missing" in detail, detail
    assert not (tmp_path / "out" / "org" / "model").exists()
    assert not list((tmp_path / "out" / "org").glob(".model.restore-*"))


def test_restore_rejects_unsafe_paths_and_existing_destination(tmp_path):
    con = _mem()
    try:
        restore.restore_repo(con, "../escape", tmp_path / "out")
        raise AssertionError("unsafe repo id should fail")
    except restore.RestoreError as exc:
        assert "unsafe repository id" in str(exc)

    repo, data = "org/model", b"ok"
    digest = _file(con, repo, "config.json", data, "aux")
    _archived(con, repo, "config.json", "config.json", "drive-00", digest, False)
    destination = tmp_path / "out" / "org" / "model"
    destination.mkdir(parents=True)
    with mock.patch.object(restore.register, "archive_path", return_value=tmp_path / "drive"):
        try:
            restore.restore_repo(con, repo, tmp_path / "out")
            raise AssertionError("existing output should not be overwritten")
        except restore.RestoreError as exc:
            assert "destination already exists" in str(exc)


def test_cli_restore_dispatches_end_to_end(tmp_path):
    con = _mem()
    repo, name, data = "org/model", "config.json", b"cli restore"
    digest = _file(con, repo, name, data, "aux")
    archive = tmp_path / "drive"
    stored = archive / "org" / "model" / name
    stored.parent.mkdir(parents=True)
    stored.write_bytes(data)
    _archived(con, repo, name, name, "drive-00", digest, False)
    out = tmp_path / "out"

    with mock.patch.object(cli.db, "connect", return_value=con), \
         mock.patch.object(restore.register, "archive_path", return_value=archive):
        cli.main(["restore", "--repo", repo, "--dest", str(out)])
    assert (out / "org" / "model" / name).read_bytes() == data


if __name__ == "__main__":
    for name, function in sorted(globals().items()):
        if name.startswith("test_") and callable(function):
            function(Path(tempfile.mkdtemp()))
            print(f"ok  {name}")
    print("all passed")
