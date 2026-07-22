"""PR-03a internal physical-mutation envelope (tests-first, RFC-002 / DEC-049 issue #35-B slice 1).

Gate 1: pins the contract for the same-host fence + dirty-generation/clean-anchor envelope BEFORE
production. RED until modelark.drive_fence / modelark.drive_mutation exist; find_spec activates the
Gate-1 guard only for an ABSENT module, so a present-but-broken module surfaces its real error.

Scope: the COMPLETE internal envelope, unconnected to any production call site.
Gate-0 corrections + Gate-1 review corrections folded in:
  * identity is proven under the locks BEFORE dirtying — an initial mismatch refuses without a dirty
    generation; a failure AFTER dirtying/writing leaves the generation durably dirty;
  * generation start requires generation zero OR an exact clean current generation whose anchor still
    matches the drive's fingerprint/capacity/authority/epoch — else DIRTY_GENERATION_CONFLICT;
  * only a stable proven drive-lock identity is accepted (unproven refused);
  * the touched path/key set is actually reconciled (injected reconciler); a reconcile failure leaves
    the generation dirty;
  * the clean anchor stores a FRESH post-reconciliation observation (free + capacity + fingerprint);
  * multi-drive: unsorted input acquires identity+epoch locks deterministically; a second-drive advance
    failure rolls back both; no SQLite write transaction is open while holding physical locks or
    reconciling;
  * physical lock namespace: same catalog + different --state-dir contend on one controller lock; same
    drive identity+epoch contend across different catalog/state paths.

Non-goals here: child-FD integration + transport conversion (03b); registration/recovery/full inventory
+ operator entry points (03c); admission cutover (#35-C); durable sessions/tokens (#39). Disposable
temp trees / fakes only.
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
    """A v3 catalog wired into the db module for the block, restoring the module globals (and closing
    the connection) on exit so no temporary path leaks between tests under any runner."""
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
    """A connection proxy that raises sqlite3.OperationalError when a (case-insensitive) marker appears
    in a statement, to inject a mid-transaction failure; everything else delegates to the real con."""

    def __init__(self, con, marker):
        self._con = con
        self._marker = marker.lower()

    def execute(self, sql, *args):
        if self._marker in sql.lower():
            raise sqlite3.OperationalError(f"injected failure at: {self._marker}")
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


def _observer(con, *observations):
    """A fenced observer returning successive observations; asserts no SQLite txn is open when called."""
    box = list(observations)

    def observe(label):
        assert not con.in_transaction, "no SQLite transaction may be open during a fenced observation"
        if not box:
            return _obs()
        return box.pop(0) if len(box) > 1 else box[0]
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


def test_controller_lock_path_is_state_dir_independent():
    _require()
    catalog = Path("/data/one/catalog.sqlite")
    db.STATE_DIR = Path("/state/a")
    path_a = drive_fence.controller_lock_path(catalog)
    db.STATE_DIR = Path("/state/b")
    path_b = drive_fence.controller_lock_path(catalog)
    assert path_a == path_b, "the controller lock file must not depend on --state-dir"


def test_drive_lock_path_is_catalog_and_state_independent():
    _require()
    db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = Path("/c1"), Path("/c1/catalog.sqlite"), Path("/s1")
    path_1 = drive_fence.drive_lock_path(_FP, 1)
    db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = Path("/c2"), Path("/c2/catalog.sqlite"), Path("/s2")
    path_2 = drive_fence.drive_lock_path(_FP, 1)
    assert path_1 == path_2, "the drive lock file must depend only on identity+epoch"


def _hold_flock_in_subprocess(tmp_path, script_body, held, release):
    holder = textwrap.dedent(script_body)
    proc = subprocess.Popen([sys.executable, "-c", holder])
    for _ in range(500):
        if held.exists():
            break
        time.sleep(0.01)
    assert held.exists(), "holder process never acquired the flock"
    return proc


def test_cross_process_controller_lock_is_shared_across_state_dirs(tmp_path):
    _require()
    catalog_dir = tmp_path / "data"
    catalog_dir.mkdir()
    held, release = tmp_path / "held", tmp_path / "release"
    # holder: SAME catalog, state dir A
    proc = _hold_flock_in_subprocess(tmp_path, f"""
        import fcntl, time
        from pathlib import Path
        from modelark.core import db
        from modelark import drive_fence
        db.configure({str(catalog_dir)!r}, {str(tmp_path / "stateA")!r})
        p = drive_fence.controller_lock_path(db.DB_PATH); p.parent.mkdir(parents=True, exist_ok=True)
        fh = open(p, "w"); fcntl.flock(fh, fcntl.LOCK_EX)
        Path({str(held)!r}).write_text("x")
        while not Path({str(release)!r}).exists(): time.sleep(0.02)
        fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
    """, held, release)
    try:
        import fcntl
        # us: SAME catalog, DIFFERENT state dir B -> must contend on the same controller lock
        db.configure(str(catalog_dir), str(tmp_path / "stateB"))
        path = drive_fence.controller_lock_path(db.DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise AssertionError("controller lock was not shared across state dirs")
            except BlockingIOError:
                pass
    finally:
        release.write_text("go")
        proc.wait(timeout=10)


def test_cross_process_drive_lock_is_shared_across_catalogs(tmp_path):
    _require()
    held, release = tmp_path / "held", tmp_path / "release"
    # holder: catalog/state pair #1 holds the drive lock for identity _FP epoch 1
    proc = _hold_flock_in_subprocess(tmp_path, f"""
        import fcntl, time
        from pathlib import Path
        from modelark.core import db
        from modelark import drive_fence
        db.configure({str(tmp_path / "c1")!r}, {str(tmp_path / "s1")!r})
        p = drive_fence.drive_lock_path({_FP!r}, 1); p.parent.mkdir(parents=True, exist_ok=True)
        fh = open(p, "w"); fcntl.flock(fh, fcntl.LOCK_EX)
        Path({str(held)!r}).write_text("x")
        while not Path({str(release)!r}).exists(): time.sleep(0.02)
        fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
    """, held, release)
    try:
        import fcntl
        # us: DIFFERENT catalog/state, SAME drive identity+epoch -> must contend on the same drive lock
        db.configure(str(tmp_path / "c2"), str(tmp_path / "s2"))
        path = drive_fence.drive_lock_path(_FP, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise AssertionError("drive lock was not shared across catalogs")
            except BlockingIOError:
                pass
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
                                   observe=_observer(con, _obs(proven=False)),
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
            with dm.drive_mutation(con, ["drive-00"], "download",
                                   observe=_observer(con, _obs(900), _obs(400)),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                raise RuntimeError("boom after dirtying")
        except RuntimeError:
            pass
        assert con.execute("SELECT generation FROM drive_dirty_generations "
                           "WHERE drive_label='drive-00'").fetchall() == [(1,)]
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
        assert con.execute("SELECT write_generation FROM drives "
                           "WHERE drive_label='drive-00'").fetchone()[0] == 1


def test_dirty_generation_advances_before_body_with_null_owner(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        seen = {}
        with dm.drive_mutation(con, ["drive-00"], "download",
                               observe=_observer(con, _obs(900), _obs(400)),
                               reconcile=_reconciler(con), now="2026-01-01"):
            seen["row"] = con.execute(
                "SELECT identity_epoch,generation,owner_session_id,owner_fencing_token "
                "FROM drive_dirty_generations WHERE drive_label='drive-00'").fetchone()
        assert seen["row"] == (1, 1, None, None), seen


def test_generation_start_guard(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "zero", generation=0)
        with dm.drive_mutation(con, ["zero"], "op", observe=_observer(con, _obs(900), _obs(400)),
                               reconcile=_reconciler(con), now="2026-01-01"):
            pass
        assert _has_anchor(con, "zero", 1, 1)

        _proven_drive(con, "clean", generation=1)
        _dirty(con, "clean", 1, 1)
        _anchor(con, "clean", 1, 1)                       # matching clean anchor -> clean current gen
        with dm.drive_mutation(con, ["clean"], "op", observe=_observer(con, _obs(900), _obs(400)),
                               reconcile=_reconciler(con), now="2026-01-01"):
            pass
        assert _has_anchor(con, "clean", 1, 2)

        _proven_drive(con, "dirty", generation=1)
        _dirty(con, "dirty", 1, 1)                        # dirty gen 1, no anchor
        try:
            with dm.drive_mutation(con, ["dirty"], "op", observe=_observer(con), reconcile=_reconciler(con),
                                   now="2026-01-01"):
                raise AssertionError("must refuse starting over a dirty generation")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DIRTY_GENERATION_CONFLICT", exc.code


def test_clean_check_requires_a_matching_anchor_identity(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        # current generation 1 has an anchor, but its fingerprint differs from the drive's current
        _proven_drive(con, "drive-00", generation=1, fp=_FP)
        _dirty(con, "drive-00", 1, 1)
        _anchor(con, "drive-00", 1, 1, fp=_FP2)           # mismatched identity -> not clean
        try:
            with dm.drive_mutation(con, ["drive-00"], "op", observe=_observer(con),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                raise AssertionError("a mismatched-identity anchor must not count as a clean generation")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DIRTY_GENERATION_CONFLICT", exc.code


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


# =========================================================================== reconciliation + fresh anchor

def test_touched_set_is_reconciled(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        record = []
        with dm.drive_mutation(con, ["drive-00"], "download",
                               observe=_observer(con, _obs(900), _obs(400)),
                               reconcile=_reconciler(con, record=record), now="2026-01-01") as writer:
            writer.record_touched("drive-00", paths=["a/b.safetensors"], keys=["KEY1"])
        assert record == [("drive-00", ("a/b.safetensors",), ("KEY1",))], record


def test_reconciliation_failure_leaves_generation_dirty(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        try:
            with dm.drive_mutation(con, ["drive-00"], "download",
                                   observe=_observer(con, _obs(900), _obs(400)),
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
        with dm.drive_mutation(con, ["drive-00"], "download",
                               observe=_observer(con, _obs(900, capacity=1000, fp=_FP),
                                                 _obs(400, capacity=1000, fp=_FP)),
                               reconcile=_reconciler(con), now="2026-01-01") as writer:
            writer.record_touched("drive-00", paths=["a/b"], keys=["KEY1"])
        row = con.execute("SELECT anchor_free_bytes,filesystem_capacity_bytes,identity_fingerprint "
                          "FROM drive_clean_anchors WHERE drive_label='drive-00'").fetchone()
        assert row == (400, 1000, _FP), row


# =========================================================================== multi-drive + txn safety

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
                                   observe=_observer(con, _obs(900), _obs(400), _obs(900), _obs(400)),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                pass
        finally:
            drive_fence.hold_drives_sorted = real
        assert captured["keys"] == sorted(captured["keys"]), captured["keys"]
        assert captured["keys"][0][0] == _FP and captured["keys"][1][0] == _FP2, captured["keys"]


def test_second_drive_advance_failure_rolls_back_both(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "aaa", fp=_FP, generation=0)          # clean
        _proven_drive(con, "ccc", fp=_FP2, generation=1)         # dirty current generation
        _dirty(con, "ccc", 1, 1)
        try:
            with dm.drive_mutation(con, ["aaa", "ccc"], "op", observe=_observer(con),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                raise AssertionError("must refuse when any drive cannot start a generation")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DIRTY_GENERATION_CONFLICT", exc.code
        # the first drive's advance must have rolled back too (atomic multi-drive advance)
        assert con.execute("SELECT write_generation FROM drives WHERE drive_label='aaa'").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM drive_dirty_generations "
                           "WHERE drive_label='aaa'").fetchone()[0] == 0


def test_no_sqlite_transaction_during_observation_or_reconciliation(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0)
        # _observer and _reconciler assert `not con.in_transaction`; reaching a clean close exercises both
        with dm.drive_mutation(con, ["drive-00"], "op", observe=_observer(con, _obs(900), _obs(400)),
                               reconcile=_reconciler(con), now="2026-01-01") as writer:
            writer.record_touched("drive-00", paths=["a"], keys=["K"])
        assert _has_anchor(con, "drive-00", 1, 1)


# =========================================================================== clean close + CAS + ordering

def test_clean_close_publishes_exactly_one_matching_anchor(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=0, fscap=1000)
        with dm.drive_mutation(con, ["drive-00"], "download",
                               observe=_observer(con, _obs(900), _obs(400)),
                               reconcile=_reconciler(con), now="2026-01-01") as writer:
            writer.record_touched("drive-00", paths=["a/b.safetensors"], keys=["KEY1"])
        anchor = con.execute(
            "SELECT identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
            "identity_fingerprint,write_authority FROM drive_clean_anchors WHERE drive_label='drive-00'"
        ).fetchall()
        assert anchor == [(1, 1, 400, 1000, _FP, "dedicated_local")], anchor


def test_clean_anchor_cas_refuses_a_stale_or_foreign_generation(tmp_path):
    _require()
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", generation=2)     # current generation is 2
        _dirty(con, "drive-00", 1, 2)
        try:
            dm.publish_clean_anchor(con, "drive-00", 1, _obs(500), now="2026-01-01")   # stale generation
            raise AssertionError("stale-generation anchor publish must fail")
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
            with dm.drive_mutation(con, ["drive-00"], "op", observe=_observer(con, _obs(900), _obs(400)),
                                   reconcile=_reconciler(con), now="2026-01-01"):
                pass
        finally:
            drive_fence.hold_controller, drive_fence.hold_drives_sorted = real_ctrl, real_drive
        assert order[:2] == ["controller", "drives"], order


# =========================================================================== dormancy guard

def test_envelope_has_no_production_call_sites():
    """PR-03a stays dormant/non-authoritative: no production module imports the envelope until 03b/03c.
    Inspect import AST nodes so `import modelark.drive_fence` and `from modelark.drive_mutation import X`
    are both caught."""
    root = Path(__file__).resolve().parent.parent / "modelark"
    envelope = {"drive_fence", "drive_mutation"}
    offenders = []
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
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], f"envelope must not be wired into production yet: {offenders}"


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
