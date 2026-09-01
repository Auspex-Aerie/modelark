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
        "done_by_drive": {"drive-00": 500_000_000},
    }
    with mock.patch.object(fill_api.data, "conn", return_value=con):
        enriched = fill_api._attach_archived_totals(event)

    assert "archived_by_drive" not in event
    assert enriched["done_by_drive"] == event["done_by_drive"]
    assert enriched["archived_by_drive"] == {
        "drive-00": 2_500_000_000,
        "drive-07": 0,
    }


def test_non_stored_event_does_not_query_or_change_shape():
    event = {"file_phase": "download", "done_by_drive": {"drive-00": 1}}
    with mock.patch.object(fill_api.data, "conn") as connection:
        assert fill_api._attach_archived_totals(event) is event
    connection.assert_not_called()


def test_telemetry_enrichment_failure_never_fails_fill_progress():
    event = {"file_phase": "stored", "done_by_drive": {"drive-00": 1}}
    with mock.patch.object(fill_api.data, "conn", side_effect=RuntimeError("catalog unavailable")):
        enriched = fill_api._attach_archived_totals(event)

    assert "archived_by_drive" not in event
    assert enriched == {**event, "archived_by_drive": None}


def test_failed_enrichment_clears_a_snapshot_retained_by_the_worker():
    worker = fill_api.fill_worker.FillWorker()
    worker._emit({"archived_by_drive": {"drive-00": 2_000_000_000}})
    event = {"file_phase": "stored", "done_by_drive": {"drive-00": 3_000_000_000}}

    with mock.patch.object(fill_api.data, "conn", side_effect=RuntimeError("catalog unavailable")):
        worker._emit(fill_api._attach_archived_totals(event))

    assert worker.status()["archived_by_drive"] is None
