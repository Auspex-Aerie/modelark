"""Read-only conversion inspect; disposable apply freezes a plan, not live cutover."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from modelark.core import db
from modelark.publication_migrate import (
    apply_conversion, inspect_conversion, resume_conversion, _source_path)
from modelark.publication_policy import PublicationRefused


def test_inspect_classifies_null_key_copies_and_does_not_write(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path)
    con = db.connect(_bootstrapping=True)
    con.execute("INSERT INTO drives(drive_label) VALUES('drive-00')")
    con.execute("INSERT INTO drives(drive_label) VALUES('drive-01')")
    con.execute("INSERT INTO models(repo_id) VALUES('org/m')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes) VALUES('org/m','.gitattributes',12)")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes) VALUES('org/m','weights.gguf',99)")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes) VALUES('org/m','notes.yaml',0)")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes) VALUES('org/m','orphan.bin',4)")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,stored_relpath,annex_key) VALUES('org/m','.gitattributes','drive-00',"
        "'abc',12,12,0,'.gitattributes',NULL)")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,annex_key) VALUES('org/m','weights.gguf','drive-00','def',99,99,0,'SHA256-s99--def')")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,annex_key) VALUES('org/m','notes.yaml','drive-01',NULL,NULL,0,0,NULL)")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,annex_key) VALUES('org/m','orphan.bin','drive-00','abcd',4,4,0,NULL)")
    before = tuple(con.iterdump())
    plan = inspect_conversion(con, drive="drive-00")
    assert plan["apply"] == "disabled-until-explicit-cutover"
    assert plan["counts"]["convertible"] == 1
    assert plan["counts"]["needs-evidence"] == 1
    assert plan["candidates"][0]["proposed_stored_relpath"].startswith("__modelark_payload_v1__/p-")
    assert inspect_conversion(con, drive="drive-01")["counts"]["needs-evidence"] == 1
    assert tuple(con.iterdump()) == before
    con.close()


def test_apply_and_resume_freeze_inspect_on_disposable_catalog_not_live(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path)
    con = db.connect(_bootstrapping=True)
    con.execute("INSERT INTO drives(drive_label) VALUES('drive-00')")
    con.execute("INSERT INTO models(repo_id) VALUES('org/m')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes) VALUES('org/m','.gitattributes',12)")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,stored_relpath,annex_key) VALUES('org/m','.gitattributes','drive-00',"
        "'abc',12,12,0,'.gitattributes',NULL)")
    with pytest.raises(PublicationRefused, match="WRITERS_STILL_RUNNING"):
        apply_conversion(con, writers_stopped=False, dest_dir=tmp_path / "plans")
    census = inspect_conversion(con)
    frozen = apply_conversion(con, census, writers_stopped=True, dest_dir=tmp_path / "plans")
    assert frozen["frozen"] is True
    assert frozen["apply"] == "frozen-inspect-only"
    assert (tmp_path / "plans" / f"annex-migrate-{frozen['seal'][:12]}.json").is_file()
    resumed = resume_conversion(con, frozen["seal"], writers_stopped=True, dest_dir=tmp_path / "plans")
    assert resumed["seal"] == frozen["seal"]
    path = Path(frozen["plan_path"])
    tampered = json.loads(path.read_text())
    tampered["candidates"] = []
    path.write_text(json.dumps(tampered, indent=2) + "\n")
    with pytest.raises(PublicationRefused, match="PLAN_UNPROVEN"):
        resume_conversion(con, frozen["seal"], writers_stopped=True, dest_dir=tmp_path / "plans")
    with pytest.raises(PublicationRefused, match="PLAN_EXISTS"):
        apply_conversion(con, census, writers_stopped=True, dest_dir=tmp_path / "plans")
    monkeypatch.setattr("modelark.publication_migrate.live_catalog_path",
                        lambda: Path(con.execute("PRAGMA database_list").fetchone()[2]).resolve())
    with pytest.raises(PublicationRefused, match="LIVE_CUTOVER_FORBIDDEN"):
        apply_conversion(con, writers_stopped=True, dest_dir=tmp_path / "other")
    con.close()


def test_physical_convert_on_disposable_annex_updates_catalog_and_resumes(tmp_path, monkeypatch):
    if not Path("/usr/bin/git-annex").is_file() and shutil.which("git-annex") is None:
        pytest.skip("git-annex required")
    payload = b"* annex.largefiles=anything\n"
    digest = hashlib.sha256(payload).hexdigest()
    archive = tmp_path / "archive"
    archive.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")

    def git(*args):
        subprocess.run(["git", "-C", str(archive), "-c", "user.name=ModelArk",
                        "-c", "user.email=publication@modelark.invalid",
                        "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                       env=env, check=True, capture_output=True)

    git("init", "-q", "--initial-branch=main")
    git("annex", "init", "--version=8", "--quiet", "migrate-test")
    git("config", "annex.backend", "SHA256")
    for repo in ("org/m", "org/n"):
        path = archive / repo
        path.mkdir(parents=True)
        (path / ".gitattributes").write_bytes(payload)
        git("add", "--", f"{repo}/.gitattributes")
    git("commit", "-qm", "git blobs")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path)
    con = db.connect(_bootstrapping=True)
    con.execute("INSERT INTO drives(drive_label) VALUES('drive-00')")
    for repo in ("org/m", "org/n"):
        con.execute("INSERT INTO models(repo_id) VALUES(?)", [repo])
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes) VALUES(?,'.gitattributes',?)",
                    [repo, len(payload)])
        con.execute(
            "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
            "compressed,stored_relpath,annex_key) VALUES(?,'.gitattributes','drive-00',?,?,?,0,"
            "'.gitattributes',NULL)",
            [repo, digest, len(payload), len(payload)])
    census = inspect_conversion(con)
    frozen = apply_conversion(con, census, writers_stopped=True, dest_dir=tmp_path / "plans",
                              archives={"drive-00": archive})
    assert frozen["apply"] == "physical-disposable"
    converted = frozen["converted"]
    assert len(converted) == 2
    keys = {row["annex_key"] for row in converted}
    assert len(keys) == 1
    assert digest in next(iter(keys))
    rows = con.execute("SELECT repo_id, annex_key, stored_relpath, stored_name FROM archived "
                       "ORDER BY repo_id").fetchall()
    assert {row[0] for row in rows} == {"org/m", "org/n"}
    for row in rows:
        assert row[1].startswith("SHA256-")
        assert row[2].startswith("__modelark_payload_v1__/")
        assert row[3] == Path(row[2]).name
        assert (archive / row[0] / row[2]).exists()
    resumed = resume_conversion(con, frozen["seal"], writers_stopped=True,
                                dest_dir=tmp_path / "plans", archives={"drive-00": archive})
    assert resumed["converted"] == []
    monkeypatch.setattr("modelark.publication_migrate.live_catalog_path",
                        lambda: Path(con.execute("PRAGMA database_list").fetchone()[2]).resolve())
    with pytest.raises(PublicationRefused, match="LIVE_CUTOVER_FORBIDDEN"):
        apply_conversion(con, census, writers_stopped=True, dest_dir=tmp_path / "other",
                         archives={"drive-00": archive})
    for repo in ("org/m", "org/n"):
        assert not (archive / repo / ".gitattributes").exists()
    con.close()


def _require_annex():
    if not Path("/usr/bin/git-annex").is_file() and shutil.which("git-annex") is None:
        pytest.skip("git-annex required")


def _init_annex(archive):
    archive.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")

    def git(*args):
        result = subprocess.run(
            ["git", "-C", str(archive), "-c", "user.name=ModelArk",
             "-c", "user.email=publication@modelark.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
            env=env, check=True, capture_output=True, text=True)
        return result.stdout

    git("init", "-q", "--initial-branch=main")
    git("annex", "init", "--version=8", "--quiet", "migrate-test")
    git("config", "annex.backend", "SHA256")
    return git


def _catalog_copy(tmp_path, monkeypatch, repo, digest, payload, stored_relpath):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path)
    con = db.connect(_bootstrapping=True)
    con.execute("INSERT INTO drives(drive_label) VALUES('drive-00')")
    con.execute("INSERT INTO models(repo_id) VALUES(?)", [repo])
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes) VALUES(?,'.gitattributes',?)",
                [repo, len(payload)])
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,stored_relpath,annex_key) VALUES(?,'.gitattributes','drive-00',?,?,?,0,?,NULL)",
        [repo, digest, len(payload), len(payload), stored_relpath])
    return con


def test_source_path_refuses_rfilename_fallback(tmp_path):
    archive = tmp_path / "archive"
    (archive / "org" / "m").mkdir(parents=True)
    (archive / "org" / "m" / ".gitattributes").write_bytes(b"payload")
    candidate = {"repo_id": "org/m", "rfilename": ".gitattributes",
                 "stored_relpath": "missing.bin"}
    with pytest.raises(PublicationRefused, match="SOURCE_UNPROVEN"):
        _source_path(archive, candidate)
    assert (archive / "org" / "m" / ".gitattributes").is_file()


def test_physical_convert_refuses_unproven_source(tmp_path, monkeypatch):
    _require_annex()
    payload = b"* annex.largefiles=anything\n"
    digest = hashlib.sha256(payload).hexdigest()
    archive = tmp_path / "archive"
    git = _init_annex(archive)
    path = archive / "org" / "m"
    path.mkdir(parents=True)
    (path / ".gitattributes").write_bytes(payload)
    git("add", "--", "org/m/.gitattributes")
    git("commit", "-qm", "git blobs")
    con = _catalog_copy(tmp_path, monkeypatch, "org/m", digest, payload, "not-the-file.bin")
    census = inspect_conversion(con)
    with pytest.raises(PublicationRefused, match="SOURCE_UNPROVEN"):
        apply_conversion(con, census, writers_stopped=True, dest_dir=tmp_path / "plans",
                         archives={"drive-00": archive})
    assert con.execute("SELECT annex_key FROM archived").fetchone()[0] is None
    assert (archive / "org" / "m" / ".gitattributes").is_file()
    con.close()


def test_physical_convert_cas_frozen_before_state(tmp_path, monkeypatch):
    _require_annex()
    payload = b"* annex.largefiles=anything\n"
    digest = hashlib.sha256(payload).hexdigest()
    archive = tmp_path / "archive"
    git = _init_annex(archive)
    path = archive / "org" / "m"
    path.mkdir(parents=True)
    (path / ".gitattributes").write_bytes(payload)
    git("add", "--", "org/m/.gitattributes")
    git("commit", "-qm", "git blobs")
    con = _catalog_copy(tmp_path, monkeypatch, "org/m", digest, payload, ".gitattributes")
    census = inspect_conversion(con)
    frozen = apply_conversion(con, census, writers_stopped=True, dest_dir=tmp_path / "plans")
    assert frozen["apply"] == "frozen-inspect-only"
    con.execute("UPDATE archived SET orig_sha256=?, compressed=1", ["0" * 64])
    con.commit()
    with pytest.raises(PublicationRefused, match="CATALOG_UNPROVEN"):
        apply_conversion(con, census, writers_stopped=True, dest_dir=tmp_path / "plans",
                         archives={"drive-00": archive})
    assert con.execute("SELECT annex_key FROM archived").fetchone()[0] is None
    con.close()


def test_physical_convert_path_limited_commit_and_required_retirement(tmp_path, monkeypatch):
    _require_annex()
    payload = b"* annex.largefiles=anything\n"
    digest = hashlib.sha256(payload).hexdigest()
    archive = tmp_path / "archive"
    git = _init_annex(archive)
    path = archive / "org" / "m"
    path.mkdir(parents=True)
    (path / ".gitattributes").write_bytes(payload)
    git("add", "--", "org/m/.gitattributes")
    git("commit", "-qm", "git blobs")
    (archive / "unrelated.txt").write_text("leave me staged\n")
    git("add", "--", "unrelated.txt")
    con = _catalog_copy(tmp_path, monkeypatch, "org/m", digest, payload, ".gitattributes")
    census = inspect_conversion(con)
    import modelark.publication_migrate as migrate
    real_git = migrate._git
    failing = {"rm": True}

    def wrapped(archive_root, *args, check=True):
        if failing["rm"] and args and args[0] == "rm":
            raise PublicationRefused("PUBLICATION_MIGRATE_GIT_FAILED", stderr="forced rm failure")
        return real_git(archive_root, *args, check=check)

    monkeypatch.setattr(migrate, "_git", wrapped)
    with pytest.raises(PublicationRefused, match="GIT_FAILED"):
        apply_conversion(con, census, writers_stopped=True, dest_dir=tmp_path / "plans",
                         archives={"drive-00": archive})
    key = con.execute("SELECT annex_key FROM archived").fetchone()[0]
    assert key and key.startswith("SHA256-")
    assert (archive / "org" / "m" / ".gitattributes").exists()
    names = git("log", "--name-only", "--pretty=format:", "-1")
    assert "unrelated.txt" not in names
    assert "unrelated.txt" in git("diff", "--cached", "--name-only")
    failing["rm"] = False
    plan_file = next((tmp_path / "plans").glob("annex-migrate-*.json"))
    resumed = resume_conversion(con, plan_file.stem.split("-", 2)[2], writers_stopped=True,
                                dest_dir=tmp_path / "plans", archives={"drive-00": archive})
    assert resumed["converted"] == []
    assert not (archive / "org" / "m" / ".gitattributes").exists()
    assert "unrelated.txt" not in git("log", "--name-only", "--pretty=format:", "-1")
    assert "unrelated.txt" in git("diff", "--cached", "--name-only")
    con.close()
