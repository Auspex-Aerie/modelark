"""PR-04 / #35-C copied-catalog acceptance (tests-first, RFC-002 / DEC-049).

The migrated real catalog contains NO fabricated anchors, so #35-C cannot claim naive equality with
legacy optimistic capacity. Copied-catalog replay instead proves:

  * fail-closed ``unknown`` where evidence is absent (pre-preparation);
  * deterministic results after synthetic/copied evidence preparation;
  * no mutation of the SOURCE catalog (byte-identical before/after).

Replay runs the OFFLINE seam path (no mounts, ``observe`` returns ``None``): a migrated drive derives
``unknown``; a prepared ``dedicated_local`` drive with a matching clean anchor derives ``anchor``.

RED until the seam + evidence-fed ``plan_capacity`` exist.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3

from modelark.core import db
from modelark import capacity, reconcile

try:
    from modelark import admission
    _HAS_ADMISSION = True
except ImportError as exc:                       # ONLY the missing shell — a real import/init defect surfaces
    if "admission" not in f"{getattr(exc, 'name', '') or ''} {exc}":
        raise
    admission = None
    _HAS_ADMISSION = False

_FP = "a" * 64


def _require_admission():
    if not _HAS_ADMISSION:
        raise AssertionError("PR-04 must add modelark/admission.py (see test_pr04_admission_seam)")


# Restore the db module globals around EVERY test so a mid-setup failure cannot leak a temp path into a
# later test (autouse under pytest; the script runner save/restores in main()).
try:
    import pytest

    @pytest.fixture(autouse=True)
    def _isolate_db_globals():
        saved = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)
        try:
            yield
        finally:
            db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = saved
except ImportError:                              # the plain script runner does not need pytest
    pass


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_source(tmp_path):
    """A migrated v3 catalog on disk: one primary drive + one finalized single-copy repo, no anchors."""
    saved = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)
    try:                                                         # restore globals even if setup fails
        db.CATALOG_DIR = tmp_path
        db.DB_PATH = tmp_path / "catalog.sqlite"
        db.STATE_DIR = tmp_path / "state"
        con = db.connect()
        try:
            assert con.execute("PRAGMA user_version").fetchone()[0] == 3, "fixture must be a v3 catalog"
            con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
            con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes) "
                        "VALUES('drive-00',1000000,500000)")
            con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','drive-00')")
            con.execute("INSERT INTO models(repo_id,numcopies) VALUES('org/model',1)")
            con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES('org/model','2026-01-01')")
            con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
                        "VALUES('org/model','model.gguf',100,'gguf',NULL)")
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")       # self-contained single file for copy/hash
        finally:
            con.close()
    finally:
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = saved
    return tmp_path / "catalog.sqlite"


def _replay_evidence(con):
    """Derive admission evidence for the plan's drives via the OFFLINE seam path (no mounts)."""
    labels = [row[0] for row in con.execute(
        "SELECT drive_label FROM plan_drives WHERE plan_id='ark' ORDER BY drive_label").fetchall()]
    return admission.preview_by_drive(con, labels, observe=lambda label: None, now="2026-07-23")


def _prepare_synthetic_evidence(con):
    """Copied-catalog preparation: bless the drive dedicated_local for the current epoch and append a
    matching clean anchor. This is the deliberate, audited test analogue of `drive reconcile`."""
    con.execute("UPDATE drives SET identity_epoch=1, write_generation=1, filesystem_capacity_bytes=1000000, "
                "identity_fingerprint=?, write_authority='dedicated_local' WHERE drive_label='drive-00'", [_FP])
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                "VALUES('drive-00',1,1,'reconcile')")
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
        "observed_at) VALUES('drive-00',1,1,100000,1000000,?, 'dedicated_local','p','p','2026-01-02')", [_FP])


def test_copied_catalog_fail_closed_then_prepared_deterministic(tmp_path):
    _require_admission()
    source = _build_source(tmp_path)
    source_digest = _sha256(source)

    replica = tmp_path / "replica.sqlite"
    shutil.copy2(source, replica)
    con = sqlite3.connect(str(replica), isolation_level=None)

    # (a) pre-preparation: no anchors -> fail-closed unknown, NOT optimistic legacy capacity
    graph = reconcile.reconcile_plan(con, "ark")
    before = capacity.plan_capacity(con, graph, evidence_by_drive=_replay_evidence(con))
    assert not before.feasible
    assert any(item.evidence_code == "CAPACITY_EVIDENCE_UNKNOWN" for item in before.failures), \
        "absent evidence must fail closed as unknown, never as observed free-space exhaustion"

    # (b) after preparing synthetic/copied evidence: deterministic and feasible
    _prepare_synthetic_evidence(con)
    graph = reconcile.reconcile_plan(con, "ark")
    first = capacity.plan_capacity(con, graph, evidence_by_drive=_replay_evidence(con))
    second = capacity.plan_capacity(con, graph, evidence_by_drive=_replay_evidence(con))
    assert first.feasible, [item.code for item in first.failures]
    assert first.to_dict() == second.to_dict()                  # deterministic replay
    drive = next(item for item in capacity.inspect_drives(con, "ark", evidence_by_drive=_replay_evidence(con))
                 if item.drive_label == "drive-00")
    assert drive.evidence_kind == "anchor" and drive.usable_now == 50000   # 100000 anchor − 50000 floor
    con.close()

    # (c) the SOURCE catalog was never mutated by replay/preparation
    assert _sha256(source) == source_digest, "copied-catalog replay must not mutate the source catalog"


def main():
    import shutil as _shutil
    import tempfile
    from pathlib import Path
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        saved = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)
        tmp = Path(tempfile.mkdtemp(prefix="mark-pr04-cc-"))
        try:
            fn(tmp)
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001
            failed.append(name)
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
        finally:
            db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = saved   # never leak a temp path into a later test
            _shutil.rmtree(tmp, ignore_errors=True)            # the standalone runner cleans its temp trees
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
