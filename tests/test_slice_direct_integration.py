"""Real descriptor/certificate delivery with disposable state and synthetic hardware evidence.

Free space is deterministic, but accounts for ALL physical descendant inode blocks, including
unknown objects. Only the device identity and free-space observation are simulated; destination
IO, xattrs, certificates, fsync, journal, authority and receipt verification are actual code.
"""
from contextlib import contextmanager
from dataclasses import replace
import errno
import hashlib
import json
import os
from pathlib import PurePosixPath
from types import SimpleNamespace
import uuid

import pytest

from modelark.slice import domain as d
from modelark.slice import state as state_module
from modelark.slice import transaction as t
from modelark.slice.destination import UsbDestination
from modelark.slice.linux import BoundTree
from test_slice_transaction import DATA, Sources, proposal


class PowerCut(BaseException):
    pass


def physical_descendants(root):
    """Measure every inode, not only objects claiming ownership; hardlinks count once."""
    seen, used = set(), 0
    for parent, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            info = os.lstat(os.path.join(parent, name))
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                used += info.st_blocks * 512
                seen.add(identity)
    return used


@pytest.fixture
def direct(tmp_path, monkeypatch):
    monkeypatch.setattr(state_module, "HOST_STATE_DIR", tmp_path / "private-host")
    store = state_module.Store()
    initial, _, snapshot = proposal()
    device = "direct-test-" + uuid.uuid4().hex
    preview = d.preview(replace(initial.spec, destination_id=device), snapshot)
    approval = d.approve(preview, expected_seal=preview.seal, current_snapshot=snapshot)
    binding = t.DestinationBinding(device, "disposable-fs", "synthetic-ext4-profile", 10_000_000)
    plan = t.TransferPlan(preview, binding, metadata_reserve_bytes=1_000_000)
    tx = store.create(plan, approval)
    store.approve(tx, expected_seal=plan.seal)
    root = tmp_path / "destination"
    root.mkdir()
    availability = {"attached": True}

    def verify():
        if not availability["attached"]:
            raise t.TransferRefusal("WAITING_DESTINATION", "synthetic unplug")

    @contextmanager
    def adapter():
        with BoundTree(root, verify=verify, writable=True) as tree:
            destination = UsbDestination(tree, binding, store, env.tx)
            def available():
                root_delta = os.fstat(tree.fd).st_blocks * 512 - destination._root_blocks
                return binding.available_bytes - physical_descendants(root) - root_delta
            monkeypatch.setattr(destination, "_available", available)
            yield destination

    output = str(PurePosixPath(preview.spec.destination_root) /
                 preview.closure[0].repo_id / preview.closure[0].rfilename)
    env = SimpleNamespace(store=store, plan=plan, tx=tx, root=root, adapter=adapter,
                          sources=Sources(snapshot, t), snapshot=snapshot, output=output,
                          availability=availability)
    return env


def assert_delivery_complete(env, adapter):
    assert env.store.status(env.tx).state == "complete"
    with adapter.read(env.output) as stream:
        assert stream.read() == DATA
    receipt_path = str(PurePosixPath(env.plan.proposal.spec.destination_root) / ".modelark-slice-receipt.json")
    with adapter.read(receipt_path) as stream:
        receipt = json.load(stream)
    assert receipt["transaction"] == env.tx
    assert receipt["seal"] == env.plan.seal
    assert receipt["status"] == "complete"
    assert receipt["files"][0]["path"] == env.output
    assert receipt["files"][0]["size"] == len(DATA)
    assert not tuple(env.root.rglob(".slice-*"))


def test_real_direct_delivery_from_empty_root_and_receipt_readback(direct):
    with direct.adapter() as adapter:
        with t.start(direct.store, direct.tx, adapter, direct.sources) as session:
            assert session.run().state == "complete"
        assert_delivery_complete(direct, adapter)


@pytest.mark.parametrize("point", ["control_created", "control_published", "directory_created",
                                  "directory_parent_flushed", "temporary_created", "chunk_written",
                                  "file_flushed", "file_prepared", "file_published", "file_complete",
                                  "receipt_prepared", "receipt_published"])
def test_certified_fault_boundaries_recover_with_reconstructed_adapter(direct, point):
    reached = []
    def crash(current):
        if current == point:
            reached.append(current)
            raise PowerCut(point)
    with direct.adapter() as adapter:
        session = None
        with pytest.raises(PowerCut):
            try:
                session = t.start(direct.store, direct.tx, adapter, direct.sources, fault=crash)
                session.run()
            finally:
                if session is not None:
                    session.lease.close()  # Simulate process fd loss, not graceful Stop.
    assert reached == [point]
    with direct.adapter() as adapter:
        with t.start(direct.store, direct.tx, adapter, direct.sources) as resumed:
            assert resumed.run().state == "complete"
        assert_delivery_complete(direct, adapter)


def test_directory_pre_certificate_crash_refuses_without_adopting_or_deleting(direct, monkeypatch):
    residue = None
    with direct.adapter() as adapter:
        certify = adapter._certify
        def crash(fd, path, token, kind):
            nonlocal residue
            if kind == "directory":
                residue = direct.root / path
                raise PowerCut("uncertified directory")
            return certify(fd, path, token, kind)
        monkeypatch.setattr(adapter, "_certify", crash)
        session = t.start(direct.store, direct.tx, adapter, direct.sources)
        try:
            with pytest.raises(PowerCut):
                session.run()
        finally:
            session.lease.close()
    before = residue.stat()
    with direct.adapter() as adapter:
        with pytest.raises(t.TransferRefusal, match="OUTPUT_COLLISION"):
            t.start(direct.store, direct.tx, adapter, direct.sources)
    after = residue.stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert residue.is_dir() and not tuple(residue.iterdir())


def test_publication_hardlinks_count_once_during_restart(direct):
    def crash(point):
        if point == "file_published":
            raise PowerCut(point)
    with direct.adapter() as adapter:
        session = t.start(direct.store, direct.tx, adapter, direct.sources, fault=crash)
        try:
            with pytest.raises(PowerCut):
                session.run()
        finally:
            session.lease.close()
        operation = next(payload for event, payload in direct.store.events(direct.tx)
                         if event == "operation" and payload["kind"] == "file")
        temporary = direct.root / operation["temporary"]
        final = direct.root / direct.output
        assert os.path.samefile(temporary, final)
        assert final.stat().st_nlink == 2
        accounted = t._allocated(direct.store, direct.tx, adapter)
        assert accounted == physical_descendants(direct.root)
        adapter.check(direct.plan.destination, accounted, direct.plan.required_bytes(direct.store.root))
    with direct.adapter() as adapter:
        with t.start(direct.store, direct.tx, adapter, direct.sources) as session:
            assert session.run().state == "complete"
        assert_delivery_complete(direct, adapter)


def test_stop_releases_attempt_and_explicit_resume_finishes_real_delivery(direct):
    with direct.adapter() as adapter:
        first = t.start(direct.store, direct.tx, adapter, direct.sources)
        direct.store.request_stop(direct.tx)
        assert first.step().state == "stopped"
        assert not first.can_write
        with t.start(direct.store, direct.tx, adapter, direct.sources) as resumed:
            assert resumed.run().state == "complete"
        first.close()
        assert_delivery_complete(direct, adapter)


def test_attended_source_wait_then_resume_retains_destination_certificates(direct):
    direct.sources.status = {source.drive.drive_label: "WAITING_SOURCE"
                             for source in direct.plan.proposal.closure[0].sources}
    with direct.adapter() as adapter:
        with t.start(direct.store, direct.tx, adapter, direct.sources) as session:
            assert session.run().state == "waiting_source"
            direct.sources.status.clear()
            assert session.run().state == "complete"
        assert_delivery_complete(direct, adapter)


def test_attended_destination_wait_then_resume_retains_certified_work(direct):
    with direct.adapter() as adapter:
        with t.start(direct.store, direct.tx, adapter, direct.sources) as session:
            direct.availability["attached"] = False
            assert session.run().state == "waiting_destination"
            assert session.can_write  # The attempt is retained; the adapter refuses IO.
            direct.availability["attached"] = True
            assert session.run().state == "complete"
        assert_delivery_complete(direct, adapter)


def test_pre_certificate_file_crash_leaves_no_named_residue_and_restarts(direct, monkeypatch):
    with direct.adapter() as adapter:
        monkeypatch.setattr(adapter, "_certify", lambda *args: (_ for _ in ()).throw(PowerCut("file certificate")))
        with pytest.raises(PowerCut):
            t.start(direct.store, direct.tx, adapter, direct.sources)
        assert not tuple(direct.root.iterdir())
    with direct.adapter() as adapter:
        with t.start(direct.store, direct.tx, adapter, direct.sources) as session:
            assert session.run().state == "complete"
        assert_delivery_complete(direct, adapter)


def test_synthetic_capacity_does_not_hide_unowned_allocation(direct):
    with direct.adapter() as adapter:
        with t.start(direct.store, direct.tx, adapter, direct.sources) as session:
            foreign = direct.root / "unowned"
            foreign.write_bytes(b"foreign" * 4096)
            with pytest.raises(t.TransferRefusal, match="DESTINATION_CAPACITY_CHANGED"):
                session.run()
            assert foreign.read_bytes() == b"foreign" * 4096


def test_actual_destination_write_error_ends_attempt_without_a_receipt(direct, monkeypatch):
    from modelark.slice import destination as destination_module
    with direct.adapter() as adapter:
        with t.start(direct.store, direct.tx, adapter, direct.sources) as session:
            def no_space(*args):
                raise OSError(errno.ENOSPC, "synthetic physical destination full")
            monkeypatch.setattr(destination_module.os, "write", no_space)
            with pytest.raises(t.TransferRefusal, match="DESTINATION_IO_FAILED"):
                session.run()
            assert direct.store.status(direct.tx).state == "invalidated"
            assert not session.can_write
            assert not any(event == "receipt" for event, _ in direct.store.events(direct.tx))
            assert not tuple(direct.root.rglob(".modelark-slice-receipt.json"))


def test_local_archive_and_fresh_catalog_deliver_under_shared_fence_without_mutation(direct, tmp_path, monkeypatch):
    from modelark import drive_fence
    from modelark.core import db
    from modelark.slice.catalog import read_catalog
    from modelark.slice.local_source import LocalArchiveReader
    from modelark.slice.sources import FencedSources
    from test_slice_catalog import seed

    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "archive-fences")
    monkeypatch.setattr(db, "connect", lambda *args, **kwargs: pytest.fail("no global catalog"))
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: pytest.fail("no retrieval or live hardware"))
    catalog = tmp_path / "catalog.sqlite"
    con = seed(catalog, d)
    digest = hashlib.sha256(DATA).hexdigest()
    con.execute("UPDATE files SET sha256=?", (digest,))
    con.execute("UPDATE archived SET orig_sha256=?,annex_key=?", (digest, f"SHA256E-s12--{digest}"))
    snapshot = read_catalog(catalog, direct.plan.proposal.spec)
    preview = d.preview(direct.plan.proposal.spec, snapshot)
    direct.plan = t.TransferPlan(preview, direct.plan.destination, metadata_reserve_bytes=1_000_000)
    direct.tx = direct.store.create(direct.plan, d.approve(preview, expected_seal=preview.seal,
                                                         current_snapshot=snapshot))
    direct.store.approve(direct.tx, expected_seal=direct.plan.seal)
    mount = tmp_path / "source-attachment"
    root = mount / "modelark"
    (root / ".git").mkdir(parents=True)
    config = root / ".git/config"
    config.write_text("[annex]\n uuid = annex-a\n")
    (root / "org/model").mkdir(parents=True)
    source_file = root / "org/model/model.safetensors"
    source_file.write_bytes(DATA)
    with BoundTree(root) as source_tree:
        source_mount_id = source_tree.mount_id
    evidence = SimpleNamespace(fs_uuid="fs-a", serial="serial-a", total_bytes=1000,
                               device_id="synthetic-source", mount_id=source_mount_id, mount_path=str(mount))
    observer = SimpleNamespace(observe=lambda *args, **kwargs: evidence,
                               check_attachment=lambda tree, *a, **k: tree.check())
    reader = LocalArchiveReader({"drive-a": root}, observer=observer)
    opened = []

    class AssertFenceReader:
        @contextmanager
        def open(self, candidate):
            key = (candidate.drive.identity_fingerprint, candidate.drive.identity_epoch)
            with reader.open(candidate) as stream:
                with pytest.raises(drive_fence.FenceUnavailable):
                    with drive_fence.hold_drives_sorted([key], blocking=False):
                        pytest.fail("archive reader did not retain the shared fence")
                opened.append(candidate.drive.drive_label)
                yield stream

    sources = FencedSources(catalog, AssertFenceReader())
    before_catalog = tuple(con.iterdump())
    before_config = config.read_bytes()
    before_source = source_file.stat()
    try:
        with direct.adapter() as adapter:
            with t.start(direct.store, direct.tx, adapter, sources) as session:
                assert session.run().state == "complete"
            assert_delivery_complete(direct, adapter)
        assert opened == ["drive-a"]
        assert tuple(con.iterdump()) == before_catalog
        assert config.read_bytes() == before_config
        assert source_file.read_bytes() == DATA
        assert source_file.stat().st_mtime_ns == before_source.st_mtime_ns
        key = (snapshot.drives[0].identity_fingerprint, snapshot.drives[0].identity_epoch)
        with drive_fence.hold_drives_sorted([key], blocking=False):
            pass  # Reader close released the archive fence.
    finally:
        con.close()
