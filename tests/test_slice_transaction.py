"""Expected-red Slice 2 contracts. Only disposable destinations and synthetic sources."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import importlib
import io
import multiprocessing
import os

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
    p = d.preview(spec(d), snapshot)
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
        self.owned = {}
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
        return self.t.ObjectInfo("directory" if p.is_dir() else "file", self.owned.get(path),
                                 0 if p.is_dir() else p.stat().st_size)

    def create_directory(self, path, token):
        (self.root / path).mkdir()
        self.owned[path] = token
        self.trace.append(("mkdir", path))

    def create_file(self, path, token):
        with (self.root / path).open("xb"):
            pass
        self.owned[path] = token

    def append(self, path, token, data):
        assert self.owned[path] == token
        with (self.root / path).open("ab") as out:
            out.write(data)

    def discard_temporary(self, path, token):
        assert self.owned[path] == token
        (self.root / path).unlink()
        del self.owned[path]

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
        assert self.owned[temporary] == token
        os.link(self.root / temporary, self.root / path)  # Atomic no-replace.
        self.owned[path] = token
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
