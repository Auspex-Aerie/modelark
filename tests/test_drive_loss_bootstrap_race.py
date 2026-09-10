"""Public first reconciliation versus loss on a second disposable connection."""
import sqlite3

import pytest

from modelark import drive_bootstrap as bs, drive_fence, drive_mutation as dm
from modelark.capacity_evidence import identity_fingerprint_v1
from modelark.core import db
from test_drive_loss_unbootstrapped import declare, registered as registered


@pytest.mark.parametrize("phase", ["inventory", "before_begin"])
def test_committed_loss_during_first_reconcile_cannot_publish_authority(
    registered, tmp_path, monkeypatch, phase,
):
    registration, label = registered
    catalog = tmp_path / "race.sqlite"
    con = sqlite3.connect(catalog, isolation_level=None)
    registration.backup(con)
    con.execute(f"PRAGMA user_version={db._SCHEMA_VERSION}")
    other = sqlite3.connect(catalog, isolation_level=None, timeout=0)
    monkeypatch.setattr(db, "DB_PATH", catalog)
    archive = tmp_path / "archive"
    archive.mkdir()
    sentinel = archive / "keep.bin"
    sentinel.write_bytes(b"disposable unclaimed bytes remain unchanged")
    fingerprint = identity_fingerprint_v1(
        fs_uuid="fs", annex_uuid="annex", serial="SERIAL", filesystem_capacity_bytes=1000)
    live = bs._LiveEvidence(str(archive), "fs", "annex", "SERIAL", 1000, 800, 1,
                            fingerprint, True)
    monkeypatch.setattr(bs, "_live_evidence", lambda *args: live)
    monkeypatch.setattr(bs.register, "archive_path", lambda *args: archive)
    before = tuple(con.iterdump())
    lost_states = []

    def lose():
        assert not con.in_transaction and not other.in_transaction
        # Reconciliation owns its prospective physical fence, but first-time
        # registration has no persisted physical identity for loss to lock.
        with pytest.raises(drive_fence.FenceUnavailable):
            with drive_fence.hold_drives_sorted([(fingerprint, 1)], blocking=False):
                pytest.fail("bootstrap must retain its prospective physical fence")
        assert declare(other, label)["lifecycle"] == "lost"
        lost_states.append(tuple(other.iterdump()))

    def progress(event):
        if phase == "inventory" and event.phase == "inventory_started":
            lose()

    if phase == "before_begin":
        original = dm._immediate

        def begin(c, body):
            lose()
            return original(c, body)

        monkeypatch.setattr(dm, "_immediate", begin)

    try:
        with pytest.raises(dm.DriveMutationRefused, match="DRIVE_RECOVERY_OWNER_CHANGED"):
            bs.reconcile_drive(con, label, now="2026-09-10T01:50:00Z", dedicated=True,
                               blocking=False, progress=progress)
        assert len(lost_states) == 1 and lost_states[0] != before
        assert tuple(con.iterdump()) == lost_states[0]
        assert con.execute("SELECT lifecycle,eligibility,identity_epoch,write_generation,"
                           "identity_fingerprint,filesystem_capacity_bytes,write_authority "
                           "FROM drives WHERE drive_label=?", [label]).fetchone() == (
                               "lost", "excluded", 1, 0, None, None, "unknown")
        assert con.execute("SELECT count(*) FROM drive_clean_anchors").fetchone() == (0,)
        assert con.execute("SELECT count(*) FROM drive_dirty_generations").fetchone() == (0,)
        assert con.execute("PRAGMA user_version").fetchone() == (db._SCHEMA_VERSION,)
        assert sentinel.read_bytes() == b"disposable unclaimed bytes remain unchanged"
        assert sorted(p.name for p in archive.iterdir()) == ["keep.bin"]
    finally:
        other.close()
        con.close()
