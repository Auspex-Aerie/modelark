"""Expected-red Slice 2 contracts. Only disposable destinations and synthetic sources."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import importlib
import io
import multiprocessing
import os
from pathlib import PurePosixPath

import pytest

from modelark.slice import domain as d
from test_slice_domain import facts, spec


DATA = b"slice bytes!"


@pytest.fixture
def api(tmp_path, monkeypatch):
    try:
        t = importlib.import_module("modelark.slice.transaction")
        s = importlib.import_module("modelark.slice.state")
    except ModuleNotFoundError as exc:
        if not exc.name.startswith("modelark.slice"):
            raise
        pytest.fail("Slice 2 transaction API is not implemented yet")
    monkeypatch.setattr(s, "HOST_STATE_DIR", tmp_path / "host-state")
    return t, s


def proposal():
    snapshot = facts(d)
    sha = hashlib.sha256(DATA).hexdigest()
    snapshot = replace(snapshot,
                       files=(replace(snapshot.files[0], sha256=sha),),
                       copies=(replace(snapshot.copies[0], orig_sha256=sha,
                                       annex_key=f"SHA256E-s12--{sha}"),))
    p = d.preview(replace(spec(d), destination_id="test-device"), snapshot)
    return p, d.approve(p, expected_seal=p.seal, current_snapshot=snapshot), snapshot


class Destination:
    """Test-only port: real files, simulated device identity and owned-object certificates.

    Production discovery, descriptor confinement and certificates are Slice 3's adapter work.
    This port never opens any path outside its pytest directory.
    """
    def __init__(self, root, t):
        self.root = root
        root.mkdir()
        self.t = t
        self.binding = t.DestinationBinding("test-device", "test-filesystem", "test-mount", 10000)
        self.trace = []
        self.available = True
        self.changed = False

    def check(self, binding, allocated):
        if not self.available:
            raise self.t.TransferRefusal("WAITING_DESTINATION")
        if self.changed or self.binding != binding:
            raise self.t.TransferRefusal("DESTINATION_CHANGED")
        self.trace.append(("check", allocated))

    def inspect(self, path):
        p = self.root / path
        if not p.exists():
            return None
        if p.is_symlink():
            raise self.t.TransferRefusal("OUTPUT_COLLISION")
        try:
            token = os.getxattr(p, "user.slice-test-owner").decode()
        except OSError:
            token = None
        return self.t.ObjectInfo("directory" if p.is_dir() else "file", token,
                                 0 if p.is_dir() else p.stat().st_size)

    def create_directory(self, path, token):
        (self.root / path).mkdir()
        os.setxattr(self.root / path, "user.slice-test-owner", token.encode())
        self.trace.append(("mkdir", path))

    def create_file(self, path, token):
        with (self.root / path).open("xb"):
            pass
        os.setxattr(self.root / path, "user.slice-test-owner", token.encode())

    def append(self, path, token, data):
        assert self.inspect(path).token == token
        with (self.root / path).open("ab") as out:
            out.write(data)

    def discard_temporary(self, path, token):
        assert self.inspect(path).token == token
        (self.root / path).unlink()

    def read(self, path):
        return (self.root / path).open("rb")

    def flush(self, path):
        self.trace.append(("flush", path))
        fd = os.open(self.root / path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def publish(self, temporary, path, token):
        assert self.inspect(temporary).token == token
        os.link(self.root / temporary, self.root / path)  # Atomic no-replace.
        self.trace.append(("publish", path))

    def list_paths(self, root):
        p = self.root / root
        return tuple(str(v.relative_to(self.root)) for v in p.rglob("*")) if p.exists() else ()


class Sources:
    def __init__(self, snapshot, t):
        self.snapshot, self.t = snapshot, t
        self.status = {}
        self.data = DATA
        self.reads = []
        self.fenced = False

    @contextmanager
    def open(self, source):
        label = source.drive.drive_label
        if label in self.status:
            raise self.t.TransferRefusal(self.status[label], label)
        self.fenced = True
        self.reads.append(label)
        try:
            yield self.snapshot, io.BytesIO(self.data)
        finally:
            self.fenced = False


@pytest.fixture
def setup(api, tmp_path):
    t, s = api
    p, approval, snapshot = proposal()
    dest = Destination(tmp_path / "destination", t)
    sources = Sources(snapshot, t)
    store = s.Store()
    plan = t.TransferPlan(p, dest.binding)
    tx = store.create(plan, approval)
    return t, store, plan, tx, dest, sources


def start(setup, fault=None):
    t, store, plan, tx, dest, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    return t.start(store, tx, dest, sources, fault=fault)


def test_approval_is_durable_and_does_not_start_or_touch_destination(setup, api):
    t, store, plan, tx, dest, sources = setup
    with pytest.raises(t.TransferRefusal, match="APPROVAL_MISSING"):
        t.start(store, tx, dest, sources)
    with pytest.raises(t.TransferRefusal, match="PREVIEW_STALE"):
        store.approve(tx, expected_seal="wrong")
    store.approve(tx, expected_seal=plan.seal)
    assert api[1].Store().status(tx).state == "approved"
    assert not list(dest.root.iterdir()) and not sources.reads


@pytest.mark.parametrize("missing_serial", [True, False])
def test_private_v1_upgrade_preserves_transaction_authority(setup, api, missing_serial):
    _, store, plan, tx, dest, _ = setup
    store.approve(tx, expected_seal=plan.seal)
    store.reserve(tx, plan.destination.device_id)
    store.request_stop(tx)
    store.append(tx, "test-marker", {"preserved": True})
    before = store.events(tx)
    with store._connection() as con:
        if missing_serial:
            con.execute("ALTER TABLE transactions DROP COLUMN stop_serial")
        con.execute("PRAGMA user_version=1")
    upgraded = api[1].Store()
    assert upgraded.load(tx) == plan
    assert upgraded.events(tx) == before
    assert upgraded.owner(plan.destination.device_id) == tx
    assert upgraded.stop_requested(tx)
    serial = upgraded.reserve(tx, plan.destination.device_id)
    upgraded.request_stop(tx)
    upgraded.activate(tx, plan.destination.device_id, serial)
    assert upgraded.stop_requested(tx)  # A newer stop still wins after migration.
    with upgraded._connection(write=False) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 2
    assert api[1].Store().events(tx) == before  # Reopening is idempotent.
    assert not list(dest.root.iterdir())


def test_run_records_actual_source_and_verified_receipt_without_catalog_mutation(setup):
    t, store, plan, tx, dest, sources = setup
    original = sources.snapshot
    with start(setup) as session:
        assert session.run().state == "complete"
    assert (dest.root / "models/org/model/model.safetensors").read_bytes() == DATA
    receipt = store.receipt(tx)
    assert receipt["seal"] == plan.seal
    assert receipt["files"][0]["source"]["drive"]["drive_label"] == "drive-a"
    assert sources.snapshot == original
    assert store.status(tx).state == "complete"


def test_duplicate_start_is_observation_not_second_writer_and_stop_reserves_device(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup) as session:
        duplicate = t.start(store, tx, dest, sources)
        assert not duplicate.can_write and duplicate.transaction_id == tx
        store.request_stop(tx)
        assert session.run().state == "stopped"
    _, approval, _ = proposal()
    another = store.create(plan, approval)
    store.approve(another, expected_seal=plan.seal)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_BUSY"):
        t.start(store, another, dest, sources)


@pytest.mark.parametrize("code,state", [("WAITING_SOURCE", "waiting_source"),
                                       ("SOURCE_BUSY", "blocked_source"),
                                       ("SOURCE_MISSING", "blocked_source")])
def test_source_waits_preserve_approval_and_can_resume(setup, code, state):
    t, store, plan, tx, dest, sources = setup
    sources.status["drive-a"] = code
    with start(setup) as session:
        assert session.run().state == state
    sources.status.clear()
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "complete"
    assert store.receipt(tx)["seal"] == plan.seal
    assert not list(dest.root.rglob(".slice-*"))


@pytest.mark.parametrize("change", ["lifecycle", "generation", "copy", "digest"])
def test_source_evidence_is_rechecked_under_read_fence(setup, change):
    t, store, plan, tx, dest, sources = setup
    snapshot = sources.snapshot
    if change == "lifecycle":
        snapshot = replace(snapshot, drives=(replace(snapshot.drives[0], lifecycle="lost"),))
    elif change == "generation":
        snapshot = replace(snapshot, drives=(replace(snapshot.drives[0], write_generation=3),))
    elif change == "copy":
        snapshot = replace(snapshot, copies=())
    else:
        sources.data = b"wrong bytes!"
    sources.snapshot = snapshot
    with start(setup) as session:
        result = session.run()
    assert result.state == "blocked_source"
    assert store.receipt(tx) is None
    assert not (dest.root / "models/org/model/model.safetensors").exists()


@pytest.mark.parametrize("boundary", [
    "reserved", "control_intent", "control_created", "control_flushed", "control_complete",
    "directory_intent", "directory_created", "directory_flushed", "directory_parent_flushed",
    "directory_complete", "file_intent", "temporary_created", "chunk_written", "file_flushed",
    "file_prepared", "file_published", "file_parent_flushed", "file_complete",
    "receipt_intent", "receipt_created", "receipt_flushed", "receipt_prepared",
    "receipt_published", "receipt_parent_flushed", "receipt_complete",
])
def test_crash_boundaries_resume_without_unverified_receipts(setup, boundary):
    t, store, plan, tx, dest, sources = setup
    class Crash(BaseException):
        pass
    fired = False
    def fault(point):
        nonlocal fired
        if point == boundary and not fired:
            fired = True
            raise Crash(point)
    with pytest.raises(Crash):
        with start(setup, fault) as session:
            session.run()
    assert fired
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "complete"
    assert (dest.root / "models/org/model/model.safetensors").read_bytes() == DATA
    assert store.receipt(tx)["seal"] == plan.seal
    assert not list(dest.root.rglob(".slice-*"))


def test_unknown_content_is_not_adopted_even_with_matching_hash(setup):
    t, store, plan, tx, dest, sources = setup
    (dest.root / "models").mkdir()
    with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
        with start(setup) as session:
            session.run()
    assert not sources.reads


def test_destination_change_blocks_before_any_bytes(setup):
    t, store, plan, tx, dest, sources = setup
    dest.changed = True
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CHANGED"):
        start(setup)
    assert not list(dest.root.iterdir())


def test_missing_completed_file_is_restored_but_not_from_unavailable_source(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup) as session:
        assert session.step().state == "transferring"
    (dest.root / "models/org/model/model.safetensors").unlink()
    sources.status["drive-a"] = "WAITING_SOURCE"
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "waiting_source"
    sources.status.clear()
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "complete"


def _contend(store, tx, dest, sources, conn):
    t = importlib.import_module("modelark.slice.transaction")
    try:
        result = t.start(store, tx, dest, sources)
        conn.send((result.can_write, result.transaction_id))
        if result.can_write:
            result.close()
    finally:
        conn.close()


def test_second_process_observes_same_active_execution(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup):
        parent, child = multiprocessing.Pipe()
        process = multiprocessing.get_context("fork").Process(target=_contend,
                                                               args=(store, tx, dest, sources, child))
        process.start()
        child.close()
        assert parent.recv() == (False, tx)
        process.join()
        assert process.exitcode == 0
        parent.close()


def _die_at_boundary(store, tx, dest, sources, boundary):
    t = importlib.import_module("modelark.slice.transaction")
    def fault(point):
        if point == boundary:
            os._exit(73)
    with t.start(store, tx, dest, sources, fault=fault) as session:
        session.run()


@pytest.mark.parametrize("boundary", ["reserved", "directory_created", "chunk_written",
                                      "file_prepared", "file_published", "receipt_published"])
def test_process_death_releases_only_process_fence_and_recovers_owned_bytes(setup, boundary):
    t, store, plan, tx, dest, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    process = multiprocessing.get_context("fork").Process(target=_die_at_boundary,
                                                         args=(store, tx, dest, sources, boundary))
    process.start()
    process.join()
    assert process.exitcode == 73
    assert store.owner(plan.destination.device_id) == tx
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "complete"


def _retain_inherited_fence(conn):
    conn.send("held")
    conn.recv()
    conn.close()


def test_surviving_child_prevents_resume_after_parent_closes_session(setup):
    t, store, plan, tx, dest, sources = setup
    session = start(setup)
    parent, child = multiprocessing.Pipe()
    process = multiprocessing.get_context("fork").Process(target=_retain_inherited_fence, args=(child,))
    process.start()
    child.close()
    assert parent.recv() == "held"
    session.close()
    try:
        assert not t.start(store, tx, dest, sources).can_write
    finally:
        parent.send("exit")
        process.join()
        parent.close()
    assert process.exitcode == 0
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "complete"


def test_unprepared_matching_temporary_requires_fresh_source(setup):
    t, store, plan, tx, dest, sources = setup
    class Crash(BaseException):
        pass
    def fault(point):
        if point == "file_flushed":
            raise Crash()
    with pytest.raises(Crash):
        with start(setup, fault) as session:
            session.run()
    sources.status["drive-a"] = "WAITING_SOURCE"
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "waiting_source"
    assert not (dest.root / "models/org/model/model.safetensors").exists()


def test_prepared_source_history_survives_later_source_lifecycle_change(setup):
    t, store, plan, tx, dest, sources = setup
    class Crash(BaseException):
        pass
    def fault(point):
        if point == "file_prepared":
            raise Crash()
    with pytest.raises(Crash):
        with start(setup, fault) as session:
            session.run()
    sources.snapshot = replace(sources.snapshot, drives=(replace(sources.snapshot.drives[0], lifecycle="lost"),))
    sources.status["drive-a"] = "SOURCE_CHANGED"
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "complete"
    assert store.receipt(tx)["files"][0]["source"]["drive"]["lifecycle"] == "active"


def test_destination_is_rechecked_before_publication(setup):
    t, store, plan, tx, dest, sources = setup
    def fault(point):
        if point == "file_prepared":
            dest.changed = True
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CHANGED"):
        with start(setup, fault) as session:
            session.run()
    assert not (dest.root / "models/org/model/model.safetensors").exists()
    assert store.receipt(tx) is None


def test_directory_parent_flush_precedes_child_creation(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup) as session:
        session.run()
    for path in ["models", "models/org", "models/org/model"]:
        idx = dest.trace.index(("mkdir", path))
        flush = dest.trace.index(("flush", str(PurePosixPath(path).parent)), idx)
        child = next((i for i, (kind, p) in enumerate(dest.trace) if kind == "mkdir" and p.startswith(path + "/")), None)
        assert flush > idx and (child is None or child > flush)


def test_journal_corruption_fails_closed(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup):
        pass
    with store._connection() as con:
        con.execute("UPDATE journal SET digest='broken' WHERE tx=?", (tx,))
    with pytest.raises(t.TransferRefusal, match="JOURNAL_CORRUPT"):
        t.start(store, tx, dest, sources)


def test_resume_rejects_a_replaced_owned_object_even_if_its_bytes_match(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup) as session:
        session.step()
    path = dest.root / "models/org/model/model.safetensors"
    path.unlink()
    path.write_bytes(DATA)
    with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
        t.start(store, tx, dest, sources)


def test_restarted_file_cannot_reuse_old_prepared_source_proof(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup) as session:
        session.step()
    (dest.root / "models/org/model/model.safetensors").unlink()
    class Crash(BaseException):
        pass
    def fault(point):
        if point == "file_flushed":
            raise Crash()
    with pytest.raises(Crash):
        with t.start(store, tx, dest, sources, fault=fault) as session:
            session.run()
    sources.status["drive-a"] = "WAITING_SOURCE"
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "waiting_source"
    assert store.receipt(tx) is None


def test_only_sealed_alternatives_may_be_used_and_actual_choice_is_retained(setup):
    t, store, plan, tx, dest, sources = setup
    snapshot = sources.snapshot
    drive = replace(snapshot.drives[0], drive_label="drive-b", fs_uuid="fs-b", annex_uuid="annex-b")
    fp = d.identity_fingerprint_v1(fs_uuid=drive.fs_uuid, annex_uuid=drive.annex_uuid,
                                   serial=drive.serial, filesystem_capacity_bytes=drive.filesystem_capacity_bytes)
    drive = replace(drive, identity_fingerprint=fp)
    snapshot = replace(snapshot, drives=(*snapshot.drives, drive),
                       copies=(*snapshot.copies, replace(snapshot.copies[0], drive_label="drive-b")),
                       anchors=(*snapshot.anchors, replace(snapshot.anchors[0], drive_label="drive-b",
                                                            identity_fingerprint=fp)))
    p = d.preview(plan.proposal.spec, snapshot)
    plan2 = t.TransferPlan(p, dest.binding)
    tx2 = store.create(plan2, d.approve(p, expected_seal=p.seal, current_snapshot=snapshot))
    sources.snapshot = snapshot
    sources.status["drive-a"] = "WAITING_SOURCE"
    # A newly discovered copy is not a substitute for the sealed candidate set.
    with start(setup) as session:
        assert session.run().state == "waiting_source"
    assert sources.reads == []
    # Use a separate fixture destination/host namespace for the separately approved alternative
    # plan: unfinished ownership of the first transaction must never be adopted.
    with pytest.raises(t.TransferRefusal, match="DESTINATION_BUSY"):
        store.approve(tx2, expected_seal=plan2.seal)
        t.start(store, tx2, dest, sources)


def test_sealed_fallback_records_the_source_actually_read(setup):
    t, store, plan, tx, dest, sources = setup
    snapshot = sources.snapshot
    b = replace(snapshot.drives[0], drive_label="drive-b")
    snapshot = replace(snapshot, drives=(*snapshot.drives, b),
                       copies=(*snapshot.copies, replace(snapshot.copies[0], drive_label="drive-b")),
                       anchors=(*snapshot.anchors, replace(snapshot.anchors[0], drive_label="drive-b")))
    p = d.preview(plan.proposal.spec, snapshot)
    plan = t.TransferPlan(p, dest.binding)
    tx = store.create(plan, d.approve(p, expected_seal=p.seal, current_snapshot=snapshot))
    sources.snapshot = snapshot
    sources.status["drive-a"] = "SOURCE_BUSY"
    store.approve(tx, expected_seal=plan.seal)
    with t.start(store, tx, dest, sources) as session:
        session.run()
    assert store.receipt(tx)["files"][0]["source"]["drive"]["drive_label"] == "drive-b"


def test_fenced_sources_use_real_archive_fence_and_read_only_snapshot(api, tmp_path, monkeypatch):
    from modelark import drive_fence
    from modelark.slice.sources import FencedSources
    from test_slice_catalog import seed
    t, _ = api
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")
    path = tmp_path / "catalog.sqlite"
    con = seed(path, d)
    before = tuple(con.iterdump())
    from modelark.slice.catalog import read_catalog
    snapshot = read_catalog(path, spec(d))
    candidate = d.preview(spec(d), snapshot).closure[0].sources[0]
    key = (candidate.drive.identity_fingerprint, candidate.drive.identity_epoch)
    class Reader:
        @contextmanager
        def open(self, source):
            with pytest.raises(drive_fence.FenceUnavailable):
                with drive_fence.hold_drives_sorted([key], blocking=False):
                    pass
            yield io.BytesIO(DATA)
    sources = FencedSources(path, Reader())
    with drive_fence.hold_drives_sorted([key], blocking=False):
        with pytest.raises(t.TransferRefusal, match="SOURCE_BUSY"):
            with sources.open(candidate):
                pass
    with sources.open(candidate) as (fresh, stream):
        assert fresh == snapshot and stream.read() == DATA
    with pytest.raises(FileNotFoundError, match="destination disappeared"):
        with sources.open(candidate):
            raise FileNotFoundError("destination disappeared")
    with drive_fence.hold_drives_sorted([key], blocking=False):
        pass
    assert tuple(con.iterdump()) == before
    con.close()


def test_capacity_drift_is_not_hidden_by_own_partial_allocation(setup):
    t, store, plan, tx, dest, sources = setup
    original_check = dest.check
    seen = []
    def check(binding, allocated):
        seen.append(allocated)
        original_check(binding, allocated)
        if (dest.root / "foreign").exists():
            raise t.TransferRefusal("DESTINATION_CAPACITY_CHANGED")
    dest.check = check
    def fault(point):
        if point == "file_prepared":
            (dest.root / "foreign").write_bytes(b"external bytes")
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CAPACITY_CHANGED"):
        with start(setup, fault) as session:
            session.run()
    assert max(seen) >= len(DATA)
    assert store.receipt(tx) is None


def test_stop_during_stream_preserves_owned_partial_and_can_resume(setup):
    t, store, plan, tx, dest, sources = setup
    def fault(point):
        if point == "chunk_written":
            store.request_stop(tx)
    with start(setup, fault) as session:
        assert session.run().state == "stopped"
    assert not (dest.root / "models/org/model/model.safetensors").exists()
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "complete"


def test_contradictory_journal_binding_is_rejected(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup):
        pass
    op = next(payload for event, payload in store.events(tx) if event == "operation")
    store.append(tx, "operation", dict(op, seal="other-seal"))
    with pytest.raises(t.TransferRefusal, match="JOURNAL_CORRUPT"):
        t.start(store, tx, dest, sources)


def test_truncated_journal_is_not_a_new_transaction(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup):
        pass
    with store._connection() as con:
        con.execute("DELETE FROM journal WHERE tx=?", (tx,))
    with pytest.raises(t.TransferRefusal, match="JOURNAL_CORRUPT"):
        t.start(store, tx, dest, sources)


def test_private_state_rejects_symlink_before_creating_anything(api, tmp_path, monkeypatch):
    _, s = api
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(s, "HOST_STATE_DIR", link / "state")
    with pytest.raises(ValueError, match="symlink"):
        s.Store()
    assert not list(elsewhere.iterdir())


def test_plan_rejects_destination_substitution_and_known_insufficient_space(setup):
    t, store, plan, tx, dest, sources = setup
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CHANGED"):
        t.TransferPlan(plan.proposal, replace(dest.binding, device_id="another-device"))
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CAPACITY_INSUFFICIENT"):
        t.TransferPlan(plan.proposal, replace(dest.binding, available_bytes=1))


@pytest.mark.parametrize("flush_number", range(1, 7))
def test_private_store_bootstrap_flush_failures_are_recoverable(api, monkeypatch, flush_number):
    _, s = api
    original = os.fsync
    count = 0
    class Crash(BaseException):
        pass
    def flush(fd):
        nonlocal count
        count += 1
        if count == flush_number:
            raise Crash()
        original(fd)
    with monkeypatch.context() as patch:
        patch.setattr(s.os, "fsync", flush)
        with pytest.raises(Crash):
            s.Store()
    store = s.Store()
    assert store.root.is_dir() and store.path.is_file()


@pytest.mark.parametrize("target", ["model.safetensors", ".modelark-slice-owner", ".modelark-slice-receipt.json"])
def test_atomic_publication_race_never_overwrites_unknown_bytes(setup, target):
    t, store, plan, tx, dest, sources = setup
    publish = dest.publish
    def race(temp, path, token):
        if path.endswith(target):
            (dest.root / path).write_bytes(b"not ours")
        publish(temp, path, token)
    dest.publish = race
    with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
        with start(setup) as session:
            session.run()
    path = (target if target == ".modelark-slice-owner" else "models/" + target
            if target == ".modelark-slice-receipt.json" else "models/org/model/" + target)
    assert (dest.root / path).read_bytes() == b"not ours"
    assert store.receipt(tx) is None


def test_different_consumer_roots_share_device_exclusion_and_busy_preserves_approval(setup):
    t, store, plan, tx, dest, sources = setup
    p = d.preview(replace(plan.proposal.spec, destination_root="another-root"), sources.snapshot)
    second_plan = t.TransferPlan(p, dest.binding)
    second = store.create(second_plan, d.approve(p, expected_seal=p.seal, current_snapshot=sources.snapshot))
    store.approve(second, expected_seal=second_plan.seal)
    with start(setup):
        with pytest.raises(t.TransferRefusal, match="DESTINATION_BUSY"):
            t.start(store, second, dest, sources)
    with pytest.raises(t.TransferRefusal, match="DESTINATION_BUSY"):
        t.start(store, second, dest, sources)
    assert store.status(second).state == "approved"
    assert not (dest.root / "another-root").exists()


def test_status_reads_do_not_require_the_active_writers_sqlite_lock(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup):
        with store._connection() as con:
            con.execute("UPDATE transactions SET reason='pending' WHERE id=?", (tx,))
            assert store.status(tx).state == "transferring"
            assert store.owner(plan.destination.device_id) == tx
            assert store.events(tx)
            assert not t.start(store, tx, dest, sources).can_write


def test_overlapping_first_start_observes_initializing_owner(setup):
    t, store, plan, tx, dest, sources = setup
    check = dest.check
    observed = []
    def overlap(binding, allocated):
        if not observed:
            observed.append(t.start(store, tx, dest, sources))
        check(binding, allocated)
    dest.check = overlap
    with start(setup):
        assert len(observed) == 1 and not observed[0].can_write
        assert observed[0].transaction_id == tx


@pytest.mark.parametrize("change", ["removed", "changed"])
def test_final_verification_requires_the_owned_control_record(setup, change):
    t, store, plan, tx, dest, sources = setup
    with start(setup) as session:
        session.step()
        control = dest.root / ".modelark-slice-owner"
        if change == "removed":
            control.unlink()
        else:
            control.write_bytes(b"changed control record")
        with pytest.raises(t.TransferRefusal, match="CONTROL_CORRUPT"):
            session.run()
    assert store.receipt(tx) is None


def test_final_layout_authenticates_reappeared_historical_temporary(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup) as session:
        session.step()
        temporary = next(payload["temporary"] for event, payload in store.events(tx)
                         if event == "operation" and payload["kind"] == "file")
        assert not (dest.root / temporary).exists()
        list_paths = dest.list_paths
        def changed_layout(root):
            (dest.root / temporary).mkdir()
            return list_paths(root)
        dest.list_paths = changed_layout
        with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
            session.run()
    assert store.receipt(tx) is None


def test_source_wait_identifies_exact_artifact_and_candidate(setup):
    t, store, plan, tx, dest, sources = setup
    sources.status["drive-a"] = "WAITING_SOURCE"
    with start(setup) as session:
        result = session.run()
    assert all(value in result.reason for value in ("org/model", "model.safetensors", "drive-a"))


def test_journal_rejects_nonhex_creation_tokens(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup):
        pass
    op = next(payload for event, payload in store.events(tx) if event == "operation")
    # Rewrite both the test's journal rows/head to isolate structural validation from hash checking.
    with store._connection() as con:
        con.execute("DELETE FROM journal WHERE tx=?", (tx,))
        con.execute("UPDATE transactions SET journal_seq=0,journal_digest='' WHERE id=?", (tx,))
    store.append(tx, "operation", dict(op, token="x" * 32))
    with pytest.raises(t.TransferRefusal, match="JOURNAL_CORRUPT"):
        t.start(store, tx, dest, sources)


@pytest.mark.parametrize("boundary", ["control_chunk_written", "control_prepared", "control_published",
                                      "control_parent_flushed"])
def test_control_publication_is_recoverable_at_each_new_boundary(setup, boundary):
    t, store, plan, tx, dest, sources = setup
    class Crash(BaseException):
        pass
    def fault(point):
        if point == boundary:
            raise Crash()
    with pytest.raises(Crash):
        with start(setup, fault) as session:
            session.run()
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "complete"


def test_stop_requested_during_startup_is_not_lost(setup):
    t, store, plan, tx, dest, sources = setup
    check = dest.check
    def stop_at_check(binding, allocated):
        store.request_stop(tx)
        check(binding, allocated)
    dest.check = stop_at_check
    with pytest.raises(t.TransferRefusal, match="STOPPED"):
        start(setup)
    assert not list(dest.root.iterdir())
    assert store.status(tx).state == "stopped"


def test_verification_checks_stop_between_read_chunks(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup) as session:
        session.step()
        read = dest.read
        class StopOnRead(io.BytesIO):
            def read(self, size=-1):
                data = super().read(min(size, 1))
                if data:
                    store.request_stop(tx)
                return data
        def stopping_read(path):
            if path.endswith("model.safetensors"):
                return StopOnRead(DATA)
            return read(path)
        dest.read = stopping_read
        assert session.run().state == "stopped"
    assert store.receipt(tx) is None


def test_streaming_does_not_replay_plan_and_journal_per_chunk(setup, monkeypatch):
    t, store, plan, tx, dest, sources = setup
    opening = sources.open
    class OneByte(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 1))
    @contextmanager
    def chunked(source):
        with opening(source) as (snapshot, _):
            yield snapshot, OneByte(DATA)
    sources.open = chunked
    counts = {"events": 0, "load": 0}
    for name in counts:
        method = getattr(store, name)
        def counted(*args, name=name, method=method, **kwargs):
            counts[name] += 1
            return method(*args, **kwargs)
        monkeypatch.setattr(store, name, counted)
    with start(setup) as session:
        counts.update(events=0, load=0)
        assert session.run().state == "complete"
    assert counts["load"] == 0 and counts["events"] <= 2


@pytest.mark.parametrize("recovered", [False, True])
def test_completed_artifact_hashing_is_linear_per_session(api, tmp_path, recovered):
    t, s = api
    _, _, snapshot = proposal()
    snapshot = replace(snapshot,
                       files=tuple(replace(snapshot.files[0], rfilename=f"shard-{i}.safetensors") for i in range(5)),
                       copies=tuple(replace(snapshot.copies[0], rfilename=f"shard-{i}.safetensors") for i in range(5)))
    p = d.preview(replace(spec(d), destination_id="test-device"), snapshot)
    approval = d.approve(p, expected_seal=p.seal, current_snapshot=snapshot)
    dest, store = Destination(tmp_path / "destination", t), s.Store()
    plan = t.TransferPlan(p, dest.binding)
    tx = store.create(plan, approval)
    store.approve(tx, expected_seal=plan.seal)
    reads = []
    reading = dest.read
    def counted(path):
        if path.endswith(".safetensors"):
            reads.append(path)
        return reading(path)
    dest.read = counted
    if recovered:
        with t.start(store, tx, dest, Sources(snapshot, t)) as session:
            session.step()
            session.step()
        reads.clear()
    with t.start(store, tx, dest, Sources(snapshot, t)) as session:
        assert session.run().state == "complete"
    # New content is hashed during publication; each final file needs only its final pass.
    assert len(reads) == len(p.closure) + (2 if recovered else 0)
    directories = [payload for event, payload in store.events(tx)
                   if event == "operation" and payload["kind"] == "directory" and payload["state"] == "complete"]
    assert len(directories) == 3


def test_session_verification_cache_does_not_skip_final_digest_pass(setup):
    t, store, _, tx, dest, _ = setup
    with start(setup) as session:
        session.step()
        (dest.root / "models/org/model/model.safetensors").write_bytes(b"wrong bytes!")
        with pytest.raises(t.TransferRefusal, match="VERIFICATION_FAILED"):
            session.run()
    assert store.receipt(tx) is None


def test_terminal_session_cannot_resume_after_adapter_condition_is_restored(setup):
    t, store, _, tx, dest, _ = setup
    with start(setup) as session:
        dest.changed = True
        with pytest.raises(t.TransferRefusal, match="DESTINATION_CHANGED"):
            session.step()
        assert store.status(tx).state == "invalidated"
        dest.changed = False
        before = tuple(dest.root.rglob("*"))
        with pytest.raises(t.TransferRefusal, match="NOT_RESUMABLE"):
            session.step()
        assert not session.can_write
        assert tuple(dest.root.rglob("*")) == before
    assert store.receipt(tx) is None


@pytest.mark.parametrize("kind", ["directory", "file", "receipt"])
def test_parent_certificate_loss_is_refused_before_child_creation(setup, kind):
    t, _, _, _, dest, _ = setup
    with start(setup) as session:
        if kind == "receipt":
            session.step()
            parent = "models"
        else:
            session._directory("models")
            parent = "models"
        os.removexattr(dest.root / parent, "user.slice-test-owner")
        before = set(dest.root.rglob("*"))
        with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
            if kind == "directory":
                session._directory("models/org")
            elif kind == "file":
                op = session._op("models/test", "receipt", size=len(DATA), sha=hashlib.sha256(DATA).hexdigest())
                session._write(op, io.BytesIO(DATA))
            else:
                session.step()
        assert set(dest.root.rglob("*")) == before


def test_completed_directory_has_no_redundant_durable_checkpoint(setup):
    _, store, _, tx, dest, _ = setup
    with start(setup) as session:
        session._directory("models")
        head = store.head(tx)
        dest.trace.clear()
        session._directory("models")
        assert store.head(tx) == head
        assert not [item for item in dest.trace if item[0] in {"mkdir", "flush"}]


def test_cached_journal_requires_unchanged_durable_head(setup):
    t, store, plan, tx, dest, sources = setup
    with start(setup) as session:
        op = next(payload for event, payload in store.events(tx) if event == "operation")
        store.append(tx, "operation", op)
        with pytest.raises(t.TransferRefusal, match="JOURNAL_CORRUPT"):
            session.run()


def _die_during_sqlite_write(store, tx):
    with store._connection() as con:
        con.execute("PRAGMA cache_size=1")
        con.execute("UPDATE transactions SET reason=? WHERE id=?", ("uncommitted" * 100000, tx))
        os._exit(73)


def test_readers_recover_a_dead_sqlite_writer_without_committing_its_changes(setup):
    t, store, plan, tx, dest, sources = setup
    process = multiprocessing.get_context("fork").Process(target=_die_during_sqlite_write, args=(store, tx))
    process.start()
    process.join()
    assert process.exitcode == 73
    assert store.status(tx).reason == ""
    with start(setup) as session:
        assert session.run().state == "complete"


def test_delayed_first_starter_cannot_downgrade_a_completed_winner(setup):
    t, store, plan, tx, dest, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    finished = False
    def fault(point):
        nonlocal finished
        if point == "reservation_committed":
            with t.start(store, tx, dest, sources) as winner:
                assert winner.run().state == "complete"
            finished = True
    result = t.start(store, tx, dest, sources, fault=fault)
    assert finished and not result.can_write and result.state == "complete"
    assert store.status(tx).state == "complete"


def test_crash_between_reservation_and_process_bind_is_resumable(setup):
    t, store, plan, tx, dest, sources = setup
    store.approve(tx, expected_seal=plan.seal)
    process = multiprocessing.get_context("fork").Process(target=_die_at_boundary,
                                                         args=(store, tx, dest, sources, "reservation_committed"))
    process.start()
    process.join()
    assert process.exitcode == 73
    assert store.owner(plan.destination.device_id) == tx
    assert not list(dest.root.iterdir())
    with t.start(store, tx, dest, sources) as session:
        assert session.run().state == "complete"
