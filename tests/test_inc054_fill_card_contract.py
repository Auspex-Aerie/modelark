"""INC-054: live Fill occupancy uses post-commit durable totals, never overlapping deltas."""
from __future__ import annotations

import sqlite3
from unittest import mock

from modelark.web import fill_api


def test_stored_event_carries_exact_durable_totals_without_mutating_input():
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
    with mock.patch.object(fill_api.data, "conn", return_value=con):
        enriched = fill_api._attach_archived_totals(event, {"drive-07": 7})

    assert "archived_by_drive" not in event
    assert enriched["done_by_drive"] == event["done_by_drive"]
    assert enriched["archived_by_drive"] == {
        "drive-00": 2_500_000_000,
        "drive-07": 7,
    }
    assert enriched["archived_by_drive_current"] is True
    assert enriched["archived_stale_drives"] == []


def test_non_stored_event_does_not_query_or_change_shape():
    event = {"file_phase": "download", "done_by_drive": {"drive-00": 1}}
    with mock.patch.object(fill_api.data, "conn") as connection:
        assert fill_api._attach_archived_totals(event) is event
    connection.assert_not_called()

    precommit_raw = {"file_phase": "stored", "drive": "drive-00", "codec": "raw"}
    with mock.patch.object(fill_api.data, "conn") as connection:
        assert fill_api._attach_archived_totals(precommit_raw) is precommit_raw
    connection.assert_not_called()


def test_telemetry_enrichment_failure_never_fails_fill_progress():
    event = {
        "file_phase": "stored", "drive": "drive-00",
        "done_by_drive": {"drive-00": 1},
    }
    with mock.patch.object(fill_api.data, "conn", side_effect=RuntimeError("catalog unavailable")):
        enriched = fill_api._attach_archived_totals(event, {"drive-00": 2})

    assert "archived_by_drive" not in event
    assert enriched["archived_by_drive"] == {"drive-00": 2}
    assert enriched["archived_by_drive_current"] is False
    assert enriched["archived_stale_drives"] == ["drive-00"]
    assert isinstance(enriched["archived_snapshot_id"], int)


def test_failed_enrichment_marks_a_retained_snapshot_non_current():
    worker = fill_api.fill_worker.FillWorker()
    worker._emit({
        "archived_by_drive": {"drive-00": 2_000_000_000},
        "archived_by_drive_current": True,
        "archived_stale_drives": [],
    })
    event = {
        "file_phase": "stored", "drive": "drive-00",
        "done_by_drive": {"drive-00": 3_000_000_000},
    }

    with mock.patch.object(fill_api.data, "conn", side_effect=RuntimeError("catalog unavailable")):
        worker._emit(fill_api._attach_archived_totals(
            event,
            worker.status()["archived_by_drive"],
            worker.status()["archived_stale_drives"],
        ))

    status = worker.status()
    assert status["archived_by_drive"] == {"drive-00": 2_000_000_000}
    assert status["archived_by_drive_current"] is False
    assert status["archived_stale_drives"] == ["drive-00"]


def test_status_retry_replaces_last_confirmed_snapshot_after_recovery():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)")
    con.execute("INSERT INTO archived VALUES(?,?)", ("drive-00", 3_000_000_000))
    stale = {
        "archived_by_drive": {"drive-00": 2_000_000_000},
        "archived_by_drive_current": False,
        "archived_stale_drives": ["drive-00"],
        "archived_snapshot_id": 100,
    }

    with mock.patch.object(fill_api.data, "conn", return_value=con):
        refreshed = fill_api._refresh_stale_archived_totals(stale)

    assert stale["archived_by_drive"] == {"drive-00": 2_000_000_000}
    assert stale["archived_by_drive_current"] is False
    assert refreshed["archived_by_drive"] == {"drive-00": 3_000_000_000}
    assert refreshed["archived_by_drive_current"] is True
    assert refreshed["archived_stale_drives"] == []
    assert refreshed["archived_snapshot_id"] != 100


def test_status_retry_persists_and_does_not_repeat_or_regress_after_recovery():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)")
    con.execute("INSERT INTO archived VALUES(?,?)", ("drive-00", 3_000_000_000))
    worker = fill_api.fill_worker.FillWorker()
    worker._emit({
        "archived_by_drive": {"drive-00": 2_000_000_000},
        "archived_by_drive_current": False,
        "archived_stale_drives": ["drive-00"],
        "archived_snapshot_id": 200,
    })

    with mock.patch.object(fill_api.data, "conn", return_value=con):
        recovered = fill_api._refresh_worker_archived_totals(worker)

    assert recovered["archived_by_drive"] == {"drive-00": 3_000_000_000}
    assert recovered["archived_by_drive_current"] is True
    assert worker.status()["archived_by_drive_current"] is True
    with mock.patch.object(fill_api.data, "conn") as connection:
        again = fill_api._refresh_worker_archived_totals(worker)
    connection.assert_not_called()
    assert again["archived_by_drive"] == {"drive-00": 3_000_000_000}


def test_status_retry_cannot_overwrite_a_newer_worker_snapshot():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)")
    con.executemany(
        "INSERT INTO archived VALUES(?,?)",
        [("drive-00", 3), ("drive-04", 4)],
    )
    worker = fill_api.fill_worker.FillWorker()
    worker._emit({
        "archived_by_drive": {"drive-00": 2},
        "archived_by_drive_current": False,
        "archived_stale_drives": ["drive-00"],
        "archived_snapshot_id": 300,
    })

    with mock.patch.object(fill_api.data, "conn", return_value=con):
        recovered_a = fill_api._refresh_stale_archived_totals(worker.status())
        drive_b = fill_api._attach_archived_totals(
            {"phase": "replica", "drive": "drive-04", "archive_changed": True},
            worker.status()["archived_by_drive"],
            worker.status()["archived_stale_drives"],
        )
    worker._emit(drive_b)

    applied = worker.compare_and_update(
        {"archived_snapshot_id": 300},
        {
            "archived_by_drive": recovered_a["archived_by_drive"],
            "archived_by_drive_current": recovered_a["archived_by_drive_current"],
            "archived_stale_drives": recovered_a["archived_stale_drives"],
            "archived_snapshot_id": recovered_a["archived_snapshot_id"],
        },
    )

    assert applied is False
    assert worker.status()["archived_by_drive"] == {"drive-00": 2, "drive-04": 4}
    assert worker.status()["archived_by_drive_current"] is False
    assert worker.status()["archived_stale_drives"] == ["drive-00"]


def test_replica_commit_refreshes_only_its_target_drive():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)")
    con.execute("INSERT INTO archived VALUES(?,?)", ("drive-04", 4_000_000_000))
    event = {"phase": "replica", "drive": "drive-04", "archive_changed": True}

    with mock.patch.object(fill_api.data, "conn", return_value=con):
        enriched = fill_api._attach_archived_totals(event, {"drive-00": 2_000_000_000})

    assert enriched["archived_by_drive"] == {
        "drive-00": 2_000_000_000,
        "drive-04": 4_000_000_000,
    }
    assert enriched["archived_by_drive_current"] is True
    assert enriched["archived_stale_drives"] == []


def test_success_on_another_drive_does_not_clear_a_pending_stale_drive():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)")
    con.execute("INSERT INTO archived VALUES(?,?)", ("drive-04", 4_000_000_000))
    event = {"phase": "replica", "drive": "drive-04", "archive_changed": True}

    with mock.patch.object(fill_api.data, "conn", return_value=con):
        enriched = fill_api._attach_archived_totals(
            event,
            {"drive-00": 2_000_000_000},
            ["drive-00"],
        )

    assert enriched["archived_by_drive"] == {
        "drive-00": 2_000_000_000,
        "drive-04": 4_000_000_000,
    }
    assert enriched["archived_by_drive_current"] is False
    assert enriched["archived_stale_drives"] == ["drive-00"]
