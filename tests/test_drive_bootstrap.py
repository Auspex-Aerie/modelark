"""PR-03c1 Gate-1 contract — drive identity BOOTSTRAP + first clean anchor + sessionless dirty RECOVERY
(RFC-002 / DEC-049 issue #35-B residual; the smallest usable predecessor to the #35-C authority cutover).

Tests-first, pinned BEFORE production. The change-contract tests are RED against the current tree (the
neutral module + the `drive reconcile` command do not exist yet); one characterization test is GREEN now
and must stay GREEN. The envelope that PR-03a/03b built is inert until a real drive is marked proven —
this slice is what establishes that identity, so #35-C has an authority to cut over to.

Binding reviewer corrections encoded here:
  C1  a "trivial" first anchor skips full inventory but still stores the fenced post-preparation
      statvfs AVAILABLE bytes (metadata/reserve/clone/annex overhead ⇒ free < capacity), never capacity.
  C2  write_authority=dedicated_local is an explicit operator/policy assertion of exclusivity, NOT
      derivable from identity probes; without it (shared/NAS/unfenceable) the drive stays `unknown`.
  C3  ONE atomic bootstrap: controller fence → derive live identity + lock key → drive fence + re-prove
      → bounded reconcile + final free observation with NO write txn → then a single short transaction
      persists identity evidence + bootstrap dirty generation + clean anchor + authority ATOMICALLY. A
      crash before that commit leaves the drive unknown + anchorless. Bootstrap never calls the ordinary
      drive_mutation() envelope to dodge its NULL-fingerprint identity refusal.
  C4  same identity + unchanged capacity → refresh, same epoch; same identity + CHANGED capacity → an
      explicit capacity-epoch transition (new epoch + fresh generation/anchor); a DIFFERENT identity
      under an existing label → refuse BEFORE any mutation (label reuse/retirement stays DEF-029).
  C5  full reconciliation is bounded and report-only: catalogued raw+annex proven present; known
      staging/.incomplete debris recognized; missing/unprovable claims never counted present (fail
      closed, no anchor); unexplained extra reported and LEFT IN PLACE (the final free observation
      accounts for its bytes). Completion is per-drive: offline/failed/shared drives stay valid
      `unknown` identities — never required or fabricated into cleanliness (the Drive-02 case). ONE
      operator op, `drive reconcile <label>`, covers migrated-drive bootstrap AND sessionless recovery.

Session-attributed recovery (owner session/token, worker/child exclusion, fencing-token recovery) is
deferred to #39; label reuse / retirement / dependency-aware replacement to DEF-029.

Proposed production API surfaced by these tests (for Gate-1 review; names are the contract to confirm):
  * new NEUTRAL module modelark/drive_bootstrap.py (no register→fetch / register→drive_bootstrap cycle):
      - reconcile_drive(con, label, *, now, dedicated=False, blocking=True) -> Reconciliation
        (report attrs: .outcome in {bootstrapped, refreshed, epoch_advanced, recovered,
         unknown_no_authority}, .identity_epoch, .generation, .anchor_free_bytes | None, .inventory);
        raises dm.DriveMutationRefused with DRIVE_IDENTITY_UNPROVEN / DRIVE_IDENTITY_MISMATCH /
        DRIVE_RECOVERY_SESSION_ACTIVE / DRIVE_RECONCILIATION_REQUIRED / CLEAN_ANCHOR_CAS_FAILED.
      - _live_observation(con, label) -> dm.Observation  (fenced live identity/free evidence seam)
      - _inventory(con, label, dest) -> report with .present/.missing/.debris/.extra and .complete
      - _annex_key_present(dest, key, *, target_uuid) -> bool  (annex presence proof seam)
      - reuses drive_mutation._publish_anchor_locked for the anchor write inside its atomic bootstrap txn
  * new CLI: a single `drive reconcile <label>` command -> cli.cmd_drive_reconcile (no command family)

Disposable temp trees / synthetic drives / mocked physical seams only — never a live catalog/drive.
"""
from __future__ import annotations

import importlib
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from modelark.core import db
from modelark import drive_mutation as dm
from modelark import register

_FP = "a" * 64
_FP2 = "c" * 64


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
    """Import the PR-03c1 neutral module, or fail RED with the reviewed reason (it does not exist yet)."""
    try:
        return importlib.import_module("modelark.drive_bootstrap")
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "PR-03c1 must add the neutral module modelark/drive_bootstrap.py exposing a single "
            f"reconcile_drive(con, label, *, now, dedicated=...) bootstrap/recovery operation: {exc}")


def _drive_row(con, label="drive-00", *, fs_uuid="fsid", annex_uuid="anx", serial="SER",
               capacity=1000, free=940):
    """A registered/migrated drive: topology + nominal capacity only, no proven identity/authority — the
    valid `unknown` state a drive sits in until `drive reconcile` bootstraps it."""
    con.execute("INSERT INTO drives(drive_label,fs_uuid,annex_uuid,serial,capacity_bytes,free_bytes) "
                "VALUES(?,?,?,?,?,?)", [label, fs_uuid, annex_uuid, serial, capacity, free])


def _proven_drive(con, label="drive-00", *, epoch=1, generation=0, fp=_FP, fscap=1000, free=900,
                  annex_uuid="anx"):
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,identity_epoch,write_generation,"
                "filesystem_capacity_bytes,identity_fingerprint,write_authority,annex_uuid) "
                "VALUES(?,?,?,?,?,?,?, 'dedicated_local', ?)",
                [label, fscap, free, epoch, generation, fscap, fp, annex_uuid])


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


def _obs(*, fp=_FP, capacity=1000, free=940, proven=True):
    return dm.Observation(identity_proven=proven, free_bytes=free, filesystem_capacity=capacity,
                          fingerprint=fp, identity_proof="live-proof", fence_proof="fence-proof")


def _clean_inv(**kw):
    """A mocked full-inventory report with nothing missing (an anchor may publish)."""
    fields = dict(present=[], missing=[], debris=[], extra=[], complete=True)
    fields.update(kw)
    return mock.Mock(**fields)


# =========================================================== identity establishment + write authority (C1,C2)

def test_bootstrap_local_dedicated_establishes_identity_authority_and_anchor(tmp_path):
    """R1+C1+C2+C3: a local drive bootstrapped WITH the dedicated_local assertion persists proven
    identity, dedicated_local authority, and a first clean anchor whose free is the OBSERVED
    post-preparation statvfs available (strictly < capacity), in one atomic step."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(fp=_FP, capacity=1000, free=940)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-22 12:00:00", dedicated=True)
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
    """C2: dedicated_local is an explicit exclusivity assertion — identity probes never prove it. Without
    the assertion (shared/NAS/unfenceable) the drive stays a VALID `unknown` identity with NO anchor;
    nothing silently upgrades it."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(fp=_FP, capacity=1000, free=940)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-22 12:00:00", dedicated=False)
        authority = con.execute("SELECT write_authority FROM drives WHERE drive_label='drive-00'").fetchone()[0]
        assert authority == "unknown", "no probe set may upgrade authority to dedicated_local"
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
        assert report.anchor_free_bytes is None, report


# =========================================================================== atomic bootstrap (C3 / R4)

def test_bootstrap_final_transaction_is_atomic(tmp_path):
    """C3/R4: identity evidence, the bootstrap dirty generation, the clean anchor, and dedicated_local
    authority are persisted in ONE transaction — a crash at anchor publication rolls ALL of them back,
    leaving the drive unknown and anchorless (never a half-established identity)."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(fp=_FP, capacity=1000, free=940)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"), \
             mock.patch.object(dm, "_publish_anchor_locked", side_effect=RuntimeError("crash at publish")):
            try:
                bs.reconcile_drive(con, "drive-00", now="2026-07-22 12:00:00", dedicated=True)
            except Exception:
                pass                                     # the crash is expected; the invariant is the rollback
        row = con.execute("SELECT identity_epoch,write_generation,identity_fingerprint,write_authority "
                          "FROM drives WHERE drive_label='drive-00'").fetchone()
        assert row == (1, 0, None, "unknown"), f"a crash before commit must leave the drive untouched: {row}"
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


def test_bootstrap_does_not_route_through_the_ordinary_envelope(tmp_path):
    """C3: bootstrap must NOT call drive_mutation.drive_mutation() to work around its NULL-fingerprint
    identity refusal — it is its own fenced, atomic operation."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-00", capacity=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(fp=_FP, capacity=1000, free=940)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"), \
             mock.patch.object(dm, "drive_mutation") as envelope:
            bs.reconcile_drive(con, "drive-00", now="2026-07-22 12:00:00", dedicated=True)
        envelope.assert_not_called()


# ======================================================= bounded full-inventory reconciliation (C5)

def test_inventory_proves_raw_and_annex_content_present(tmp_path):
    """C5: a populated drive reconciles when every catalogued claim is proven present — a raw file on the
    worktree and an annex object proven by key (no worktree symlink required)."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", annex_uuid="anx")
        _catalogued(con, "repo", "raw.bin")
        _catalogued(con, "repo", "blob.gguf")
        _archived(con, "repo", "raw.bin", "raw.bin", "drive-00", compressed=0)
        _archived(con, "repo", "blob.gguf", "blob.gguf", "drive-00", annex_key="KEY-1")
        dest = tmp_path / "mount"
        (dest / "repo").mkdir(parents=True)
        (dest / "repo" / "raw.bin").write_bytes(b"bytes")            # raw present; annex proven by key
        bs = _bootstrap()
        with mock.patch.object(bs, "_annex_key_present", return_value=True):
            inv = bs._inventory(con, "drive-00", dest)
        assert set(inv.present) >= {("repo", "raw.bin"), ("repo", "blob.gguf")}, inv.present
        assert not inv.missing and inv.complete, inv.missing


def test_inventory_fails_closed_on_missing_or_unprovable_claim(tmp_path):
    """C5: a catalogued raw file that is absent, or an annex claim whose key cannot be proven, is NEVER
    counted present — it lands in `missing` and blocks completion (so no anchor publishes)."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        _catalogued(con, "repo", "gone.bin")
        _catalogued(con, "repo", "blob.gguf")
        _archived(con, "repo", "gone.bin", "gone.bin", "drive-00", compressed=0)         # raw, file ABSENT
        _archived(con, "repo", "blob.gguf", "blob.gguf", "drive-00", annex_key="KEY-1")  # annex, unprovable
        dest = tmp_path / "mount"
        (dest / "repo").mkdir(parents=True)                                              # no files written
        bs = _bootstrap()
        with mock.patch.object(bs, "_annex_key_present", return_value=False):
            inv = bs._inventory(con, "drive-00", dest)
        assert ("repo", "gone.bin") in inv.missing and ("repo", "blob.gguf") in inv.missing, inv.missing
        assert not inv.complete, "an unprovable/absent archived claim must never count as present"


def test_inventory_reports_extra_and_debris_without_deleting(tmp_path):
    """C5: known staging/.incomplete debris is recognized (not counted archived) and unexplained extra
    content is REPORTED but LEFT IN PLACE — reconciliation never auto-deletes; the final free observation
    accounts for those bytes."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        _catalogued(con, "repo", "raw.bin")
        _archived(con, "repo", "raw.bin", "raw.bin", "drive-00", compressed=0)
        dest = tmp_path / "mount"
        (dest / "repo").mkdir(parents=True)
        (dest / "repo" / "raw.bin").write_bytes(b"bytes")                 # the one catalogued file
        debris = dest / "repo" / "raw.bin.incomplete"
        debris.write_bytes(b"partial")                                    # known debris
        extra = dest / "repo" / "mystery.bin"
        extra.write_bytes(b"unexplained")                                 # unexplained extra
        bs = _bootstrap()
        with mock.patch.object(bs, "_annex_key_present", return_value=True):
            inv = bs._inventory(con, "drive-00", dest)
        assert ("repo", "raw.bin") in inv.present and inv.complete, inv
        assert any("incomplete" in str(d) for d in inv.debris), inv.debris
        assert any("mystery.bin" in str(x) for x in inv.extra), inv.extra
        assert debris.exists() and extra.exists(), "reconciliation must NOT delete debris or extra content"


# ================================================================= capacity-epoch handling (C4)

def test_same_identity_same_capacity_refreshes_without_new_epoch(tmp_path):
    """C4: re-reconciling the same identity at unchanged capacity refreshes evidence WITHOUT advancing
    the capacity epoch."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1)
        _anchor(con, "drive-00", epoch=1, gen=1, free=900, fscap=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(fp=_FP, capacity=1000, free=880)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-22 12:00:00", dedicated=True)
        assert con.execute("SELECT identity_epoch FROM drives WHERE drive_label='drive-00'").fetchone()[0] == 1
        assert con.execute("SELECT count(DISTINCT identity_epoch) FROM drive_clean_anchors "
                           "WHERE drive_label='drive-00'").fetchone()[0] == 1, "no new epoch namespace"
        assert report.identity_epoch == 1, report


def test_same_identity_changed_capacity_advances_epoch_and_reanchors(tmp_path):
    """C4: same identity + a DIFFERENT filesystem capacity is an explicit capacity-epoch transition — a
    new epoch with a fresh generation and anchor (not a silent same-epoch refresh, and not DEF-029)."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1)
        _anchor(con, "drive-00", epoch=1, gen=1, free=900, fscap=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(fp=_FP, capacity=2000, free=1900)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-22 12:00:00", dedicated=True)
        row = con.execute("SELECT identity_epoch,filesystem_capacity_bytes FROM drives "
                          "WHERE drive_label='drive-00'").fetchone()
        assert row == (2, 2000), row
        new = con.execute("SELECT anchor_free_bytes,filesystem_capacity_bytes FROM drive_clean_anchors "
                          "WHERE drive_label='drive-00' AND identity_epoch=2").fetchone()
        assert new == (1900, 2000), new
        assert report.outcome == "epoch_advanced" and report.identity_epoch == 2, report


def test_different_identity_under_existing_label_refuses_before_mutation(tmp_path):
    """C4: a different underlying identity under an existing label must REFUSE before any format/clone/
    remote/catalog mutation — durable byte claims are never silently rebound to new media (label reuse
    and retirement stay DEF-029)."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1)
        _anchor(con, "drive-00", epoch=1, gen=1, free=900, fscap=1000)
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(fp=_FP2, capacity=1000)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            try:
                bs.reconcile_drive(con, "drive-00", now="2026-07-22 12:00:00", dedicated=True)
                raise AssertionError("a different identity under an existing label must refuse")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_IDENTITY_MISMATCH", exc.code
        row = con.execute("SELECT identity_epoch,write_generation,identity_fingerprint FROM drives "
                          "WHERE drive_label='drive-00'").fetchone()
        assert row == (1, 1, _FP), f"the original identity must be untouched by a refused mismatch: {row}"
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 1


# ===================================================== sessionless dirty recovery, same command (R3 / C5)

def test_reconcile_recovers_sessionless_dirty_generation(tmp_path):
    """R3: a dirty generation with no anchor and NO owner session (sessionless) is recovered by
    `drive reconcile` — it reconciles and republishes THAT generation's anchor via the (epoch,generation)
    CAS, without opening a new generation."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1, owner=None)          # dirty, sessionless, no anchor
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(fp=_FP, capacity=1000, free=870)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            report = bs.reconcile_drive(con, "drive-00", now="2026-07-22 12:00:00", dedicated=True)
        assert con.execute("SELECT write_generation FROM drives WHERE drive_label='drive-00'").fetchone()[0] == 1, \
            "recovery republishes the existing generation; it does not open a new one"
        anchor = con.execute("SELECT generation,anchor_free_bytes FROM drive_clean_anchors "
                            "WHERE drive_label='drive-00'").fetchone()
        assert anchor == (1, 870), anchor
        assert report.outcome == "recovered", report


def test_reconcile_refuses_a_live_session_dirty_generation(tmp_path):
    """R3 boundary: a dirty generation attributed to a live session (owner fields set) is NOT recovered
    here — sessionless recovery refuses and defers session-attributed recovery to #39."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=1, fscap=1000)
        _dirty_gen(con, "drive-00", epoch=1, gen=1, owner="sess-1", token=1)     # live / attributed
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(fp=_FP, capacity=1000)), \
             mock.patch.object(bs, "_inventory", return_value=_clean_inv()), \
             mock.patch.object(register, "archive_path", return_value=tmp_path / "mount"):
            try:
                bs.reconcile_drive(con, "drive-00", now="2026-07-22 12:00:00", dedicated=True)
                raise AssertionError("a session-attributed dirty generation must not be recovered here (#39)")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_RECOVERY_SESSION_ACTIVE", exc.code
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


# ================================================================ completion semantics + single command (C5)

def test_offline_drive_stays_valid_unknown_and_is_not_fabricated_clean(tmp_path):
    """C5 completion: a drive whose live identity cannot be proven (offline/failed — the Drive-02 case)
    stays a VALID registered identity with unknown evidence. reconcile fails closed and fabricates no
    anchor; a drive is never required or forced into cleanliness to complete the fleet."""
    with _catalog(tmp_path) as con:
        _drive_row(con, "drive-02")
        bs = _bootstrap()
        with mock.patch.object(bs, "_live_observation", return_value=_obs(proven=False)), \
             mock.patch.object(register, "archive_path", return_value=None):
            try:
                bs.reconcile_drive(con, "drive-02", now="2026-07-22 12:00:00", dedicated=True)
                raise AssertionError("an unprovable/offline drive must fail closed, not fabricate an anchor")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_IDENTITY_UNPROVEN", exc.code
        row = con.execute("SELECT identity_fingerprint,write_authority FROM drives "
                          "WHERE drive_label='drive-02'").fetchone()
        assert row == (None, "unknown"), f"an offline drive stays a valid unknown identity: {row}"
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


def test_single_drive_reconcile_command_covers_bootstrap_and_recovery(tmp_path):
    """C5: exactly ONE operator command — `drive reconcile <label>` — covers migrated-drive bootstrap and
    sessionless dirty recovery. There is no family of bootstrap/recover commands."""
    from modelark import cli
    assert hasattr(cli, "cmd_drive_reconcile"), \
        "PR-03c1 must add a single `drive reconcile <label>` command (cli.cmd_drive_reconcile)"
    assert not hasattr(cli, "cmd_drive_bootstrap") and not hasattr(cli, "cmd_drive_recover"), \
        "bootstrap and recovery are one `drive reconcile` operation, not a command family"


# ================================================================================= GREEN characterization

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
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(Path(tempfile.mkdtemp()))
                print(f"ok   {name}")
            except Exception as exc:                     # noqa: BLE001 — script runner reports, never aborts
                failures += 1
                print(f"FAIL {name}: {exc}")
            finally:
                db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = saved
    print("all passed" if not failures else f"{failures} failing (expected RED at Gate 1)")
