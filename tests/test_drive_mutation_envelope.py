"""PR-03a internal physical-mutation envelope (tests-first, RFC-002 / DEC-049 issue #35-B slice 1).

Gate 1: pins the contract for the same-host fence + dirty-generation/clean-anchor envelope BEFORE
production. RED until modelark.drive_fence / modelark.drive_mutation exist; the lazy import + _require()
guard makes each test fail cleanly for missing behavior, not a fixture cascade.

Scope: the COMPLETE internal envelope, unconnected to any production call site.
  - canonical same-host controller + identity+epoch drive-lock keys;
  - controller -> sorted drive locks -> short SQLite transaction ordering;
  - fenced identity/live-capacity observation (injected here; real observation is exercised with fakes);
  - atomic dirty-generation advance before any allocation, both owner fields null;
  - generation-touched reconciliation; exact epoch/generation clean-anchor CAS;
  - failures leave the generation durably dirty.
Gate-0 corrections folded in: (1) generation start requires generation zero OR an exact clean current
generation, else DIRTY_GENERATION_CONFLICT; (2) only a stable proven drive-lock identity is accepted,
missing/unproven identity is refused (migrated-drive reconciliation / provisional registration = 03c).

Non-goals here: child-FD integration + transport conversion (03b); registration/recovery/full inventory
+ operator entry points (03c); admission cutover (#35-C); durable sessions/tokens (#39).
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

from modelark.core import db

try:
    from modelark import drive_fence
    from modelark import drive_mutation as dm
    _HAS = True
except Exception:                                # noqa: BLE001 — modules under construction
    drive_fence = dm = None
    _HAS = False

_FP = "a" * 64


def _require():
    if not _HAS:
        raise AssertionError("drive_fence/drive_mutation envelope not implemented yet (Gate-1 red)")


def _catalog(tmp_path):
    db.CATALOG_DIR = tmp_path
    db.DB_PATH = tmp_path / "catalog.sqlite"
    db.STATE_DIR = tmp_path / "state"
    con = db.connect()
    assert con.execute("PRAGMA user_version").fetchone()[0] == 3, "fixture must be a v3 catalog"
    return con


def _proven_drive(con, label="drive-00", *, epoch=1, generation=0, fp=_FP, fscap=1000, free=900):
    """A drive with a proven identity (fingerprint set), as if already registered/reconciled."""
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,identity_epoch,write_generation,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority) "
        "VALUES(?,?,?,?,?,?,?, 'dedicated_local')", [label, fscap, free, epoch, generation, fscap, fp])


def _dirty(con, label, epoch, generation, op="test"):
    con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,operation_code) "
                "VALUES(?,?,?,?)", [label, epoch, generation, op])


def _anchor(con, label, epoch, generation, free=500, fscap=1000, fp=_FP):
    con.execute(
        "INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,anchor_free_bytes,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,identity_proof,fence_proof,"
        "observed_at) VALUES(?,?,?,?,?,?, 'dedicated_local','p','p','2026-01-01')",
        [label, epoch, generation, free, fscap, fp])


def _observe(free=500, proven=True):
    def observe(label):
        return dm.Observation(identity_proven=proven, free_bytes=free,
                              identity_proof="proof", fence_proof="fence")
    return observe


def _has_anchor(con, label, epoch, generation):
    return con.execute("SELECT count(*) FROM drive_clean_anchors "
                       "WHERE drive_label=? AND identity_epoch=? AND generation=?",
                       [label, epoch, generation]).fetchone()[0] == 1


# =========================================================================== lock keys

def test_controller_lock_key_is_catalog_identity_not_state_dir():
    _require()
    catalog = Path("/data/one/catalog.sqlite")
    assert drive_fence.controller_lock_key(catalog) == drive_fence.controller_lock_key(catalog)
    assert drive_fence.controller_lock_key(catalog) != \
        drive_fence.controller_lock_key(Path("/data/two/catalog.sqlite"))


def test_drive_lock_key_from_identity_and_epoch():
    _require()
    assert drive_fence.drive_lock_key(_FP, 1) == drive_fence.drive_lock_key(_FP, 1)
    assert drive_fence.drive_lock_key(_FP, 1) != drive_fence.drive_lock_key(_FP, 2)  # epoch bump
    assert drive_fence.drive_lock_key(_FP, 1) != drive_fence.drive_lock_key("b" * 64, 1)


def test_drive_lock_key_refuses_unproven_identity():
    _require()
    for missing in (None, "", "   "):
        try:
            drive_fence.drive_lock_key(missing, 1)
            raise AssertionError(f"unproven identity {missing!r} must be refused")
        except ValueError:
            pass


# =========================================================================== envelope: identity + generation

def test_envelope_refuses_a_drive_with_unproven_identity(tmp_path):
    _require()
    con = _catalog(tmp_path)
    _proven_drive(con, "drive-00", fp=None)          # migrated shape: no fingerprint -> unproven
    try:
        with dm.drive_mutation(con, ["drive-00"], "download", observe=_observe(), now="2026-01-01"):
            raise AssertionError("body should not run for an unproven drive")
    except dm.DriveMutationRefused as exc:
        assert exc.code == "DRIVE_IDENTITY_UNPROVEN", exc.code
    assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
    con.close()


def test_dirty_generation_advances_before_body_with_null_owner(tmp_path):
    _require()
    con = _catalog(tmp_path)
    _proven_drive(con, "drive-00", generation=0)
    seen = {}
    with dm.drive_mutation(con, ["drive-00"], "download", observe=_observe(), now="2026-01-01"):
        row = con.execute("SELECT identity_epoch,generation,owner_session_id,owner_fencing_token "
                          "FROM drive_dirty_generations WHERE drive_label='drive-00'").fetchone()
        seen["row"] = row
        seen["write_generation"] = con.execute(
            "SELECT write_generation FROM drives WHERE drive_label='drive-00'").fetchone()[0]
    assert seen["row"] == (1, 1, None, None), seen           # gen 1, both owner fields null, before body
    assert seen["write_generation"] == 1
    con.close()


def test_generation_start_guard(tmp_path):
    _require()
    con = _catalog(tmp_path)
    # (a) generation zero -> allowed
    _proven_drive(con, "zero", generation=0)
    with dm.drive_mutation(con, ["zero"], "op", observe=_observe(), now="2026-01-01"):
        pass
    assert _has_anchor(con, "zero", 1, 1)
    # (b) clean current generation -> allowed (advances to the next generation)
    _proven_drive(con, "clean", generation=1)
    _dirty(con, "clean", 1, 1)
    _anchor(con, "clean", 1, 1)
    with dm.drive_mutation(con, ["clean"], "op", observe=_observe(), now="2026-01-01"):
        pass
    assert _has_anchor(con, "clean", 1, 2)
    # (c) dirty current generation (no matching anchor) -> refused
    _proven_drive(con, "dirty", generation=1)
    _dirty(con, "dirty", 1, 1)                               # dirty gen 1, no anchor
    try:
        with dm.drive_mutation(con, ["dirty"], "op", observe=_observe(), now="2026-01-01"):
            raise AssertionError("must refuse starting over a dirty generation")
    except dm.DriveMutationRefused as exc:
        assert exc.code == "DIRTY_GENERATION_CONFLICT", exc.code
    con.close()


# =========================================================================== envelope: clean close vs dirty

def test_clean_close_publishes_exactly_one_matching_anchor(tmp_path):
    _require()
    con = _catalog(tmp_path)
    _proven_drive(con, "drive-00", generation=0, fscap=1000)
    with dm.drive_mutation(con, ["drive-00"], "download", observe=_observe(free=400),
                           now="2026-01-01") as writer:
        writer.record_touched("drive-00", paths=["a/b.safetensors"], keys=["KEY1"])
    anchor = con.execute(
        "SELECT identity_epoch,generation,anchor_free_bytes,filesystem_capacity_bytes,"
        "identity_fingerprint,write_authority FROM drive_clean_anchors WHERE drive_label='drive-00'"
    ).fetchall()
    assert anchor == [(1, 1, 400, 1000, _FP, "dedicated_local")], anchor
    con.close()


def test_failure_in_body_leaves_generation_durably_dirty(tmp_path):
    _require()
    con = _catalog(tmp_path)
    _proven_drive(con, "drive-00", generation=0)
    try:
        with dm.drive_mutation(con, ["drive-00"], "download", observe=_observe(), now="2026-01-01"):
            raise RuntimeError("boom mid-write")
    except RuntimeError:
        pass
    # dirty generation recorded, no clean anchor -> durably dirty
    assert con.execute("SELECT generation FROM drive_dirty_generations "
                       "WHERE drive_label='drive-00'").fetchall() == [(1,)]
    assert con.execute("SELECT count(*) FROM drive_clean_anchors "
                       "WHERE drive_label='drive-00'").fetchone()[0] == 0
    assert con.execute("SELECT write_generation FROM drives "
                       "WHERE drive_label='drive-00'").fetchone()[0] == 1   # advance persisted
    con.close()


def test_identity_mismatch_during_observation_leaves_generation_dirty(tmp_path):
    _require()
    con = _catalog(tmp_path)
    _proven_drive(con, "drive-00", generation=0)
    try:
        with dm.drive_mutation(con, ["drive-00"], "download", observe=_observe(proven=False),
                               now="2026-01-01"):
            pass
    except dm.DriveMutationRefused as exc:
        assert exc.code in ("DRIVE_IDENTITY_UNPROVEN", "DRIVE_FENCE_UNAVAILABLE"), exc.code
    assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0   # no anchor
    con.close()


# =========================================================================== clean-anchor CAS

def test_clean_anchor_cas_refuses_a_stale_or_foreign_generation(tmp_path):
    _require()
    con = _catalog(tmp_path)
    _proven_drive(con, "drive-00", generation=2)             # current generation is 2
    _dirty(con, "drive-00", 1, 2)
    obs = _observe()("drive-00")
    # publishing an anchor for a generation other than the current one is a CAS failure (no cross-clear)
    try:
        dm.publish_clean_anchor(con, "drive-00", 1, obs, now="2026-01-01")   # stale/foreign generation
        raise AssertionError("stale-generation anchor publish must fail")
    except dm.DriveMutationRefused as exc:
        assert exc.code == "CLEAN_ANCHOR_CAS_FAILED", exc.code
    assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0] == 0
    con.close()


# =========================================================================== lock ordering + contention

def test_controller_lock_is_acquired_before_drive_locks(tmp_path):
    _require()
    con = _catalog(tmp_path)
    _proven_drive(con, "drive-00", generation=0)
    order = []
    real_ctrl, real_drive = drive_fence.hold_controller, drive_fence.hold_drives_sorted

    def traced_ctrl(*a, **k):
        order.append("controller")
        return real_ctrl(*a, **k)

    def traced_drive(*a, **k):
        order.append("drives")
        return real_drive(*a, **k)

    drive_fence.hold_controller, drive_fence.hold_drives_sorted = traced_ctrl, traced_drive
    try:
        with dm.drive_mutation(con, ["drive-00"], "op", observe=_observe(), now="2026-01-01"):
            pass
    finally:
        drive_fence.hold_controller, drive_fence.hold_drives_sorted = real_ctrl, real_drive
    assert order[:2] == ["controller", "drives"], order
    con.close()


def test_cross_process_drive_lock_is_mutually_exclusive(tmp_path):
    _require()
    con = _catalog(tmp_path)
    _proven_drive(con, "drive-00", epoch=1, generation=0)
    held = tmp_path / "held.flag"
    release = tmp_path / "release.flag"
    # a separate PROCESS holds the exact drive flock, then waits for us to release it
    holder = textwrap.dedent(f"""
        import fcntl, time
        from pathlib import Path
        from modelark.core import db
        from modelark import drive_fence
        db.configure({str(tmp_path)!r}, {str(tmp_path / "state")!r})
        path = drive_fence.drive_lock_path({_FP!r}, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        Path({str(held)!r}).write_text("held")
        while not Path({str(release)!r}).exists():
            time.sleep(0.02)
        fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder])
    try:
        for _ in range(500):                                 # wait (no fixed sleep) until the flock is held
            if held.exists():
                break
            time.sleep(0.01)
        assert held.exists(), "holder process never acquired the drive flock"
        # with the drive flock held cross-process, a non-blocking envelope must refuse, not hang
        try:
            with dm.drive_mutation(con, ["drive-00"], "op", observe=_observe(), now="2026-01-01",
                                   blocking=False):
                raise AssertionError("envelope acquired a flock held by another process")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DRIVE_FENCE_UNAVAILABLE", exc.code
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0
    finally:
        release.write_text("go")
        proc.wait(timeout=10)
    con.close()


# =========================================================================== dormancy guard

def test_envelope_has_no_production_call_sites():
    """PR-03a stays dormant/non-authoritative: no production module imports the envelope until 03b/03c."""
    root = Path(__file__).resolve().parent.parent / "modelark"
    offenders = []
    for path in root.rglob("*.py"):
        if path.name in ("drive_fence.py", "drive_mutation.py"):
            continue
        text = path.read_text()
        if "import drive_fence" in text or "import drive_mutation" in text \
                or "drive_mutation(" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], f"envelope must not be wired into production yet: {offenders}"


def main():
    import inspect
    import tempfile
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
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
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
