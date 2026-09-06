"""INC-063: Fill cards follow the admitted execution, not a fresh advisory replan."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from modelark.execution_projection import ExecutionProjection, canonical_projection_hash
from modelark.web import execution_view, fill_api, fill_worker


def _task(requirement_id, repo, target, *, kind="executable", source=None, size=10):
    return {
        "requirement_id": requirement_id,
        "row_kind": kind,
        "repo_id": repo,
        "target_drive": target,
        "satisfying_drive": target if kind == "baseline_satisfied" else None,
        "source_drive": source,
        "guaranteed_durable": size,
    }


def _session_start():
    baseline = _task(
        "primary:old", "org/old", "drive-00",
        kind="baseline_satisfied", size=100,
    )
    baseline_replica = _task(
        "replica:old", "org/old", "drive-04",
        kind="baseline_satisfied", size=100,
    )
    drive_zero = _task("primary:new", "org/new", "drive-00", size=20)
    drive_seven = _task("primary:large", "org/large", "drive-07", size=30)
    landed = _task("primary:landed", "org/landed", "drive-07", size=40)
    projected = (drive_zero, drive_seven)
    projection = ExecutionProjection(
        proposal_id="proposal-11",
        tasks=projected,
        projection_hash=canonical_projection_hash(projected),
    )
    start = SimpleNamespace(
        session=SimpleNamespace(session_id="session-11", bound_planner_revision=11),
        projection=projection,
    )
    start._proposal = {
        "proposal_id": "proposal-11",
        "tasks": [baseline, baseline_replica, drive_zero, drive_seven, landed],
    }
    return start


def _drives(status):
    return {row["label"]: row for row in status["execution"]["drives"]}


def test_build_preserves_approved_baseline_and_admitted_projection_counts():
    view = execution_view.build(_session_start())
    drives = {row["label"]: row for row in view["drives"]}

    assert view["authority"] == "approved_execution"
    assert view["proposal_id"] == "proposal-11"
    assert view["session_id"] == "session-11"
    assert view["bound_revision"] == 11
    assert view["batch_order"] == ["drive-00", "drive-07"]
    assert view["totals"] == {
        "approved_requirements": 5,
        "baseline_satisfied": 2,
        "approved_executable": 3,
        "remaining_at_start": 2,
        "satisfied_since_approval": 1,
    }
    assert drives["drive-00"]["baseline_satisfied"] == 1
    assert drives["drive-00"]["tier"] == "raid"
    assert drives["drive-04"]["tier"] == "replica"
    assert drives["drive-07"]["tier"] == "primary"
    assert drives["drive-00"]["approved_requirements"] == 2
    assert drives["drive-00"]["remaining_at_start"] == 1
    assert drives["drive-00"]["remaining_guaranteed_bytes"] == 20
    assert drives["drive-07"]["satisfied_since_approval"] == 1
    assert [model["repo"] for model in drives["drive-07"]["models"]] == ["org/large"]


def test_build_decodes_stored_primary_replica_vocabulary_for_card_grouping():
    home = _task("primary:protected", "org/protected", "drive-00")
    replica = _task("replica:protected", "org/protected", "drive-04")
    projection = ExecutionProjection(
        proposal_id="proposal-protected",
        tasks=(home, replica),
        projection_hash=canonical_projection_hash((home, replica)),
    )
    start = SimpleNamespace(
        session=SimpleNamespace(session_id="session-protected", bound_planner_revision=12),
        projection=projection,
        _proposal={"proposal_id": "proposal-protected", "tasks": [home, replica]},
    )

    drives = {row["label"]: row for row in execution_view.build(start)["drives"]}

    assert drives["drive-00"]["tier"] == "raid"
    assert drives["drive-00"]["models"][0]["copy"] == "1"
    assert drives["drive-04"]["tier"] == "replica"
    assert drives["drive-04"]["models"][0]["copy"] == "2"


def test_execution_drive_facts_are_read_as_one_physical_snapshot(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.execute(
        "CREATE TABLE drives(drive_label TEXT PRIMARY KEY, role TEXT, raid_backed INTEGER, "
        "capacity_bytes INTEGER, lifecycle TEXT, eligibility TEXT)"
    )
    con.execute(
        "CREATE TABLE archived(drive_label TEXT, stored_bytes INTEGER)"
    )
    con.executemany(
        "INSERT INTO drives VALUES(?,?,?,?,?,?)",
        [
            ("drive-00", "primary", 1, 1_000, "active", "enabled"),
            ("drive-04", "replica", 0, 2_000, "retired", "excluded"),
            ("outside", "primary", 0, 3_000, "active", "enabled"),
        ],
    )
    con.executemany(
        "INSERT INTO archived VALUES(?,?)",
        [("drive-00", 60), ("drive-00", 40), ("outside", 500)],
    )
    monkeypatch.setattr(fill_api.data, "conn", lambda: con)

    facts = fill_api._read_execution_drive_facts(("drive-00", "drive-04"))

    assert set(facts) == {"drive-00", "drive-04"}
    assert facts["drive-00"] == {
        "role": "primary", "raid_backed": True, "capacity_bytes_at_start": 1_000,
        "lifecycle_at_start": "active", "eligibility_at_start": "enabled",
        "archived_bytes_at_start": 100,
    }
    assert facts["drive-04"]["archived_bytes_at_start"] == 0


def test_runtime_labels_every_drive_and_keeps_skipped_access_visible():
    plan = execution_view.build(_session_start())

    running = execution_view.with_runtime_state({
        "status": "running", "drive": "drive-07", "execution_plan": plan,
        "notice": {
            "id": "access-gated:org/large:skip", "type": "access-gated",
            "repo": "org/large",
        },
    })
    running_drives = _drives(running)
    assert "execution_plan" not in running
    assert running_drives["drive-00"]["state"] == "approved_remaining"
    assert running_drives["drive-07"]["state"] == "writing"
    assert running_drives["drive-07"]["access_followups"] == ["org/large"]

    waiting = execution_view.with_runtime_state({
        "status": "running", "drive": "drive-07", "awaiting_drive": "drive-00",
        "execution_plan": plan,
    })
    assert _drives(waiting)["drive-00"]["state"] == "waiting_for_drive"

    terminal = execution_view.with_runtime_state({
        "status": "done", "code": "PLAN_COMPLETE_WITH_FOLLOWUPS",
        "evidence": {"access_gated": ["org/large"]},
        "execution_completed_requirements": ["primary:new"], "execution_plan": plan,
    })
    assert _drives(terminal)["drive-00"]["state"] == "complete"
    assert _drives(terminal)["drive-07"]["state"] == "access_followup"

    mixed_followups = execution_view.with_runtime_state({
        "status": "done", "code": "PLAN_COMPLETE_WITH_FOLLOWUPS",
        "evidence": {
            "content_refusals": [{"repo_id": "org/new"}],
            "waiting_requirements": ["primary:large"],
        },
        "execution_plan": plan,
    })
    assert _drives(mixed_followups)["drive-00"]["state"] == "access_followup"
    assert _drives(mixed_followups)["drive-07"]["state"] == "waiting_dependency"


def test_runtime_maps_typed_drive_stops_without_reassigning_other_drives():
    plan = execution_view.build(_session_start())
    stopped = execution_view.with_runtime_state({
        "status": "plan-capacity-stop", "drive": "drive-07",
        "code": "PLAN_CAPACITY_STOP", "execution_plan": plan,
    })

    drives = _drives(stopped)
    assert drives["drive-00"]["state"] == "approved_remaining"
    assert drives["drive-07"]["state"] == "capacity_stop"
    assert drives["drive-00"]["models"][0]["repo"] == "org/new"
    assert drives["drive-07"]["models"][0]["repo"] == "org/large"

    failed = execution_view.with_runtime_state({
        "status": "failed", "drive": "drive-07",
        "code": "PROJECTION_REFRESH_FAILED", "execution_plan": plan,
    })
    assert _drives(failed)["drive-07"]["state"] == "error"

    source_offline = execution_view.with_runtime_state({
        "status": "paused", "drive": "drive-04", "awaiting_drive": "drive-00",
        "code": "SOURCE_UNAVAILABLE",
        "evidence": {
            "source_offline": True, "deferred_sources": ["drive-00"],
            "deferred_targets": [],
        },
        "execution_plan": plan,
    })
    assert _drives(source_offline)["drive-00"]["state"] == "waiting_for_drive"
    assert _drives(source_offline)["drive-04"]["state"] == "satisfied"

    both_endpoints = execution_view.with_runtime_state({
        "status": "paused", "drive": "drive-04", "awaiting_drive": "drive-04",
        "code": "SOURCE_UNAVAILABLE",
        "evidence": {
            "source_offline": True, "deferred_sources": ["drive-00"],
            "deferred_targets": ["drive-04"],
        },
        "execution_plan": plan,
    })
    assert _drives(both_endpoints)["drive-00"]["state"] == "waiting_for_drive"
    assert _drives(both_endpoints)["drive-04"]["state"] == "waiting_for_drive"


def test_runtime_maps_dependency_evidence_and_ignores_stale_drive_while_awaiting():
    plan = execution_view.build(_session_start())

    waiting_drive = execution_view.with_runtime_state({
        "status": "running", "drive": "drive-00", "awaiting_drive": "drive-07",
        "execution_plan": plan,
    })
    assert _drives(waiting_drive)["drive-00"]["state"] == "approved_remaining"
    assert _drives(waiting_drive)["drive-07"]["state"] == "waiting_for_drive"

    dependency = execution_view.with_runtime_state({
        "status": "paused", "drive": "drive-00", "code": "WAITING_DEPENDENCY",
        "evidence": {"requirements": ["primary:large"]}, "execution_plan": plan,
    })
    assert _drives(dependency)["drive-00"]["state"] == "approved_remaining"
    assert _drives(dependency)["drive-07"]["state"] == "waiting_dependency"


def test_runtime_uses_durable_completion_progress_and_preserves_throttle_semantics():
    plan = execution_view.build(_session_start())
    advanced = execution_view.with_runtime_state({
        "status": "running", "drive": "drive-07",
        "execution_completed_requirements": ["primary:new"], "execution_plan": plan,
    })
    assert _drives(advanced)["drive-00"]["state"] == "complete"
    assert "execution_completed_requirements" not in advanced
    assert _drives(advanced)["drive-00"]["completed_in_run"] == 1
    assert advanced["execution"]["totals"]["completed_in_run"] == 1
    assert advanced["execution"]["totals"]["unresolved"] == 1

    throttled = execution_view.with_runtime_state({
        "status": "paused", "drive": "drive-07", "code": "DOWNLOAD_THROTTLED",
        "execution_plan": plan,
    })
    assert _drives(throttled)["drive-07"]["state"] == "download_throttled"
    assert _drives(throttled)["drive-07"]["state_label"] == "Download cap reached"


def test_worker_owns_bound_execution_plan_for_one_run():
    worker = fill_worker.FillWorker()
    supplied = {
        "session_id": "s",
        "drives": [{
            "label": "drive-07",
            "models": [{"requirement_id": "primary:large"}],
        }],
    }
    release = threading.Event()

    assert worker.start(
        lambda _should_stop, _emit: release.wait(),
        initial_state={"execution_plan": supplied, "status": "forged"},
    ) == {"ok": True}
    supplied["drives"][0]["label"] = "mutated-outside"
    worker._emit({"execution_plan": {"session_id": "forged"}})
    worker._emit({"execution_completed_requirements": ["forged", "primary:large"]})
    snapshot = worker.status()
    snapshot["execution_plan"]["drives"][0]["label"] = "mutated-snapshot"

    assert worker.status()["status"] == "running"
    assert worker.status()["execution_plan"]["session_id"] == "s"
    assert worker.status()["execution_plan"]["drives"][0]["label"] == "drive-07"
    assert worker.status()["execution_completed_requirements"] == ["primary:large"]
    worker._emit({"execution_completed_requirements": []})
    assert worker.status()["execution_completed_requirements"] == ["primary:large"]
    release.set()
    worker._thread.join()


def test_fill_start_binds_the_execution_view_before_worker_launch(monkeypatch):
    start = _session_start()
    captured = {}

    class Worker:
        def start(self, work, *, initial_state=None):
            captured["work"] = work
            captured["initial_state"] = initial_state
            return {"ok": True}

    from modelark import execution_service

    monkeypatch.setattr(execution_service, "start_fill", lambda **_kwargs: start)
    monkeypatch.setattr(fill_api.fill_worker, "WORKER", Worker())
    monkeypatch.setattr(fill_api, "_read_execution_drive_facts", lambda _labels: {
        "drive-00": {
            "role": "primary", "raid_backed": False, "capacity_bytes_at_start": 1_000,
            "lifecycle_at_start": "active", "eligibility_at_start": "enabled",
            "archived_bytes_at_start": 90,
        },
        "drive-04": {
            "role": "replica", "raid_backed": False, "capacity_bytes_at_start": 2_000,
            "lifecycle_at_start": "active", "eligibility_at_start": "enabled",
            "archived_bytes_at_start": 40,
        },
        # This target carries only an ordinary bulk requirement. Its physical RAID identity must
        # win over the task kind when the advisory endpoint is unavailable.
        "drive-07": {
            "role": "primary", "raid_backed": True, "capacity_bytes_at_start": 8_000,
            "lifecycle_at_start": "active", "eligibility_at_start": "enabled",
            "archived_bytes_at_start": 70,
        },
    })
    monkeypatch.setattr(fill_api.wishlist, "download", lambda: {"max_24h_gb": 0})
    monkeypatch.setattr(fill_api.data, "conn", lambda: object())

    assert fill_api.start({}) == {"ok": True}
    assert callable(captured["work"])
    assert captured["initial_state"]["execution_plan"]["session_id"] == "session-11"
    assert captured["initial_state"]["execution_plan"]["totals"]["baseline_satisfied"] == 2
    bound_drives = {
        row["label"]: row for row in captured["initial_state"]["execution_plan"]["drives"]
    }
    assert bound_drives["drive-00"]["archived_bytes_at_start"] == 90
    assert bound_drives["drive-07"]["archived_bytes_at_start"] == 70
    assert bound_drives["drive-07"]["tier"] == "raid"
    assert bound_drives["drive-07"]["raid_backed"] is True
    assert bound_drives["drive-07"]["capacity_bytes_at_start"] == 8_000
    assert all(row["drive_metadata_bound"] for row in bound_drives.values())


def test_successful_followup_terminal_preserves_evidence_for_exact_drive_states(monkeypatch):
    start = _session_start()
    captured = {}

    class Worker:
        def start(self, work, *, initial_state=None):
            captured["work"] = work
            captured["initial_state"] = initial_state
            return {"ok": True}

        def await_action(self, _prompt, _timeout):
            return "skip"

    from modelark import execution_service

    followup = {
        "ok": True,
        "stopped": False,
        "state": "done",
        "message": "fill complete with operator follow-ups",
        "code": "PLAN_COMPLETE_WITH_FOLLOWUPS",
        "gate": "C",
        "evidence": {
            "content_refusals": [{"repo_id": "org/new"}],
            "waiting_requirements": ["primary:large"],
        },
        "actions": ["review_followups", "start_fill"],
    }
    monkeypatch.setattr(execution_service, "start_fill", lambda **_kwargs: start)
    monkeypatch.setattr(fill_api.fill_worker, "WORKER", Worker())
    monkeypatch.setattr(fill_api, "_read_execution_drive_facts", lambda labels: {
        label: {
            "role": "primary", "raid_backed": False, "capacity_bytes_at_start": None,
            "lifecycle_at_start": "active", "eligibility_at_start": "enabled",
            "archived_bytes_at_start": 0,
        }
        for label in labels
    })
    monkeypatch.setattr(fill_api.wishlist, "download", lambda: {"max_24h_gb": 0})
    monkeypatch.setattr(fill_api.data, "conn", lambda: object())
    monkeypatch.setattr(fill_api.fill, "execute", lambda *_args, **_kwargs: followup)
    monkeypatch.setattr(fill_api, "_persist_terminal", lambda terminal: captured.setdefault(
        "persisted", terminal,
    ))

    assert fill_api.start({}) == {"ok": True}
    terminal = captured["work"](lambda: False, lambda _event: None)

    assert terminal["evidence"] == followup["evidence"]
    assert terminal["actions"] == followup["actions"]
    assert terminal["gate"] == "C"
    public = execution_view.with_runtime_state({
        **terminal,
        "execution_plan": captured["initial_state"]["execution_plan"],
    })
    assert _drives(public)["drive-00"]["state"] == "access_followup"
    assert _drives(public)["drive-07"]["state"] == "waiting_dependency"


def test_fill_status_publishes_runtime_view_without_internal_worker_key(monkeypatch):
    plan = execution_view.build(_session_start())
    monkeypatch.setattr(
        fill_api, "_refresh_worker_archived_totals",
        lambda _worker: {"status": "running", "drive": "drive-07", "execution_plan": plan},
    )
    monkeypatch.setattr(fill_api, "_rx_bytes", lambda: None)

    status = fill_api.status()
    assert "execution_plan" not in status
    assert _drives(status)["drive-07"]["state_label"] == "Writing now"


def test_fill_javascript_switches_cards_to_exact_execution_evidence():
    source = Path("modelark/web/static/fill.js").read_text()

    assert "displayData(data, lastStatus)" in source
    assert "exact.remaining_at_start" in source
    assert "exact.baseline_satisfied" in source
    assert "approved execution workload at Fill start" in source
    assert "Planning view · current fleet forecast" in source
    assert "displayEnvelope" in source
    assert "rerenderDisplayEnvelope()" in source
    assert "if (s && s.drive && !s.execution)" in source
    assert "view.drives.map(exact" in source
    assert "admitted work items completed this run" in source
