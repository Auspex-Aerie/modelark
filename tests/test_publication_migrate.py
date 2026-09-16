"""Read-only conversion inspect; apply/resume stay disabled (no live cutover)."""
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


def test_apply_and_resume_refuse_without_live_cutover():
    with pytest.raises(PublicationRefused, match="CONVERSION_DISABLED"):
        apply_conversion(plan_id="p", seal="s", writers_stopped=True)
    with pytest.raises(PublicationRefused, match="CONVERSION_DISABLED"):
        resume_conversion(plan_id="p", writers_stopped=True)
