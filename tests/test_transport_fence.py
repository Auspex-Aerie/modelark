"""PR-03b Gate-1 contract — wire the Fill/download/compression/annex/replica transport through the
merged catalog-v3 mutation envelope with inherited child fence FDs (RFC-002 / DEC-049 issue #35-B slice 2).

Tests-first, pinned BEFORE production. The change-contract tests are RED against the current transport
(the envelope is dormant); the characterization/mechanism tests are GREEN now and must stay GREEN.

Binding Gate-0 rulings encoded here:
  R1  an unproven target FAILS CLOSED with DRIVE_IDENTITY_UNPROVEN and mutates nothing (no unfenced
      fallback); everything is tested with synthetic proven drives.
  R2  acquisition is controller -> sorted drive locks -> committed dirty advance, then the controller
      is RELEASED while the drive locks are retained across body, reconciliation, and anchor publish.
  R3  sessionless FD inheritance + the parent-death lock invariant (tokens/leases/recovery are #39).
  R4  the envelope wraps ONE drive batch of fetch.run() (not per repository), so an isolated repo
      failure cannot leave the whole drive dirty and DIRTY_GENERATION_CONFLICT every later repo.
  R5  replica fences BOTH source and target and passes BOTH FDs to writing children.
  R6  every mutating child (download/compress, git annex add/metadata/copy/sync, git remote config)
      inherits the fence FDs; read-only lookups/version/whereis do NOT.
  R7  touched reconciliation is narrow and real: it validates only the recorded published paths, annex
      keys, and their durable catalog facts (no full-drive scan, no new cleanup policy).

Second-round reviewer seams pinned here:
  1. live identity proof — the observer derives fingerprint/capacity/free from live evidence;
  2. end-to-end fence-FD flow — writer exposes the actual held FDs, threaded run -> fetch_model ->
     download/compress/annex/remote children and source+target replica children;
  3. reconciliation is connected to transport — a published path/key reaches reconciliation, and a
     durable row with absent bytes fails closed;
  4. mutating writability probes run only after dirtying (replica + run() error path);
  5. the fetch_events "terminal" defect is fixed in scope (allowed outcome + code in detail), no mock.

Proposed production API surfaced by these tests (for Gate-1 review; names are the contract to confirm):
  * fetch._observe_drive(con, label) -> dm.Observation, deriving identity via
    fetch._live_drive_evidence(con, label), which reads only low-level probes:
    register.probe_fs_uuid / probe_annex_uuid / probe_serial(mount) + os.statvfs(mount)
  * dm mutation writer exposes .child_fence_fds (the actual held drive-lock FDs)
  * fetch.fetch_model(..., mutation_writer=...) uses writer.child_fence_fds for its mutating children
    and calls writer.record_touched(paths, keys) AFTER physical publication + the durable archived row
  * fetch._reconcile_touched validates recorded annex KEYS (via _annex_key_on_uuid / _annex_key_for_path),
    not only paths; run() converts a clean-close reconciliation refusal into a typed terminal
  * fetch._run_monitored/_download_shard/_compress_isolated(..., inherit_fds=()) -> Popen(pass_fds=...)
  * fetch._annex_add(dest, path, inherit_fds=())  / fetch._annex_metadata(..., inherit_fds=())
  * fetch._reconcile_touched(con, label, dest, annex, paths, keys) -> None | raise DriveMutationRefused
  * run() wraps its drive-batch loop in drive_mutation, surfaces DRIVE_IDENTITY_UNPROVEN as a typed
    terminal, records a typed terminal as an ALLOWED fetch_events outcome (code in detail); replica
    enters a two-drive drive_mutation with the writability probe fenced.

Disposable temp trees / synthetic proven drives / real subprocesses only — never a live catalog/drive.
"""
from __future__ import annotations

import fcntl
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from modelark.core import db
from modelark import archive_manifest
from modelark import capacity_evidence
from modelark import drive_fence
from modelark import drive_mutation as dm
from modelark import fetch

_FP = "a" * 64
_FP2 = "c" * 64


# Restore the db module globals around EVERY test so a test that repoints CATALOG_DIR/DB_PATH/STATE_DIR
# cannot leak a temp path into a later test (autouse under pytest; main() save/restores under the script
# runner, matching tests/test_drive_mutation_envelope.py).
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


def _proven_drive(con, label="drive-00", *, epoch=1, generation=0, fp=_FP,
                  fscap=1000, free=900, annex_uuid=None):
    """A drive with proven identity for the current epoch (PR-03c will establish this on real drives)."""
    con.execute(
        "INSERT INTO drives(drive_label,capacity_bytes,free_bytes,identity_epoch,write_generation,"
        "filesystem_capacity_bytes,identity_fingerprint,write_authority,annex_uuid) "
        "VALUES(?,?,?,?,?,?,?, 'dedicated_local', ?)",
        [label, fscap, free, epoch, generation, fscap, fp, annex_uuid])


def _unproven_drive(con, label="drive-00"):
    """A migrated row: epoch-1 namespace only, no fingerprint/authority (DEC-049 / #35 contract)."""
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes) VALUES(?,?,?)",
                [label, 1000, 900])


def _obs(free=500, *, capacity=1000, fp=_FP, proven=True):
    return dm.Observation(identity_proven=proven, free_bytes=free, filesystem_capacity=capacity,
                          fingerprint=fp, identity_proof="proof", fence_proof="fence")


def _observer(con, *frees):
    """A fenced observer attesting the drive's CURRENT fingerprint/capacity, yielding successive frees."""
    box = list(frees)

    def observe(label):
        assert not con.in_transaction, "no SQLite transaction may be open during a fenced observation"
        fp, cap = con.execute(
            "SELECT identity_fingerprint, filesystem_capacity_bytes FROM drives WHERE drive_label=?",
            [label]).fetchone()
        free = box.pop(0) if len(box) > 1 else (box[0] if box else 500)
        return _obs(free, capacity=cap, fp=fp)
    return observe


def _reconciler(con):
    def reconcile(label, paths, keys):
        assert not con.in_transaction, "no SQLite transaction may be open during reconciliation"
    return reconcile


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


def _drive_acquirable(fp, epoch):
    try:
        with drive_fence.hold_drives_sorted([(fp, epoch)], blocking=False):
            return True
    except drive_fence.FenceUnavailable:
        return False


def _run_kwargs(**over):
    base = {"dest": None, "drive_label": "drive-00", "repos": ["a"], "max_24h_gb": 0}
    base.update(over)
    return base


def _observe_from_rows(con):
    """A stand-in for the production ``_observe_drive`` used by batch-wiring tests (Gate-0 R1 permits
    mocking the adapter here): attest each drive's CURRENT stored fingerprint/capacity so identity
    proves. The dedicated adapter tests exercise the REAL observer over live evidence."""
    def observe(_con, label):
        fp, cap, free = con.execute(
            "SELECT identity_fingerprint, filesystem_capacity_bytes, free_bytes FROM drives "
            "WHERE drive_label=?", [label]).fetchone()
        return _obs(free if free is not None else 500, capacity=cap, fp=fp)
    return observe


@contextmanager
def _capture_held_fence_fds(store):
    """Wrap drive_fence.hold_drives_sorted to record the ACTUAL held drive-lock FDs, so a test can
    assert those exact FDs reach the writer / children rather than arbitrary values."""
    real = drive_fence.hold_drives_sorted

    @contextmanager
    def traced(keyed_drives, *a, **k):
        with real(keyed_drives, *a, **k) as handles:
            store["fds"] = tuple(h.fileno() for h in handles)
            yield handles

    with mock.patch.object(drive_fence, "hold_drives_sorted", traced):
        yield store


def _replica_setup(con, tmp_path):
    """Two proven drives (source/target) + a durable source archived row + a synthetic replica task."""
    _proven_drive(con, "drive-00", fp=_FP, annex_uuid="uuid-src")
    _proven_drive(con, "drive-04", fp=_FP2, annex_uuid="uuid-tgt")
    con.execute("INSERT INTO models(repo_id) VALUES('must')")
    con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                "VALUES('must','model.gguf',200,'gguf')")
    con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
                "orig_sha256,znn_sha256,orig_bytes,stored_bytes,compressed,annex_key) "
                "VALUES('must','model.gguf','model.gguf','model.gguf','drive-00',"
                "'orig','stored',200,200,0,'key-must')")
    source, target, library = tmp_path / "src", tmp_path / "tgt", tmp_path / "lib"
    for d in (source, target, library):
        d.mkdir()
    task = types.SimpleNamespace(source_drive="drive-00", target_drive="drive-04",
                                 requirement_id="r1", repo_id="must",
                                 budget=types.SimpleNamespace(missing_files=["model.gguf"]))

    def archive_path(_con, label):
        return source if label == "drive-00" else target

    return task, archive_path, library


# ===================================================================== R4/R1: dirty before mutation

def test_dirty_generation_committed_before_first_batch_mutation(tmp_path):
    """The drive's dirty generation must be committed (visible to an INDEPENDENT connection) before the
    first batch mutation — the first staging/temp/probe/download the transport performs."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        probe = sqlite3.connect(str(db.DB_PATH))
        seen = {}

        def fake_model(ctx, rid, dest, label, annex, cfg, **kw):
            seen[rid] = probe.execute(
                "SELECT count(*) FROM drive_dirty_generations WHERE drive_label='drive-00'"
            ).fetchone()[0]
            return {"repo_id": rid, "files": 1, "skipped": 0, "bytes": 1}

        try:
            with mock.patch.object(fetch, "_observe_drive", side_effect=_observe_from_rows(con), create=True), \
                 mock.patch.object(fetch, "fetch_model", side_effect=fake_model), \
                 mock.patch.object(fetch, "_is_annex", return_value=False), \
                 mock.patch.object(fetch.wishlist, "compression", return_value={"threads": 1}):
                fetch.run(ctx=fetch.RunCtx(con=con), **_run_kwargs(dest=tmp_path))
        finally:
            probe.close()
        assert seen.get("a") == 1, (
            "the dirty generation must be committed and visible to a second connection BEFORE the first "
            f"batch mutation runs (saw {seen.get('a')})")


def test_one_dirty_generation_and_anchor_per_drive_batch_not_per_repo(tmp_path):
    """R4: the envelope wraps ONE drive batch, so N repositories share ONE dirty generation and publish
    ONE clean anchor — not one generation per repo (which would DIRTY_GENERATION_CONFLICT repo 2)."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")

        def fake_model(ctx, rid, dest, label, annex, cfg, **kw):
            return {"repo_id": rid, "files": 1, "skipped": 0, "bytes": 1}

        with mock.patch.object(fetch, "_observe_drive", side_effect=_observe_from_rows(con), create=True), \
             mock.patch.object(fetch, "fetch_model", side_effect=fake_model), \
             mock.patch.object(fetch, "_is_annex", return_value=False), \
             mock.patch.object(fetch.wishlist, "compression", return_value={"threads": 1}):
            fetch.run(ctx=fetch.RunCtx(con=con), **_run_kwargs(dest=tmp_path, repos=["a", "b", "c"]))
        dirty = con.execute("SELECT count(*) FROM drive_dirty_generations "
                            "WHERE drive_label='drive-00'").fetchone()[0]
        anchors = con.execute("SELECT count(*) FROM drive_clean_anchors "
                              "WHERE drive_label='drive-00'").fetchone()[0]
        assert (dirty, anchors) == (1, 1), (
            f"a 3-repo drive batch must open exactly one generation and publish one anchor; "
            f"got dirty={dirty} anchors={anchors}")


# ===================================================================== R2: controller release

def test_controller_released_but_drive_fence_retained_during_body(tmp_path):
    """R2: once the dirty advance is committed the controller fence is released (so operator graph
    writes / recovery are not blocked for the whole multi-day body) while the drive fence stays held."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP, epoch=1, generation=0)
        probe = {}
        with dm.drive_mutation(con, ["drive-00"], "op", observe=_observer(con, 900, 400),
                               reconcile=_reconciler(con), now="2026-01-01"):
            probe["controller_free"] = _acquirable(drive_fence.controller_lock_path(db.DB_PATH))
            probe["drive_locked"] = not _acquirable(drive_fence.drive_lock_path(_FP, 1))
        assert probe["drive_locked"], "the drive fence must remain HELD across the transport body"
        assert probe["controller_free"], (
            "the controller fence must be RELEASED after the committed dirty advance, not held across "
            "the transport body")


# ===================================================================== R1: unproven fails closed

def test_unproven_target_fails_closed_without_filesystem_mutation(tmp_path):
    """R1: a not-yet-proven drive must refuse with DRIVE_IDENTITY_UNPROVEN before any transport runs —
    never the old unfenced transport. The integration branch is deliberately not live-ready until 03c."""
    with _catalog(tmp_path) as con:
        _unproven_drive(con, "drive-00")
        called = {"n": 0}

        def fake_model(ctx, rid, dest, label, annex, cfg, **kw):
            called["n"] += 1
            return {"repo_id": rid, "files": 0, "skipped": 0, "bytes": 0}

        with mock.patch.object(fetch, "fetch_model", side_effect=fake_model), \
             mock.patch.object(fetch, "_is_annex", return_value=False), \
             mock.patch.object(fetch.wishlist, "compression", return_value={"threads": 1}):
            result = fetch.run(ctx=fetch.RunCtx(con=con), **_run_kwargs(dest=tmp_path))
        assert called["n"] == 0, "an unproven drive must fail closed BEFORE any transport mutation runs"
        terminal = result.get("terminal_failure") or {}
        assert terminal.get("code") == "DRIVE_IDENTITY_UNPROVEN", (
            f"an unproven target must surface a typed DRIVE_IDENTITY_UNPROVEN terminal; got {terminal}")
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0


# ===================================================================== R3: parent-death lock invariant

def test_inherited_fd_survives_parent_death(tmp_path):
    """R3 core crash-safety invariant (pure OS + drive_fence mechanism): a child that inherits the
    drive-fence FD keeps the flock held after the parent dies, so nothing else can acquire the drive
    lock while the child may still be writing. Releases only when the child exits."""
    fpd, epoch = "e" * 64, 7
    ready, release = tmp_path / "ready", tmp_path / "release"
    child_script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "rel = Path(sys.argv[2])\n"          # argv[1] is the fd number (kept open via pass_fds)
        "while not rel.exists():\n"
        "    time.sleep(0.02)\n"
    )
    parent_src = (
        "import os, sys, time, subprocess\n"
        "from pathlib import Path\n"
        "from modelark import drive_fence\n"
        # keep `cm` referenced: a temporary would be GC'd, closing the fence handle at once
        f"cm = drive_fence.hold_drives_sorted([({fpd!r}, {epoch})])\n"
        "handles = cm.__enter__()\n"
        "fd = handles[0].fileno()\n"
        "os.set_inheritable(fd, True)\n"
        f"subprocess.Popen([sys.executable, '-c', {child_script!r}, str(fd), {str(release)!r}], "
        "pass_fds=[fd])\n"
        f"Path({str(ready)!r}).write_text('x')\n"
        "time.sleep(600)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", parent_src])
    try:
        for _ in range(500):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists(), "parent never acquired the fence and spawned the inheriting child"
        parent.kill()
        parent.wait(timeout=10)                       # reap so the parent's own fd is definitely closed
        assert not _drive_acquirable(fpd, epoch), (
            "the drive lock must NOT be acquirable while a surviving child still holds the inherited "
            "fence FD after parent death")
    finally:
        release.write_text("go")
        if parent.poll() is None:                 # only if an early assertion skipped the kill above
            parent.kill()
            parent.wait(timeout=10)
    acquired = False
    for _ in range(500):
        if _drive_acquirable(fpd, epoch):
            acquired = True
            break
        time.sleep(0.01)
    assert acquired, "the drive lock must become acquirable once the inheriting child exits"


# ===================================================================== R6: every mutating child gets FDs

def test_run_monitored_forwards_inherit_fds_to_child(tmp_path):
    """R6: the monitored-child spawner (download + compression) forwards the held fence FDs to the
    child via pass_fds, so a killed/orphaned child keeps the drive fence."""
    assert "inherit_fds" in inspect.signature(fetch._run_monitored).parameters, \
        "PR-03b must add an `inherit_fds` parameter to _run_monitored (download + compression children)"
    fd = os.open(os.devnull, os.O_RDONLY)
    captured = {}

    class _FakeProc:
        returncode = 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    def fake_popen(cmd, **kw):
        captured["pass_fds"] = kw.get("pass_fds")
        return _FakeProc()

    try:
        with mock.patch.object(fetch.subprocess, "Popen", side_effect=fake_popen):
            fetch._run_monitored([sys.executable, "-c", "pass"], lambda: 0, 999,
                                 lambda: False, inherit_fds=(fd,))
    finally:
        os.close(fd)
    assert tuple(captured.get("pass_fds") or ()) == (fd,), (
        f"the monitored child must inherit the held fence FD via pass_fds; got {captured.get('pass_fds')}")


def test_annex_children_inherit_fence_fds_only_when_mutating(tmp_path):
    """R6: git-annex add/metadata (mutating) inherit the fence FDs; the read-only lookupkey does not."""
    assert "inherit_fds" in inspect.signature(fetch._annex_add).parameters, \
        "PR-03b must add `inherit_fds` to _annex_add"
    assert "inherit_fds" in inspect.signature(fetch._annex_metadata).parameters, \
        "PR-03b must add `inherit_fds` to _annex_metadata"
    dest = tmp_path
    (dest / "must").mkdir()
    target = dest / "must" / "model.gguf"
    target.write_bytes(b"x")
    fd = os.open(os.devnull, os.O_RDONLY)
    calls = []

    def cap_run(cmd, *a, **k):
        calls.append((tuple(cmd), k.get("pass_fds")))
        return subprocess.CompletedProcess(cmd, 0, "key123\n", "")

    try:
        with mock.patch.object(fetch.subprocess, "run", side_effect=cap_run):
            fetch._annex_add(dest, target, inherit_fds=(fd,))
            fetch._annex_metadata(dest, "key123", "org/repo", None, "gguf", None, inherit_fds=(fd,))
    finally:
        os.close(fd)
    add = [fds for cmd, fds in calls if "annex" in cmd and "add" in cmd]
    meta = [fds for cmd, fds in calls if "annex" in cmd and "metadata" in cmd]
    lookup = [fds for cmd, fds in calls if "lookupkey" in cmd]
    assert add and all(tuple(fds or ()) == (fd,) for fds in add), f"annex add must inherit the fence FD: {calls}"
    assert meta and all(tuple(fds or ()) == (fd,) for fds in meta), f"annex metadata must inherit the FD: {calls}"
    assert lookup and all(not fds for fds in lookup), \
        f"read-only annex lookupkey must NOT inherit the fence FD: {calls}"


def test_readonly_git_probes_do_not_inherit_fence_fds(tmp_path):
    """R6 characterization: read-only probes (annex version, whereis) never carry a fence FD."""
    (tmp_path / ".git").mkdir()
    calls = []

    def cap_run(cmd, *a, **k):
        calls.append((tuple(cmd), k.get("pass_fds")))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch.object(fetch.subprocess, "run", side_effect=cap_run):
        fetch._is_annex(tmp_path)                                   # git annex version
        fetch._annex_key_on_uuid(tmp_path, "key", "uuid")          # git annex whereis --key
    assert calls, "expected the read-only git probes to run"
    assert all(not fds for _cmd, fds in calls), \
        f"read-only probes must not carry pass_fds: {calls}"


# ===================================================================== R5: replica fences source+target

def test_replica_fences_source_and_target_and_passes_both_fds(tmp_path):
    """R5/seam 2: a replica copy mutates the target (copy) and the source (remote config + bookkeeping);
    the envelope holds BOTH identity+epoch fences and every mutating child inherits the ACTUAL held
    source+target FDs."""
    with _catalog(tmp_path) as con:
        task, archive_path, library = _replica_setup(con, tmp_path)
        held, captured, reconciled = {}, [], []

        def cap_run(cmd, *a, **k):
            captured.append((tuple(cmd), k.get("pass_fds")))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def cap_reconcile(_con, label, dest, annex, paths, keys):
            reconciled.append((label, tuple(paths), tuple(keys)))

        with _capture_held_fence_fds(held), \
             mock.patch.object(fetch, "_observe_drive", side_effect=_observe_from_rows(con), create=True), \
             mock.patch.object(fetch, "_reconcile_touched", side_effect=cap_reconcile, create=True), \
             mock.patch.object(fetch.register, "archive_path", side_effect=archive_path), \
             mock.patch.object(fetch.register, "library_root", return_value=library), \
             mock.patch.object(fetch, "_dest_writable", return_value=True), \
             mock.patch.object(fetch, "_annex_key_on_uuid", return_value=True), \
             mock.patch.object(fetch.subprocess, "run", side_effect=cap_run):
            fetch.run_replica_tasks([task], ctx=fetch.RunCtx(con=con))
        assert held.get("fds") and len(held["fds"]) == 2, \
            "both source and target identity+epoch fences must be held for a replica copy"
        mutating = [(cmd, fds) for cmd, fds in captured
                    if "remote" in cmd or ("annex" in cmd and ("copy" in cmd or "sync" in cmd))]
        assert mutating, f"expected mutating git children (remote/copy/sync); captured {[c for c, _ in captured]}"
        for cmd, fds in mutating:
            assert tuple(fds or ()) == held["fds"], \
                f"replica child {cmd[:5]} must inherit BOTH actual held fence FDs; got {fds}"
        syncs = [cmd for cmd, _fds in captured if "annex" in cmd and "sync" in cmd]
        assert syncs and all("drive-00" in cmd and "drive-04" in cmd for cmd in syncs), \
            f"the replica map sync must name only this group's source+target remotes; got {syncs}"
        # the copied target key must reach reconciliation (not just be written and forgotten)
        assert any(label == "drive-04" and "key-must" in keys for label, _paths, keys in reconciled), \
            f"the copied target path/key must reach reconciliation; saw {reconciled}"


# ===================================================================== R7: narrow real reconciliation

def test_touched_reconciliation_validates_recorded_set_narrowly(tmp_path):
    """R7: reconciliation validates only the recorded touched paths/keys against durable catalog facts;
    an unrelated (untouched) durable row + file on the same drive is ignored (no full-drive scan)."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        # durable facts for the touched path (archived FK -> files -> models); FK enforcement is ON,
        # so the parents must exist or the inserts IntegrityError before the narrowing is exercised.
        con.execute("INSERT INTO models(repo_id) VALUES('must')")
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                    "VALUES('must','model.safetensors',5,'safetensors')")
        (tmp_path / "must").mkdir()
        (tmp_path / "must" / "model.safetensors").write_bytes(b"bytes")
        con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
                    "orig_bytes,stored_bytes,compressed) VALUES('must','model.safetensors',"
                    "'model.safetensors','model.safetensors','drive-00',5,5,0)")
        # a decoy unrelated durable row + file that a NARROW reconciler must never inspect
        con.execute("INSERT INTO models(repo_id) VALUES('decoy')")
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                    "VALUES('decoy','x.bin',1,'other')")
        (tmp_path / "other").mkdir()
        (tmp_path / "other" / "x.bin").write_bytes(b"z")
        con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
                    "orig_bytes,stored_bytes,compressed) VALUES('decoy','x.bin','x.bin',"
                    "'other/x.bin','drive-00',1,1,0)")
        # guard AFTER setup so the FK-correct fixture is exercised even while this stays RED at Gate-1
        assert hasattr(fetch, "_reconcile_touched"), \
            "PR-03b must add a narrow touched-set reconciler (fetch._reconcile_touched)"
        fetch._reconcile_touched(con, "drive-00", tmp_path, False, ["must/model.safetensors"], [])


def test_touched_reconciliation_fails_closed_on_unprovable_annex_key(tmp_path):
    """R7: reconciliation validates annex KEYS, not just paths — a recorded key that cannot be proven on
    the drive fails closed. An implementation that ignores keys entirely cannot pass this."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        con.execute("INSERT INTO models(repo_id) VALUES('must')")
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                    "VALUES('must','model.gguf',5,'gguf')")
        (tmp_path / "must").mkdir()
        (tmp_path / "must" / "model.gguf").write_bytes(b"bytes")   # bytes present — the KEY is the problem
        con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
                    "orig_bytes,stored_bytes,compressed,annex_key) VALUES('must','model.gguf',"
                    "'model.gguf','model.gguf','drive-00',5,5,0,'KEY-x')")
        assert hasattr(fetch, "_reconcile_touched"), "PR-03b must add fetch._reconcile_touched"
        # both annex-key proof helpers report the key unprovable on this drive
        with mock.patch.object(fetch, "_annex_key_on_uuid", return_value=False), \
             mock.patch.object(fetch, "_annex_key_for_path", return_value=None):
            try:
                fetch._reconcile_touched(con, "drive-00", tmp_path, True,
                                         ["must/model.gguf"], ["KEY-x"])
                raise AssertionError("reconciliation must fail closed on an unprovable annex key")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_RECONCILIATION_REQUIRED", exc.code


# ===================================================================== R (stop/error): typed + dirty

def test_typed_terminal_is_preserved_as_result_and_durable_event(tmp_path):
    """Seam 5: a per-repo typed terminal must surface in the result AND be recorded durably WITHOUT the
    schema-forbidden 'terminal' fetch_events outcome. The preservation fix records an ALLOWED outcome
    carrying the typed code in `detail` (no schema migration); the envelope must preserve both."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")

        def terminal_model(ctx, rid, dest, label, annex, cfg, **kw):
            raise fetch.TargetPathConflictError(rid, "existing file has different verified bytes")

        with mock.patch.object(fetch, "_observe_drive", side_effect=_observe_from_rows(con), create=True), \
             mock.patch.object(fetch, "fetch_model", side_effect=terminal_model), \
             mock.patch.object(fetch, "_is_annex", return_value=False), \
             mock.patch.object(fetch.wishlist, "compression", return_value={"threads": 1}):
            try:
                result = fetch.run(ctx=fetch.RunCtx(con=con), **_run_kwargs(dest=tmp_path))
            except sqlite3.IntegrityError as exc:
                raise AssertionError(
                    "run() must record the typed terminal as an ALLOWED fetch_events outcome with the "
                    f"code in detail, not the schema-forbidden 'terminal' outcome: {exc}") from exc
        assert result["terminal_failure"]["code"] == "TARGET_PATH_CONFLICT", result
        assert result["terminal_repo"] == "a"
        rows = con.execute("SELECT outcome, detail FROM fetch_events WHERE repo_id='a'").fetchall()
        assert rows, "the typed terminal must be recorded as a durable fetch_events row"
        outcome, detail = rows[-1]
        assert outcome == "error", \
            f"the typed terminal must be recorded under the allowed 'error' outcome, not {outcome!r}"
        assert "TARGET_PATH_CONFLICT" in (detail or ""), \
            f"the typed terminal code must be preserved in the event detail; got {detail!r}"


def test_crash_during_batch_leaves_generation_dirty_without_anchor(tmp_path):
    """An abrupt crash mid-batch (no clean close) must leave the generation durably dirty with no clean
    anchor, so 03c recovery reconciles it rather than trusting stale free-space."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")

        class _Crash(BaseException):
            pass

        def crashing_model(ctx, rid, dest, label, annex, cfg, **kw):
            raise _Crash("abrupt death mid-transport")

        with mock.patch.object(fetch, "_observe_drive", side_effect=_observe_from_rows(con), create=True), \
             mock.patch.object(fetch, "fetch_model", side_effect=crashing_model), \
             mock.patch.object(fetch, "_is_annex", return_value=False), \
             mock.patch.object(fetch.wishlist, "compression", return_value={"threads": 1}):
            try:
                fetch.run(ctx=fetch.RunCtx(con=con), **_run_kwargs(dest=tmp_path))
            except _Crash:
                pass
        dirty = con.execute("SELECT count(*) FROM drive_dirty_generations "
                            "WHERE drive_label='drive-00'").fetchone()[0]
        anchors = con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0]
        assert (dirty, anchors) == (1, 0), (
            f"an abrupt crash must leave the generation dirty with no clean anchor; "
            f"dirty={dirty} anchors={anchors}")


# ===================================================================== seam 1: live identity proof

def test_observe_drive_derives_identity_from_live_evidence(tmp_path):
    """The REAL `_observe_drive`+`_live_drive_evidence` derive fingerprint/capacity/free from LIVE probes
    (fs_uuid/annex_uuid/serial/statvfs), NOT the persisted row. Only the low-level probes are mocked; the
    persisted row is set to deliberately CONFLICTING values, so trusting the catalog would fail the test."""
    with _catalog(tmp_path) as con:
        # persisted row deliberately conflicts with the live probes below (stale identity + free/cap)
        stale_fp = capacity_evidence.identity_fingerprint_v1(
            fs_uuid="STALE-uuid", annex_uuid="STALE-annex", serial="STALE-serial",
            filesystem_capacity_bytes=222)
        _proven_drive(con, "drive-00", fp=stale_fp, fscap=222, free=111)
        assert hasattr(fetch, "_observe_drive") and hasattr(fetch, "_live_drive_evidence"), \
            "PR-03b must add fetch._observe_drive + fetch._live_drive_evidence (live identity proof)"
        live_fp = capacity_evidence.identity_fingerprint_v1(
            fs_uuid="LIVE-uuid", annex_uuid="LIVE-annex", serial="LIVE-serial",
            filesystem_capacity_bytes=1000)
        statvfs = types.SimpleNamespace(f_frsize=1, f_blocks=1000, f_bavail=850)
        # mock ONLY the low-level probes; the real _live_drive_evidence + _observe_drive run
        with mock.patch.object(fetch.register, "archive_path", return_value=tmp_path), \
             mock.patch.object(fetch.register, "probe_fs_uuid", return_value="LIVE-uuid", create=True), \
             mock.patch.object(fetch.register, "probe_annex_uuid", return_value="LIVE-annex", create=True), \
             mock.patch.object(fetch.register, "probe_serial", return_value="LIVE-serial", create=True), \
             mock.patch.object(fetch.os, "statvfs", return_value=statvfs):
            obs = fetch._observe_drive(con, "drive-00")
        assert obs.identity_proven is True
        assert obs.fingerprint == live_fp, "fingerprint must come from live probes, not the persisted row"
        assert obs.filesystem_capacity == 1000, "capacity must be the live statvfs total, not persisted 222"
        assert obs.free_bytes == 850, "free must be the live statvfs value, not persisted 111"


def test_live_identity_mismatch_refuses(tmp_path):
    """If the LIVE probes yield a different identity than the proven row, the mutation refuses
    DRIVE_IDENTITY_UNPROVEN and never dirties (a swapped/mismounted volume). Real derivation; only the
    low-level probes are mocked, with a divergent live fs_uuid."""
    with _catalog(tmp_path) as con:
        proven_fp = capacity_evidence.identity_fingerprint_v1(
            fs_uuid="PROVEN-uuid", annex_uuid="PROVEN-annex", serial="PROVEN-serial",
            filesystem_capacity_bytes=1000)
        _proven_drive(con, "drive-00", fp=proven_fp, fscap=1000, free=900)
        assert hasattr(fetch, "_observe_drive") and hasattr(fetch, "_live_drive_evidence"), \
            "PR-03b must add fetch._observe_drive + fetch._live_drive_evidence"
        statvfs = types.SimpleNamespace(f_frsize=1, f_blocks=1000, f_bavail=900)
        with mock.patch.object(fetch.register, "archive_path", return_value=tmp_path), \
             mock.patch.object(fetch.register, "probe_fs_uuid", return_value="SWAPPED-uuid", create=True), \
             mock.patch.object(fetch.register, "probe_annex_uuid", return_value="PROVEN-annex", create=True), \
             mock.patch.object(fetch.register, "probe_serial", return_value="PROVEN-serial", create=True), \
             mock.patch.object(fetch.os, "statvfs", return_value=statvfs):
            try:
                with dm.drive_mutation(con, ["drive-00"], "op",
                                       observe=lambda label: fetch._observe_drive(con, label),
                                       reconcile=_reconciler(con), now="2026-01-01"):
                    raise AssertionError("mutation must refuse when live identity != proven identity")
            except dm.DriveMutationRefused as exc:
                assert exc.code == "DRIVE_IDENTITY_UNPROVEN", exc.code
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone()[0] == 0


# ===================================================================== seam 2: end-to-end fence FDs

def test_writer_exposes_actual_held_drive_fence_fds(tmp_path):
    """The mutation writer exposes the ACTUAL held drive-lock FDs (child_fence_fds), so children can
    inherit exactly what the parent holds."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP)
        held = {}
        with _capture_held_fence_fds(held):
            with dm.drive_mutation(con, ["drive-00"], "op", observe=_observer(con, 900, 400),
                                   reconcile=_reconciler(con), now="2026-01-01") as writer:
                assert hasattr(writer, "child_fence_fds"), \
                    "the mutation writer must expose child_fence_fds"
                assert tuple(writer.child_fence_fds) == held.get("fds"), \
                    "child_fence_fds must be the ACTUAL held drive-fence FDs"


def test_run_threads_writer_fence_fds_into_fetch_model_and_tail_sync(tmp_path):
    """run() passes the mutation writer (carrying the actual held FDs) into fetch_model, and the
    run()-tail `git annex sync` inherits those FDs."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP)
        (tmp_path / "lib").mkdir()
        held, seen_model, runs = {}, {}, []

        def fake_model(ctx, rid, dest, label, annex, cfg, *, mutation_writer=None, **kw):
            seen_model["writer"] = mutation_writer
            return {"repo_id": rid, "files": 1, "skipped": 0, "bytes": 1}

        def cap_run(cmd, *a, **k):
            runs.append((tuple(cmd), k.get("pass_fds")))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with _capture_held_fence_fds(held), \
             mock.patch.object(fetch, "_observe_drive", side_effect=_observe_from_rows(con), create=True), \
             mock.patch.object(fetch, "fetch_model", side_effect=fake_model), \
             mock.patch.object(fetch, "_is_annex", return_value=True), \
             mock.patch.object(fetch.register, "library_root", return_value=tmp_path / "lib"), \
             mock.patch.object(fetch.subprocess, "run", side_effect=cap_run), \
             mock.patch.object(fetch.wishlist, "compression", return_value={"threads": 1}):
            fetch.run(ctx=fetch.RunCtx(con=con), **_run_kwargs(dest=tmp_path))
        writer = seen_model.get("writer")
        assert writer is not None and hasattr(writer, "child_fence_fds"), \
            "run() must pass the mutation writer (with child_fence_fds) into fetch_model"
        assert tuple(writer.child_fence_fds) == held.get("fds"), \
            "fetch_model must receive the ACTUAL held drive-fence FDs"
        syncs = [(cmd, fds) for cmd, fds in runs if "annex" in cmd and "sync" in cmd]
        assert syncs and all(tuple(f or ()) == held.get("fds") for _c, f in syncs), \
            f"the run()-tail annex sync must inherit the held fence FDs; got {syncs}"
        map_sync = [cmd for cmd, _f in syncs if str(tmp_path / "lib") in cmd]
        assert map_sync and "drive-00" in map_sync[0], \
            f"the library-map sync must name only the current drive remote (drive-00); got {map_sync}"


def test_download_and_compression_forward_inherit_fds_to_run_monitored(tmp_path):
    """_download_shard and _compress_isolated forward their inherited fence FDs to the monitored-child
    spawner, so the download/compress children keep the fence."""
    for fn_name in ("_download_shard", "_compress_isolated"):
        assert "inherit_fds" in inspect.signature(getattr(fetch, fn_name)).parameters, \
            f"PR-03b must add `inherit_fds` to fetch.{fn_name}"
    fd = os.open(os.devnull, os.O_RDONLY)
    seen = {}

    def fake_monitored(cmd, progress, stall, should_stop, *, inherit_fds=()):
        if "download_worker" in " ".join(cmd):
            seen["download"] = tuple(inherit_fds)
            out = tmp_path / "dl.bin"
            out.write_bytes(b"x")
            Path(json.loads(cmd[-1])["result"]).write_text(json.dumps({"ok": True, "path": str(out)}))
            return {"outcome": "exited", "rc": 0, "stderr": ""}
        seen["compress"] = tuple(inherit_fds)
        return {"outcome": "stalled", "rc": None, "stderr": ""}   # bail compression cheaply

    local = tmp_path / "shard.bin"
    local.write_bytes(b"x" * 16)
    try:
        with mock.patch.object(fetch, "_run_monitored", side_effect=fake_monitored):
            fetch._download_shard(fetch.RunCtx(con=None), "repo", "f.bin", tmp_path, {}, inherit_fds=(fd,))
            fetch._compress_isolated(local, "bf16", "zstd", 1, "deadbeef", lambda: False, inherit_fds=(fd,))
    finally:
        os.close(fd)
    assert seen.get("download") == (fd,), f"download child must inherit the fence FD; got {seen.get('download')}"
    assert seen.get("compress") == (fd,), f"compress child must inherit the fence FD; got {seen.get('compress')}"


# ===================================================================== seam 3: reconciliation <-> transport

def test_fetch_model_forwards_writer_fds_and_records_touch_after_publish(tmp_path):
    """Seam 3/2 end-to-end through the REAL fetch_model (one file, minimally mocked): the writer's actual
    FDs reach its mutating children (download + annex-add), and the touch (path + annex key) is recorded
    only AFTER the physical publication and the durable archived row."""
    assert "mutation_writer" in inspect.signature(fetch.fetch_model).parameters, \
        "PR-03b must add `mutation_writer` to fetch_model (writer FDs + record_touched after publish)"
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        con.execute("INSERT INTO models(repo_id) VALUES('org/repo')")
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                    "VALUES('org/repo','config.json',2,'aux')")
        # a supported weight so the repo satisfies the acquisition policy (the end-of-fetch_model
        # canonical check reads the files table); the passed one-file manifest still fetches only config
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                    "VALUES('org/repo','model.safetensors',100,'safetensors')")
        fd = os.open(os.devnull, os.O_RDONLY)
        order, seen = [], {}

        class _Writer:
            child_fence_fds = (fd,)

            def record_touched(self, label, *, paths=(), keys=()):
                order.append("touch")
                seen["touch"] = (label, tuple(paths), tuple(keys))

        def fake_monitored(cmd, progress, stall, should_stop, *, inherit_fds=()):
            seen["download_fds"] = tuple(inherit_fds)
            req = json.loads(cmd[-1])
            out = Path(req["local_dir"]) / "config.json"
            out.write_text("{}")
            Path(req["result"]).write_text(json.dumps({"ok": True, "path": str(out)}))
            return {"outcome": "exited", "rc": 0, "stderr": ""}

        def fake_annex_add(dest, path, *, inherit_fds=()):
            seen["annex_add_fds"] = tuple(inherit_fds)
            order.append("annex_add")
            return "KEY-x"

        real_publish, real_upsert = fetch._publish_staged, fetch.db.upsert

        def wrapped_publish(*a, **k):
            result = real_publish(*a, **k)
            order.append("publish")
            return result

        def wrapped_upsert(c, table, row, **k):
            real_upsert(c, table, row, **k)
            if table == "archived":
                order.append("archived")

        manifest = [archive_manifest.ManifestFile("config.json", 2, None, "aux", None, "raw")]
        try:
            with mock.patch.object(fetch, "_run_monitored", side_effect=fake_monitored), \
                 mock.patch.object(fetch, "_annex_add", side_effect=fake_annex_add), \
                 mock.patch.object(fetch, "_annex_metadata"), \
                 mock.patch.object(fetch, "_publish_staged", side_effect=wrapped_publish), \
                 mock.patch.object(fetch.db, "upsert", side_effect=wrapped_upsert):
                fetch.fetch_model(fetch.RunCtx(con=con), "org/repo", tmp_path, "drive-00", True,
                                  {"threads": 1}, manifest=manifest, mutation_writer=_Writer())
        finally:
            os.close(fd)
        assert seen.get("download_fds") == (fd,), \
            f"the download child must inherit the writer's fence FDs; got {seen.get('download_fds')}"
        assert seen.get("annex_add_fds") == (fd,), \
            f"the annex-add child must inherit the writer's fence FDs; got {seen.get('annex_add_fds')}"
        _label, paths, keys = seen.get("touch", (None, (), ()))
        assert paths and "KEY-x" in keys, \
            f"fetch_model must record the published path + annex key on the writer; got {seen.get('touch')}"
        assert "publish" in order and "archived" in order and "touch" in order, order
        assert order.index("touch") > order.index("publish"), "touch must be recorded after physical publication"
        assert order.index("touch") > order.index("archived"), "touch must be recorded after the durable archived row"


def test_touched_reconciliation_fails_closed_on_missing_physical_path(tmp_path):
    """A recorded touched path with a durable row but NO bytes on disk fails closed — reconciliation
    validates physical presence, not just the catalog row."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")
        con.execute("INSERT INTO models(repo_id) VALUES('must')")
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format) "
                    "VALUES('must','model.safetensors',5,'safetensors')")
        con.execute("INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
                    "orig_bytes,stored_bytes,compressed) VALUES('must','model.safetensors',"
                    "'model.safetensors','model.safetensors','drive-00',5,5,0)")
        # durable row present, but the published bytes are absent at dest
        assert hasattr(fetch, "_reconcile_touched"), "PR-03b must add fetch._reconcile_touched"
        try:
            fetch._reconcile_touched(con, "drive-00", tmp_path, False, ["must/model.safetensors"], [])
            raise AssertionError("reconciliation must fail closed when the published bytes are absent")
        except dm.DriveMutationRefused as exc:
            assert exc.code == "DRIVE_RECONCILIATION_REQUIRED", exc.code


def test_run_returns_typed_terminal_when_clean_close_reconciliation_refuses(tmp_path):
    """A clean-close reconciliation refusal must be surfaced by run() as the typed
    DRIVE_RECONCILIATION_REQUIRED terminal, leaving the generation dirty with no anchor — not a leaked
    exception and not a generic failure."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00")

        def ok_model(ctx, rid, dest, label, annex, cfg, *, mutation_writer=None, **kw):
            return {"repo_id": rid, "files": 1, "skipped": 0, "bytes": 1}

        def refuse_reconcile(_con, label, dest, annex, paths, keys):
            raise dm.DriveMutationRefused("DRIVE_RECONCILIATION_REQUIRED", drive=label)

        with mock.patch.object(fetch, "_observe_drive", side_effect=_observe_from_rows(con), create=True), \
             mock.patch.object(fetch, "_reconcile_touched", side_effect=refuse_reconcile, create=True), \
             mock.patch.object(fetch, "fetch_model", side_effect=ok_model), \
             mock.patch.object(fetch, "_is_annex", return_value=False), \
             mock.patch.object(fetch, "_event"), \
             mock.patch.object(fetch.wishlist, "compression", return_value={"threads": 1}):
            try:
                result = fetch.run(ctx=fetch.RunCtx(con=con), **_run_kwargs(dest=tmp_path))
            except dm.DriveMutationRefused as exc:
                raise AssertionError(
                    "run() must convert a clean-close reconciliation refusal into a typed terminal, "
                    f"not leak it: {exc}") from exc
        terminal = result.get("terminal_failure") or {}
        assert terminal.get("code") == "DRIVE_RECONCILIATION_REQUIRED", \
            f"run() must surface the typed reconciliation terminal; got {terminal}"
        dirty = con.execute("SELECT count(*) FROM drive_dirty_generations "
                            "WHERE drive_label='drive-00'").fetchone()[0]
        anchors = con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone()[0]
        assert (dirty, anchors) == (1, 0), \
            f"a reconciliation refusal must leave the generation dirty with no anchor; dirty={dirty} anchors={anchors}"


# ===================================================================== seam 4: writability-probe ordering

def test_replica_writability_probe_runs_after_both_generations_committed(tmp_path):
    """The replica's MUTATING writability probe runs only after BOTH source+target dirty generations
    are committed (presence is checked non-mutatingly before the fence; the write probe is fenced)."""
    with _catalog(tmp_path) as con:
        task, archive_path, library = _replica_setup(con, tmp_path)
        probe = sqlite3.connect(str(db.DB_PATH))
        seen = []

        def dest_writable(_p):
            seen.append(probe.execute(
                "SELECT count(*) FROM drive_dirty_generations "
                "WHERE drive_label IN ('drive-00','drive-04')").fetchone()[0])
            return True

        try:
            with mock.patch.object(fetch, "_observe_drive", side_effect=_observe_from_rows(con), create=True), \
                 mock.patch.object(fetch.register, "archive_path", side_effect=archive_path), \
                 mock.patch.object(fetch.register, "library_root", return_value=library), \
                 mock.patch.object(fetch, "_dest_writable", side_effect=dest_writable), \
                 mock.patch.object(fetch, "_annex_key_on_uuid", return_value=True), \
                 mock.patch.object(fetch.subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, "", "")):
                fetch.run_replica_tasks([task], ctx=fetch.RunCtx(con=con))
        finally:
            probe.close()
        assert seen, "replica must run a mutating writability probe"
        assert all(n == 2 for n in seen), \
            f"the mutating _dest_writable probe must run only after BOTH dirty generations commit; saw {seen}"


def test_run_error_path_writability_probe_runs_inside_the_batch_envelope(tmp_path):
    """run()'s error-path drive-writability probe runs inside the batch envelope (dirty generation
    already committed), not before it."""
    with _catalog(tmp_path) as con:
        _proven_drive(con, "drive-00", fp=_FP)
        probe = sqlite3.connect(str(db.DB_PATH))
        seen = []

        def dest_writable(_p):
            seen.append(probe.execute("SELECT count(*) FROM drive_dirty_generations "
                                      "WHERE drive_label='drive-00'").fetchone()[0])
            return True

        def boom(ctx, rid, dest, label, annex, cfg, **kw):
            raise RuntimeError("write failed mid-batch")

        try:
            with mock.patch.object(fetch, "_observe_drive", side_effect=_observe_from_rows(con), create=True), \
                 mock.patch.object(fetch, "fetch_model", side_effect=boom), \
                 mock.patch.object(fetch, "_is_annex", return_value=False), \
                 mock.patch.object(fetch, "_dest_writable", side_effect=dest_writable), \
                 mock.patch.object(fetch, "_event"), \
                 mock.patch.object(fetch.wishlist, "compression", return_value={"threads": 1}):
                fetch.run(ctx=fetch.RunCtx(con=con), **_run_kwargs(dest=tmp_path))
        finally:
            probe.close()
        assert seen, "run() error path must probe drive writability"
        assert all(n == 1 for n in seen), \
            f"the error-path _dest_writable probe must run inside the batch envelope; saw {seen}"


def main():
    import tempfile
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        db_globals = (db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR)
        tmp = None
        try:
            if "tmp_path" in inspect.signature(fn).parameters:
                tmp = Path(tempfile.mkdtemp(prefix="mark-03b-"))
                fn(tmp)
            else:
                fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001 — Gate-1 wants the full red/green map
            failed.append(name)
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
        finally:
            db.CATALOG_DIR, db.DB_PATH, db.STATE_DIR = db_globals
            if tmp is not None:                  # the standalone runner cleans its own temp trees
                shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
