"""PR-04 / #35-C per-file admission transport integration (tests-first — Gate-1 amendment).

The highest-risk seam identified at Gate 0: the per-file guard must admit from a FRESH observation taken
while the transport's ``drive_mutation`` envelope ALREADY holds the drive fence — it must never reacquire
the fence and never reuse the earlier (roomy) preview/legacy free space.

This exercises the REAL ``fill._file_guard`` inside a REAL ``drive_mutation`` envelope (the exact context
the transport runs it in) — not the admission function in isolation, which is what the equivalence suite
does. RED until ``fill._file_guard`` is cut over to ``admission.execution_evidence`` + a fresh held-fence
observation.

Synthetic proven drives + disposable temp trees only — never a live catalog/drive.
"""
from __future__ import annotations

import fcntl
import inspect
import shutil
import types
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from modelark.core import db
from modelark import archive_manifest, capacity, drive_fence, drive_mutation as dm, fetch, fill, reconcile

try:
    from modelark import admission  # noqa: F401 — presence gate for the cutover
    _HAS_ADMISSION = True
except ImportError as exc:                       # ONLY the missing shell — a real import/init defect surfaces
    if "admission" not in f"{getattr(exc, 'name', '') or ''} {exc}":
        raise
    _HAS_ADMISSION = False

_FP = "a" * 64


def _require_admission():
    if not _HAS_ADMISSION:
        raise AssertionError("PR-04 must add modelark/admission.py (see test_pr04_admission_seam)")


# Restore the db module globals around EVERY test (autouse under pytest; main() save/restores otherwise).
try:
    import pytest

    @pytest.fixture(autouse=True)
    def _isolate_db_globals():
        saved = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)
        try:
            yield
        finally:
            db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = saved
except ImportError:
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


def _proven_drive(con, label="drive-00", *, fp=_FP, fscap=1000, free=900):
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,identity_epoch,write_generation,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority) "
        "VALUES(?,?,?,1,0,?,?, 'dedicated_local')", [label, fscap, free, fscap, fp])
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _obs(*, free, fscap=1000, fp=_FP):
    return dm.Observation(identity_proven=True, free_bytes=free, filesystem_capacity=fscap,
                          fingerprint=fp, identity_proof="p", fence_proof="p")


def _fetch_task(target="drive-00", rfilename="model.gguf", size=500):
    budget = capacity.TaskBudget(
        task_id="fetch:primary:org/m", requirement_id="primary:org/m", repo_id="org/m",
        kind=reconcile.TaskKind.FETCH, target_drive=target, source_drive=None,
        missing_files=(rfilename,),
        file_budgets=(capacity.FileBudget(
            rfilename=rfilename, guaranteed_durable=size, expected_durable=size,
            workspace_peak_guaranteed=0, workspace_peak_expected=0, evidence="estimate"),),
        guaranteed_durable=size, expected_durable=size, workspace_peak_guaranteed=0,
        workspace_peak_expected=0, evidence="estimate")
    return capacity.AssignedTask(
        task_id=budget.task_id, requirement_id=budget.requirement_id, repo_id="org/m",
        kind=reconcile.TaskKind.FETCH, target_drive=target, source_drive=None,
        depends_on_requirement=None, budget=budget)


def _acquirable(path):
    """True iff the advisory flock at `path` can be taken non-blocking right now (released immediately)."""
    try:
        fh = open(path, "w")                      # noqa: SIM115 — released below
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fcntl.flock(fh, fcntl.LOCK_UN)
    fh.close()
    return True


def test_per_file_guard_admits_from_fresh_held_fence_observation_not_preview(tmp_path):
    """Inside the held-fence envelope the guard admits from a FRESH observation (100 free -> 50
    admissible) and REFUSES the 500-byte file — it does not admit on the roomy preview/legacy free (900)
    and does not reacquire the drive fence the envelope already holds."""
    _require_admission()
    with _catalog(tmp_path) as con:
        con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
        _proven_drive(con, "drive-00", fp=_FP, fscap=1000, free=900)      # roomy legacy/preview free
        task = _fetch_task()
        before_file = fill._file_guard(fetch.RunCtx(con=con), "ark", "guaranteed", task)
        item = archive_manifest.ManifestFile("model.gguf", 500, None, "gguf", None, "raw")

        hold_calls = {"n": 0}
        real_hold = drive_fence.hold_drives_sorted

        def counting_hold(keyed, **kwargs):
            hold_calls["n"] += 1
            return real_hold(keyed, **kwargs)

        captured = {}
        with mock.patch.object(fetch, "_observe_drive", return_value=_obs(free=100, fscap=1000)) as obs_spy, \
             mock.patch.object(fetch.register, "archive_path", return_value=tmp_path), \
             mock.patch.object(fill.shutil, "disk_usage",
                               return_value=types.SimpleNamespace(total=1000, used=100, free=900)), \
             mock.patch.object(drive_fence, "hold_drives_sorted", side_effect=counting_hold):
            # run the guard exactly where the transport runs it: inside the held-fence batch envelope
            with dm.drive_mutation(con, ["drive-00"], "fetch",
                                   observe=lambda label: fetch._observe_drive(con, label),
                                   reconcile=lambda *a, **k: None, now="2026-01-01"):
                captured["fence_held"] = not _acquirable(drive_fence.drive_lock_path(_FP, 1))
                holds_before_guard = hold_calls["n"]
                try:
                    before_file("org/m", item)
                    captured["refused"] = False
                except fetch.CapacityPreflightError as exc:
                    captured["refused"] = True
                    captured["available"] = exc.failure.available_bytes
                captured["guard_reacquired"] = hold_calls["n"] > holds_before_guard

        assert captured["fence_held"] is True, "the guard must run under the already-held drive fence"
        assert captured["refused"] is True, (
            "the guard must refuse on the FRESH held-fence observation (100 free), not the roomy "
            "preview/legacy free (900)")
        assert captured["available"] == 50, "admission must reflect the fresh observation (100 − 50 floor)"
        obs_spy.assert_called()                                          # a fresh held-fence observation
        assert captured["guard_reacquired"] is False, \
            "the per-file guard must NOT reacquire the drive fence the envelope already holds"


def test_file_guard_does_not_read_live_free_via_disk_usage_after_cutover():
    """Characterization of the cutover boundary: the guard obtains admission from the evidence seam, so
    it no longer reads live free directly through shutil.disk_usage. RED until the guard is cut over."""
    _require_admission()
    src = inspect.getsource(fill._file_guard)
    assert "disk_usage" not in src, \
        "the per-file guard must admit from admission.execution_evidence, not a direct shutil.disk_usage read"


def main():
    import tempfile
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        saved = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)
        tmp = None
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                tmp = Path(tempfile.mkdtemp(prefix="mark-pr04-tx-"))
                fn(tmp)
            else:
                fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001 — Gate-1 wants the full red/green map
            failed.append(name)
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
        finally:
            db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = saved
            if tmp is not None:                  # the standalone runner cleans its own temp trees
                shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
