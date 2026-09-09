"""Cross-workflow physical fencing, using only disposable catalogs and lock files."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import io
import sqlite3
import threading
from dataclasses import replace

import pytest

from modelark import drive_fence, drive_lifecycle, proposal
from modelark.drive_identity import FenceIdentity
from modelark.core import db
from modelark.execution_service import production_services
from modelark.slice import domain
from modelark.slice.catalog import read_catalog
from modelark.slice.sources import FencedSources
from modelark.slice.transaction import TransferRefusal
from test_slice_catalog import seed
from test_slice_domain import spec


class Reader:
    def __init__(self):
        self.opened = False
        self.opens = 0

    @contextmanager
    def open(self, candidate):
        self.opened = True
        self.opens += 1
        try:
            with io.BytesIO(b"source bytes") as stream:
                yield stream
        finally:
            self.opened = False


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.setattr(drive_fence, "_LOCK_DIR", tmp_path / "locks")

    def no_global_catalog(*args, **kwargs):
        pytest.fail("cross-workflow tests must use their explicit disposable catalog")

    monkeypatch.setattr(db, "connect", no_global_catalog)
    path = tmp_path / "catalog.sqlite"
    con = seed(path, domain)
    snapshot = read_catalog(path, spec(domain))
    candidate = domain.preview(spec(domain), snapshot).closure[0].sources[0]
    reader = Reader()
    sources = FencedSources(path, reader)
    try:
        yield path, con, candidate, reader, sources
    finally:
        con.close()


def test_fill_fence_blocks_slice_source_open_on_same_identity(archive, tmp_path):
    path, con, candidate, reader, sources = archive
    before = tuple(con.iterdump())
    services = production_services(con, catalog_path=path, state_dir=tmp_path / "fill-state")
    with services.drive_fences.hold_all_sorted([candidate.drive.drive_label]):
        with pytest.raises(TransferRefusal) as error:
            with sources.open(candidate):
                pytest.fail("Slice must not open a source under Fill's drive fence")
        assert error.value.code == "SOURCE_BUSY"
        assert reader.opens == 0
    with sources.open(candidate) as (_, stream):
        assert stream.read() == b"source bytes"
    assert tuple(con.iterdump()) == before


def test_open_slice_reader_blocks_fill_on_same_fingerprint_epoch(archive, tmp_path, monkeypatch):
    path, con, candidate, reader, sources = archive
    drive = candidate.drive
    keys = FenceIdentity(drive.fs_uuid, drive.annex_uuid, drive.serial,
                         drive.filesystem_capacity_bytes, drive.identity_epoch,
                         drive.identity_fingerprint).lock_keys()
    expected_paths = {drive_fence.drive_lock_path(*key) for key in keys}
    original_acquire = drive_fence._acquire
    main_thread = threading.get_ident()
    contention_proved = threading.Event()
    acquired = threading.Event()

    def observe_acquire(lock_path, blocking, *, shared=False):
        if threading.get_ident() != main_thread:
            assert lock_path in expected_paths and blocking is True
            # Prove real kernel contention before the production blocking acquisition.
            # The main thread will release the open reader after this event; no sleeps
            # or scheduling-based assumption that an unstarted worker is blocked.
            if not contention_proved.is_set():
                with lock_path.open("a") as probe:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                contention_proved.set()
        return original_acquire(lock_path, blocking, shared=shared)

    monkeypatch.setattr(drive_fence, "_acquire", observe_acquire)

    def fill_contender():
        worker_con = sqlite3.connect(path, isolation_level=None)
        try:
            services = production_services(worker_con, catalog_path=path,
                                           state_dir=tmp_path / "another-fill-state")
            with services.drive_fences.hold_all_sorted([candidate.drive.drive_label]):
                acquired.set()
        finally:
            worker_con.close()

    before = tuple(con.iterdump())
    with ThreadPoolExecutor(max_workers=1) as workers:
        with sources.open(candidate) as (_, stream):
            assert reader.opened and stream.read(6) == b"source"
            contender = workers.submit(fill_contender)
            assert contention_proved.wait(5), "Fill did not reach its physical fence"
            assert not acquired.is_set()
        assert not reader.opened
        contender.result(timeout=5)
    assert acquired.is_set()
    assert tuple(con.iterdump()) == before


def test_lifecycle_revocation_waits_for_actual_slice_reader_close(archive):
    path, con, candidate, reader, sources = archive
    preview = drive_lifecycle.loss_preview(con, candidate.drive.drive_label)

    def declare():
        return drive_lifecycle.declare_lost(
            con, candidate.drive.drive_label,
            expected_revision=preview["planner_revision"],
            expected_identity_epoch=preview["identity_epoch"],
            expected_identity_fingerprint=preview["identity_fingerprint"],
            confirmation=preview["confirmation"])

    with sources.open(candidate) as (_, stream):
        assert reader.opened
        with pytest.raises(proposal.Refusal) as error:
            declare()
        assert error.value.code == "DRIVE_BUSY"
        assert drive_lifecycle.loss_preview(con, candidate.drive.drive_label) == preview
        assert stream.read() == b"source bytes"
    assert not reader.opened
    assert declare()["lifecycle"] == "lost"
    refreshed = domain.preview(spec(domain), read_catalog(path, spec(domain)))
    assert not refreshed.source_ready
    assert any(gap.code == "SOURCE_INACTIVE" for gap in refreshed.gaps)


@pytest.mark.parametrize("legacy", [False, True])
def test_either_old_single_key_blocks_source(archive, legacy):
    from modelark.capacity_evidence import identity_fingerprint_v1
    _, _, candidate, reader, sources = archive
    drive = candidate.drive
    fingerprint = identity_fingerprint_v1(
        fs_uuid=drive.fs_uuid, annex_uuid=drive.annex_uuid,
        serial=None if legacy else drive.serial,
        filesystem_capacity_bytes=drive.filesystem_capacity_bytes)
    with drive_fence.hold_drives_sorted([(fingerprint, drive.identity_epoch)]):
        with pytest.raises(TransferRefusal, match="SOURCE_BUSY"):
            with sources.open(candidate):
                pytest.fail("single-key older contender must exclude both-key source")
    assert reader.opens == 0


def test_old_sealed_candidate_contends_with_new_identity_then_refuses_stale_catalog(archive):
    from modelark.capacity_evidence import identity_fingerprint_v1
    _, _, candidate, reader, sources = archive
    drive = candidate.drive
    legacy = identity_fingerprint_v1(
        fs_uuid=drive.fs_uuid, annex_uuid=drive.annex_uuid, serial=None,
        filesystem_capacity_bytes=drive.filesystem_capacity_bytes)
    old_candidate = replace(candidate, drive=replace(drive, identity_fingerprint=legacy))
    with drive_fence.hold_drives_sorted([(drive.identity_fingerprint, drive.identity_epoch)]):
        with pytest.raises(TransferRefusal, match="SOURCE_BUSY"):
            with sources.open(old_candidate):
                pytest.fail("old seal must expand canonical key too")
    with pytest.raises(TransferRefusal, match="SOURCE_EVIDENCE_UNAVAILABLE"):
        with sources.open(old_candidate):
            pytest.fail("aliases must never admit stale identity")
    assert reader.opens == 0


def test_changed_catalog_serial_cannot_open_under_old_seal_locks(archive):
    _, con, candidate, reader, sources = archive
    con.execute("UPDATE drives SET serial='replacement' WHERE drive_label=?",
                [candidate.drive.drive_label])
    with pytest.raises(TransferRefusal, match="SOURCE_EVIDENCE_UNAVAILABLE"):
        with sources.open(candidate):
            pytest.fail("replacement would require an unheld physical lock")
    assert reader.opens == 0
