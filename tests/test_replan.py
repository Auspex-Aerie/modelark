"""Re-plan-per-drive-batch + the drive write-probe (the INC drive-01 hit: a fill marching off a
half-empty NAS, and a mounted-but-dead drive getting its whole assignment silently skipped).

Covers:
  • _writable          — probes real writability (mounted != healthy); OSError → False.
  • _await_drive       — a mounted-but-UNWRITABLE drive is awaited, never accepted (no silent skip).
  • execute() re-plan  — the primary tier re-plans each pass, fills the priority drive first, then
                         advances, then does replica copy #2, and TERMINATES (GATE-C ok).
  • loop-guard         — a repo that never places is blocked after _MAX_REPO_ATTEMPTS, so one bad
                         repo can't spin the loop forever.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest import mock

from modelark.core import db
from modelark import capacity, fetch, fill, plan, reconcile

# ---- the write-probe --------------------------------------------------------------------------

def test_writable_probe(tmp_path):
    ctx = fetch.RunCtx(con=None)
    with mock.patch.object(fill.register, "archive_path", side_effect=lambda con, l: tmp_path):
        assert fill._writable(ctx, "drive-00") is True                 # real writable dir
        assert not (tmp_path / fill._PROBE_NAME).exists(), "probe file must be cleaned up"
    missing = tmp_path / "gone" / "deeper"                             # parent absent → write raises → False
    with mock.patch.object(fill.register, "archive_path", side_effect=lambda con, l: missing):
        assert fill._writable(ctx, "drive-00") is False
    with mock.patch.object(fill.register, "archive_path", side_effect=lambda con, l: None):
        assert fill._writable(ctx, "drive-00") is False               # unmounted → False


# ---- _await_drive: never accept a mounted-but-dead drive ------------------------------------

def test_await_drive_accepts_writable(tmp_path):
    ctx = fetch.RunCtx(con=None)
    with mock.patch.object(fill, "_mounted", side_effect=lambda c, l: (True, True)), \
         mock.patch.object(fill, "_writable", side_effect=lambda c, l: True):
        assert fill._await_drive(ctx, "drive-01", 0.01) is True


def test_await_drive_awaits_unwritable(tmp_path):
    stop = {"v": False}
    def writable(c, l):
        stop["v"] = True                                              # after the first probe, ask to stop so the wait ends
        return False
    ev = []
    ctx = fetch.RunCtx(con=None, should_stop=lambda: stop["v"], on_progress=ev.append)
    with mock.patch.object(fill, "_mounted", side_effect=lambda c, l: (True, True)), \
         mock.patch.object(fill, "_writable", side_effect=writable), \
         mock.patch.object(fill.time, "sleep", side_effect=lambda s: None):
        res = fill._await_drive(ctx, "drive-01", 0.01)
    assert res is False, "a mounted-but-unwritable drive must NOT be accepted"
    assert any(e.get("awaiting_drive") == "drive-01" for e in ev), "should prompt to re-seat, not silently skip"


# ---- reconciled execute loop -----------------------------------------------------------------

def _mem():
    con = sqlite3.connect(":memory:", isolation_level=None)
    for stmt in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(stmt)
    return con


def _executor_harness(*, failed=()):
    con = _mem()
    con.execute("INSERT INTO plans(plan_id,name,is_active) VALUES('ark','Ark',1)")
    for label, role, raid, size in (
        ("drive-00", "primary", 1, 1_000),
        ("drive-01", "primary", 0, 1_000),
        ("drive-04", "replica", 0, 2_000),
    ):
        con.execute(
            "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes,annex_uuid) "
            "VALUES(?,?,?,?,?,?)", [label, role, raid, size, size, f"uuid-{label}"],
        )
        con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])
    for repo, copies, size in (("a", 1, 300), ("b", 1, 600), ("must", 2, 200)):
        con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,?)", [repo, copies])
        con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-01-01')", [repo])
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
            "VALUES(?,'model.gguf',?,'gguf',NULL)", [repo, size],
        )
    calls = []

    def fake_run(*, drive_label, repos, task_manifests, **kwargs):
        calls.append(("fetch", drive_label, list(repos)))
        stored, bad = [], []
        for repo in repos:
            if repo in failed:
                bad.append(repo)
                continue
            for item in task_manifests[repo]:
                con.execute(
                    "INSERT OR IGNORE INTO archived"
                    "(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed,annex_key) "
                    "VALUES(?,?,?,?,?,0,?)",
                    [repo, item.rfilename, drive_label, item.size_bytes, item.size_bytes,
                     f"key-{repo}-{item.rfilename}"],
                )
            stored.append(repo)
        return {"stored_repos": stored, "failed_repos": bad, "capacity_failure": None,
                "terminal_failure": None, "terminal_repo": None, "throttled": False,
                "stopped": False, "drive_unwritable": False}

    def fake_replica(tasks, ctx=None):
        calls.append(("replica", tasks[0].target_drive, [task.repo_id for task in tasks]))
        copied = 0
        for task in tasks:
            for name in task.budget.missing_files:
                row = con.execute(
                    "SELECT orig_bytes,stored_bytes,compressed,annex_key FROM archived "
                    "WHERE repo_id=? AND rfilename=? AND drive_label=?",
                    [task.repo_id, name, task.source_drive],
                ).fetchone()
                con.execute(
                    "INSERT OR IGNORE INTO archived"
                    "(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed,annex_key) "
                    "VALUES(?,?,?,?,?,?,?)", [task.repo_id, name, task.target_drive, *row],
                )
                copied += 1
        return {"deferred": False, "source_offline": False, "deferred_targets": [],
                "copied_targets": [tasks[0].target_drive], "copied_files": copied, "failed": []}

    return con, calls, fake_run, fake_replica


def test_reconciled_executor_drains_drive_batches_then_replica():
    con, calls, fake_run, fake_replica = _executor_harness()
    with mock.patch.object(fill.fetch, "run", side_effect=fake_run), \
         mock.patch.object(fill.fetch, "run_replica_tasks", side_effect=fake_replica), \
         mock.patch.object(fill, "_await_drive", return_value=True):
        result = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    assert result["ok"], result
    assert [kind for kind, _, _ in calls] == ["fetch", "fetch", "replica"], calls
    assert calls[0][1] == "drive-00" and calls[1][1] == "drive-01", calls
    assert con.execute("SELECT count(*) FROM archived").fetchone()[0] == 4


def test_satisfied_home_copy_is_not_reserved_or_fetched_again():
    con, calls, fake_run, fake_replica = _executor_harness()
    con.execute(
        "INSERT INTO archived"
        "(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed,annex_key) "
        "VALUES('must','model.gguf','drive-00',200,200,0,'key-must-model.gguf')"
    )
    with mock.patch.object(fill.fetch, "run", side_effect=fake_run), \
         mock.patch.object(fill.fetch, "run_replica_tasks", side_effect=fake_replica), \
         mock.patch.object(fill, "_await_drive", return_value=True):
        result = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    assert result["ok"], result
    assert all("must" not in repos for kind, _, repos in calls if kind == "fetch"), calls
    assert any(kind == "replica" and "must" in repos for kind, _, repos in calls), calls
    assert con.execute(
        "SELECT count(*) FROM archived WHERE repo_id='must' AND drive_label='drive-00'"
    ).fetchone()[0] == 1


def test_bulk_fetch_always_ranks_before_partial_replica():
    con, _, _, _ = _executor_harness()
    con.execute(
        "INSERT INTO archived"
        "(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed,annex_key) "
        "VALUES('must','model.gguf','drive-00',200,200,0,'key-must-model.gguf')"
    )
    snapshot = fill._reconcile(fetch.RunCtx(con=con), "ark", "guaranteed", None)
    fetch_tasks = [task for task in snapshot.ledger.tasks if task.kind == reconcile.TaskKind.FETCH]
    replica_tasks = [task for task in snapshot.ledger.tasks if task.kind == reconcile.TaskKind.REPLICATE]
    assert fetch_tasks and replica_tasks
    assert max(capacity.execution_rank(task, snapshot.graph)[0] for task in fetch_tasks) < min(
        capacity.execution_rank(task, snapshot.graph)[0] for task in replica_tasks
    )


def test_reconcile_uses_and_closes_dedicated_read_connection():
    con, _, _, _ = _executor_harness()

    class ReadConnection:
        closed = False

        def close(self):
            self.closed = True

    read_con = ReadConnection()
    graph = object()
    ledger = object()
    ctx = fetch.RunCtx(con=con, read_connection_factory=lambda: read_con)
    with mock.patch.object(fill.reconcile, "reconcile_plan", return_value=graph) as reconcile_plan, \
         mock.patch.object(fill.capacity, "plan_capacity", return_value=ledger) as plan_capacity:
        snapshot = fill._reconcile(ctx, "ark", "guaranteed", None)
    assert snapshot == fill._Snapshot(graph, ledger)
    reconcile_plan.assert_called_once_with(read_con, "ark", None)
    assert plan_capacity.call_args.args[:2] == (read_con, graph)
    assert read_con.closed


def test_file_guard_types_a_target_removed_after_reconciliation(tmp_path):
    con, _, _, _ = _executor_harness()
    ctx = fetch.RunCtx(con=con)
    snapshot = fill._reconcile(ctx, "ark", "guaranteed", None)
    task = next(item for item in snapshot.ledger.tasks if item.kind == reconcile.TaskKind.FETCH)
    manifest_item = next(
        item for item in snapshot.graph.manifests[task.repo_id]
        if item.rfilename in task.budget.missing_files
    )
    guard = fill._file_guard(ctx, "ark", "guaranteed", task)

    for archive_path in (None, tmp_path):
        with mock.patch.object(fill.register, "archive_path", return_value=archive_path), \
             mock.patch.object(fill.capacity, "inspect_drives", return_value=()):
            try:
                guard(task.repo_id, manifest_item)
                raise AssertionError("a stale target must be a typed re-plan boundary")
            except fetch.CapacityPreflightError as exc:
                failure = exc.failure
        assert failure.code == capacity.FailureCode.TARGET_DRIVE_CHANGED
        assert failure.requirement_id == task.requirement_id
        assert failure.actions == ("reconcile_plan", "restore_target_drive_to_plan")


def test_reconciled_executor_bounds_failed_task_retries():
    con, calls, fake_run, fake_replica = _executor_harness(failed={"b"})
    with mock.patch.object(fill.fetch, "run", side_effect=fake_run), \
         mock.patch.object(fill.fetch, "run_replica_tasks", side_effect=fake_replica), \
         mock.patch.object(fill, "_await_drive", return_value=True):
        result = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    assert result["state"] == "error" and result["code"] == "FETCH_TASK_FAILED", result
    assert sum("b" in repos for kind, _, repos in calls if kind == "fetch") == fill._MAX_TASK_ATTEMPTS


def _gated_executor(action):
    blocked = {"b"}
    con, calls, fake_run, fake_replica = _executor_harness(failed=blocked)
    prompts, progress = [], []

    def decide(prompt, timeout):
        prompts.append((prompt, timeout))
        if action == "retry":
            blocked.clear()                         # access became effective before the retry click
        return action

    def gated_run(**kwargs):
        outcome = fake_run(**kwargs)
        outcome.update({"gated_repos": [], "gated_retry": None})
        if "b" not in kwargs["repos"] or "b" not in outcome["failed_repos"]:
            return outcome
        outcome["failed_repos"].remove("b")
        response = kwargs["on_gated"]("b")
        if response == "retry":
            outcome["gated_retry"] = "b"
        elif response in {"skip", "timeout"}:
            outcome["gated_repos"].append({"repo": "b", "resolution": response})
        return outcome

    ctx = fetch.RunCtx(con=con, on_progress=progress.append, request_action=decide)
    with mock.patch.object(fill.fetch, "run", side_effect=gated_run), \
         mock.patch.object(fill.fetch, "run_replica_tasks", side_effect=fake_replica), \
         mock.patch.object(fill, "_await_drive", return_value=True):
        result = fill.execute(ctx, guided=True, max_24h_gb=0)
    return result, calls, prompts, progress


def test_gated_first_toasts_second_skip_becomes_followup_without_generic_failure():
    result, calls, prompts, progress = _gated_executor("skip")
    assert result["state"] == "done" and result["code"] == "PLAN_COMPLETE_WITH_FOLLOWUPS", result
    assert result["evidence"] == {"access_gated": ["b"]}
    assert len(prompts) == 1 and prompts[0][0]["repo"] == "b"
    assert prompts[0][1] == fill._GATED_DECISION_TIMEOUT
    notices = [e["notice"] for e in progress if e.get("notice")]
    assert notices[0]["type"] == "access-gated" and "continuing other work" in notices[0]["message"]
    assert any("added to Verify follow-ups" in n["message"] for n in notices)
    assert sum("b" in repos for kind, _, repos in calls if kind == "fetch") == 2


def test_gated_retry_after_access_reconciles_and_completes():
    result, calls, prompts, _ = _gated_executor("retry")
    assert result["state"] == "done" and result["code"] == "PLAN_SATISFIED", result
    assert len(prompts) == 1
    assert sum("b" in repos for kind, _, repos in calls if kind == "fetch") == 3


def test_gated_retry_does_not_reset_another_failed_repos_attempt_budget():
    blocked = {"a", "b"}
    con, calls, fake_run, fake_replica = _executor_harness(failed=blocked)
    con.execute(
        "UPDATE drives SET capacity_bytes=2000,free_bytes=2000 WHERE drive_label='drive-00'"
    )

    def decide(_prompt, _timeout):
        blocked.remove("b")
        return "retry"

    def gated_run(**kwargs):
        outcome = fake_run(**kwargs)
        outcome.update({"gated_repos": [], "gated_retry": None})
        if "b" not in kwargs["repos"] or "b" not in outcome["failed_repos"]:
            return outcome
        outcome["failed_repos"].remove("b")
        if kwargs["on_gated"]("b") == "retry":
            outcome["gated_retry"] = "b"
        return outcome

    ctx = fetch.RunCtx(con=con, request_action=decide)
    with mock.patch.object(fill.fetch, "run", side_effect=gated_run), \
         mock.patch.object(fill.fetch, "run_replica_tasks", side_effect=fake_replica), \
         mock.patch.object(fill, "_await_drive", return_value=True):
        result = fill.execute(ctx, guided=True, max_24h_gb=0)

    assert result["state"] == "error" and result["code"] == "FETCH_TASK_FAILED", result
    assert result["evidence"] == {"repo": "a", "attempts": fill._MAX_TASK_ATTEMPTS}
    assert sum("a" in repos for kind, _, repos in calls if kind == "fetch") == 2
    assert any(
        {"a", "b"}.issubset(repos) for kind, _, repos in calls if kind == "fetch"
    ), "the regression requires an ordinary failure and gated retry in the same batch"


def test_executor_blocks_before_reconciliation_when_configured_hf_token_is_invalid():
    con, calls, _, _ = _executor_harness()
    failure = {
        "code": "HF_AUTH_INVALID",
        "message": "configured Hugging Face credential is invalid",
        "evidence": {"credential_source": "cached"},
        "actions": ["hf_auth_login_force", "retry_fill"],
        "gate": "A",
    }
    ctx = fetch.RunCtx(con=con, check_hf_auth=True)
    with mock.patch.object(fill.fetch, "hf_auth_preflight", return_value=failure), \
         mock.patch.object(fill, "_reconcile") as reconcile_plan:
        result = fill.execute(ctx, guided=True, max_24h_gb=0)
    assert result["state"] == "blocked" and result["code"] == "HF_AUTH_INVALID", result
    assert result["gate"] == "A" and result["evidence"] == failure["evidence"]
    reconcile_plan.assert_not_called()
    assert calls == []


def test_executor_stops_batch_on_typed_fetch_terminal_after_durable_progress():
    con, calls, fake_run, fake_replica = _executor_harness()
    terminal = {
        "code": "TARGET_PATH_CONFLICT",
        "message": "archive target is not safely replaceable",
        "evidence": {"rfilename": "model.gguf"},
        "actions": ["inspect_annex_placeholder", "retry_fill"],
        "gate": "C",
    }

    def terminal_run(**kwargs):
        outcome = fake_run(**kwargs)
        outcome["terminal_failure"] = terminal
        outcome["terminal_repo"] = kwargs["repos"][-1]
        return outcome

    with mock.patch.object(fill.fetch, "run", side_effect=terminal_run), \
         mock.patch.object(fill.fetch, "run_replica_tasks", side_effect=fake_replica), \
         mock.patch.object(fill, "_await_drive", return_value=True):
        result = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    assert result["state"] == "paused" and result["code"] == "TARGET_PATH_CONFLICT", result
    assert result["failed"] and result["failed"][0]["repo"]
    assert len(calls) == 1, "a typed terminal must stop the batch without cycling repositories"


def test_dead_drive_parks_without_running_task():
    con, calls, fake_run, fake_replica = _executor_harness()
    with mock.patch.object(fill.fetch, "run", side_effect=fake_run), \
         mock.patch.object(fill.fetch, "run_replica_tasks", side_effect=fake_replica), \
         mock.patch.object(fill, "_await_drive", return_value=False):
        result = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    assert result["stopped"] and result["code"] == "OPERATOR_STOP", result
    assert calls == []


def test_worker_terminal_labels(tmp_path):
    # a resumable cap/throttle must read 'paused', GATE-B 'blocked', a clean finish 'done' — never
    # mislabel a throttle as "fill complete" (the bug the 24h cap surfaced).
    import time as _t
    from modelark.web import fill_worker
    for outcome, expect in [({"status": "paused", "message": "24h cap"}, "paused"),
                            ({"status": "blocked", "message": "add a drive"}, "blocked"),
                            (None, "done")]:
        w = fill_worker.FillWorker()
        w.start(lambda ss, em, o=outcome: o)
        for _ in range(100):
            if not w.running():
                break
            _t.sleep(0.02)
        assert w.status()["status"] == expect, (outcome, w.status())


def test_worker_gated_prompt_accepts_only_matching_decision_and_times_out():
    import time as _t
    from modelark.web import fill_worker

    prompt = {"id": "gated:a:2", "type": "access-gated", "repo": "a"}
    w = fill_worker.FillWorker()

    def wait_for_retry(stop, emit):
        action = w.await_action(prompt, 2)
        return {"status": "done", "message": action, "action": action}

    assert w.start(wait_for_retry)["ok"]
    for _ in range(100):
        if w.status().get("operator_prompt"):
            break
        _t.sleep(0.01)
    assert w.status()["operator_prompt"]["repo"] == "a"
    assert not w.resolve_action("stale", "retry")["ok"]
    assert w.resolve_action("gated:a:2", "retry")["ok"]
    assert not w.resolve_action("gated:a:2", "skip")["ok"], "one prompt accepts one decision"
    for _ in range(100):
        if not w.running():
            break
        _t.sleep(0.01)
    assert w.status()["status"] == "done" and w.status()["action"] == "retry"
    assert w.status().get("operator_prompt") is None

    timed = fill_worker.FillWorker()
    timed.start(lambda stop, emit: {
        "status": "done", "message": timed.await_action(prompt, 0.02)
    })
    for _ in range(100):
        if not timed.running():
            break
        _t.sleep(0.01)
    assert timed.status()["message"] == "timeout"


def test_dest_writable(tmp_path):
    from modelark import fetch
    assert fetch._dest_writable(tmp_path) is True
    assert not (tmp_path / ".modelark-write-probe").exists(), "probe must be cleaned up"
    assert fetch._dest_writable(tmp_path / "missing" / "deeper") is False


def test_fetch_run_bails_on_dead_drive_midbatch(tmp_path):
    # a drive that dies mid-batch must bail after the FIRST failure (re-plan loop re-awaits it),
    # not churn one error per repo through the whole assignment.
    from modelark import fetch
    events, calls = [], {"n": 0}
    def boom(ctx, rid, dest, label, annex, cfg):
        calls["n"] += 1
        raise RuntimeError("write failed: I/O error")
    ctx = fetch.RunCtx(con=object(), on_progress=events.append)
    with mock.patch.object(fetch.register, "archive_path", side_effect=lambda con, l: tmp_path), \
         mock.patch.object(fetch, "_is_annex", side_effect=lambda d: False), \
         mock.patch.object(fetch, "fetch_model", side_effect=boom), \
         mock.patch.object(fetch, "_dest_writable", side_effect=lambda d: False), \
         mock.patch.object(fetch, "_bytes_last_24h", side_effect=lambda c: 0), \
         mock.patch.object(fetch, "_event", side_effect=lambda *a, **k: None), \
         mock.patch.object(fetch.wishlist, "compression",
                           side_effect=lambda: {"threads": 1, "stream_compress": True, "max_compress_ram_gb": 4.0}):
        fetch.run(drive_label="drive-01", repos=["a", "b", "c"], max_24h_gb=0, ctx=ctx)
    assert calls["n"] == 1, f"dead drive must bail after the 1st failure, not churn all 3 (got {calls['n']})"
    assert any(e.get("awaiting_drive") == "drive-01" for e in events), "should emit awaiting on dead-drive bail"


def test_fetch_run_stops_on_midrun_unauthorized_without_repo_churn(tmp_path):
    import httpx
    from huggingface_hub.errors import HfHubHTTPError

    calls = []

    def unauthorized(ctx, rid, dest, label, annex, cfg):
        calls.append(rid)
        response = httpx.Response(401, request=httpx.Request("GET", "https://huggingface.co"))
        raise HfHubHTTPError("unauthorized", response=response)

    ctx = fetch.RunCtx(con=object())
    with mock.patch.object(fetch.register, "archive_path", return_value=tmp_path), \
         mock.patch.object(fetch, "_is_annex", return_value=False), \
         mock.patch.object(fetch, "fetch_model", side_effect=unauthorized), \
         mock.patch.object(fetch, "_bytes_last_24h", return_value=0), \
         mock.patch.object(fetch, "_event"), \
         mock.patch.object(fetch.wishlist, "compression", return_value={}):
        result = fetch.run(
            drive_label="drive-00", repos=["first", "second", "third"],
            max_24h_gb=0, ctx=ctx,
        )
    assert calls == ["first"], "a systemic 401 must stop immediately, not rotate through repositories"
    assert result["terminal_failure"] == fetch._hf_auth_invalid_failure()
    assert result["terminal_repo"] == "first"
    assert result["failed_repos"] == []


def test_fetch_run_records_typed_gated_followup_without_generic_retry(tmp_path):
    import httpx
    import json
    from huggingface_hub.errors import GatedRepoError

    con = _mem()
    response = httpx.Response(403, request=httpx.Request("GET", "https://huggingface.co/org/gated"))
    gated = GatedRepoError("access required", response=response)
    ctx = fetch.RunCtx(con=con)
    with mock.patch.object(fetch.register, "archive_path", return_value=tmp_path), \
         mock.patch.object(fetch, "_is_annex", return_value=False), \
         mock.patch.object(fetch, "fetch_model", side_effect=gated), \
         mock.patch.object(fetch.wishlist, "compression", return_value={}):
        result = fetch.run(
            drive_label="drive-00", repos=["org/gated"], max_24h_gb=0, ctx=ctx,
            on_gated=lambda repo: "timeout",
        )
    assert result["failed_repos"] == []
    assert result["gated_repos"] == [{"repo": "org/gated", "resolution": "timeout"}]
    details = [row[0] for row in con.execute(
        "SELECT detail FROM fetch_events WHERE repo_id='org/gated' AND outcome='auth' ORDER BY rowid"
    ).fetchall()]
    typed = json.loads(details[-1])
    assert typed == {
        "resolution": "timeout", "type": "access-gated",
        "url": "https://huggingface.co/org/gated",
    }


# ---- per-file capacity failsafe (real reconciler + ledger) ----------------------------------


def _capacity_run(capacity_mode, free_bytes, a_stored):
    """A fresh per-file guard stops the second model after the first consumes forecast slack."""
    con = _mem()
    con.execute("INSERT INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed) "
                "VALUES('drive-00',?,?,'primary',0)", [free_bytes, free_bytes])
    for r in ("a", "b"):
        con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [r])
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
                    "VALUES(?, 'model.safetensors', 100, 'safetensors', 'bf16')", [r])
        con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?, '2026-01-01')", [r])
    plan.bootstrap(con)                                   # plan `ark` owns drive-00, active
    plan.set_capacity_mode(con, "ark", capacity_mode)

    def fake_run(*, drive_label, repos, task_manifests, before_file, **kwargs):
        result = {"stored_repos": [], "failed_repos": [], "capacity_failure": None,
                  "terminal_failure": None, "terminal_repo": None, "throttled": False,
                  "stopped": False, "drive_unwritable": False}
        for r in repos:
            item = task_manifests[r][0]
            try:
                before_file(r, item)
            except fetch.CapacityPreflightError as exc:
                result["capacity_failure"] = exc.failure
                return result
            stored = a_stored if r == "a" else 100
            con.execute("INSERT INTO archived(repo_id,rfilename,drive_label,orig_bytes,stored_bytes,compressed) "
                        "VALUES(?, 'model.safetensors', 'drive-00', 100, ?, 0)", [r, stored])
            result["stored_repos"].append(r)
        return result

    with mock.patch.object(fill.fetch, "run", side_effect=fake_run), \
         mock.patch.object(fill, "_await_drive", side_effect=lambda ctx, l, p: True):
        return fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)


def test_plan_capacity_stop_guaranteed(tmp_path):
    res = _capacity_run("guaranteed", free_bytes=350, a_stored=250)
    assert res["state"] == "plan-capacity-stop", res
    assert res["ok"] is False and res["stopped"] is False


def test_plan_capacity_stop_compression_aware(tmp_path):
    res = _capacity_run("compression_aware", free_bytes=300, a_stored=100)
    assert res["state"] == "plan-capacity-stop", res


# ---- DEF-022 fail-soft replica + GATE-C softening -------------------------------------------

def test_run_replica_defers_on_offline_source(tmp_path):
    # A dead/read-only source → deferred + awaiting-drive, NO per-repo copy churn (INC-009).
    events = []
    ctx = fetch.RunCtx(con=object(), on_progress=events.append)
    with mock.patch.object(fetch.register, "archive_path",
                           side_effect=lambda con, l: (tmp_path if l == "drive-04" else tmp_path / "gone")), \
         mock.patch.object(fetch, "_dest_writable", side_effect=lambda p: "gone" not in str(p)):
        res = fetch.run_replica({"drive-04": [{"repo": "m1"}]}, "drive-00", ctx=ctx)
    assert res["deferred"] and res["source_offline"], res
    assert res["copied_targets"] == []
    assert any(e.get("awaiting_drive") == "drive-00" for e in events), "should prompt to re-seat the source"


def test_gatec_pauses_on_deferred_copy2(tmp_path):
    con, calls, fake_run, _ = _executor_harness()

    def deferring_replica(tasks, ctx=None):
        return {"deferred": True, "source_offline": True,
                "deferred_targets": [tasks[0].target_drive], "copied_targets": [],
                "copied_files": 0, "failed": []}

    with mock.patch.object(fill.fetch, "run", side_effect=fake_run), \
         mock.patch.object(fill.fetch, "run_replica_tasks", side_effect=deferring_replica), \
         mock.patch.object(fill, "_await_drive", return_value=True):
        res = fill.execute(fetch.RunCtx(con=con), guided=True, max_24h_gb=0)
    assert res["state"] == "paused" and res["code"] == "SOURCE_UNAVAILABLE", res
    assert res["ok"] is False and res["stopped"] is False
    assert con.execute(
        "SELECT count(DISTINCT drive_label) FROM archived WHERE repo_id='must'"
    ).fetchone()[0] == 1


def test_replica_records_only_after_target_uuid_proof(tmp_path):
    con, _, _, _ = _executor_harness()
    con.execute(
        "INSERT INTO archived"
        "(repo_id,rfilename,drive_label,stored_name,stored_relpath,orig_sha256,znn_sha256,"
        "orig_bytes,stored_bytes,compressed,annex_key) "
        "VALUES('must','model.gguf','drive-00','model.gguf','must/model.gguf','orig','stored',"
        "200,200,0,'key-must-model.gguf')"
    )
    snapshot = fill._reconcile(fetch.RunCtx(con=con), "ark", "guaranteed", None)
    task = next(
        item for item in snapshot.ledger.tasks if item.kind == reconcile.TaskKind.REPLICATE
    )
    source = tmp_path / "source"
    target = tmp_path / "target"
    library = tmp_path / "library"
    source.mkdir()
    target.mkdir()
    library.mkdir()

    def archive_path(_con, label):
        return source if label == "drive-00" else target

    completed = mock.Mock(returncode=0, stdout="", stderr="")
    ctx = fetch.RunCtx(con=con)
    with mock.patch.object(fetch.register, "archive_path", side_effect=archive_path), \
         mock.patch.object(fetch.register, "library_root", return_value=library), \
         mock.patch.object(fetch, "_dest_writable", return_value=True), \
         mock.patch.object(fetch.subprocess, "run", return_value=completed), \
         mock.patch.object(fetch, "_annex_key_on_uuid", return_value=False):
        unverified = fetch.run_replica_tasks([task], ctx=ctx)
    assert unverified["failed"][0]["code"] == "TARGET_KEY_UNVERIFIED"
    assert con.execute(
        "SELECT 1 FROM archived WHERE repo_id='must' AND drive_label='drive-04'"
    ).fetchone() is None

    with mock.patch.object(fetch.register, "archive_path", side_effect=archive_path), \
         mock.patch.object(fetch.register, "library_root", return_value=library), \
         mock.patch.object(fetch, "_dest_writable", return_value=True), \
         mock.patch.object(fetch.subprocess, "run", return_value=completed), \
         mock.patch.object(fetch, "_annex_key_on_uuid", return_value=True):
        verified = fetch.run_replica_tasks([task], ctx=ctx)
    assert verified["failed"] == [] and verified["copied_files"] == 1
    row = con.execute(
        "SELECT orig_sha256,znn_sha256,stored_bytes,annex_key FROM archived "
        "WHERE repo_id='must' AND drive_label='drive-04'"
    ).fetchone()
    assert row == ("orig", "stored", 200, "key-must-model.gguf")


def test_sweep_incomplete(tmp_path):
    # INC-010: after a store, orphaned .incomplete leftovers are reclaimed; a fresh (active) .incomplete
    # + non-.incomplete files are kept (age guard). Missing cache dir → 0, no error.
    import os
    import time as _t
    dl = tmp_path / ".cache" / "huggingface" / "download"
    dl.mkdir(parents=True)
    orphan = dl / "old.incomplete"
    orphan.write_bytes(b"x" * 1000)
    os.utime(orphan, (_t.time() - 120, _t.time() - 120))          # 2 min idle → orphan, swept
    fresh = dl / "new.incomplete"
    fresh.write_bytes(b"y" * 500)                                 # just written → kept (age guard)
    other = dl / "keep.bin"
    other.write_bytes(b"z" * 200)                                 # not .incomplete → kept
    freed = fetch._sweep_incomplete(tmp_path)
    assert freed == 1000, freed
    assert not orphan.exists() and fresh.exists() and other.exists()
    assert fetch._sweep_incomplete(tmp_path / "nope") == 0        # no cache dir → 0, no crash


if __name__ == "__main__":
    import inspect
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            with tempfile.TemporaryDirectory() as td:
                if inspect.signature(fn).parameters:
                    fn(Path(td))
                else:
                    fn()
            print(f"ok  {name}")
    print("all passed")
