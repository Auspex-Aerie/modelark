"""DEC-053/054 Gate-2 remediation regressions (seven independent failures).

Does not weaken, delete, skip, or rewrite Gate-1 contracts.
Contracts-only additions for connect refusal, rehearsal path containment,
publication revalidation + lock hold, and repair identity/hub-halt.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from modelark.core import db
from modelark import hash_repair


def _h(ch: str) -> str:
    return (ch * 64)[:64]


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fingerprint(path: Path) -> dict:
    return {
        "sha256": _sha_file(path) if path.is_file() else None,
        "size": path.stat().st_size if path.is_file() else None,
        "wal_present": (path.parent / f"{path.name}-wal").is_file(),
        "shm_present": (path.parent / f"{path.name}-shm").is_file(),
        "user_version": None,
    }


def _user_version(path: Path) -> int:
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        return int(con.execute("PRAGMA user_version").fetchone()[0])
    finally:
        con.close()


def _tables(path: Path) -> set[str]:
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        return {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")
        }
    finally:
        con.close()


def _seed_versioned(path: Path, version: int) -> None:
    """Minimal legal catalog at a frozen user_version (pre-v7)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        con.executescript(
            """
            CREATE TABLE models(
                repo_id VARCHAR PRIMARY KEY NOT NULL,
                status VARCHAR, numcopies INTEGER DEFAULT 1);
            CREATE TABLE files(
                repo_id VARCHAR NOT NULL, rfilename VARCHAR NOT NULL,
                size_bytes INTEGER, sha256 VARCHAR, format VARCHAR,
                PRIMARY KEY(repo_id, rfilename));
            CREATE TABLE drives(
                drive_label VARCHAR PRIMARY KEY NOT NULL,
                capacity_bytes INTEGER, free_bytes INTEGER,
                role VARCHAR DEFAULT 'primary', raid_backed INTEGER DEFAULT 0,
                identity_epoch INTEGER DEFAULT 1,
                identity_fingerprint VARCHAR,
                lifecycle VARCHAR DEFAULT 'active',
                eligibility VARCHAR DEFAULT 'enabled',
                write_authority VARCHAR DEFAULT 'unknown');
            CREATE TABLE archived(
                repo_id VARCHAR NOT NULL, rfilename VARCHAR NOT NULL,
                drive_label VARCHAR NOT NULL, stored_name VARCHAR,
                stored_relpath VARCHAR, orig_sha256 VARCHAR,
                orig_bytes INTEGER, stored_bytes INTEGER,
                compressed INTEGER NOT NULL DEFAULT 0, annex_key VARCHAR,
                PRIMARY KEY(repo_id, rfilename, drive_label));
            CREATE TABLE plans(
                plan_id VARCHAR PRIMARY KEY NOT NULL, name VARCHAR,
                is_active INTEGER DEFAULT 0,
                capacity_mode VARCHAR DEFAULT 'guaranteed');
            CREATE TABLE plan_drives(
                plan_id VARCHAR NOT NULL, drive_label VARCHAR NOT NULL,
                PRIMARY KEY(plan_id, drive_label));
            INSERT INTO models(repo_id,status,numcopies) VALUES('org/m','archived',1);
            INSERT INTO files(repo_id,rfilename,size_bytes,sha256,format)
                VALUES('org/m','w.bin',10,NULL,'aux');
            INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,
                identity_epoch,identity_fingerprint)
                VALUES('d0',1000,1000,'primary',1,NULL);
            INSERT INTO archived(repo_id,rfilename,drive_label,stored_name,
                stored_relpath,orig_sha256,orig_bytes,stored_bytes,compressed)
                VALUES('org/m','w.bin','d0','w.bin','w.bin',NULL,10,10,0);
            INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1);
            INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','d0');
            """
        )
        # For v5+, add planner_state so a partial schema is still "canonical enough".
        if version >= 5:
            con.execute(
                "CREATE TABLE IF NOT EXISTS planner_state("
                "singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),"
                "planner_revision INTEGER NOT NULL DEFAULT 0,"
                "active_approved_proposal_id VARCHAR,"
                "next_fencing_token INTEGER NOT NULL DEFAULT 0)")
            con.execute(
                "INSERT OR IGNORE INTO planner_state(singleton_id,planner_revision) "
                "VALUES(1,0)")
            con.execute(
                "CREATE TABLE IF NOT EXISTS placement_proposals("
                "proposal_id VARCHAR PRIMARY KEY NOT NULL,"
                "plan_id VARCHAR NOT NULL,"
                "based_on_revision INTEGER NOT NULL DEFAULT 0,"
                "lifecycle VARCHAR NOT NULL DEFAULT 'draft',"
                "canonical_hash VARCHAR NOT NULL,"
                "mutation_kind VARCHAR NOT NULL DEFAULT 'adopt_current',"
                "mutation_args_json TEXT NOT NULL DEFAULT '[]',"
                "serializer_version VARCHAR NOT NULL DEFAULT '1',"
                "derivation_mode VARCHAR)")
            con.execute(
                "INSERT INTO placement_proposals("
                "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
                "mutation_kind,mutation_args_json,serializer_version,derivation_mode) "
                "VALUES('p0','ark',0,'draft',?,?,?,?,NULL)",
                [_h("1"), "adopt_current", "[]", "1"])
        if version >= 6:
            # unconstrained execution_config_hash column (pre-check repair shape)
            try:
                con.execute(
                    "ALTER TABLE placement_proposals "
                    "ADD COLUMN execution_config_hash VARCHAR")
            except sqlite3.OperationalError:
                pass
        con.execute(f"PRAGMA user_version={int(version)}")
    finally:
        con.close()


@pytest.fixture(autouse=True)
def _isolate_db_paths():
    old = db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR
    try:
        yield
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = old


# ---------------------------------------------------------------------------
# 1. connect refuses every existing pre-v7 catalog without mutation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("version", [5, 6])
def test_connect_refuses_existing_pre_v7_unchanged(tmp_path, version):
    data = tmp_path / "data"
    data.mkdir()
    path = data / "catalog.sqlite"
    _seed_versioned(path, version)
    before = {
        "sha256": _sha_file(path),
        "size": path.stat().st_size,
        "user_version": _user_version(path),
        "tables": _tables(path),
    }
    assert before["user_version"] == version
    db.configure(data, data / "state")
    with pytest.raises(RuntimeError) as ei:
        con = db.connect()
        con.close()
    msg = str(ei.value).lower()
    assert "clone-first" in msg or "rehearse" in msg or "will not auto-migrate" in msg
    after = {
        "sha256": _sha_file(path),
        "size": path.stat().st_size,
        "user_version": _user_version(path),
        "tables": _tables(path),
    }
    assert after == before, "refusal must leave bytes/schema/version unchanged"
    assert "orig_sha256_provenance" not in {
        r[1] for r in sqlite3.connect(str(path)).execute("PRAGMA table_info(archived)")
    }
    assert "drive_hash_repair_state" not in after["tables"]


def test_connect_fresh_creates_v7(tmp_path):
    data = tmp_path / "fresh"
    data.mkdir()
    db.configure(data, data / "state")
    assert not (data / "catalog.sqlite").exists()
    con = db.connect()
    try:
        assert int(con.execute("PRAGMA user_version").fetchone()[0]) == db._SCHEMA_VERSION
        assert db._SCHEMA_VERSION >= 7
        cols = {r[1] for r in con.execute("PRAGMA table_info(archived)")}
        assert "orig_sha256_provenance" in cols
        tables = {
            r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "drive_hash_repair_state" in tables
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 2. Rehearsal path containment
# ---------------------------------------------------------------------------


def test_rehearse_rejects_unsafe_run_ids(tmp_path):
    data = tmp_path / "src"
    data.mkdir()
    _seed_versioned(data / "catalog.sqlite", 6)
    # Bump identity so seed is closer to a real fixture; rehearse may still
    # fail later on incomplete schema — path validation must fire first.
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside-sentinel"
    outside.write_text("untouched")
    outside_sha = _sha_file(outside)

    for bad in (
        "",
        ".",
        "..",
        "../escape",
        "a/b",
        "a\\b",
        "/abs",
        str(tmp_path / "abs-run"),
        "~evil",
        "has space",
    ):
        with pytest.raises((ValueError, FileNotFoundError, RuntimeError)):
            db.rehearse_provenance_migration(data, work, run_id=bad)
        assert outside.read_text() == "untouched"
        assert _sha_file(outside) == outside_sha


def test_rehearse_relative_and_absolute_escape_leave_sentinel(tmp_path):
    data = tmp_path / "src"
    data.mkdir()
    # Use full frozen-v6 path via contracts fixture when available.
    fixture = Path(__file__).parent / "fixtures" / "catalog_v6.sql"
    if fixture.is_file():
        con = sqlite3.connect(str(data / "catalog.sqlite"), isolation_level=None)
        try:
            con.executescript(fixture.read_text())
        finally:
            con.close()
    else:
        _seed_versioned(data / "catalog.sqlite", 6)

    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside-sentinel"
    outside.write_text("sentinel-bytes")
    outside_sha = _sha_file(outside)

    # Relative escape
    with pytest.raises(ValueError):
        db.rehearse_provenance_migration(data, work, run_id="../outside-sentinel")
    assert outside.read_text() == "sentinel-bytes"
    assert _sha_file(outside) == outside_sha
    assert not (tmp_path / "outside-sentinel" / "snapshot").exists()

    # Absolute path as run_id
    with pytest.raises(ValueError):
        db.rehearse_provenance_migration(
            data, work, run_id=str(outside.resolve()))
    assert outside.read_text() == "sentinel-bytes"
    assert _sha_file(outside) == outside_sha


def test_rehearse_refuses_existing_run_directory(tmp_path):
    from tests.test_dec053_054_gate1_contracts import (
        _seed_frozen_v6, _catalog, _logical_identity, _close, _open_ro,
    )
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    db.rehearse_provenance_migration(data, work, run_id="run-a")
    with pytest.raises(FileExistsError):
        db.rehearse_provenance_migration(data, work, run_id="run-a")
    _ = ident


# ---------------------------------------------------------------------------
# 3–4. Publication treats report as untrusted; holds source lock
# ---------------------------------------------------------------------------


def _rehearse_ok(tmp_path):
    from tests.test_dec053_054_gate1_contracts import (
        _seed_frozen_v6, _catalog, _logical_identity, _close, _open_ro,
        _require_report,
    )
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        db.rehearse_provenance_migration(data, work, run_id="pub"),
        source_identity=ident,
    )
    return data, report, work


def test_publish_refuses_modified_clone_without_destination(tmp_path):
    data, report, work = _rehearse_ok(tmp_path)
    clone = Path(report["clone_catalog_path"])
    con = sqlite3.connect(str(clone), isolation_level=None)
    try:
        # Distinct mutation so logical identity diverges from the rehearsal report.
        con.execute(
            "INSERT INTO models(repo_id,status,numcopies) "
            "VALUES('org/clone-tamper','discovered',1)")
    finally:
        con.close()
    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError) as ei:
        db.publish_provenance_migration(
            work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert "clone" in str(ei.value).lower() or "identity" in str(ei.value).lower()
    assert not (dest / "catalog.sqlite").exists()


def test_publish_refuses_changed_source_without_destination(tmp_path):
    data, report, work = _rehearse_ok(tmp_path)
    src = Path(report["source_catalog"])
    con = sqlite3.connect(str(src), isolation_level=None)
    try:
        con.execute(
            "INSERT INTO models(repo_id,status,numcopies) "
            "VALUES('org/changed','discovered',1)")
    finally:
        con.close()
    dest = tmp_path / "dest"
    with pytest.raises(RuntimeError) as ei:
        db.publish_provenance_migration(
            work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert "source" in str(ei.value).lower()
    assert not (dest / "catalog.sqlite").exists()


def test_publish_holds_source_lock_through_replace_blocks_writer(tmp_path):
    """Another writer cannot BEGIN IMMEDIATE between validation and replace."""
    data, report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    src = Path(report["source_catalog"])
    real_replace = os.replace
    writer_saw_busy = {"yes": False}

    def slow_replace(src_p, dst_p):
        # While publish holds the source lock, a concurrent writer must fail.
        barrier = threading.Barrier(2, timeout=5)
        result = {"err": None}

        def rival():
            barrier.wait()
            c = sqlite3.connect(str(src), isolation_level=None, timeout=0.05)
            try:
                try:
                    c.execute("BEGIN IMMEDIATE")
                    c.execute("ROLLBACK")
                except sqlite3.OperationalError as exc:
                    result["err"] = exc
            finally:
                c.close()

        t = threading.Thread(target=rival)
        t.start()
        barrier.wait()
        time.sleep(0.1)  # give rival a chance while we still hold the lock
        out = real_replace(src_p, dst_p)
        t.join(timeout=5)
        if result["err"] is not None:
            writer_saw_busy["yes"] = True
        return out

    with mock.patch("os.replace", side_effect=slow_replace):
        pub = db.publish_provenance_migration(
            work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert pub["status"] == "ok"
    assert (dest / "catalog.sqlite").is_file()
    assert writer_saw_busy["yes"] is True, (
        "concurrent writer must observe lock held through final replace"
    )


# ---------------------------------------------------------------------------
# 5–6. Repair exact identity + hub disagreement halt (both tiers)
# ---------------------------------------------------------------------------


def _migrated_clone(tmp_path):
    from tests.test_dec053_054_gate1_contracts import (
        _seed_frozen_v6, _catalog, _logical_identity, _close, _open_ro,
        _require_report,
    )
    data = _seed_frozen_v6(tmp_path / "src")
    idc = _open_ro(_catalog(data))
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        db.rehearse_provenance_migration(data, work, run_id="rep"),
        source_identity=ident,
    )
    con = sqlite3.connect(str(report["clone_catalog_path"]), isolation_level=None)
    con.execute("PRAGMA foreign_keys=ON")
    return con


def test_repair_wrong_epoch_halts_without_archive_mutation(tmp_path):
    con = _migrated_clone(tmp_path)
    try:
        before = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        rep = hash_repair.run_explicit_drive_repair(
            con, "d0", identity_epoch=99, identity_fingerprint=_h("f"))
        assert rep["status"] == "halted"
        assert "epoch" in (rep.get("detail") or "").lower()
        after = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        assert after == before
    finally:
        con.close()


def test_repair_missing_fingerprint_refuses(tmp_path):
    con = _migrated_clone(tmp_path)
    try:
        before = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        with pytest.raises(hash_repair.HashRepairError) as ei:
            hash_repair.run_explicit_drive_repair(
                con, "d0", identity_epoch=1, identity_fingerprint=None)
        assert "fingerprint" in str(ei.value).lower()
        after = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        assert after == before
    finally:
        con.close()


def test_repair_annex_hub_disagreement_halts_no_partial(tmp_path):
    """Tier-1 annex digest disagrees with non-null files.sha256 → halted, no mutations."""
    con = _migrated_clone(tmp_path)
    try:
        # Plant a null-digest raw row with annex key AND a conflicting Hub digest.
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','conflict.bin',50,?,'safetensors')", [_h("a")])
        annex = f"SHA256E-s50--{_h('d')}"  # digests to _h('d'), not _h('a')
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,annex_key,"
            "orig_sha256_provenance) "
            "VALUES('org/m','conflict.bin','conflict.bin','conflict.bin','d0',"
            "NULL,50,50,0,?,NULL)", [annex])
        before = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        rep = hash_repair.run_explicit_drive_repair(
            con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        assert rep["status"] == "halted"
        assert "disagree" in (rep.get("detail") or "").lower()
        after = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        assert after == before
        # No partial: shard.bin also unrepaired if it was pending in same run —
        # before snapshot includes post-insert state; equality is the pin.
    finally:
        con.close()


def test_repair_archive_head_hub_disagreement_halts(tmp_path):
    """Tier-2 archive-head digest disagrees with files.sha256 → halted."""
    import subprocess
    archive = tmp_path / "archive"
    archive.mkdir()
    subprocess.run(["git", "init"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=archive, check=True,
        capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=archive, check=True,
        capture_output=True)
    repo_dir = archive / "org" / "m"
    repo_dir.mkdir(parents=True)
    content = b"archive-head-unique-bytes"
    (repo_dir / "head.bin").write_bytes(content)
    subprocess.run(["git", "add", "-A"], cwd=archive, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "s"], cwd=archive, check=True, capture_output=True)
    real_digest = hashlib.sha256(content).hexdigest()
    hub_other = _h("a")
    assert real_digest != hub_other

    con = _migrated_clone(tmp_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO files(repo_id,rfilename,size_bytes,sha256,format) "
            "VALUES('org/m','head.bin',?,?,'aux')", [len(content), hub_other])
        con.execute(
            "INSERT OR REPLACE INTO archived(repo_id,rfilename,stored_name,stored_relpath,"
            "drive_label,orig_sha256,orig_bytes,stored_bytes,compressed,annex_key,"
            "orig_sha256_provenance) "
            "VALUES('org/m','head.bin','head.bin','head.bin','d0',"
            "NULL,?,?,0,NULL,NULL)", [len(content), len(content)])
        before = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        rep = hash_repair.run_explicit_drive_repair(
            con, "d0", identity_epoch=1, identity_fingerprint=_h("f"),
            archive_resolver=lambda *a, **k: archive,
        )
        assert rep["status"] == "halted"
        assert "disagree" in (rep.get("detail") or "").lower()
        after = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        assert after == before
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Remediation 2 — five independent permanent cases
# ---------------------------------------------------------------------------


def test_connect_refuses_existing_populated_v0_unchanged(tmp_path):
    """Existing populated user_version=0 file is not fresh; refuse without mutation."""
    data = tmp_path / "data"
    data.mkdir()
    path = data / "catalog.sqlite"
    _seed_versioned(path, 0)
    # Ensure it is populated (tables + rows), not an absent-file bootstrap.
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        n = con.execute("SELECT count(*) FROM models").fetchone()[0]
        assert n >= 1
        con.execute("PRAGMA user_version=0")
    finally:
        con.close()
    before = {
        "sha256": _sha_file(path),
        "size": path.stat().st_size,
        "user_version": _user_version(path),
        "tables": _tables(path),
    }
    assert before["user_version"] == 0
    db.configure(data, data / "state")
    with pytest.raises(RuntimeError) as ei:
        c = db.connect()
        c.close()
    msg = str(ei.value).lower()
    assert "clone-first" in msg or "rehearse" in msg or "will not auto-migrate" in msg
    after = {
        "sha256": _sha_file(path),
        "size": path.stat().st_size,
        "user_version": _user_version(path),
        "tables": _tables(path),
    }
    assert after == before


def test_repair_identity_race_rereads_under_lock(tmp_path):
    """Proxy-hook race: identity changes immediately before BEGIN IMMEDIATE."""
    con = _migrated_clone(tmp_path)
    try:
        db_path = Path(con.execute("PRAGMA database_list").fetchone()[2])
        before = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        flipped = {"done": False}

        class _RaceCon:
            """Wrap the real connection; fire concurrent identity flip on BEGIN."""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, *a, **k):
                s = str(sql).strip().upper()
                if (not flipped["done"]) and s.startswith("BEGIN"):
                    flipped["done"] = True
                    other = sqlite3.connect(str(db_path), isolation_level=None)
                    try:
                        other.execute(
                            "UPDATE drives SET identity_epoch=9, "
                            "identity_fingerprint=? WHERE drive_label='d0'",
                            [_h("9")],
                        )
                    finally:
                        other.close()
                return self._real.execute(sql, *a, **k)

            def __getattr__(self, name):
                return getattr(self._real, name)

        rep = hash_repair.run_explicit_drive_repair(
            _RaceCon(con), "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        assert flipped["done"] is True
        assert rep["status"] == "halted"
        detail = (rep.get("detail") or "").lower()
        assert "epoch" in detail or "fingerprint" in detail or "mismatch" in detail
        after = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        assert after == before
    finally:
        con.close()


def test_repair_matching_nonhex_with_unresolved_never_completes(tmp_path):
    """Matching non-hex fingerprints with unresolved work must halt, not complete."""
    con = _migrated_clone(tmp_path)
    try:
        bad = "Z" * 64
        assert not hash_repair._valid_identity_fingerprint(bad)
        con.execute(
            "UPDATE drives SET identity_fingerprint=? WHERE drive_label='d0'", [bad])
        con.execute(
            "UPDATE archived SET orig_sha256=NULL, annex_key=?, compressed=0, "
            "orig_sha256_provenance=NULL WHERE rfilename='shard.bin'",
            [f"SHA256E-s50--{_h('d')}"])
        before = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        rep = hash_repair.run_explicit_drive_repair(
            con, "d0", identity_epoch=1, identity_fingerprint=bad)
        assert rep["status"] == "halted"
        assert rep.get("applied", 0) == 0
        assert list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename")) == before
    finally:
        con.close()


def test_repair_matching_nonhex_zero_unresolved_never_completes(tmp_path):
    """Matching non-hex with zero unresolved rows must halt, not complete."""
    con = _migrated_clone(tmp_path)
    try:
        bad = "Z" * 64
        # Resolve all digests first so unresolved count is zero.
        hash_repair.run_explicit_drive_repair(
            con, "d0", identity_epoch=1, identity_fingerprint=_h("f"))
        assert con.execute(
            "SELECT count(*) FROM archived WHERE drive_label='d0' AND orig_sha256 IS NULL"
        ).fetchone()[0] == 0
        con.execute(
            "UPDATE drives SET identity_fingerprint=? WHERE drive_label='d0'", [bad])
        before = list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename"))
        rep = hash_repair.run_explicit_drive_repair(
            con, "d0", identity_epoch=1, identity_fingerprint=bad)
        assert rep["status"] == "halted"
        assert rep["status"] != "complete"
        assert list(con.execute(
            "SELECT rfilename,orig_sha256,orig_sha256_provenance FROM archived "
            "ORDER BY rfilename")) == before
    finally:
        con.close()


def test_publish_refuses_clone_index_drop_and_check_weaken(tmp_path):
    """Dropping an index or weakening a CHECK after rehearsal blocks publication."""
    data, report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest-idx"
    clone = Path(report["clone_catalog_path"])

    # Case A: drop a secondary index on the clone.
    c = sqlite3.connect(str(clone), isolation_level=None)
    try:
        c.execute("DROP INDEX IF EXISTS idx_placement_proposals_plan")
    finally:
        c.close()
    with pytest.raises(RuntimeError) as ei:
        db.publish_provenance_migration(
            work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert "index" in str(ei.value).lower() or "schema" in str(ei.value).lower() \
        or "clone" in str(ei.value).lower()
    assert not (dest / "catalog.sqlite").exists()

    # Restore via fresh rehearsal for CHECK weaken case.
    data2, report2, work2 = _rehearse_ok(tmp_path / "check")
    dest2 = tmp_path / "dest-check"
    clone2 = Path(report2["clone_catalog_path"])
    c2 = sqlite3.connect(str(clone2), isolation_level=None)
    try:
        # Rebuild placement_proposals without derivation_mode CHECK (weaken).
        cols = [r[1] for r in c2.execute("PRAGMA table_info(placement_proposals)")]
        rows = c2.execute(
            f"SELECT {','.join(cols)} FROM placement_proposals").fetchall()
        c2.execute("PRAGMA foreign_keys=OFF")
        c2.execute(
            "CREATE TABLE placement_proposals__weak ("
            "proposal_id VARCHAR PRIMARY KEY NOT NULL,"
            "plan_id VARCHAR NOT NULL,"
            "based_on_revision INTEGER NOT NULL,"
            "lifecycle VARCHAR NOT NULL,"
            "canonical_hash VARCHAR NOT NULL,"
            "mutation_kind VARCHAR NOT NULL,"
            "mutation_args_json TEXT NOT NULL,"
            "serializer_version VARCHAR NOT NULL,"
            "derivation_mode VARCHAR,"  # unconstrained — CHECK removed
            "execution_config_hash VARCHAR,"
            "created_at TIMESTAMP,"
            "approved_at TIMESTAMP,"
            "superseded_at TIMESTAMP,"
            "requirement_set_hash VARCHAR,"
            "semantic_input_hash VARCHAR,"
            "selection_before_hash VARCHAR,"
            "selection_after_hash VARCHAR,"
            "capacity_mode VARCHAR,"
            "policy_version VARCHAR,"
            "solver_version VARCHAR,"
            "gate_b_code VARCHAR"
            ")"
        )
        # Best-effort copy common columns.
        weak_cols = [r[1] for r in c2.execute(
            "PRAGMA table_info(placement_proposals__weak)")]
        common = [c for c in cols if c in weak_cols]
        for row in rows:
            data_row = dict(zip(cols, row))
            c2.execute(
                f"INSERT INTO placement_proposals__weak({','.join(common)}) "
                f"VALUES({','.join('?' for _ in common)})",
                [data_row[c] for c in common],
            )
        c2.execute("DROP TABLE placement_proposals")
        c2.execute(
            "ALTER TABLE placement_proposals__weak RENAME TO placement_proposals")
        c2.execute("PRAGMA foreign_keys=ON")
    finally:
        c2.close()
    with pytest.raises(RuntimeError) as ei2:
        db.publish_provenance_migration(
            work2, dest2, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert any(k in str(ei2.value).lower() for k in (
        "check", "derivation", "schema", "clone", "token", "column"))
    assert not (dest2 / "catalog.sqlite").exists()


def test_publish_refuses_source_schema_drift_since_snapshot(tmp_path):
    """Source schema change after snapshot (drop index) refuses publication."""
    data, report, work = _rehearse_ok(tmp_path)
    src = Path(report["source_catalog"])
    # Drop an index on the frozen-v6 source if present; otherwise add then drop a marker.
    s = sqlite3.connect(str(src), isolation_level=None)
    try:
        s.execute(
            "CREATE INDEX IF NOT EXISTS idx_models_status_drift ON models(status)")
        s.execute("DROP INDEX idx_models_status_drift")
        # Force a durable schema difference vs snapshot: leave a new empty table.
        s.execute("CREATE TABLE IF NOT EXISTS schema_drift_marker(x INTEGER)")
    finally:
        s.close()
    dest = tmp_path / "dest-src-drift"
    with pytest.raises(RuntimeError) as ei:
        db.publish_provenance_migration(
            work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    msg = str(ei.value).lower()
    assert "schema" in msg or "drift" in msg or "source" in msg
    assert not (dest / "catalog.sqlite").exists()


# ---------------------------------------------------------------------------
# Remediation 3 — staging seam, WAL checkpoint, remigration validation
# ---------------------------------------------------------------------------


def test_publish_refuses_clone_mutation_at_staging_seam(tmp_path):
    """Mutating the staged catalog at validation seam must refuse; dest absent."""
    data, report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest-seam"
    real_metrics = db._catalog_snapshot_metrics_con
    mutated = {"yes": False}

    def evil_metrics(con):
        # Staging validation uses the retained locked connection — corrupt via it.
        if not mutated["yes"]:
            try:
                db_list = con.execute("PRAGMA database_list").fetchone()
                main_path = Path(db_list[2]) if db_list and db_list[2] else None
            except Exception:
                main_path = None
            if main_path is not None and "publish-staging" in main_path.name:
                mutated["yes"] = True
                con.execute(
                    "INSERT INTO models(repo_id,status,numcopies) "
                    "VALUES('org/stage-tamper','discovered',1)")
        return real_metrics(con)

    with mock.patch.object(db, "_catalog_snapshot_metrics_con", side_effect=evil_metrics):
        with pytest.raises(RuntimeError) as ei:
            db.publish_provenance_migration(
                work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert mutated["yes"] is True
    msg = str(ei.value).lower()
    assert any(k in msg for k in (
        "staged", "identity", "remigrat", "row", "clone", "logical"))
    assert not (dest / "catalog.sqlite").exists()


def test_publish_succeeds_after_source_wal_checkpoint_without_logical_change(tmp_path):
    """WAL checkpoint/truncate after rehearse must not block publication."""
    from tests.test_dec053_054_gate1_contracts import (
        _seed_frozen_v6, _catalog, _logical_identity, _close, _open_ro,
        _require_report,
    )
    data = _seed_frozen_v6(tmp_path / "src", enable_wal=True)
    path = _catalog(data)
    # Leave a committed WAL-resident marker, then rehearse.
    keeper = sqlite3.connect(str(path), isolation_level=None)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute(
        "INSERT INTO models(repo_id,status,numcopies) "
        "VALUES('org/wal-pub','discovered',1)")
    idc = _open_ro(path)
    try:
        ident = _logical_identity(idc)
    finally:
        _close(idc)
    work = tmp_path / "work"
    work.mkdir()
    report = _require_report(
        db.rehearse_provenance_migration(data, work, run_id="wal-pub"),
        source_identity=ident,
    )
    _close(keeper)
    # Checkpoint/truncate physical WAL without logical content change.
    src = Path(report["source_catalog"])
    c = sqlite3.connect(str(src), isolation_level=None)
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        c.close()
    dest = tmp_path / "dest-wal"
    pub = db.publish_provenance_migration(
        work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert pub["status"] == "ok"
    assert (dest / "catalog.sqlite").is_file()
    dcon = sqlite3.connect(
        f"file:{(dest / 'catalog.sqlite').resolve().as_posix()}?mode=ro", uri=True)
    try:
        assert dcon.execute(
            "SELECT 1 FROM models WHERE repo_id='org/wal-pub'").fetchone()
        assert int(dcon.execute("PRAGMA user_version").fetchone()[0]) > 6
    finally:
        dcon.close()


def test_publish_staging_exclusive_lock_blocks_adversary_through_replace(tmp_path):
    """Second SQLite writer is blocked on staging through final os.replace."""
    data, report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest-lock"
    real_replace = os.replace
    adversary = {"blocked": False, "altered": False, "staging": None}

    def replace_hook(src_p, dst_p):
        staging = Path(src_p)
        adversary["staging"] = staging
        # Adversary tries to open and mutate staging while publication still holds
        # the exclusive SQLite lock on that exact database.
        try:
            evil = sqlite3.connect(str(staging), isolation_level=None, timeout=0.05)
            try:
                evil.execute("BEGIN IMMEDIATE")
                evil.execute(
                    "INSERT INTO models(repo_id,status,numcopies) "
                    "VALUES('org/adversary','discovered',1)")
                adversary["altered"] = True
                evil.execute("COMMIT")
            finally:
                evil.close()
        except sqlite3.OperationalError:
            adversary["blocked"] = True
        return real_replace(src_p, dst_p)

    with mock.patch.object(os, "replace", side_effect=replace_hook):
        # Also patch where publish looks it up (module-level os).
        with mock.patch("modelark.core.db.os.replace", side_effect=replace_hook):
            pub = db.publish_provenance_migration(
                work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True)
    assert pub["status"] == "ok"
    assert adversary["blocked"] is True, (
        "adversary must be busy/blocked while exclusive staging lock is held"
    )
    assert adversary["altered"] is False, (
        "adversary must not alter the staging artifact"
    )
    dest_cat = dest / "catalog.sqlite"
    assert dest_cat.is_file()
    # Published catalog must be the original unmodified migration (no adversary row).
    dcon = sqlite3.connect(
        f"file:{dest_cat.resolve().as_posix()}?mode=ro", uri=True)
    try:
        assert dcon.execute(
            "SELECT 1 FROM models WHERE repo_id='org/adversary'"
        ).fetchone() is None
        assert int(dcon.execute("PRAGMA user_version").fetchone()[0]) > 6
    finally:
        dcon.close()
