"""PR-03a internal physical-mutation envelope (tests-first, RFC-002 / DEC-049 issue #35-B slice 1).

Gate 1/2 test contract for the same-host fence + dirty-generation/clean-anchor envelope, pinned BEFORE
production. RED until modelark.drive_fence / modelark.drive_mutation exist; find_spec activates the
guard only for an ABSENT module, so a present-but-broken module surfaces its real error.

Scope: the COMPLETE internal envelope contract; PR-03b now wires it into exactly one reviewed transport
(modelark/fetch.py), enforced by the wiring guard below. Corrections folded in across review:
  * identity proven under the locks BEFORE dirtying (initial mismatch -> refuse, no dirty; failure
    after dirtying -> durably dirty, no anchor), with dirty durability proven via a SECOND connection;
  * generation start requires generation zero OR an exact clean current generation whose anchor still
    matches fingerprint/capacity/authority/epoch, else DIRTY_GENERATION_CONFLICT;
  * clean-anchor publication is bound to the captured (identity_epoch, generation) tuple (a matching
    generation number under a changed epoch is refused);
  * a post-reconciliation identity change publishes no anchor and leaves the generation dirty;
  * the touched path/key set is actually reconciled; a reconcile failure leaves the generation dirty;
  * the clean anchor stores a FRESH post-reconciliation observation (free + capacity + fingerprint);
  * multi-drive: unsorted input acquires identity+epoch locks deterministically; a real SQL failure
    while advancing the second drive rolls back both (verified via an independent connection); no
    SQLite write transaction is open while holding physical locks or reconciling;
  * physical lock namespace + the actual lock WRAPPER under cross-process contention (typed refusal).

Non-goals: child-FD integration + transport conversion (03b); registration/recovery/full inventory +
operator entry points (03c); admission cutover (#35-C); durable sessions/tokens (#39). Disposable temp
trees / fakes only.
"""
from __future__ import annotations

import ast
import importlib.util
import sqlite3
import subprocess
import sys
import textwrap
import time
from contextlib import contextmanager
from pathlib import Path

from modelark.core import db

# find_spec locates the module file without executing it: only an ABSENT envelope module activates the
# Gate-1 guard, while a present-but-broken module surfaces its real import error from the import below.
_ENVELOPE_MODULES = ("modelark.drive_fence", "modelark.drive_mutation")
if all(importlib.util.find_spec(m) is not None for m in _ENVELOPE_MODULES):
    from modelark import drive_fence
    from modelark import drive_mutation as dm
    _HAS = True
else:
    drive_fence = dm = None
    _HAS = False

_FP = "a" * 64
_FP2 = "c" * 64


def _require():
    if not _HAS:
        raise AssertionError("drive_fence/drive_mutation envelope not implemented yet (Gate-1 red)")


# Restore the db module globals around EVERY test (not only the _catalog ones), so a test that sets
# CATALOG_DIR/DB_PATH/STATE_DIR or calls db.configure() cannot leak a temporary path into a later test.
# Covered under pytest by this autouse fixture and under the script runner by main()'s save/restore.
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


class _FailOn:
    """A connection proxy that raises sqlite3.OperationalError on the `nth` statement whose text
    contains a (case-insensitive) marker, to inject a mid-transaction failure; else delegates."""

    def __init__(self, con, marker, nth=1):
        self._con = con
        self._marker = marker.lower()
        self._nth = nth
        self._seen = 0

    def execute(self, sql, *args):
        if self._marker in sql.lower():
            self._seen += 1
            if self._seen == self._nth:
                raise sqlite3.OperationalError(f"injected failure at #{self._nth}: {self._marker}")
        return self._con.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._con, name)


def _proven_drive(con, label="drive-00", *, epoch=1, generation=0, fp=_FP, fscap=1000, free=900):
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,identity_epoch,write_generation,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority) "
        "VALUES(?,?,?,?,?,?,?, 'dedicated_local')", [label, fscap, free, epoch, generation, fscap, fp])


def _dirty(con, label, epoch, generation, op="test"):
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                "VALUES(?,?,?,?)", [label, epoch, generation, op])


def _anchor(con, label, epoch, generation, *, free=500, fscap=1000, fp=_FP, authority="dedicated_local"):
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
        "observed_at) VALUES(?,?,?,?,?,?,?, 'p','p','2026-01-01')",
        [label, epoch, generation, free, fscap, fp, authority])


def _obs(free=500, *, capacity=1000, fp=_FP, proven=True):
    return dm.Observation(identity_proven=proven, free_bytes=free, filesystem_capacity=capacity,
                          fingerprint=fp, identity_proof="proof", fence_proof="fence")


def _observer(con, *frees):
    """A fenced observer that attests the drive's CURRENT fingerprint/capacity (so multi-drive drives
    each match), yielding successive free-byte values; asserts no SQLite txn is open when called."""
    box = list(frees)

    def observe(label):
        assert not con.in_transaction, "no SQLite transaction may be open during a fenced observation"
        fp, cap = con.execute(
            "SELECT identity_fingerprint, filesystem_capacity_bytes FROM drives WHERE drive_label=?",
            [label]).fetchone()
        free = box.pop(0) if len(box) > 1 else (box[0] if box else 500)
        return _obs(free, capacity=cap, fp=fp)
    return observe


def _reconciler(con, record=None, fail=False):
    def reconcile(label, paths, keys):
        assert not con.in_transaction, "no SQLite transaction may be open during reconciliation"
        if record is not None:
            record.append((label, tuple(paths), tuple(keys)))
        if fail:
            raise RuntimeError("reconcile failed")
    return reconcile


def _has_anchor(con, label, epoch, generation):
    return con.execute("SELECT count(*) FROM drive_clean_anchors "
                       "WHERE drive_label=? AND identity_epoch=? AND generation=?",
                       [label, epoch, generation]).fetchone()[0] == 1


def _hold_flock_in_subprocess(script_body, held):
    proc = subprocess.Popen([sys.executable, "-c", textwrap.dedent(script_body)])
    for _ in range(500):
        if held.exists():
            break
        time.sleep(0.01)
    assert held.exists(), "holder process never acquired the flock"
    return proc


# =========================================================================== lock keys + namespace

def test_controller_lock_key_is_catalog_identity_not_state_dir():
    _require()
    catalog = Path("/data/one/catalog.sqlite")
    assert drive_fence.controller_lock_key(catalog) == drive_fence.controller_lock_key(catalog)
    assert drive_fence.controller_lock_key(catalog) != \
        drive_fence.controller_lock_key(Path("/data/two/catalog.sqlite"))


def test_drive_lock_key_from_identity_and_epoch():
    _require()
    assert drive_fence.drive_lock_key(_FP, 1) == drive_fence.drive_lock_key(_FP, 1)
    assert drive_fence.drive_lock_key(_FP, 1) != drive_fence.drive_lock_key(_FP, 2)
    assert drive_fence.drive_lock_key(_FP, 1) != drive_fence.drive_lock_key(_FP2, 1)


def test_drive_lock_key_refuses_unproven_identity():
    _require()
    for missing in (None, "", "   "):
        try:
            drive_fence.drive_lock_key(missing, 1)
            raise AssertionError(f"unproven identity {missing!r} must be refused")
        except ValueError:
            pass


def test_cross_process_wrapper_refuses_when_drive_lock_held(tmp_path):
    """Exercise the actual lock WRAPPER (drive_mutation), not just raw fcntl: with the drive flock held
    by another process, a non-blocking mutation returns the typed DRIVE_FENCE_UNAVAILABLE refusal."""
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", epoch=1, generation=0)
        held, release = tmp_path / "held", tmp_path / "release"
        proc = _hold_flock_in_subprocess(f"""
            import fcntl, time
            from pathlib import Path
            from modelark.core import db
            from modelark import drive_fence
            db.configure({str(db.CATALOG_DIR)!r}, {str(db.STATE_DIR)!r})
            p = drive_fence.drive_lock_path({_FP!r}, 1); p.parent.mkdir(parents=True, exist_ok=True)
            fh = open(p, "w"); fcntl.flock(fh, fcntl.LOCK_EX)
            Path({str(held)!r}).write_text("x")
            while not Path({str(release)!r}).exists(): time.sleep(0.02)
            fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
        """, held)
        try:
            try:
                with dm.drive_mutation(con, ["drive-00"], "op", observe=_observer(con),
                                       reconcile=_reconciler(con), now="2026-01-01", blocking=False):
                    raise AssertionError("wrapper acquired a drive lock held by another process")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_FENCE_UNAVAILABLE", exc.code
            assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
        finally:
            release.write_text("go")
            proc.wait(timeout=10)


# =========================================================================== identity proof + generation

def test_initial_identity_mismatch_refuses_without_dirtying(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)     # row proven; the initial fenced observation fails
        try:
            with dm.drive_mutation(con, ["drive-00"], "download",
                                   observe=lambda label: _obs(proven=False),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                raise AssertionError("body must not run when initial identity proof fails")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DRIVE_IDENTITY_UNPROVEN", exc.code
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
        assert con.execute("SELECT write_generation FROM drives "
                           "WHERE drive_label='drive-00'").fetchone()[0] == 0


def test_failure_after_dirtying_leaves_generation_durably_dirty(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        try:
            with dm.drive_mutation(con, ["drive-00"], "download", observe=_observer(con, 900, 400),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                raise RuntimeError("boom after dirtying")
        except RuntimeError:
            pass
        assert con.execute("SELECT generation FROM drive_dirty_generations "
                           "WHERE drive_label='drive-00'").fetchall() == [(1,)]
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
        assert con.execute("SELECT write_generation FROM drives "
                           "WHERE drive_label='drive-00'").fetchone()[0] == 1


def test_dirty_generation_is_committed_and_durable_via_second_connection(tmp_path):
    """The dirty generation must be committed (visible to an independent connection) BEFORE the body
    can allocate, and remain committed after a body/reconcile failure."""
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        other = sqlite3.connect(str(db.DB_PATH))
        try:
            seen = {}
            try:
                with dm.drive_mutation(con, ["drive-00"], "download", observe=_observer(con, 900, 400),
                                       reconcile=_reconciler(con), now="2026-01-01"):
                    seen["mid"] = other.execute("SELECT generation FROM drive_dirty_generations "
                                                "WHERE drive_label='drive-00'").fetchall()
                    raise RuntimeError("fail after dirty, before anchor")
            except RuntimeError:
                pass
            assert seen["mid"] == [(1,)], "dirty generation must be committed before the body allocates"
            assert other.execute("SELECT generation FROM drive_dirty_generations "
                                 "WHERE drive_label='drive-00'").fetchall() == [(1,)]   # durable
            assert other.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
        finally:
            other.close()


def test_dirty_generation_advances_before_body_with_null_owner(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        seen = {}
        with dm.drive_mutation(con, ["drive-00"], "download", observe=_observer(con, 900, 400),
                               reconcile=_reconciler(con), now="2026-01-01"):
            seen["row"] = con.execute(
                "SELECT identity_epoch,generation,owner_session_id,owner_fencing_token "
                "FROM drive_dirty_generations WHERE drive_label='drive-00'").fetchone()
        assert seen["row"] == (1, 1, None, None), seen


def test_generation_start_guard(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "zero", generation=0)
        with dm.drive_mutation(con, ["zero"], "op", observe=_observer(con, 900, 400),
                               reconcile=_reconciler(con), now="2026-01-01"):
            pass
        assert _has_anchor(con, "zero", 1, 1)

        _proven_drive(con, "clean", generation=1)
        _dirty(con, "clean", 1, 1)
        _anchor(con, "clean", 1, 1)                       # matching clean anchor -> clean current gen
        with dm.drive_mutation(con, ["clean"], "op", observe=_observer(con, 900, 400),
                               reconcile=_reconciler(con), now="2026-01-01"):
            pass
        assert _has_anchor(con, "clean", 1, 2)

        _proven_drive(con, "dirty", generation=1)
        _dirty(con, "dirty", 1, 1)                        # dirty gen 1, no anchor
        try:
            with dm.drive_mutation(con, ["dirty"], "op", observe=_observer(con),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                raise AssertionError("must refuse starting over a dirty generation")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DIRTY_GENERATION_CONFLICT", exc.code


def test_clean_check_requires_a_matching_anchor_identity(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=1, fp=_FP)
        _dirty(con, "drive-00", 1, 1)
        _anchor(con, "drive-00", 1, 1, fp=_FP2)           # mismatched identity -> not clean
        try:
            with dm.drive_mutation(con, ["drive-00"], "op", observe=_observer(con),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                raise AssertionError("a mismatched-identity anchor must not count as a clean generation")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DIRTY_GENERATION_CONFLICT", exc.code


# =========================================================================== atomic advance (single + multi)

def test_generation_advance_is_atomic(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        proxy = _FailOn(con, "update drives")            # fail the write_generation bump, mid-advance
        try:
            dm.begin_generation(proxy, "drive-00", "op")
            raise AssertionError("the advance must raise on the injected failure")
        except sqlite3.OperationalError:
            pass
        assert con.execute("SELECT count(*) FROM drive_dirty_generations "
                           "WHERE drive_label='drive-00'").fetchone()[0] == 0, "dirty row must roll back"
        assert con.execute("SELECT write_generation FROM drives "
                           "WHERE drive_label='drive-00'").fetchone()[0] == 0, "write_generation must roll back"


def test_second_drive_real_sql_failure_rolls_back_both(tmp_path):
    """A real SQL failure while advancing the SECOND drive rolls back BOTH advances; verified through
    an independent connection (the multi-drive advance is one transaction)."""
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "aaa", fp=_FP, generation=0)
        _proven_drive(con, "ccc", fp=_FP2, generation=0)
        proxy = _FailOn(con, "insert into drive_dirty_generations", nth=2)   # 2nd drive's advance
        raised = False
        try:
            with dm.drive_mutation(proxy, ["aaa", "ccc"], "op", observe=_observer(con),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                raise AssertionError("advance should have failed on the second drive")
        except Exception:                                # noqa: BLE001 — any raise is fine; atomicity is the point
            raised = True
        assert raised
        other = sqlite3.connect(str(db.DB_PATH))
        try:
            assert other.execute("SELECT write_generation FROM drives ORDER BY drive_label"
                                 ).fetchall() == [(0,), (0,)], "neither drive may have advanced"
            assert other.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
        finally:
            other.close()


# =========================================================================== reconciliation + fresh anchor

def test_touched_set_is_reconciled(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        record = []
        with dm.drive_mutation(con, ["drive-00"], "download", observe=_observer(con, 900, 400),
                               reconcile=_reconciler(con, record=record), now="2026-01-01") as writer:
            writer.record_touched("drive-00", paths=["a/b.safetensors"], keys=["KEY1"])
        assert record == [("drive-00", ("a/b.safetensors",), ("KEY1",))], record


def test_reconciliation_failure_leaves_generation_dirty(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        try:
            with dm.drive_mutation(con, ["drive-00"], "download", observe=_observer(con, 900, 400),
                                   reconcile=_reconciler(con, fail=True), now="2026-01-01") as writer:
                writer.record_touched("drive-00", paths=["a/b"], keys=["KEY1"])
        except RuntimeError:
            pass
        assert con.execute("SELECT generation FROM drive_dirty_generations "
                           "WHERE drive_label='drive-00'").fetchall() == [(1,)]
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


def test_anchor_uses_fresh_post_reconciliation_observation(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0, fscap=1000)
        # 900 free before allocation, 400 after reconciliation: the anchor must store the FRESH 400.
        with dm.drive_mutation(con, ["drive-00"], "download", observe=_observer(con, 900, 400),
                               reconcile=_reconciler(con), now="2026-01-01") as writer:
            writer.record_touched("drive-00", paths=["a/b"], keys=["KEY1"])
        row = con.execute("SELECT anchor_free_bytes,filesystem_capacity_bytes,identity_fingerprint "
                          "FROM drive_clean_anchors WHERE drive_label='drive-00'").fetchone()
        assert row == (400, 1000, _FP), row


def test_post_reconciliation_identity_change_leaves_generation_dirty(tmp_path):
    """If the identity changes (or cannot be re-proven) at the post-reconciliation observation, no
    anchor is published and the generation remains dirty."""
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0, fp=_FP)
        calls = []

        def observe(label):
            calls.append(label)
            return _obs(900, fp=_FP) if len(calls) == 1 else _obs(400, fp=_FP2)   # identity changed post

        try:
            with dm.drive_mutation(con, ["drive-00"], "download", observe=observe,
                                   reconcile=_reconciler(con), now="2026-01-01") as writer:
                writer.record_touched("drive-00", paths=["a/b"], keys=["KEY1"])
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DRIVE_IDENTITY_UNPROVEN", exc.code
        assert con.execute("SELECT generation FROM drive_dirty_generations "
                           "WHERE drive_label='drive-00'").fetchall() == [(1,)]
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


# =========================================================================== multi-drive lock order + txn safety

def test_unsorted_drives_acquire_locks_in_deterministic_identity_order(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "later", fp=_FP2, generation=0)   # identity 'c...'
        _proven_drive(con, "early", fp=_FP, generation=0)    # identity 'a...'
        captured = {}
        real = drive_fence.hold_drives_sorted

        def traced(keyed_drives, *a, **k):
            captured["keys"] = list(keyed_drives)
            return real(keyed_drives, *a, **k)

        drive_fence.hold_drives_sorted = traced
        try:
            with dm.drive_mutation(con, ["later", "early"], "op",     # unsorted input
                                   observe=_observer(con, 900, 400), reconcile=_reconciler(con),
                                   now="2026-01-01"):
                pass
        finally:
            drive_fence.hold_drives_sorted = real
        assert captured["keys"] == sorted(captured["keys"]), captured["keys"]
        assert captured["keys"][0][0] == _FP and captured["keys"][1][0] == _FP2, captured["keys"]


def test_no_sqlite_transaction_during_observation_or_reconciliation(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        # _observer and _reconciler assert `not con.in_transaction`; a clean close exercises both
        with dm.drive_mutation(con, ["drive-00"], "op", observe=_observer(con, 900, 400),
                               reconcile=_reconciler(con), now="2026-01-01") as writer:
            writer.record_touched("drive-00", paths=["a"], keys=["K"])
        assert _has_anchor(con, "drive-00", 1, 1)


# =========================================================================== clean close + CAS + ordering

def test_clean_close_publishes_exactly_one_matching_anchor(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0, fscap=1000)
        with dm.drive_mutation(con, ["drive-00"], "download", observe=_observer(con, 900, 400),
                               reconcile=_reconciler(con), now="2026-01-01") as writer:
            writer.record_touched("drive-00", paths=["a/b.safetensors"], keys=["KEY1"])
        anchor = con.execute(
            "SELECT identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
            "identity_fingerprint,write_authority FROM drive_clean_anchors WHERE drive_label='drive-00'"
        ).fetchall()
        assert anchor == [(1, 1, 400, 1000, _FP, "dedicated_local")], anchor


def test_clean_anchor_cas_refuses_a_stale_generation(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", epoch=1, generation=2)   # current generation is 2
        _dirty(con, "drive-00", 1, 2)
        try:
            dm.publish_clean_anchor(con, "drive-00", 1, 1, _obs(500), now="2026-01-01")  # captured gen 1
            raise AssertionError("stale-generation anchor publish must fail")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "CLEAN_ANCHOR_CAS_FAILED", exc.code
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


def test_clean_anchor_is_bound_to_captured_epoch_not_just_generation(tmp_path):
    """The anchor CAS binds to the captured (identity_epoch, generation): a matching generation NUMBER
    under a changed epoch must be refused."""
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", epoch=2, generation=1)   # now epoch 2, generation 1
        _dirty(con, "drive-00", 2, 1)
        try:
            dm.publish_clean_anchor(con, "drive-00", 1, 1, _obs(500), now="2026-01-01")  # captured epoch 1
            raise AssertionError("an anchor captured under a stale epoch must be refused")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "CLEAN_ANCHOR_CAS_FAILED", exc.code
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0


def test_controller_lock_is_acquired_before_drive_locks(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        order = []
        real_ctrl, real_drive = drive_fence.hold_controller, drive_fence.hold_drives_sorted
        drive_fence.hold_controller = lambda *a, **k: (order.append("controller"), real_ctrl(*a, **k))[1]
        drive_fence.hold_drives_sorted = lambda *a, **k: (order.append("drives"), real_drive(*a, **k))[1]
        try:
            with dm.drive_mutation(con, ["drive-00"], "op", observe=_observer(con, 900, 400),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                pass
        finally:
            drive_fence.hold_controller, drive_fence.hold_drives_sorted = real_ctrl, real_drive
        assert order[:2] == ["controller", "drives"], order


# =========================================================================== boundary races (review)

def test_body_cannot_run_against_facts_changed_after_capture(tmp_path):
    """Facts are captured under the controller fence and revalidated before dirtying: if the drive's
    identity/epoch changes between capture and the advance, the mutation refuses and the body never
    runs (no dirty generation)."""
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", epoch=1, generation=0, fp=_FP)

        def observe(label):
            # simulate a lifecycle epoch bump racing in after facts were captured (fingerprint
            # unchanged, so identity proof passes; the epoch moved out from under the operation)
            con.execute("UPDATE drives SET identity_epoch=2, write_generation=0 WHERE drive_label=?",
                        [label])
            return _obs(900, capacity=1000, fp=_FP)

        try:
            with dm.drive_mutation(con, ["drive-00"], "op", observe=observe,
                                   reconcile=_reconciler(con), now="2026-01-01"):
                raise AssertionError("body must not run against changed facts")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DRIVE_IDENTITY_UNPROVEN", exc.code
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0


def test_multi_drive_anchor_publish_is_all_or_nothing(tmp_path):
    """Candidate anchors are collected for every drive, then published in ONE transaction: a failure
    publishing the second drive's anchor leaves NO anchor and both generations dirty."""
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "aaa", fp=_FP, generation=0)
        _proven_drive(con, "ccc", fp=_FP2, generation=0)
        proxy = _FailOn(con, "insert into drive_clean_anchors", nth=2)   # 2nd drive's anchor insert
        raised = False
        try:
            with dm.drive_mutation(proxy, ["aaa", "ccc"], "op", observe=_observer(con),
                                   reconcile=_reconciler(con), now="2026-01-01") as writer:
                writer.record_touched("aaa", paths=["a"], keys=["K"])
                writer.record_touched("ccc", paths=["c"], keys=["K"])
        except Exception:                                # noqa: BLE001 — atomicity is the point
            raised = True
        assert raised
        other = sqlite3.connect(str(db.DB_PATH))
        try:
            assert other.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0, \
                "no anchor may be published when any drive's publish fails"
            assert other.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 2, \
                "both generations remain dirty"
        finally:
            other.close()


# =========================================================================== wiring guard (PR-03b)

def test_envelope_wired_only_into_reviewed_transport():
    """PR-03b wires the envelope into exactly ONE reviewed production transport (modelark/fetch.py) and
    nothing else. This supersedes the PR-03a dormancy guard: fetch.py MUST import the envelope, and no
    other production module may (registration/recovery/operator paths are PR-03c; admission is #35-C).
    Inspect import AST nodes so `import modelark.drive_fence` and `from modelark.drive_mutation import X`
    are both caught."""
    root = Path(__file__).resolve().parent.parent / "modelark"
    envelope = {"drive_fence", "drive_mutation"}
    allowed = {"fetch.py"}
    importers, offenders = set(), []
    for path in root.rglob("*.py"):
        if path.name in ("drive_fence.py", "drive_mutation.py"):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.Import):
                hit = any(alias.name.split(".")[-1] in envelope for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = (node.module or "").split(".")
                hit = bool(set(module) & envelope) or any(a.name in envelope for a in node.names)
            if hit:
                importers.add(path.name)
                if path.name not in allowed:
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], f"only {sorted(allowed)} may import the envelope; found: {offenders}"
    assert "fetch.py" in importers, (
        "PR-03b must wire the envelope into modelark/fetch.py (the reviewed transport); "
        "the dormancy phase is over")


def main():
    import inspect
    import tempfile
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        db_globals = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)   # isolate every test under the script runner
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                fn(Path(tempfile.mkdtemp(prefix="mark-03a-")))
            else:
                fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001 — Gate-1 wants the full red/green map
            failed.append(name)
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
        finally:
            db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = db_globals
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
