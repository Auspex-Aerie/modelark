"""Explicit operator assembly against disposable state and mocked hardware only."""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest

from modelark.slice import domain as d
from modelark.slice import state as s
from modelark.slice import transaction as t
from modelark.slice.linux import BoundTree
from modelark.slice.capacity import _root_metadata as PRODUCTION_ROOT_METADATA
from test_slice_transaction import Destination, Sources, proposal


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "HOST_STATE_DIR", tmp_path / "private-state")
    return s.Store()


def sealed(store, admission=True):
    p, approval, snapshot = proposal()
    record = {"version": "modelark.slice.direct.v1", "catalog": "/explicit/catalog.sqlite",
              "capacity": {"profile": "test-profile"}}
    mount = "direct-v1:" + hashlib.sha256(d._json(record)).hexdigest()
    binding = t.DestinationBinding("test-device", "test-filesystem", mount, 1_000_000)
    plan = t.TransferPlan(p, binding, metadata_reserve_bytes=100_000)
    tx = store.create(plan, approval, admission=record if admission else None)
    return tx, plan, snapshot, record


def test_admission_is_atomic_sealed_private_record(store):
    tx, plan, _, admission = sealed(store)
    assert store.load_admission(tx) == admission
    with store._connection() as con:
        con.execute("UPDATE transactions SET admission=? WHERE id=?",
                    (json.dumps({**admission, "catalog": "/other/catalog"}), tx))
    with pytest.raises(t.TransferRefusal, match="ADMISSION_CORRUPT"):
        store.load_admission(tx)
    assert store.load(tx).seal == plan.seal


def test_invalid_admission_never_inserts_transaction(store):
    tx, plan, snapshot, admission = sealed(store)
    approval = d.approve(plan.proposal, expected_seal=plan.proposal.seal, current_snapshot=snapshot)
    with pytest.raises(t.TransferRefusal, match="ADMISSION_CORRUPT"):
        store.create(plan, approval, admission={**admission, "catalog": "/other/catalog"})
    with store._connection(write=False) as con:
        assert con.execute("SELECT id FROM transactions").fetchall() == [(tx,)]


def test_v5_migration_preserves_legacy_transaction_without_inventing_admission(store):
    tx, plan, _, _ = sealed(store, admission=False)
    with store._connection() as con:
        con.execute("ALTER TABLE transactions DROP COLUMN admission")
        con.execute("PRAGMA user_version=5")
    upgraded = s.Store()
    assert upgraded.load(tx).seal == plan.seal
    with pytest.raises(t.TransferRefusal, match="ADMISSION_MISSING"):
        upgraded.load_admission(tx)
    with upgraded._connection(write=False) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 8


@pytest.fixture
def operator(monkeypatch):
    module = importlib.import_module("modelark.slice.operator")
    monkeypatch.setattr(module, "LinuxObserver", lambda: pytest.fail("unexpected hardware observation"))
    return module


def test_start_requires_approval_before_observing_destination(store, operator):
    tx, _, _, _ = sealed(store)
    with pytest.raises(t.TransferRefusal, match="APPROVAL_MISSING"):
        operator.start(tx, "/never-observe", {})


def test_completed_status_needs_no_destination(store, operator):
    tx, _, _, _ = sealed(store)
    with store._connection() as con:
        con.execute("UPDATE transactions SET state='complete' WHERE id=?", (tx,))
    result = operator.start(tx, "/absent-device", {})
    assert result["state"] == "complete" and result["can_write"] is False


@pytest.mark.parametrize("state", ["failed", "invalidated"])
def test_terminal_transaction_refuses_before_destination_observation(store, operator, state):
    tx, _, _, _ = sealed(store)
    with store._connection() as con:
        con.execute("UPDATE transactions SET state=? WHERE id=?", (state, tx))
    with pytest.raises(t.TransferRefusal, match="NOT_RESUMABLE"):
        operator.start(tx, "/never-observe", {})


def test_operator_refuses_legacy_internal_transaction(store, operator):
    tx, plan, _, _ = sealed(store, admission=False)
    store.approve(tx, expected_seal=plan.seal)
    with pytest.raises(t.TransferRefusal, match="ADMISSION_MISSING"):
        operator.start(tx, "/never-observe", {})


def test_approve_rechecks_exact_sealed_catalog(store, operator, monkeypatch):
    tx, plan, snapshot, record = sealed(store)
    calls = []
    monkeypatch.setattr(operator, "read_catalog", lambda path, spec: calls.append((path, spec)) or snapshot)
    result = operator.approve(tx, plan.seal)
    assert result["state"] == "approved"
    assert calls == [(record["catalog"], plan.proposal.spec)]


def test_stale_approval_never_changes_state(store, operator, monkeypatch):
    tx, plan, snapshot, _ = sealed(store)
    changed = replace(snapshot, copies=())
    monkeypatch.setattr(operator, "read_catalog", lambda *a: changed)
    with pytest.raises(d.SliceRefusal, match="PREVIEW_STALE"):
        operator.approve(tx, plan.seal)
    assert store.status(tx).state == "ready"


def test_preview_gaps_need_no_hardware_or_store(tmp_path, operator, monkeypatch):
    _, _, snapshot = proposal()
    monkeypatch.setattr(operator, "read_catalog", lambda *a: replace(snapshot, copies=()))
    monkeypatch.setattr(operator, "Store", lambda: pytest.fail("blocked preview created Store"))
    result = operator.preview(tmp_path / "catalog", "/never-observe", ["org/model"], "output")
    assert result["ok"] is False and result["executable"] is False
    assert result["gaps"] and "transaction_id" not in result


def test_status_and_stop_are_private_only(store, operator):
    tx, _, _, _ = sealed(store)
    assert operator.status(tx)["state"] == "ready"
    result = operator.stop(tx)
    assert result["stop_requested"] is True
    assert store.stop_requested(tx)


@pytest.fixture
def assembled(store, operator, monkeypatch, tmp_path):
    tx, plan, snapshot, admission = sealed(store)
    store.approve(tx, expected_seal=plan.seal)
    destination = Destination(tmp_path / "destination", t)
    destination.binding = plan.destination
    sources = Sources(snapshot, t)
    calls = []
    archives = (SimpleNamespace(fs_uuid="unselected-archive", serial="other-disk"),)
    monkeypatch.setattr(operator, "_archives", lambda path: calls.append(("registry", path)) or archives)
    evidence = SimpleNamespace(device_id="test-device", fs_uuid="test-filesystem", available_bytes=1_000_000)

    class Observer:
        def observe(self, path, **kwargs):
            calls.append(("observe", kwargs))
            return evidence

    monkeypatch.setattr(operator, "LinuxObserver", Observer)
    monkeypatch.setattr(operator, "_CheckedDestination", lambda *a: destination)
    monkeypatch.setattr(operator, "FencedSources", lambda catalog, reader:
                        calls.append(("sources", catalog, reader.attachments)) or sources)
    return operator, tx, store, plan, destination, sources, admission, calls


def test_start_uses_sealed_catalog_and_all_registry_exclusions(assembled):
    operator, tx, _, _, destination, _, admission, calls = assembled
    result = operator.start(tx, destination.root, {"drive-a": "/explicit/modelark"})
    assert result["state"] == "complete" and result["can_write"] is False
    assert calls[0] == ("registry", admission["catalog"])
    observed = next(value for name, value in calls if name == "observe")
    assert observed["writable"] is True
    assert observed["archives"][0].fs_uuid == "unselected-archive"
    assert next(call for call in calls if call[0] == "sources")[1] == admission["catalog"]


@pytest.mark.parametrize("code,expected", [("SOURCE_BLOCKED", "blocked_source"),
                                           ("WAITING_SOURCE", "waiting_source")])
def test_direct_preflight_returns_source_status_without_output(assembled, monkeypatch, code, expected):
    operator, tx, store, _, destination, sources, _, _ = assembled
    def refuse(proposal, check):
        check()
        raise t.TransferRefusal(code, "preflight source unavailable")
    monkeypatch.setattr(sources, "preflight", refuse, raising=False)
    result = operator.start(tx, destination.root, {})
    assert result["state"] == expected and not result["ok"] and not result["can_write"]
    assert code in result["reason"]
    assert store.events(tx) == [] and not list(destination.root.iterdir())
    monkeypatch.setattr(sources, "preflight", lambda proposal, check: check())
    assert operator.start(tx, destination.root, {})["state"] == "complete"


def test_unsealed_attachment_label_never_observes(assembled):
    operator, tx, _, _, destination, _, _, calls = assembled
    with pytest.raises(t.TransferRefusal, match="SOURCE_ATTACHMENT_UNSEALED"):
        operator.start(tx, destination.root, {"unreviewed-drive": "/path"})
    assert calls == []


def test_source_wait_closes_attempt_without_losing_wait_state(assembled):
    operator, tx, store, _, destination, sources, _, _ = assembled
    sources.status["drive-a"] = "WAITING_SOURCE"
    result = operator.start(tx, destination.root, {})
    assert result["state"] == "waiting_source" and result["ok"] is False
    assert store.status(tx).state == "waiting_source"
    assert store.current_attempt(destination.binding.device_id) is not None
    # A fresh kernel lease proves the synchronous waiter released its process exclusion.
    lease = t._Lease(destination.binding.device_id)
    lease.close()


def test_ctrl_c_during_start_acknowledges_without_destination_writes(assembled, monkeypatch):
    operator, tx, store, _, destination, _, _, _ = assembled
    real_start = t.start
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            def fault(point):
                if point == "reserved":
                    raise KeyboardInterrupt
            return real_start(*args, fault=fault)
        return real_start(*args, **kwargs)

    monkeypatch.setattr(t, "start", interrupted)
    result = operator.start(tx, destination.root, {})
    assert result["state"] == "stopped" and result["stop_requested"] is True
    assert store.status(tx).state == "stopped"
    assert not list(destination.root.iterdir())


def test_ctrl_c_during_transfer_acknowledges_stop_and_releases(assembled, monkeypatch):
    operator, tx, store, _, destination, sources, _, _ = assembled

    @contextmanager
    def interrupted(source):
        raise KeyboardInterrupt
        yield  # pragma: no cover - makes this an actual context manager

    monkeypatch.setattr(sources, "open", interrupted)
    result = operator.start(tx, destination.root, {})
    assert result["state"] == "stopped" and result["stop_requested"] is True
    assert store.status(tx).state == "stopped"
    lease = t._Lease(destination.binding.device_id)
    lease.close()


def test_missing_destination_does_not_claim_attempt(assembled):
    operator, tx, store, _, destination, _, _, _ = assembled
    destination.root.rmdir()
    with pytest.raises(t.TransferRefusal, match="WAITING_DESTINATION"):
        operator.start(tx, destination.root, {})
    assert store.status(tx).state == "approved"
    assert store.current_attempt(destination.binding.device_id) is None


def test_ready_preview_persists_admission_without_output_writes(store, operator, monkeypatch, tmp_path):
    _, _, snapshot = proposal()
    path = tmp_path / "destination"
    path.mkdir()
    monkeypatch.setattr(operator, "read_catalog", lambda *a: snapshot)
    archive = SimpleNamespace(fs_uuid="all-registered", serial="other")
    monkeypatch.setattr(operator, "_archives", lambda path: (archive,))
    observations = []
    evidence = SimpleNamespace(device_id="test-device", fs_uuid="test-filesystem", available_bytes=1_000_000)

    class Observer:
        def observe(self, path, **kwargs):
            observations.append(kwargs)
            return evidence

    monkeypatch.setattr(operator, "LinuxObserver", Observer)
    verified = []
    policy = SimpleNamespace(capture=lambda *a: {"profile": "qualified"},
                             metadata_reserve_bytes=lambda *a: 100_000,
                             verify=lambda *a, **k: verified.append(a))
    monkeypatch.setattr(operator, "_capacity", lambda: policy)
    result = operator.preview(tmp_path / "explicit-catalog", path, ["org/model"], "delivery")
    assert result["state"] == "ready" and result["executable"] is False
    tx = result["transaction_id"]
    assert store.status(tx).state == "ready"
    assert store.load_admission(tx)["catalog"] == str(tmp_path / "explicit-catalog")
    assert all(item["archives"] == (archive,) for item in observations)
    assert verified and not list(path.iterdir())

    def inaccessible():
        raise OSError("private state became inaccessible")

    monkeypatch.setattr(operator, "Store", inaccessible)
    with pytest.raises(t.TransferRefusal, match="STATE_UNAVAILABLE"):
        operator.preview(tmp_path / "explicit-catalog", path, ["org/model"], "delivery")
    assert not list(path.iterdir())


@pytest.mark.parametrize('failure', ['policy', 'io', 'absent', 'replaced', 'unknown'])
def test_capacity_refusal_does_not_create_transaction(store, operator, monkeypatch, tmp_path, failure):
    _, _, snapshot = proposal()
    path = tmp_path / "destination"
    path.mkdir()
    monkeypatch.setattr(operator, "read_catalog", lambda *a: snapshot)
    monkeypatch.setattr(operator, "_archives", lambda path: ())
    evidence = SimpleNamespace(device_id="test-device", fs_uuid="test-filesystem", available_bytes=1_000_000)
    def recheck(tree, observed):
        tree.check()
        if failure in {'absent', 'replaced'}:
            raise t.TransferRefusal('WAITING_DESTINATION' if failure == 'absent' else 'DESTINATION_CHANGED')
        if failure == 'unknown':
            raise OSError('reprobe unavailable')
    monkeypatch.setattr(operator, "LinuxObserver", lambda: SimpleNamespace(
        observe=lambda *a, **k: evidence, recheck_attachment=recheck))

    def refuse(*args):
        if failure != 'policy':
            import errno
            from modelark.slice.io_errors import ProbeFailure
            raise ProbeFailure(OSError(errno.EIO, 'capture ioctl failed'),
                               'DESTINATION_CAPACITY_UNPROVEN', 'capacity capture')
        raise t.TransferRefusal("DESTINATION_CAPACITY_UNPROVEN")

    monkeypatch.setattr(operator, "_capacity", lambda: SimpleNamespace(capture=refuse))
    expected = {'absent': 'WAITING_DESTINATION', 'replaced': 'DESTINATION_CHANGED'}.get(
        failure, 'DESTINATION_CAPACITY_UNPROVEN')
    with pytest.raises(t.TransferRefusal, match=expected):
        operator.preview(tmp_path / "explicit-catalog", path, ["org/model"], "delivery")
    with store._connection(write=False) as con:
        assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert not list(path.iterdir())


@pytest.mark.parametrize("physical_version", [7, 8])
def test_registry_reader_uses_all_drives_and_never_creates_missing_catalog(operator, tmp_path, physical_version):
    import sqlite3
    path = tmp_path / "catalog.sqlite"
    with pytest.raises(d.SliceRefusal, match="CATALOG_UNAVAILABLE"):
        operator._archives(path)
    assert not path.exists()
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE drives(drive_label TEXT,fs_uuid TEXT,serial TEXT)")
        con.executemany("INSERT INTO drives VALUES(?,?,?)", [("a", "uuid-a", "serial-a"),
                                                            ("b", "uuid-b", "serial-b")])
        con.execute(f"PRAGMA user_version={physical_version}")
    before = path.read_bytes()
    assert [(a.fs_uuid, a.serial) for a in operator._archives(path)] == [
        ("uuid-a", "serial-a"), ("uuid-b", "serial-b")]
    assert path.read_bytes() == before


@pytest.mark.parametrize("physical_version", [0, 6, 9, 99])
def test_registry_reader_refuses_unknown_versions_without_mutation(operator, tmp_path, physical_version):
    import sqlite3
    path = tmp_path / "catalog.sqlite"
    with sqlite3.connect(path) as con:
        con.execute(f"PRAGMA user_version={physical_version}")
    before = path.read_bytes()
    with pytest.raises(d.SliceRefusal, match="CATALOG_VERSION_UNSUPPORTED"):
        operator._archives(path)
    assert path.read_bytes() == before


def test_boundary_capacity_refusal_precedes_destination_mutation_gate(operator, monkeypatch):
    adapter = object.__new__(operator._CheckedDestination)
    checked = []
    adapter.tree = SimpleNamespace(check=lambda: checked.append("tree"))
    adapter._attachment_path, adapter._catalog = "/destination", "/sealed/catalog"
    adapter._proposal, adapter._caps = object(), {"policy": "sealed"}
    binding = t.DestinationBinding("device", "filesystem", "binding", 1000)
    adapter._budget = None
    adapter._evidence = SimpleNamespace(device_id='device', fs_uuid='filesystem')
    adapter._observer = SimpleNamespace(observe=lambda *a, **k:
                                       SimpleNamespace(device_id="device", fs_uuid="filesystem"),
                                       check_attachment=lambda tree, *a, **k: tree.check())
    monkeypatch.setattr(operator, "_archives", lambda path: checked.append(path) or ())
    monkeypatch.setattr(operator.UsbDestination, "check", lambda *a: pytest.fail("capacity refusal ignored"))

    def refuse(tree, evidence, proposal, caps, *, adapter, refresh_volume):
        assert caps == {"policy": "sealed"}
        raise t.TransferRefusal("DESTINATION_CAPACITY_CHANGED")

    monkeypatch.setattr(operator, "_capacity", lambda: SimpleNamespace(verify=refuse))
    with pytest.raises(t.TransferRefusal, match="DESTINATION_CAPACITY_CHANGED"):
        adapter.check(binding, 0, 100)
    assert checked == ["/sealed/catalog", "tree"]


@pytest.mark.parametrize("error", [ValueError("unsafe state"), OSError("state inaccessible")])
@pytest.mark.parametrize("operation,args", [("status", ("tx",)), ("approve", ("tx", "seal")),
                                           ("start", ("tx", "/never-observe", {})), ("stop", ("tx",))])
def test_private_store_bootstrap_failure_is_typed_before_device(operator, monkeypatch, error, operation, args):
    def fail():
        raise error

    monkeypatch.setattr(operator, "Store", fail)
    with pytest.raises(t.TransferRefusal) as caught:
        getattr(operator, operation)(*args)
    assert caught.value.code == "STATE_UNAVAILABLE"
    assert str(error) in caught.value.detail


def test_corrupt_sqlite_bootstrap_is_typed(operator, monkeypatch):
    import sqlite3

    def fail():
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(operator, "Store", fail)
    with pytest.raises(t.TransferRefusal, match="STATE_UNAVAILABLE"):
        operator.status("tx")


def test_already_typed_bootstrap_refusal_is_preserved(operator, monkeypatch):
    def fail():
        raise t.TransferRefusal("STATE_BUSY", "private writer")

    monkeypatch.setattr(operator, "Store", fail)
    with pytest.raises(t.TransferRefusal) as caught:
        operator.status("tx")
    assert caught.value.code == "STATE_BUSY"


@pytest.mark.parametrize('mode', ['small', 'parent', 'large', 'unplug', 'acl-unplug', 'metadata-unplug'])
def test_actual_operator_catalog_reader_capacity_and_delivery_roundtrip(store, monkeypatch, tmp_path, mode):
    """Only hardware/free-space observations are fake; all assembly and IO are real."""
    import os
    import uuid
    from modelark.core import db
    from modelark.slice import capacity, operator
    from modelark.slice.hardware import DeviceEvidence
    from test_slice_catalog import seed
    from test_slice_direct_integration import physical_descendants
    from test_slice_transaction import DATA
    if mode not in {'small', 'parent'}:
        DATA = b'x' * (2 * 1024 * 1024 + 17)

    catalog = tmp_path / "explicit-catalog.sqlite"
    con = seed(catalog, d)
    digest = hashlib.sha256(DATA).hexdigest()
    con.execute("UPDATE files SET sha256=?", (digest,))
    con.execute("UPDATE archived SET orig_sha256=?,annex_key=?", (digest, f"SHA256E-s12--{digest}"))
    con.execute('UPDATE files SET size_bytes=?', (len(DATA),))
    con.execute('UPDATE archived SET orig_bytes=?,stored_bytes=?,annex_key=?',
                (len(DATA), len(DATA), f'SHA256E-s{len(DATA)}--{digest}'))
    con.execute("INSERT INTO drives(drive_label,fs_uuid,serial) VALUES('unselected','other-fs','other-disk')")
    before = tuple(con.iterdump())
    con.close()
    archive = tmp_path / "source-mount/modelark"
    (archive / ".git").mkdir(parents=True)
    (archive / ".git/config").write_text("[annex]\n uuid=annex-a\n")
    (archive / "org/model").mkdir(parents=True)
    (archive / "org/model/model.safetensors").write_bytes(DATA)
    destination = tmp_path / "usb"
    destination.mkdir()
    initial_root_blocks = destination.stat().st_blocks * 512
    baseline, initial_inodes = 100_000_000, 1_000_000
    device = "operator-integration-" + uuid.uuid4().hex
    observations = []
    connected = [True]
    payload_written = [False]

    def availability(tree):
        identities = set()
        for parent, directories, files in os.walk(destination):
            for name in (*directories, *files):
                value = os.lstat(os.path.join(parent, name))
                identities.add((value.st_dev, value.st_ino))
        root_delta = destination.stat().st_blocks * 512 - initial_root_blocks
        return (baseline - physical_descendants(destination) - root_delta,
                initial_inodes - len(identities))

    class Observer:
        def recheck_attachment(self, tree, evidence):
            self.check_attachment(tree, evidence)

        def check_attachment(self, tree, evidence, **kwargs):
            if tree.path == destination and not connected[0]:
                # Model physical backing disappearance while the mount ID remains present.
                tree._attachment_lost = True
                raise t.TransferRefusal('WAITING_DESTINATION')
            tree.check()

        def observe(self, path, *, writable=False, archives=()):
            from pathlib import Path
            path = Path(path)
            if path == archive:
                with BoundTree(archive) as source_tree:
                    source_mount_id = source_tree.mount_id
                return SimpleNamespace(fs_uuid="fs-a", serial="serial-a", total_bytes=1000,
                                       device_id="source-disk", mount_id=source_mount_id,
                                       mount_path=str(archive.parent))
            assert path == destination and writable
            observations.append(tuple(a.fs_uuid for a in archives))
            with BoundTree(destination) as observed_tree:
                mount_id = observed_tree.mount_id
            return DeviceEvidence(device, "destination-fs", "destination-serial", 200_000_000,
                                  str(destination), mount_id, "ext4", availability(None)[0],
                                  4096, 255, 200_000_000, "synthetic-stable-hardware-profile")

    monkeypatch.setattr(operator, "LinuxObserver", Observer)
    monkeypatch.setattr(capacity, "_volume", lambda *a: {"uuid": "destination-fs", "block_size": 4096})
    monkeypatch.setattr(capacity, "_root_metadata", lambda tree: (4096, 0))
    monkeypatch.setattr(capacity, "_free", availability)
    monkeypatch.setattr(operator.UsbDestination, "_available", lambda self: availability(self.tree)[0])
    monkeypatch.setattr(db, "connect", lambda *a, **k: pytest.fail("global catalog opened"))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("retrieval/real observer invoked"))
    original_write = operator.UsbDestination.append
    def unplug_after_payload(self, path, token, data):
        result = original_write(self, path, token, data)
        if data == DATA[:len(data)]:
            connected[0] = False
        return result
    if mode == 'unplug':
        monkeypatch.setattr(operator.UsbDestination, 'append', unplug_after_payload)
    if mode == 'metadata-unplug':
        def arm_metadata_fault(self, path, token, data):
            result = original_write(self, path, token, data)
            if data == DATA[:len(data)]:
                payload_written[0] = True
            return result
        monkeypatch.setattr(operator.UsbDestination, 'append', arm_metadata_fault)
    original_getxattr = os.getxattr
    def unplug_during_owned_directory_acl(fd, name, *args, **kwargs):
        import errno
        if (name == 'system.posix_acl_default'
                and os.fstat(fd).st_ino != destination.stat().st_ino):
            connected[0] = False
            raise OSError(errno.EIO, 'unplug during owned-directory ACL proof')
        return original_getxattr(fd, name, *args, **kwargs)
    if mode == 'acl-unplug':
        monkeypatch.setattr(os, 'getxattr', unplug_during_owned_directory_acl)

    supplied_destination = destination / '..' / destination.name if mode == 'parent' else destination
    reviewed = operator.preview(catalog, supplied_destination, ["org/model"], "delivery")
    tx = reviewed["transaction_id"]
    assert reviewed["state"] == "ready" and not list(destination.iterdir())
    assert operator.approve(tx, reviewed["seal"])["state"] == "approved"
    if mode == 'metadata-unplug':
        # Retain the production metadata helper; inject below it at the actual
        # ioctl boundary after payload has already been written/certified.
        def ioctl(fd, command, flags, mutate=True):
            import errno
            if payload_written[0]:
                connected[0] = False
                raise OSError(errno.EIO, 'unplug during root metadata ioctl')
            flags[0] = 0
        monkeypatch.setattr(capacity.fcntl, 'ioctl', ioctl)
        monkeypatch.setattr(capacity, '_root_metadata', PRODUCTION_ROOT_METADATA)
    result = operator.start(tx, supplied_destination, {"drive-a": archive})
    if mode in {'unplug', 'acl-unplug', 'metadata-unplug'}:
        assert result['state'] == 'waiting_destination'
        assert store.status(tx).state == 'waiting_destination'
        assert not (destination / 'delivery/.modelark-slice-receipt.json').exists()
        connected[0] = True
        payload_written[0] = False
        monkeypatch.setattr(operator.UsbDestination, 'append', original_write)
        monkeypatch.setattr(os, 'getxattr', original_getxattr)
        result = operator.start(tx, destination, {'drive-a': archive})
    assert result["state"] == "complete" and result["can_write"] is False
    assert (destination / "delivery/org/model/model.safetensors").read_bytes() == DATA
    receipt = json.loads((destination / "delivery/.modelark-slice-receipt.json").read_text())
    assert receipt["status"] == "complete" and receipt["transaction"] == tx
    assert receipt["seal"] == reviewed["seal"]
    assert all(set(uuids) == {"fs-a", "other-fs"} for uuids in observations)
    assert len(observations) <= (30 if mode in {'unplug', 'acl-unplug', 'metadata-unplug'} else 16)
    assert not tuple(destination.rglob(".slice-*"))
    import sqlite3
    with sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True) as con:
        assert tuple(con.iterdump()) == before
