"""Execution and independent child holders share every physical alias."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import _pr09_gate1_fixtures as f

from modelark import drive_fence, execution_recovery as recovery
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.drive_identity import FenceIdentity
from modelark.execution_service import production_services
from modelark.proposal import Refusal


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute("CREATE TABLE drives(drive_label TEXT,fs_uuid TEXT,annex_uuid TEXT,"
                "serial TEXT,filesystem_capacity_bytes INTEGER,identity_epoch INTEGER,"
                "identity_fingerprint TEXT)")
    yield con
    for sid in list(recovery._CHILD_FENCE_HANDLES):
        recovery.release_child_fences(sid)
    con.close()


def seed(con, label="d0", *, legacy=False, fs="fs"):
    fingerprint = identity_fingerprint_v1(
        fs_uuid=fs, annex_uuid="annex", serial=None if legacy else "SERIAL",
        filesystem_capacity_bytes=1000)
    identity = FenceIdentity(fs, "annex", "SERIAL", 1000, 1, fingerprint)
    con.execute("INSERT INTO drives VALUES(?,?,?,?,?,?,?)",
                (label, fs, "annex", "SERIAL", 1000, 1, fingerprint))
    return identity


def assert_held(keys):
    for key in keys:
        with pytest.raises(drive_fence.FenceUnavailable):
            with drive_fence.hold_drives_sorted([key], blocking=False):
                pytest.fail("every compatible key must remain held")


@pytest.mark.parametrize("legacy", [True, False])
def test_production_service_holds_both_identities_and_deduplicates(catalog, legacy):
    identity = seed(catalog, "z", legacy=legacy)
    seed(catalog, "a", legacy=not legacy)
    service = production_services(catalog)
    with service.drive_fences.hold_all_sorted(["z", "a", "z"]) as handles:
        assert len(handles) == 2
        assert_held(identity.lock_keys())


@pytest.mark.parametrize("child", [True, False])
@pytest.mark.parametrize("bad", ["missing", "fingerprint", "uuid", "no_connection"])
def test_unproven_identity_never_falls_back_to_label(catalog, child, bad):
    seed(catalog)
    if bad == "missing":
        catalog.execute("DELETE FROM drives")
    elif bad == "fingerprint":
        catalog.execute("UPDATE drives SET identity_fingerprint='invented'")
    elif bad == "uuid":
        catalog.execute("UPDATE drives SET fs_uuid=NULL,annex_uuid=NULL")
    con = None if bad == "no_connection" else catalog
    with pytest.raises(Refusal, match="DRIVE_IDENTITY_UNPROVEN"):
        if child:
            recovery.inherit_drive_fence_fds(session_id="unproven", drive_labels=["d0"], con=con)
        else:
            with production_services(con).drive_fences.hold_all_sorted(["d0"]):
                pytest.fail("unproven physical lock")
    assert not recovery._CHILD_FENCE_HANDLES


def test_child_keys_globally_sorted_and_deduped_with_marker(catalog):
    a = seed(catalog, "z", fs="a")
    b = seed(catalog, "a", fs="z")
    fds = recovery.inherit_drive_fence_fds(
        session_id="ordered", drive_labels=["z", "a", "z"], con=catalog)
    keys = sorted(set(a.lock_keys() + b.lock_keys()))
    assert len(fds) == len(keys) + 1
    assert recovery._CHILD_FENCE_META["ordered"][1:] == [
        str(drive_fence.drive_lock_path(*key)) for key in keys]
    assert_held(keys)
    recovery.release_child_fences("ordered")
    assert not recovery.child_fence_still_held(session_id="ordered")
    assert not (drive_fence._LOCK_DIR / "session-child-ordered.json").exists()
    assert (drive_fence._LOCK_DIR / "session-child-ordered.lock").exists()
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_partial_child_acquisition_closes_failed_fd_and_all_prior_handles(catalog, monkeypatch):
    keys = seed(catalog).lock_keys()
    opened = []
    real_open = open

    def track_open(*args, **kwargs):
        handle = real_open(*args, **kwargs)
        opened.append(handle)
        return handle

    with drive_fence.hold_drives_sorted([keys[-1]], blocking=False):
        monkeypatch.setattr(recovery, "open", track_open, raising=False)
        with pytest.raises(Refusal, match="CHILD_FENCE_HELD"):
            recovery.inherit_drive_fence_fds(session_id="partial", drive_labels=["d0"], con=catalog)
        assert len(opened) == 3
        assert all(h.closed for h in opened)
        assert "partial" not in recovery._CHILD_FENCE_HANDLES
        assert "partial" not in recovery._CHILD_FENCE_META
        assert not recovery.child_fence_still_held(session_id="partial")
        with drive_fence.hold_drives_sorted([keys[0]], blocking=False):
            pass


def test_metadata_write_failure_releases_all_child_handles(catalog, monkeypatch):
    keys = seed(catalog).lock_keys()

    def fail_write(*args, **kwargs):
        raise OSError("metadata unavailable")

    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError, match="metadata unavailable"):
        recovery.inherit_drive_fence_fds(session_id="meta", drive_labels=["d0"], con=catalog)
    assert not recovery.child_fence_still_held(session_id="meta")
    assert "meta" not in recovery._CHILD_FENCE_META
    with drive_fence.hold_drives_sorted(keys, blocking=False):
        pass


def test_failed_marker_acquisition_preserves_other_holders_metadata(catalog):
    seed(catalog)
    marker = drive_fence._LOCK_DIR / "session-child-other.lock"
    handle = drive_fence._acquire(marker, blocking=False)
    meta = drive_fence._LOCK_DIR / "session-child-other.json"
    meta.write_text("other holder metadata")
    try:
        with pytest.raises(Refusal, match="CHILD_FENCE_HELD"):
            recovery.inherit_drive_fence_fds(session_id="other", drive_labels=["d0"], con=catalog)
        recovery.release_child_fences("other")
        assert meta.read_text() == "other holder metadata"
        assert recovery.child_fence_still_held(session_id="other")
    finally:
        handle.close()


def test_production_lock_recaptures_facts_after_wait(catalog, monkeypatch):
    seed(catalog)
    original = drive_fence.hold_drives_sorted

    @contextmanager
    def changed(*args, **kwargs):
        with original(*args, **kwargs) as handles:
            catalog.execute("UPDATE drives SET identity_epoch=2")
            yield handles

    monkeypatch.setattr(drive_fence, "hold_drives_sorted", changed)
    with pytest.raises(Refusal, match="DRIVE_IDENTITY_CHANGED"):
        with production_services(catalog).drive_fences.hold_all_sorted(["d0"]):
            pytest.fail("captured identity changed while waiting")


def test_child_lock_recaptures_facts_after_acquisition(catalog, monkeypatch):
    seed(catalog)
    original = fcntl.flock
    count = 0

    def changed(handle, flags):
        nonlocal count
        result = original(handle, flags)
        count += 1
        if count == 3:
            catalog.execute("UPDATE drives SET identity_epoch=2")
        return result

    monkeypatch.setattr(fcntl, "flock", changed)
    with pytest.raises(Refusal, match="DRIVE_IDENTITY_CHANGED"):
        recovery.inherit_drive_fence_fds(session_id="race", drive_labels=["d0"], con=catalog)
    assert "race" not in recovery._CHILD_FENCE_HANDLES


def test_surviving_child_keeps_all_aliases_and_marker_after_parent_release(catalog):
    keys = seed(catalog).lock_keys()
    fds = recovery.inherit_drive_fence_fds(
        session_id="survivor", drive_labels=["d0"], con=catalog)
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
        pass_fds=fds, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        recovery.release_child_fences("survivor")
        assert recovery.child_fence_still_held(session_id="survivor")
        assert_held(keys)
        with pytest.raises(Refusal, match="CHILD_FENCE_HELD"):
            recovery.inherit_drive_fence_fds(
                session_id="survivor", drive_labels=["d0"], con=catalog)
        assert_held(keys)
    finally:
        child.stdin.close()
        child.wait()
        child.stdout.close()
    assert not recovery.child_fence_still_held(session_id="survivor")
    with drive_fence.hold_drives_sorted(keys, blocking=False):
        pass


def test_reacquisition_never_drops_existing_child_hold(catalog):
    keys = seed(catalog).lock_keys()
    recovery.inherit_drive_fence_fds(session_id="same", drive_labels=["d0"], con=catalog)
    with pytest.raises(Refusal, match="CHILD_FENCE_HELD"):
        recovery.inherit_drive_fence_fds(session_id="same", drive_labels=["d0"], con=catalog)
    assert_held(keys)


def test_catalog_free_marker_requires_explicit_empty_fixture_opt_in(catalog):
    with pytest.raises(Refusal, match="DRIVE_IDENTITY_UNPROVEN"):
        recovery.inherit_drive_fence_fds(session_id="fixture", drive_labels=[])
    assert not drive_fence._LOCK_DIR.exists()
    fds = recovery.inherit_drive_fence_fds(
        session_id="fixture", drive_labels=[], marker_only=True)
    assert len(fds) == 1
    assert recovery.child_fence_still_held(session_id="fixture")
    recovery.release_child_fences("fixture")
    assert not recovery.child_fence_still_held(session_id="fixture")


def test_marker_only_cannot_override_nonempty_drive_authority(catalog):
    seed(catalog)
    with pytest.raises(Refusal, match="DRIVE_IDENTITY_UNPROVEN"):
        recovery.inherit_drive_fence_fds(
            session_id="fixture", drive_labels=["d0"], con=catalog, marker_only=True)
    assert not drive_fence._LOCK_DIR.exists()


def test_explicit_catalog_empty_selection_remains_a_zero_drive_hold(catalog):
    fds = recovery.inherit_drive_fence_fds(session_id="empty", drive_labels=[], con=catalog)
    assert len(fds) == 1
    recovery.release_child_fences("empty")
    assert not recovery.child_fence_still_held(session_id="empty")


@pytest.fixture
def ready_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    con = f.mem_con()
    f.seed_plan_selection(con, repos=("org/a",))
    # Authentic legacy-null fingerprints remain valid, but the compatible
    # canonical alias is sensitive to this saved physical serial.
    con.execute("UPDATE drives SET serial='before'")
    _, pid, _ = f.create_and_approve(con)
    services = f.default_services()
    physical = production_services(con, catalog_path=tmp_path / "catalog.sqlite")
    services.controller_flock = physical.controller_flock
    services.drive_fences = physical.drive_fences
    yield con, pid, services
    con.close()


@pytest.mark.parametrize("field", ["serial", "fs_uuid", "annex_uuid"])
@pytest.mark.parametrize("resume", [False, True])
def test_start_resume_rechecks_physical_facts_inside_transaction(ready_execution, field, resume):
    from modelark import execution_session
    con, pid, services = ready_execution
    predecessor = None
    if resume:
        predecessor = "paused"
        con.execute(
            "INSERT INTO execution_sessions("
            "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
            "state,bound_planner_revision,fencing_token) "
            "VALUES('paused','ark',?,'controller','worker','paused',0,1)", [pid])
    original = services.config.read_graph_affecting_config

    def racing_config():
        con.execute(f"UPDATE drives SET {field}='after' WHERE drive_label='d0'")
        return original()

    services.config.read_graph_affecting_config = racing_config
    before_sessions = con.execute("SELECT * FROM execution_sessions").fetchall()
    before_token = con.execute("SELECT next_fencing_token FROM planner_state").fetchone()
    with pytest.raises(Refusal, match="DRIVE_IDENTITY_(CHANGED|UNPROVEN)"):
        execution_session.start_session(con, pid, predecessor, services)
    assert con.execute("SELECT * FROM execution_sessions").fetchall() == before_sessions
    assert con.execute("SELECT next_fencing_token FROM planner_state").fetchone() == before_token
    assert not con.in_transaction


@pytest.mark.parametrize("field", ["serial", "fs_uuid", "annex_uuid"])
def test_recovery_rechecks_physical_facts_inside_transaction(ready_execution, field):
    con, pid, services = ready_execution
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('expired','ark',?,'controller','worker','running',0,1,"
        "'2000-01-01T00:00:00Z')", [pid])
    original = services.drive_fences.hold_all_sorted

    @contextmanager
    def racing_fences(labels):
        with original(labels) as binding:
            con.execute(f"UPDATE drives SET {field}='after' WHERE drive_label='d0'")
            yield binding

    services.drive_fences.hold_all_sorted = racing_fences
    before = con.execute("SELECT * FROM execution_sessions").fetchall()
    with pytest.raises(Refusal, match="DRIVE_IDENTITY_(CHANGED|UNPROVEN)"):
        recovery.recover_expired_session(con, session_id="expired", services=services)
    assert con.execute("SELECT * FROM execution_sessions").fetchall() == before
    assert not con.in_transaction


def test_recovery_rechecks_facts_after_in_transaction_clock_callback(ready_execution):
    con, pid, services = ready_execution
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,worker_identity,"
        "state,bound_planner_revision,fencing_token,expires_at) "
        "VALUES('expired','ark',?,'controller','worker','running',0,1,"
        "'2000-01-01T00:00:00Z')", [pid])
    original = services.clock.now
    seen = []

    def racing_clock():
        if con.in_transaction:
            seen.append(True)
            con.execute("UPDATE drives SET serial='after' WHERE drive_label='d0'")
        return original()

    services.clock.now = racing_clock
    before = tuple(con.iterdump())
    with pytest.raises(Refusal, match="DRIVE_IDENTITY_CHANGED"):
        recovery.recover_expired_session(con, session_id="expired", services=services)
    assert seen == [True]
    assert tuple(con.iterdump()) == before
    assert not con.in_transaction
