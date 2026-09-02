"""INC-054: live Fill occupancy uses atomic per-drive durable observations."""
from __future__ import annotations

import sqlite3
from unittest import mock

from modelark.web import fill_api


def _worker_with_total(label: str, total: int):
    worker = fill_api.fill_worker.FillWorker()
    generation = worker.mark_archive_changed(label)
    assert worker.confirm_archive_total(label, generation, total) is True
    return worker


def test_stored_event_records_exact_durable_total_without_mutating_input():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)")
    con.executemany(
        "INSERT INTO archived VALUES(?,?)",
        [("drive-00", 2_000_000_000), ("drive-00", 500_000_000), ("drive-07", None)],
    )
    event = {
        "file_phase": "stored",
        "drive": "drive-00",
        "done_by_drive": {"drive-00": 500_000_000},
    }
    worker = _worker_with_total("drive-07", 7)

    with mock.patch.object(fill_api.data, "conn", return_value=con):
        fill_api._observe_archive_change(worker, event)

    assert "archived_by_drive" not in event
    assert event["done_by_drive"] == {"drive-00": 500_000_000}
    assert worker.status()["archived_by_drive"] == {
        "drive-00": 2_500_000_000,
        "drive-07": 7,
    }
    assert worker.status()["archived_by_drive_current"] is True
    assert worker.status()["archived_stale_drives"] == []


def test_non_stored_event_does_not_query_or_change_archive_state():
    worker = fill_api.fill_worker.FillWorker()
    event = {"file_phase": "download", "done_by_drive": {"drive-00": 1}}
    with mock.patch.object(fill_api.data, "conn") as connection:
        fill_api._observe_archive_change(worker, event)
    connection.assert_not_called()
    assert "archived_by_drive" not in worker.status()

    precommit_raw = {"file_phase": "stored", "drive": "drive-00", "codec": "raw"}
    with mock.patch.object(fill_api.data, "conn") as connection:
        fill_api._observe_archive_change(worker, precommit_raw)
    connection.assert_not_called()
    assert "archived_by_drive" not in worker.status()


def test_telemetry_failure_keeps_last_confirmed_total_and_marks_only_that_drive_stale():
    worker = _worker_with_total("drive-00", 2_000_000_000)
    event = {
        "file_phase": "stored", "drive": "drive-00",
        "done_by_drive": {"drive-00": 1},
    }

    with mock.patch.object(fill_api.data, "conn", side_effect=RuntimeError("catalog unavailable")):
        fill_api._observe_archive_change(worker, event)

    status = worker.status()
    assert status["archived_by_drive"] == {"drive-00": 2_000_000_000}
    assert status["archived_by_drive_current"] is False
    assert status["archived_stale_drives"] == ["drive-00"]


def test_status_retry_persists_recovered_total_and_does_not_repeat():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)")
    con.execute("INSERT INTO archived VALUES(?,?)", ("drive-00", 3_000_000_000))
    worker = _worker_with_total("drive-00", 2_000_000_000)
    worker.mark_archive_changed("drive-00")

    with mock.patch.object(fill_api.data, "conn", return_value=con):
        recovered = fill_api._refresh_worker_archived_totals(worker)

    assert recovered["archived_by_drive"] == {"drive-00": 3_000_000_000}
    assert recovered["archived_by_drive_current"] is True
    assert recovered["archived_stale_drives"] == []
    with mock.patch.object(fill_api.data, "conn") as connection:
        again = fill_api._refresh_worker_archived_totals(worker)
    connection.assert_not_called()
    assert again["archived_by_drive"] == {"drive-00": 3_000_000_000}


def test_older_same_drive_read_cannot_overwrite_a_newer_generation():
    worker = _worker_with_total("drive-00", 2)
    old_generation = worker.mark_archive_changed("drive-00")
    new_generation = worker.mark_archive_changed("drive-00")

    assert worker.confirm_archive_total("drive-00", new_generation, 4) is True
    assert worker.confirm_archive_total("drive-00", old_generation, 3) is False
    assert worker.status()["archived_by_drive"] == {"drive-00": 4}
    assert worker.status()["archived_stale_drives"] == []


def test_drive_event_cannot_restore_another_drives_stale_snapshot():
    """Greptile iteration 6: B finishes after A's recovery and must patch only B."""
    worker = _worker_with_total("drive-00", 2)
    drive_a_generation = worker.mark_archive_changed("drive-00")
    drive_b_generation = worker.mark_archive_changed("drive-04")

    assert worker.confirm_archive_total("drive-00", drive_a_generation, 3) is True
    assert worker.confirm_archive_total("drive-04", drive_b_generation, 4) is True

    status = worker.status()
    assert status["archived_by_drive"] == {"drive-00": 3, "drive-04": 4}
    assert status["archived_by_drive_current"] is True
    assert status["archived_stale_drives"] == []


def test_generic_progress_cannot_replace_worker_owned_archive_evidence():
    worker = _worker_with_total("drive-00", 3)

    worker._emit({
        "phase": "replica",
        "archived_by_drive": {"drive-00": 2},
        "archived_by_drive_current": False,
        "archived_stale_drives": ["drive-00"],
    })

    status = worker.status()
    assert status["phase"] == "replica"
    assert status["archived_by_drive"] == {"drive-00": 3}
    assert status["archived_by_drive_current"] is True
    assert status["archived_stale_drives"] == []


def test_status_snapshot_cannot_mutate_worker_owned_archive_evidence():
    worker = _worker_with_total("drive-00", 3)

    snapshot = worker.status()
    snapshot["archived_by_drive"]["drive-00"] = 1
    snapshot["archived_stale_drives"].append("drive-00")

    assert worker.status()["archived_by_drive"] == {"drive-00": 3}
    assert worker.status()["archived_stale_drives"] == []


def test_replica_commit_refreshes_only_its_target_drive():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)")
    con.execute("INSERT INTO archived VALUES(?,?)", ("drive-04", 4_000_000_000))
    worker = _worker_with_total("drive-00", 2_000_000_000)
    event = {"phase": "replica", "drive": "drive-04", "archive_changed": True}

    with mock.patch.object(fill_api.data, "conn", return_value=con):
        fill_api._observe_archive_change(worker, event)

    assert worker.status()["archived_by_drive"] == {
        "drive-00": 2_000_000_000,
        "drive-04": 4_000_000_000,
    }
    assert worker.status()["archived_by_drive_current"] is True
    assert worker.status()["archived_stale_drives"] == []


def test_success_on_another_drive_does_not_clear_a_pending_stale_drive():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)")
    con.execute("INSERT INTO archived VALUES(?,?)", ("drive-04", 4_000_000_000))
    worker = _worker_with_total("drive-00", 2_000_000_000)
    drive_a_generation = worker.mark_archive_changed("drive-00")

    with mock.patch.object(fill_api.data, "conn", return_value=con):
        fill_api._observe_archive_change(
            worker,
            {"phase": "replica", "drive": "drive-04", "archive_changed": True},
        )

    assert worker.status()["archived_by_drive"] == {
        "drive-00": 2_000_000_000,
        "drive-04": 4_000_000_000,
    }
    assert worker.status()["archived_by_drive_current"] is False
    assert worker.status()["archived_stale_drives"] == ["drive-00"]

    assert worker.confirm_archive_total("drive-00", drive_a_generation, 3_000_000_000) is True
    assert worker.status()["archived_by_drive"] == {
        "drive-00": 3_000_000_000,
        "drive-04": 4_000_000_000,
    }
    assert worker.status()["archived_by_drive_current"] is True
    assert worker.status()["archived_stale_drives"] == []
