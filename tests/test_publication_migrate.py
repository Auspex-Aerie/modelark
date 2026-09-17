"""Read-only conversion inspect; disposable apply freezes a plan, not live cutover."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from modelark.core import db
from modelark.publication_migrate import apply_conversion, inspect_conversion, resume_conversion
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
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,annex_key) VALUES('org/m','.gitattributes','drive-00','abc',12,12,0,NULL)")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,annex_key) VALUES('org/m','weights.gguf','drive-00','def',99,99,0,'SHA256-s99--def')")
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,annex_key) VALUES('org/m','notes.yaml','drive-01',NULL,NULL,0,0,NULL)")
    before = tuple(con.iterdump())
    plan = inspect_conversion(con, drive="drive-00")
    assert plan["apply"] == "disabled-until-explicit-cutover"
    assert plan["counts"]["convertible"] == 1
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
        "compressed,annex_key) VALUES('org/m','.gitattributes','drive-00','abc',12,12,0,NULL)")
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
    (archive / ".gitattributes").write_bytes(payload)
    git("add", "--", ".gitattributes")
    git("commit", "-qm", "git blob")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "catalog.sqlite")
    monkeypatch.setattr(db, "CATALOG_DIR", tmp_path)
    con = db.connect(_bootstrapping=True)
    con.execute("INSERT INTO drives(drive_label) VALUES('drive-00')")
    con.execute("INSERT INTO models(repo_id) VALUES('org/m')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes) VALUES('org/m','.gitattributes',?)",
                [len(payload)])
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,drive_label,orig_sha256,orig_bytes,stored_bytes,"
        "compressed,stored_relpath,annex_key) VALUES('org/m','.gitattributes','drive-00',?,?,?,0,"
        "'.gitattributes',NULL)",
        [digest, len(payload), len(payload)])
    census = inspect_conversion(con)
    frozen = apply_conversion(con, census, writers_stopped=True, dest_dir=tmp_path / "plans",
                              archives={"drive-00": archive})
    assert frozen["apply"] == "physical-disposable"
    converted = frozen["converted"]
    assert len(converted) == 1
    assert converted[0]["annex_key"].startswith("SHA256-")
    assert digest in converted[0]["annex_key"]
    row = con.execute("SELECT annex_key, stored_relpath FROM archived").fetchone()
    assert row[0] == converted[0]["annex_key"]
    assert row[1].startswith("__modelark_payload_v1__/")
    resumed = resume_conversion(con, frozen["seal"], writers_stopped=True,
                                dest_dir=tmp_path / "plans", archives={"drive-00": archive})
    assert resumed["converted"] == []
    con.close()
