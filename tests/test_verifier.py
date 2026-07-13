"""DEF-021 Verifier: disruption-suspect detection + offline record-consistency re-verify (the
decompress-canary needs a mounted drive, so it's exercised only in the record path here)."""
from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from modelark.core import db
from modelark import fetch, verifier


def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)   # autocommit, matching db.connect()
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _archive(con, repo, rf, drive, sha, compressed, va="2026-07-11 10:00:00", rel=None):
    stored_name = Path(rf).name + (".znn" if compressed else "")
    rel = rel or str(Path(rf).parent / stored_name)
    con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,orig_sha256,orig_bytes,"
                "stored_bytes,compressed,verified_at) VALUES(?,?,?,?,?,?,100,80,?,?)",
                [repo, rf, stored_name, rel, drive, sha, 1 if compressed else 0, va])


def test_suspects():
    con = _mem()
    # A: a float safetensors stored RAW → raw-fallback suspect
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES('A','m.safetensors',100,'safetensors','bf16','sA')")
    _archive(con, "A", "m.safetensors", "drive-00", "sA", compressed=False)
    # B: 2 planned files, only 1 archived on the drive → partial copy suspect
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES('B','m.safetensors',100,'safetensors','bf16','sB')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES('B','config.json',10,'aux',NULL,NULL)")
    _archive(con, "B", "m.safetensors", "drive-01", "sB", compressed=True)
    # C: clean + complete, but a disruption event within the window → disruption suspect
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES('C','m.safetensors',100,'safetensors','bf16','sC')")
    _archive(con, "C", "m.safetensors", "drive-02", "sC", compressed=True, va="2026-07-11 12:00:00")
    con.execute("INSERT INTO fetch_events(repo_id,event_at,outcome,detail) VALUES('C','2026-07-11 12:05:00','awaiting-drive','drive drop')")
    # D: clean + complete, no disruption → NOT a suspect
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES('D','m.safetensors',100,'safetensors','bf16','sD')")
    _archive(con, "D", "m.safetensors", "drive-03", "sD", compressed=True)

    reps = {s["repo"]: s for s in verifier.suspects(con)}
    assert set(reps) == {"A", "B", "C"}, list(reps)
    assert "float weights stored raw (compress fallback / over-budget)" in reps["A"]["reasons"]
    assert "partial copy (interrupted)" in reps["B"]["reasons"]
    assert "archived near a disruption event" in reps["C"]["reasons"]


def test_reverify_record_consistency():
    con = _mem()
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES('A','m.safetensors',100,'safetensors','bf16','sA')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES('A','config.json',10,'aux',NULL,NULL)")
    _archive(con, "A", "m.safetensors", "drive-00", "sA", compressed=True)
    _archive(con, "A", "config.json", "drive-00", None, compressed=False)
    r = verifier.reverify(con, "A", deep=True)                      # drive not mounted → canary skipped, record ok
    assert r["archived"] and r["record_ok"] and not r["ok"] and r["status"] == "unknown" and not r["deep_ran"], r

    con.execute("UPDATE archived SET orig_sha256='WRONG' WHERE repo_id='A' AND rfilename='m.safetensors'")
    r2 = verifier.reverify(con, "A", deep=False)                    # stored hash disagrees with the catalog
    assert not r2["record_ok"] and r2["sha_mismatch"] == ["m.safetensors"], r2

    assert verifier.reverify(con, "ZZ")["archived"] is False        # nothing archived


def _raw_file(root: Path, repo: str, rel: str, data=b"weights") -> str:
    p = root / repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def test_mounted_missing_and_broken_annex_are_hard_failures():
    con = _mem()
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
                "VALUES('A','nested/m.safetensors',100,'safetensors','bf16','abc')")
    _archive(con, "A", "nested/m.safetensors", "drive-00", "abc", compressed=False)
    with tempfile.TemporaryDirectory() as td, mock.patch.object(
            verifier.register, "archive_path", return_value=Path(td)):
        r = verifier.reverify(con, "A")
        assert r["status"] == "failed" and r["deep_ran"] and r["deep_checks"][0]["ok"] is False, r
        broken = Path(td) / "A" / "nested" / "m.safetensors"
        broken.parent.mkdir(parents=True, exist_ok=True)
        broken.symlink_to(Path(td) / "missing-annex-object")
        r2 = verifier.reverify(con, "A")
        assert r2["status"] == "failed" and "missing" in r2["deep_checks"][0]["err"], r2


def test_nested_relpath_verifies_and_copy_count_is_enforced():
    con = _mem()
    con.execute("INSERT INTO models(repo_id,numcopies) VALUES('A',2)")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d0, d1 = root / "d0", root / "d1"
        sha = _raw_file(d0, "A", "nested/m.safetensors")
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) VALUES(?,?,?,?,?,?)",
                    ["A", "nested/m.safetensors", 7, "safetensors", "bf16", sha])
        _archive(con, "A", "nested/m.safetensors", "drive-00", sha, compressed=False,
                 rel="nested/m.safetensors")
        mounts = {"drive-00": d0, "drive-01": d1}
        with mock.patch.object(verifier.register, "archive_path", side_effect=lambda c, d: mounts.get(d)):
            one = verifier.reverify(con, "A")
            assert one["status"] == "failed" and one["insufficient"] == ["nested/m.safetensors"], one
            _raw_file(d1, "A", "nested/m.safetensors")
            _archive(con, "A", "nested/m.safetensors", "drive-01", sha, compressed=False,
                     rel="nested/m.safetensors")
            two = verifier.reverify(con, "A")
            assert two["status"] == "verified" and two["deep_ok"] is True, two


def test_stored_relative_path_rejects_escape():
    root = Path("archive/org/model")
    assert fetch._stored_relative_path(root / "nested" / "x", root) == "nested/x"
    try:
        fetch._stored_relative_path(Path("archive/elsewhere/x"), root)
        raise AssertionError("path outside model root must fail")
    except RuntimeError as e:
        assert "escaped" in str(e)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all passed")
