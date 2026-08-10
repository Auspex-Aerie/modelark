"""INC-029 Gate-1 contracts for whole-PR Findings 2, 6, and 8.

Contracts only: production is intentionally unfixed.  These tests must remain
behavior-specific red at the Gate-1 tip and turn green only in an authorized
Gate 2.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from modelark.core import db
from test_dec053_054_gate1_contracts import _catalog, _logical_identity, _seed_frozen_v6
from test_dec053_054_gate2_remediation import _rehearse_ok


_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _sidecar(catalog: Path, suffix: str) -> Path:
    return catalog.with_name(catalog.name + suffix)


def _bundle_bytes(catalog: Path) -> dict[str, bytes | None]:
    paths = {
        "source_db": catalog,
        "source_wal": _sidecar(catalog, "-wal"),
        "source_shm": _sidecar(catalog, "-shm"),
    }
    return {
        name: path.read_bytes() if path.is_file() else None
        for name, path in paths.items()
    }


def _manifest_fingerprint(path: Path, payload: bytes | None) -> dict:
    return {
        "path": str(path.resolve()) if payload is not None else None,
        "size": len(payload) if payload is not None else None,
        "sha256": hashlib.sha256(payload).hexdigest() if payload is not None else None,
        "present": payload is not None,
    }


def _stopped_hot_wal_source(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes | None]]:
    """Make a frozen-v6 bundle whose committed WAL writer exits without close."""
    data = _seed_frozen_v6(tmp_path / "src")
    catalog = _catalog(data)
    script = """
import os
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1], isolation_level=None)
assert con.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
con.execute("PRAGMA wal_autocheckpoint=0")
con.execute(
    "INSERT INTO models(repo_id,status,numcopies) "
    "VALUES('org/wal-resident','discovered',1)"
)
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(catalog)], check=True)
    before = _bundle_bytes(catalog)
    assert before["source_db"] is not None
    assert before["source_wal"] is not None, "fixture must contain a stopped-writer hot WAL"
    assert before["source_shm"] is not None, "fixture must retain the stopped writer SHM"
    return data, catalog, before


def _logically_unchanged_stopped_wal(catalog: Path) -> dict[str, bytes | None]:
    """Add a real WAL transaction that leaves user-table identity unchanged."""
    script = """
import os
import sqlite3
import sys

con = sqlite3.connect(sys.argv[1], isolation_level=None)
assert con.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() == "wal"
con.execute("PRAGMA wal_autocheckpoint=0")
con.execute("BEGIN IMMEDIATE")
con.execute("UPDATE models SET status='discovered' WHERE repo_id='org/m'")
con.execute("UPDATE models SET status='archived' WHERE repo_id='org/m'")
con.execute("COMMIT")
assert con.total_changes == 2
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(catalog)], check=True)
    before = _bundle_bytes(catalog)
    assert all(before.values()), "fixture must retain a stopped main/WAL/SHM bundle"
    return before


@pytest.mark.parametrize("suffix", _SIDECAR_SUFFIXES)
def test_a01_publish_refuses_preexisting_destination_sidecar_before_staging(
    tmp_path, suffix,
):
    """A sidecar-only destination is occupied and must be refused before staging."""
    _data, _report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    dest_catalog = dest / "catalog.sqlite"
    sentinel = _sidecar(dest_catalog, suffix)
    sentinel_bytes = f"foreign{suffix}".encode()
    sentinel.write_bytes(sentinel_bytes)
    real_replace = os.replace

    error = None
    with mock.patch.object(db.os, "replace", wraps=real_replace) as replace_spy:
        try:
            db.publish_provenance_migration(
                work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True
            )
        except RuntimeError as exc:
            error = exc

    assert error is not None, f"sidecar-only destination {suffix} must be refused"
    assert replace_spy.call_count == 0, "refusal must happen before atomic publication"
    assert sentinel.read_bytes() == sentinel_bytes
    assert not dest_catalog.exists()
    assert not (dest / ".catalog.sqlite.publish-staging").exists()


def test_a02_publish_rechecks_destination_sidecars_immediately_before_replace(tmp_path):
    """A sidecar arriving after the initial guard must still prevent replace."""
    _data, report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    sentinel = _sidecar(dest_catalog, "-wal")
    sentinel_bytes = b"foreign-wal-after-first-guard"
    real_remigrate = db._remigrate_snapshot_to_expected
    real_replace = os.replace
    injected = {"yes": False}
    evidence_paths = [
        Path(report["snapshot_path"]),
        Path(report["clone_catalog_path"]),
        Path(report["manifest_path"]),
        Path(report["work_dir"]) / "report.json",
    ]
    evidence_before = {path: path.read_bytes() for path in evidence_paths}

    def inject_after_initial_guard(*args, **kwargs):
        out = real_remigrate(*args, **kwargs)
        sentinel.write_bytes(sentinel_bytes)
        injected["yes"] = True
        return out

    error = None
    with mock.patch.object(
        db, "_remigrate_snapshot_to_expected", side_effect=inject_after_initial_guard
    ), mock.patch.object(db.os, "replace", wraps=real_replace) as replace_spy:
        try:
            db.publish_provenance_migration(
                work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True
            )
        except RuntimeError as exc:
            error = exc

    assert injected["yes"] is True, "contract must inject after the first destination guard"
    assert error is not None, "late destination sidecar must be refused"
    assert replace_spy.call_count == 0, "late guard must run immediately before replace"
    assert sentinel.read_bytes() == sentinel_bytes
    assert not dest_catalog.exists()
    assert {path: path.read_bytes() for path in evidence_paths} == evidence_before
    rollback = Path(report["work_dir"]) / "rollback" / "catalog.sqlite.pre-publish"
    assert rollback.read_bytes() == Path(report["snapshot_path"]).read_bytes()


@pytest.mark.parametrize("attack", ("foreign_sidecar", "rival_main"))
def test_a03_publish_revalidates_exact_destination_path_before_releasing_staging_lock(
    tmp_path, attack,
):
    """Post-replace pathname attacks cannot be reported as successful publication."""
    _data, report, work = _rehearse_ok(tmp_path)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    real_replace = os.replace
    attacked = {"yes": False}

    def replace_then_attack(src, dst):
        out = real_replace(src, dst)
        if Path(dst) == dest_catalog:
            if attack == "foreign_sidecar":
                _sidecar(dest_catalog, "-wal").write_bytes(b"foreign-post-replace-wal")
            else:
                rival = tmp_path / "rival.sqlite"
                rival_con = sqlite3.connect(str(rival), isolation_level=None)
                try:
                    rival_con.execute("PRAGMA user_version=1")
                    rival_con.execute("CREATE TABLE rival_only(value TEXT)")
                finally:
                    rival_con.close()
                real_replace(rival, dest_catalog)
            attacked["yes"] = True
        return out

    error = None
    with mock.patch.object(db.os, "replace", side_effect=replace_then_attack):
        try:
            db.publish_provenance_migration(
                work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True
            )
        except RuntimeError as exc:
            error = exc

    assert attacked["yes"] is True, "contract must attack the published pathname"
    assert error is not None, f"post-replace {attack} must prevent a success report"
    if attack == "rival_main":
        fresh = sqlite3.connect(f"file:{dest_catalog.resolve().as_posix()}?mode=ro", uri=True)
        try:
            assert int(fresh.execute("PRAGMA user_version").fetchone()[0]) != int(
                report["clone_user_version"]
            )
        finally:
            fresh.close()


def test_b01_hot_wal_rehearsal_preserves_exact_canonical_bundle_and_prework_manifest(
    tmp_path,
):
    """Rehearsal recovers a copy while preserving the stopped source byte-for-byte."""
    data, catalog, before = _stopped_hot_wal_source(tmp_path)
    work = tmp_path / "work"
    report = db.rehearse_provenance_migration(data, work, run_id="hot-wal")

    assert _bundle_bytes(catalog) == before, "canonical main/WAL/SHM changed during rehearsal"
    manifest = report["manifest"]
    expected_manifest = {
        "source_db": _manifest_fingerprint(catalog, before["source_db"]),
        "source_wal": _manifest_fingerprint(
            _sidecar(catalog, "-wal"), before["source_wal"]
        ),
        "source_shm": _manifest_fingerprint(
            _sidecar(catalog, "-shm"), before["source_shm"]
        ),
    }
    for key, expected in expected_manifest.items():
        assert manifest[key] == expected, f"{key} must describe the pre-work source bundle"

    clone = sqlite3.connect(str(report["clone_catalog_path"]), isolation_level=None)
    try:
        assert clone.execute(
            "SELECT 1 FROM models WHERE repo_id='org/wal-resident'"
        ).fetchone() == (1,)
    finally:
        clone.close()


def test_b02_rehearsal_never_opens_canonical_source_recovery_capable(tmp_path):
    """Only a copied bundle may receive a recovery-capable SQLite open."""
    data, catalog, _before = _stopped_hot_wal_source(tmp_path)
    work = tmp_path / "work"
    real_connect = sqlite3.connect
    poisoned = {"count": 0}

    def guarded_connect(database, *args, **kwargs):
        raw = os.fspath(database)
        is_read_only_uri = raw.startswith("file:") and "mode=ro" in raw
        raw_path = raw[5:].split("?", 1)[0] if raw.startswith("file:") else raw
        try:
            is_canonical = Path(raw_path).resolve() == catalog.resolve()
        except (OSError, RuntimeError, ValueError):
            is_canonical = False
        if is_canonical and not is_read_only_uri:
            poisoned["count"] += 1
            raise AssertionError("rehearsal opened canonical source recovery-capable")
        return real_connect(database, *args, **kwargs)

    with mock.patch.object(db.sqlite3, "connect", side_effect=guarded_connect):
        report = db.rehearse_provenance_migration(data, work, run_id="poison-source")

    assert poisoned["count"] == 0
    clone = real_connect(str(report["clone_catalog_path"]), isolation_level=None)
    try:
        assert clone.execute(
            "SELECT 1 FROM models WHERE repo_id='org/wal-resident'"
        ).fetchone() == (1,)
    finally:
        clone.close()


@pytest.mark.parametrize("outcome", ("success", "late_refusal"))
def test_b03_publication_preserves_verified_exact_prelock_rollback_bundle(
    tmp_path, outcome,
):
    """Publication preserves and validates exact pre-lock bytes for every outcome."""
    data, report, work = _rehearse_ok(tmp_path)
    catalog = _catalog(data)
    before = _logically_unchanged_stopped_wal(catalog)
    dest = tmp_path / "dest"
    dest_catalog = dest / "catalog.sqlite"
    real_replace = os.replace
    attacked = {"yes": False}

    def replace_then_inject_late_refusal(src, dst):
        result = real_replace(src, dst)
        if outcome == "late_refusal" and Path(dst) == dest_catalog:
            _sidecar(dest_catalog, "-wal").write_bytes(b"foreign-post-replace-wal")
            attacked["yes"] = True
        return result

    published = None
    error = None
    with mock.patch.object(db.os, "replace", side_effect=replace_then_inject_late_refusal):
        try:
            published = db.publish_provenance_migration(
                work, dest, confirm_stopped="MODELARK-STOPPED", writers_stopped=True
            )
        except RuntimeError as exc:
            error = exc

    if outcome == "success":
        assert error is None
        assert published is not None and published["status"] == "ok"
    else:
        assert attacked["yes"] is True, "contract must reach the post-replace refusal seam"
        assert error is not None, "late destination conflict must refuse publication"
        assert published is None

    bundle_dir = Path(report["work_dir"]) / "rollback" / "source-bundle.pre-publish"
    manifest_path = bundle_dir / "manifest.json"
    assert manifest_path.is_file(), "publication must retain a verified bundle manifest"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["status"] == "validated"
    assert manifest["source_content_identity"] == report["source_content_identity"]

    source_paths = {
        "source_db": catalog,
        "source_wal": _sidecar(catalog, "-wal"),
        "source_shm": _sidecar(catalog, "-shm"),
    }
    copied_paths = {
        "source_db": bundle_dir / "catalog.sqlite",
        "source_wal": bundle_dir / "catalog.sqlite-wal",
        "source_shm": bundle_dir / "catalog.sqlite-shm",
    }
    for key, copied in copied_paths.items():
        entry = manifest["artifacts"][key]
        assert entry["source_path"] == str(source_paths[key].resolve())
        assert Path(entry["rollback_path"]).resolve() == copied.resolve()
        assert entry["present"] is True
        assert entry["size"] == len(before[key])
        assert entry["sha256"] == hashlib.sha256(before[key]).hexdigest()
        assert copied.read_bytes() == before[key]

    # Recover only a disposable copy. The exact retained rollback bundle must
    # remain byte-identical after the recoverability proof.
    recovery_dir = tmp_path / f"rollback-recovery-{outcome}"
    recovery_dir.mkdir()
    for copied in copied_paths.values():
        shutil.copy2(copied, recovery_dir / copied.name)
    recovered = sqlite3.connect(str(recovery_dir / "catalog.sqlite"), isolation_level=None)
    try:
        assert recovered.execute(
            "SELECT status FROM models WHERE repo_id='org/m'"
        ).fetchone() == ("archived",)
        assert _logical_identity(recovered) == report["source_content_identity"]
    finally:
        recovered.close()
    assert {
        key: copied.read_bytes() for key, copied in copied_paths.items()
    } == before


def test_c01_rehearsal_report_consumes_backfill_returned_classification(tmp_path):
    """The report threads measured backfill counts; it may not requery substitutes."""
    data = _seed_frozen_v6(tmp_path / "src")
    work = tmp_path / "work"
    distinctive = {
        "hub_confirmed": 101,
        "legacy_unknown": 202,
        "null_digest": 303,
        "disagreement": 0,
    }
    real_backfill = db._apply_provenance_backfill

    def measured_backfill(con):
        measured = real_backfill(con)
        assert measured["disagreement"] == 0
        return dict(distinctive)

    with mock.patch.object(
        db, "_apply_provenance_backfill", side_effect=measured_backfill
    ) as backfill_spy:
        report = db.rehearse_provenance_migration(data, work, run_id="classification")

    assert backfill_spy.call_count == 1, "contract must exercise the real migration backfill seam"
    assert report["classification"] == distinctive
    on_disk = json.loads((Path(report["work_dir"]) / "report.json").read_text())
    assert on_disk["classification"] == distinctive
