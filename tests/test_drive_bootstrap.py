"""PR-03c1 contract — drive identity BOOTSTRAP + first clean anchor + sessionless dirty RECOVERY
(RFC-002 / DEC-049 issue #35-B residual; the smallest usable predecessor to the #35-C authority cutover).

The regressions here are written against REAL disk evidence, not a single fake fingerprint: the identity
fingerprint folds ``filesystem_capacity_bytes`` in, so every decision compares the STABLE identity
(fs/annex UUID) — never the capacity-bearing fingerprint — and a capacity change is an explicit epoch
transition that holds both the old and prospective-new drive fences and persists the new fingerprint.
Anchors are published from a FRESH post-inventory observation, so a drive changing during inventory
leaves no anchor.

Encoded invariants:
  * bootstrap establishes proven identity + dedicated_local authority + a first anchor storing the RAW
    post-preparation observed free (< capacity);
  * dedicated_local is an explicit assertion (never a probe result): dedicated=False persists no
    authoritative evidence and refuses to downgrade an authoritative drive;
  * a migrated row (fingerprint NULL) is adopted only when the live stable identity equals the persisted
    fs/annex UUID — different media under an existing label refuses (DEF-029 reuse deferred);
  * one atomic bootstrap transaction (a crash before it leaves the drive unknown/anchorless), never a
    route through the ordinary drive_mutation envelope;
  * bounded, report-only full reconciliation (present/debris/missing/extra) that never auto-deletes and
    never counts an unproved copy present; offline drives stay valid unknown (Drive-02 not fabricated);
  * drift-gated refresh: within the versioned diagnostic tolerance re-anchor the fresh free; above
    tolerance -> DRIVE_FREE_DRIFT unless --accept-drift re-anchors under a distinct op code;
  * capacity change -> epoch transition to (new_epoch, generation 1) persisting the new fingerprint;
  * sessionless dirty recovery republishes the existing generation's anchor (session-attributed = #39);
  * the anchor uses the FINAL observation; a changed/unproven final observation publishes no anchor.

Proposed production API (the names are the contract to confirm): modelark/drive_bootstrap.py exposing
reconcile_drive(con, label, *, now, dedicated=False, accept_drift=False), the seams _live_evidence /
_inventory / _annex_key_present, free_drift_tolerance_v1, and a single `drive reconcile <label>` command.

Disposable temp trees / synthetic drives / mocked physical seams only — never a live catalog/drive.
"""
from __future__ import annotations

import importlib
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from modelark.core import db
from modelark import capacity_evidence
from modelark import drive_fence
from modelark import drive_mutation as dm
from modelark import register

_FP = "a" * 64
_FS, _ANX, _SER = "fs-uuid-1", "annex-uuid-1", "serial-1"


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


@contextmanager
def _catalog(tmp_path):
    saved = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    db.STATE_DIR = tmp_path / "state"
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3, "fixture must be a v3 catalog"
    try:
        yield con
    finally:
        con.close()
        db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = saved


def _bootstrap():
    """Import the PR-03c1 neutral module, or fail with the reviewed reason (implementation follows the
    Gate-1 contract)."""
    try:
        return importlib.import_module("modelark.drive_bootstrap")
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "PR-03c1 must add the neutral module modelark/drive_bootstrap.py exposing a single "
            f"reconcile_drive(con, label, *, now, dedicated=..., accept_drift=...) operation: {exc}")


def _drive_row(con, label="drive-00", *, fs_uuid=_FS, annex_uuid=_ANX, serial=_SER,
               capacity=1000, free=940):
    """A registered/migrated drive: topology + stable identifiers + nominal capacity only, no proven
    identity/authority — the valid `unknown` state until `drive reconcile` bootstraps it."""
    con.execute("INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial,capacity_bytes,free_bytes) "
                "VALUES(?,?,?,?,?,?)", [label, fs_uuid, annex_uuid, serial, capacity, free])


def _proven_drive(con, label="drive-00", *, epoch=1, generation=0, fp=_FP, fscap=1000, free=900,
                  fs_uuid=_FS, annex_uuid=_ANX):
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,identity_epoch,write_generation,"
                "filesystem_capacity_bytes,identity_fingerprint,write_authority,fs_uuid,annex_uuid) "
                "VALUES(?,?,?,?,?,?,?, 'dedicated_local', ?,?)",
                [label, fscap, free, epoch, generation, fscap, fp, fs_uuid, annex_uuid])


def _dirty_gen(con, label="drive-00", *, epoch=1, gen=1, op="reconcile", owner=None, token=None):
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code,"
                "owner_session_id,owner_fencing_token) VALUES(?,?,?,?,?,?)",
                [label, epoch, gen, op, owner, token])


def _anchor(con, label="drive-00", *, epoch=1, gen=1, free=900, fscap=1000, fp=_FP,
            now="2026-01-01 00:00:00"):
    con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
                "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
                "observed_at) VALUES(?,?,?,?,?,?, 'dedicated_local','proof','fence', ?)",
                [label, epoch, gen, free, fscap, fp, now])


def _catalogued(con, repo, rfile, *, size=5):
    con.execute("INSERT OR IGNORE INTO models(repo_id) VALUES(?)", [repo])
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) VALUES(?,?,?, 'safetensors')",
                [repo, rfile, size])


def _archived(con, repo, rfile, relpath, label, *, annex_key=None, compressed=0):
    con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
                "orig_bytes,stored_bytes,compressed,annex_key) VALUES(?,?,?,?,?,?,?,?,?)",
                [repo, rfile, relpath, relpath, label, 5, 5, compressed, annex_key])


def _ev(*, fp=_FP, fs_uuid=_FS, annex_uuid=_ANX, serial=_SER, capacity=1000, free=940,
        alloc_unit=4096, proven=True):
    """A mocked live-evidence snapshot; .observation() yields a real dm.Observation for the anchor."""
    ev = mock.Mock(fs_uuid=fs_uuid, annex_uuid=annex_uuid, serial=serial, capacity=capacity,
                   free=free, alloc_unit=alloc_unit, fingerprint=fp, proven=proven)
    ev.observation.return_value = dm.Observation(proven, free, capacity, fp, "proof", "proof")
    return ev


def _clean_inv(**kw):
    fields = dict(present=[], missing=[], debris=[], extra=[], complete=True)
    fields.update(kw)
    return mock.Mock(**fields)


def _real_fp(capacity):
    return capacity_evidence.identity_fingerprint_v1(
        fs_uuid=_FS, annex_uuid=_ANX, serial=_SER, filesystem_capacity_bytes=capacity)


# =========================================================== identity establishment + write authority

def test_bootstrap_local_dedicated_establishes_identity_authority_and_anchor(tmp_path):
    """A local drive bootstrapped WITH the dedicated_local assertion persists proven identity,
    dedicated_local authority, and a first clean anchor whose free is the observed post-preparation
    available (strictly < capacity), in one atomic step."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence", return_value=_ev(fp=_FP, capacity=1000, free=940)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
        row = con.execute("SELECT identity_epoch,write_generation,filesystem_capacity_bytes,"
                          "identity_fingerprint,write_authority FROM drives WHERE drive_label='drive-00'"
                          ).fetchone()
        assert row == (1, 1, 1000, _FP, "dedicated_local"), row
        anchor = con.execute("SELECT identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
                             "write_authority FROM drive_clean_anchors WHERE drive_label='drive-00'"
                             ).fetchone()
        assert anchor == (1, 1, 940, 1000, "dedicated_local"), anchor
        assert anchor[2] < anchor[3], "the anchor free must be the observed available, never capacity"
        assert report.outcome == "bootstrapped" and report.anchor_free_bytes == 940, report


def test_bootstrap_without_dedicated_assertion_stays_unknown(tmp_path):
    """dedicated_local is an explicit exclusivity assertion — a dedicated=False reconcile persists NO
    catalog-v3 authoritative evidence (fingerprint/capacity NULL, authority unknown, no epoch/generation/
    anchor). The drive remains a valid `unknown` identity."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence", return_value=_ev(fp=_FP, capacity=1000, free=940)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=False)
        row = con.execute("SELECT identity_fingerprint,filesystem_capacity_bytes,write_authority,"
                          "identity_epoch,write_generation FROM drives WHERE drive_label='drive-00'").fetchone()
        assert row == (None, None, "unknown", 1, 0), \
            f"dedicated=False must persist no authoritative evidence: {row}"
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
        assert report.anchor_free_bytes is None and report.outcome == "unknown_no_authority", report


def test_dedicated_false_refuses_to_downgrade_an_authoritative_drive(tmp_path):
    """A dedicated=False reconcile against an ALREADY dedicated_local drive refuses with a typed
    policy/lifecycle result and leaves every piece of evidence unchanged (revocation is a separate
    lifecycle operation, never a side effect)."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1)
        _anchor(con, "drive-00", epoch=1, gen=1, free=900, fscap=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence", return_value=_ev(fp=_FP, capacity=1000, free=880)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            try:
                bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=False)
                raise AssertionError("dedicated=False must not downgrade an authoritative drive")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_AUTHORITY_DOWNGRADE_REFUSED", exc.code
        row = con.execute("SELECT identity_epoch,write_generation,identity_fingerprint,write_authority "
                          "FROM drives WHERE drive_label='drive-00'").fetchone()
        assert row == (1, 1, _FP, "dedicated_local"), f"authoritative evidence must be untouched: {row}"
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 1


def test_migrated_label_refuses_different_stable_identity(tmp_path):
    """Regression (defect 2): a migrated row (identity_fingerprint NULL) still carries the registered
    fs/annex UUID. Bootstrap must compare the LIVE stable identity against those before granting
    authority — different media under an existing label (a replacement disk in Drive-02's slot) is a
    typed DRIVE_IDENTITY_MISMATCH with no evidence mutation. Label reuse stays deferred (DEF-029)."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-02", fs_uuid=_FS, annex_uuid=_ANX)         # migrated: fp NULL, fs/annex set
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence",
                               return_value=_ev(fs_uuid="OTHER-fs", annex_uuid="OTHER-annex")), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            try:
                bs.reconcile_drive(con, "drive-02", now="2026-07-23 12:00:00", dedicated=True)
                raise AssertionError("different live media under an existing label must refuse")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_IDENTITY_MISMATCH", exc.code
        row = con.execute("SELECT identity_fingerprint,write_authority FROM drives "
                          "WHERE drive_label='drive-02'").fetchone()
        assert row == (None, "unknown"), f"a refused mismatch must not adopt the replacement: {row}"
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


def test_different_identity_on_a_proven_drive_refuses_before_mutation(tmp_path):
    """A proven drive whose LIVE stable identity no longer matches the persisted fs/annex refuses before
    any mutation — durable claims are never silently rebound to new media."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1)
        _anchor(con, "drive-00", epoch=1, gen=1, free=900, fscap=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence",
                               return_value=_ev(fs_uuid="OTHER-fs", annex_uuid="OTHER-annex")), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            try:
                bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
                raise AssertionError("a different stable identity under an existing label must refuse")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_IDENTITY_MISMATCH", exc.code
        assert con.execute("SELECT identity_epoch,write_generation,identity_fingerprint FROM drives "
                          "WHERE drive_label='drive-00'").fetchone() == (1, 1, _FP)
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 1


# =========================================================================== atomic bootstrap + envelope

def test_bootstrap_final_transaction_is_atomic(tmp_path):
    """Identity evidence, the bootstrap dirty generation, the clean anchor, and dedicated_local authority
    are persisted in ONE transaction — a crash at anchor publication rolls ALL of them back, leaving the
    drive unknown and anchorless (never a half-established identity)."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence", return_value=_ev(fp=_FP, capacity=1000, free=940)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"), \
             mock.patch.object(dm, "_publish_anchor_locked", side_effect=RuntimeError("crash at publish")):
            try:
                bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
            except Exception:
                pass                                     # the crash is expected; the invariant is the rollback
        row = con.execute("SELECT identity_epoch,write_generation,identity_fingerprint,write_authority "
                          "FROM drives WHERE drive_label='drive-00'").fetchone()
        assert row == (1, 0, None, "unknown"), f"a crash before commit must leave the drive untouched: {row}"
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


def test_bootstrap_does_not_route_through_the_ordinary_envelope(tmp_path):
    """Bootstrap must NOT call drive_mutation.drive_mutation() to work around its NULL-fingerprint
    identity refusal — it is its own fenced, atomic operation."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence", return_value=_ev(fp=_FP, capacity=1000, free=940)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"), \
             mock.patch.object(dm, "drive_mutation") as envelope:
            bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
        envelope.assert_not_called()


# =============================================================== the anchor uses a FINAL observation

def test_anchor_uses_the_final_post_inventory_observation(tmp_path):
    """Regression (defect 3): the anchor must store the free from a FRESH observation taken AFTER
    inventory, not the pre-inventory value. Sequential observations (pre=940, final=925) must anchor 925."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        pre, final = _ev(fp=_FP, capacity=1000, free=940), _ev(fp=_FP, capacity=1000, free=925)
        with mock.patch.object(bs, "_live_evidence", side_effect=[pre, final]), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
        anchored = con.execute("SELECT anchor_free_bytes FROM drive_clean_anchors "
                              "WHERE drive_label='drive-00'").fetchone()[0]
        assert anchored == 925 and report.anchor_free_bytes == 925, \
            f"the anchor must use the final observed free (925), not the pre-inventory 940: {anchored}"


def test_final_observation_unproven_leaves_no_anchor(tmp_path):
    """Regression (defect 3): if the drive vanishes or its identity changes DURING inventory, the fresh
    final observation is unproven and the whole operation fails closed — no anchor, no identity mutation."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        pre, final = _ev(fp=_FP, capacity=1000, free=940), _ev(proven=False)
        with mock.patch.object(bs, "_live_evidence", side_effect=[pre, final]), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            try:
                bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
                raise AssertionError("an unproven final observation must publish no anchor")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_IDENTITY_UNPROVEN", exc.code
        row = con.execute("SELECT identity_fingerprint,write_authority FROM drives "
                          "WHERE drive_label='drive-00'").fetchone()
        assert row == (None, "unknown"), row
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


# ======================================================= bounded full-inventory reconciliation

def test_inventory_proves_raw_and_annex_content_present(tmp_path):
    """A populated drive reconciles when every catalogued claim is proven — a raw file on the worktree
    and an annex object proven by key (no worktree symlink required)."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        _catalogued(con, "repo", "raw.bin")
        _catalogued(con, "repo", "blob.gguf")
        _archived(con, "repo", "raw.bin", "raw.bin", "drive-00", compressed=0)
        _archived(con, "repo", "blob.gguf", "blob.gguf", "drive-00", annex_key="KEY-1")
        dest = tmp_path / "mount"
        (dest / "repo").mkdir(parents=True)
        (dest / "repo" / "raw.bin").write_bytes(b"bytes")
        bs = _bootstrap()
        with mock.patch.object(bs, "_annex_key_present", return_value=True):
            inv = bs._inventory(con, "drive-00", dest)
        assert set(inv.present) >= {("repo", "raw.bin"), ("repo", "blob.gguf")}, inv.present
        assert not inv.missing and inv.complete, inv.missing


def test_inventory_fails_closed_on_missing_or_unprovable_claim(tmp_path):
    """A catalogued raw file that is absent, or an annex claim whose key cannot be proven, is never
    counted present — it lands in `missing` and blocks completion (so no anchor publishes)."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        _catalogued(con, "repo", "gone.bin")
        _catalogued(con, "repo", "blob.gguf")
        _archived(con, "repo", "gone.bin", "gone.bin", "drive-00", compressed=0)
        _archived(con, "repo", "blob.gguf", "blob.gguf", "drive-00", annex_key="KEY-1")
        dest = tmp_path / "mount"
        (dest / "repo").mkdir(parents=True)
        bs = _bootstrap()
        with mock.patch.object(bs, "_annex_key_present", return_value=False):
            inv = bs._inventory(con, "drive-00", dest)
        assert ("repo", "gone.bin") in inv.missing and ("repo", "blob.gguf") in inv.missing, inv.missing
        assert not inv.complete, "an unprovable/absent archived claim must never count as present"


def test_inventory_reports_extra_and_debris_without_deleting(tmp_path):
    """Known staging/.incomplete debris is recognized (not counted archived) and unexplained extra
    content is REPORTED but LEFT IN PLACE — reconciliation never auto-deletes."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        _catalogued(con, "repo", "raw.bin")
        _archived(con, "repo", "raw.bin", "raw.bin", "drive-00", compressed=0)
        dest = tmp_path / "mount"
        (dest / "repo").mkdir(parents=True)
        (dest / "repo" / "raw.bin").write_bytes(b"bytes")
        debris = dest / "repo" / "raw.bin.incomplete"
        debris.write_bytes(b"partial")
        extra = dest / "repo" / "mystery.bin"
        extra.write_bytes(b"unexplained")
        bs = _bootstrap()
        with mock.patch.object(bs, "_annex_key_present", return_value=True):
            inv = bs._inventory(con, "drive-00", dest)
        assert ("repo", "raw.bin") in inv.present and inv.complete, inv
        assert any("incomplete" in str(d) for d in inv.debris), inv.debris
        assert any("mystery.bin" in str(x) for x in inv.extra), inv.extra
        assert debris.exists() and extra.exists(), "reconciliation must NOT delete debris or extra content"


# ================================================================= drift-gated refresh + tolerance

def test_free_drift_tolerance_v1_is_alloc_unit_plus_bounded_metadata(tmp_path):
    """The drift tolerance is versioned and DIAGNOSTIC-ONLY: one filesystem allocation unit plus a bounded
    metadata allowance — never capacity headroom. One representative formula is pinned."""
    bs = _bootstrap()
    allowance = 1 << 20                                        # v1: a 1 MiB bounded metadata allowance
    assert bs.free_drift_tolerance_v1(4096) == 4096 + allowance
    assert bs.free_drift_tolerance_v1(65536) == 65536 + allowance


def test_within_tolerance_refresh_reanchors_generation_2_with_fresh_free(tmp_path):
    """A same-identity/same-capacity refresh whose free drifted only WITHIN the tolerance advances to
    generation 2 and re-anchors the fresh raw free (a no-op refresh keeping stale free is not allowed)."""
    cap, anchored_free = 8_000_000_000_000, 4_000_000_000_000
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=cap, free=anchored_free)
        _dirty_gen(con, "drive-00", epoch=1, gen=1)
        _anchor(con, "drive-00", epoch=1, gen=1, free=anchored_free, fscap=cap)
        bs = _bootstrap()
        fresh = anchored_free - (bs.free_drift_tolerance_v1(4096) - 1)          # drifted, strictly BELOW tolerance
        with mock.patch.object(bs, "_live_evidence",
                               return_value=_ev(fp=_FP, capacity=cap, free=fresh, alloc_unit=4096)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
        assert con.execute("SELECT identity_epoch,write_generation FROM drives WHERE drive_label='drive-00'"
                           ).fetchone() == (1, 2), "same epoch, advanced to a fresh generation 2"
        anchor = con.execute("SELECT anchor_free_bytes FROM drive_clean_anchors WHERE drive_label='drive-00' "
                            "AND identity_epoch=1 AND generation=2").fetchone()
        assert anchor == (fresh,), f"generation 2 must store the fresh raw observed free: {anchor}"
        assert report.identity_epoch == 1 and report.outcome == "refreshed", report


def test_refresh_above_drift_tolerance_refuses_without_acceptance(tmp_path):
    """A free delta ABOVE the tolerance on a currently-clean anchored drive is a typed DRIVE_FREE_DRIFT
    refusal without --accept-drift — the old generation/anchor are unchanged (real capacity loss is never
    silently re-anchored away)."""
    cap, anchored_free = 8_000_000_000_000, 4_000_000_000_000
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=cap, free=anchored_free)
        _dirty_gen(con, "drive-00", epoch=1, gen=1)
        _anchor(con, "drive-00", epoch=1, gen=1, free=anchored_free, fscap=cap)
        bs = _bootstrap()
        drifted = anchored_free - (bs.free_drift_tolerance_v1(4096) + 1)        # strictly ABOVE tolerance
        with mock.patch.object(bs, "_live_evidence",
                               return_value=_ev(fp=_FP, capacity=cap, free=drifted, alloc_unit=4096)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            try:
                bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
                raise AssertionError("above-tolerance free drift must refuse without --accept-drift")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_FREE_DRIFT", exc.code
        assert con.execute("SELECT write_generation FROM drives WHERE drive_label='drive-00'").fetchone()[0] == 1
        anchors = con.execute("SELECT identity_epoch,generation,anchor_free_bytes FROM drive_clean_anchors "
                            "WHERE drive_label='drive-00'").fetchall()
        assert anchors == [(1, 1, anchored_free)], f"the old generation/anchor must be unchanged: {anchors}"


def test_refresh_above_drift_tolerance_reanchors_with_accept_drift(tmp_path):
    """With --accept-drift, an above-tolerance delta re-anchors AFTER a full reconciliation — a fresh
    generation 2 + anchor storing the observed free, under a DISTINCT 'accept-drift' operation code."""
    cap, anchored_free = 8_000_000_000_000, 4_000_000_000_000
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=cap, free=anchored_free)
        _dirty_gen(con, "drive-00", epoch=1, gen=1)
        _anchor(con, "drive-00", epoch=1, gen=1, free=anchored_free, fscap=cap)
        bs = _bootstrap()
        drifted = anchored_free - (bs.free_drift_tolerance_v1(4096) + 1)        # above tolerance, but accepted
        with mock.patch.object(bs, "_live_evidence",
                               return_value=_ev(fp=_FP, capacity=cap, free=drifted, alloc_unit=4096)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00",
                                        dedicated=True, accept_drift=True)
        gen2 = con.execute("SELECT operation_code FROM drive_dirty_generations WHERE drive_label='drive-00' "
                          "AND identity_epoch=1 AND generation=2").fetchone()
        assert gen2 == ("accept-drift",), f"drift acceptance must use a distinct operation code: {gen2}"
        anchor = con.execute("SELECT anchor_free_bytes FROM drive_clean_anchors WHERE drive_label='drive-00' "
                            "AND identity_epoch=1 AND generation=2").fetchone()
        assert anchor == (drifted,), f"the accepted anchor must store the observed free: {anchor}"
        assert report.identity_epoch == 1 and report.outcome == "drift_accepted", report


# ================================================================ capacity-epoch transition (real evidence)

def test_same_identity_changed_capacity_advances_epoch_and_reanchors(tmp_path):
    """Regression (defect 1): the fingerprint folds capacity in, so a resize necessarily changes the
    fingerprint while the STABLE identity (fs/annex) is unchanged. That is recognized as the same drive
    and transitions the capacity epoch: a new epoch, generation 1, the NEW fingerprint persisted, and a
    matching anchor — with real old/new fingerprints, not one fake value shared across capacities."""
    old_fp, new_fp = _real_fp(1000), _real_fp(2000)
    assert old_fp != new_fp, "a capacity change must change the composite fingerprint"
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=old_fp, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1)
        _anchor(con, "drive-00", epoch=1, gen=1, free=900, fscap=1000, fp=old_fp)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence",
                               return_value=_ev(fp=new_fp, capacity=2000, free=1900)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
        row = con.execute("SELECT identity_epoch,write_generation,filesystem_capacity_bytes,identity_fingerprint "
                          "FROM drives WHERE drive_label='drive-00'").fetchone()
        assert row == (2, 1, 2000, new_fp), f"the transition must persist (epoch 2, gen 1, new fp): {row}"
        new = con.execute("SELECT generation,anchor_free_bytes,filesystem_capacity_bytes,identity_fingerprint "
                          "FROM drive_clean_anchors WHERE drive_label='drive-00' AND identity_epoch=2").fetchone()
        assert new == (1, 1900, 2000, new_fp), f"epoch 2's first anchor must be generation 1 + new fp: {new}"
        assert report.outcome == "epoch_advanced" and report.identity_epoch == 2, report


def test_epoch_transition_holds_both_old_and_new_epoch_fences(tmp_path):
    """The transition must acquire BOTH the persisted old (fingerprint, epoch) fence and the prospective
    new (fingerprint, epoch+1) fence: an existing writer holding EITHER makes the transition refuse with
    DRIVE_FENCE_UNAVAILABLE and change nothing."""
    old_fp, new_fp = _real_fp(1000), _real_fp(2000)
    for held in ((old_fp, 1), (new_fp, 2)):
        with _catalog(tmp_path / f"e{held[1]}") as con:
            _proven_drive(con, "drive-00", fp=old_fp, epoch=1, generation=1, fscap=1000)
            _dirty_gen(con, "drive-00", epoch=1, gen=1)
            _anchor(con, "drive-00", epoch=1, gen=1, free=900, fscap=1000, fp=old_fp)
            bs = _bootstrap()
            with mock.patch.object(bs, "_live_evidence",
                                   return_value=_ev(fp=new_fp, capacity=2000, free=1900)), \
                 mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
                 mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"), \
                 drive_fence.hold_drives_sorted([held]):          # an existing writer holds one of the fences
                try:
                    bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00",
                                       dedicated=True, blocking=False)
                    raise AssertionError(f"transition must refuse while {held} is held")
                except dm.DriveMutationRefused as exc:
                    assert exc.code == "DRIVE_FENCE_UNAVAILABLE", (held, exc.code)
            assert con.execute("SELECT identity_epoch FROM drives WHERE drive_label='drive-00'"
                               ).fetchone()[0] == 1, "a fenced-out transition must change nothing"


# ================================================================ sessionless dirty recovery

def test_reconcile_recovers_sessionless_dirty_generation(tmp_path):
    """A dirty generation with no anchor and NO owner session is recovered — reconcile republishes THAT
    generation's anchor via the (epoch, generation) CAS, without opening a new generation, and preserves
    the identity/authority."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1, owner=None)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence", return_value=_ev(fp=_FP, capacity=1000, free=870)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
        assert con.execute("SELECT write_generation FROM drives WHERE drive_label='drive-00'").fetchone()[0] == 1, \
            "recovery republishes the existing generation; it does not open a new one"
        anchor = con.execute("SELECT generation,anchor_free_bytes FROM drive_clean_anchors "
                            "WHERE drive_label='drive-00'").fetchone()
        assert anchor == (1, 870), anchor
        assert con.execute("SELECT identity_fingerprint,write_authority FROM drives "
                          "WHERE drive_label='drive-00'").fetchone() == (_FP, "dedicated_local"), \
            "recovery must not downgrade identity/authority during the anchor CAS"
        assert report.outcome == "recovered", report


def test_reconcile_refuses_a_live_session_dirty_generation(tmp_path):
    """A dirty generation attributed to a live session (owner fields set) is NOT recovered here —
    sessionless recovery refuses and defers session-attributed recovery to #39."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1, owner="sess-1", token=1)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence", return_value=_ev(fp=_FP, capacity=1000, free=870)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            try:
                bs.reconcile_drive(con, "drive-00", now="2026-07-23 12:00:00", dedicated=True)
                raise AssertionError("a session-attributed dirty generation must not be recovered here (#39)")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_RECOVERY_SESSION_ACTIVE", exc.code
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


# ================================================================ completion + single command + GREEN floor

def test_offline_drive_stays_valid_unknown_and_is_not_fabricated_clean(tmp_path):
    """A drive whose live identity cannot be proven (offline/failed — the Drive-02 case) stays a VALID
    registered identity with unknown evidence; reconcile fails closed and fabricates no anchor."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-02")
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_evidence", return_value=_ev(proven=False)), \
             mock.patch.object(register, "archive_path", return_value=None):
            try:
                bs.reconcile_drive(con, "drive-02", now="2026-07-23 12:00:00", dedicated=True)
                raise AssertionError("an unprovable/offline drive must fail closed, not fabricate an anchor")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_IDENTITY_UNPROVEN", exc.code
        assert con.execute("SELECT identity_fingerprint,write_authority FROM drives "
                          "WHERE drive_label='drive-02'").fetchone() == (None, "unknown")
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


def test_single_drive_reconcile_command_covers_bootstrap_and_recovery(tmp_path):
    """Exactly ONE operator command — `drive reconcile <label>` — covers bootstrap and recovery, and it
    is WIRED into argparse (checked at the parser level; the real op is never executed). No command
    family."""
    from modelark import cli
    assert hasattr(cli, "cmd_drive_reconcile"), \
        "PR-03c1 must add a single `drive reconcile <label>` command (cli.cmd_drive_reconcile)"
    assert not hasattr(cli, "cmd_drive_bootstrap") and not hasattr(cli, "cmd_drive_recover"), \
        "bootstrap and recovery are one `drive reconcile` operation, not a command family"
    with mock.patch.object(cli, "cmd_drive_reconcile") as handler:
        try:
            cli.main(["drive", "reconcile", "drive-00"])      # argparse binds func to the mocked handler
        except SystemExit as exc:                             # 'invalid choice: reconcile' => not wired
            raise AssertionError(f"`drive reconcile <label>` is not wired into argparse (SystemExit {exc.code})")
    handler.assert_called_once()


def test_migrated_drive_is_unknown_and_anchorless_until_reconcile(tmp_path):
    """GREEN floor (must stay true): registration/migration records a valid drive identity but NO write
    authority and NO clean anchor — only `drive reconcile` establishes them. This is the inert state the
    already-wired Fill envelope refuses today, and exactly what PR-03c1 changes."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00")
        row = con.execute("SELECT identity_fingerprint,write_authority,write_generation "
                          "FROM drives WHERE drive_label='drive-00'").fetchone()
        assert row == (None, "unknown", 0), row
        assert con.execute("SELECT count(*) FROM drive_clean_anchors WHERE drive_label='drive-00'"
                           ).fetchone()[0] == 0


if __name__ == "__main__":
    import tempfile

    saved = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)
    passed, failed = [], []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            tmp = Path(tempfile.mkdtemp(prefix="mark-03c1-"))
            try:
                fn(tmp)
                passed.append(name)
                print(f"PASS  {name}")
            except Exception as exc:                     # noqa: BLE001 — full red/green map
                failed.append(name)
                print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
            finally:
                db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = saved
                shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)                              # a failing test MUST fail CI (set -e; python "$t")
