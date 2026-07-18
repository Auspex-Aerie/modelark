"""DEF-023: the persisted last-terminal oopsie — a fill that fell over is surfaced loudly on portal
open (survives reload/restart) until acknowledged. A clean 'done' / user 'stopped' clears it."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

from modelark.web import fill_api


def test_persist_last_ack(tmp_path):
    fill_api._TERMINAL_PATH = tmp_path / "last_fill.json"          # isolate from the real catalog

    # a non-DONE terminal is persisted with a timestamp + affected models
    fill_api._persist_terminal({
        "status": "plan-capacity-stop", "message": "a drive is full",
        "failed": [{"repo": "a", "have": 1, "need": 2}], "gate": "B",
        "code": "CAPACITY_WORKSPACE_SHORT", "evidence": {"shortfall_bytes": 10},
        "actions": ["add_capacity", "start_fill"],
    })
    t = fill_api.last_terminal()
    assert t["status"] == "plan-capacity-stop" and t["message"] == "a drive is full" and t.get("when")
    assert t["version"] == 2 and t["code"] == "CAPACITY_WORKSPACE_SHORT" and t["gate"] == "B"
    assert t["evidence"] == {"shortfall_bytes": 10}
    assert t["actions"] == ["add_capacity", "start_fill"]
    assert t["failed"] == [{"repo": "a", "have": 1, "need": 2}]
    assert not fill_api._TERMINAL_PATH.with_name("last_fill.json.tmp").exists()

    # acknowledging clears it (stops popping)
    fill_api.ack_terminal({})
    assert fill_api.last_terminal() == {}

    # every oopsie state persists; a clean 'done' or a user 'stopped' clears any prior one
    for st in ("error", "blocked", "paused"):
        fill_api._persist_terminal({"status": st, "message": st})
        assert fill_api.last_terminal()["status"] == st
    fill_api._persist_terminal({"status": "done", "message": "all good"})
    assert fill_api.last_terminal() == {}
    fill_api._persist_terminal({"status": "error", "message": "boom"})
    fill_api._persist_terminal({"status": "stopped", "message": "by request"})
    assert fill_api.last_terminal() == {}, "a user Stop is not an oopsie — must clear"


def test_expected_fill_error_keeps_typed_terminal_instead_of_unhandled(tmp_path):
    import time
    from modelark.web import fill_worker

    fill_api._TERMINAL_PATH = tmp_path / "last_fill.json"
    worker = fill_worker.FillWorker()
    typed = {
        "ok": False, "stopped": False, "state": "error",
        "message": "repository access is still gated", "code": "HF_REPO_GATED",
        "gate": "C", "evidence": {"repo": "org/gated"},
        "actions": ["open_huggingface", "retry_fill"],
        "failed": [{"repo": "org/gated"}],
    }
    with mock.patch.object(fill_api.fill_worker, "WORKER", worker), \
         mock.patch.object(fill_api.data, "conn", return_value=object()), \
         mock.patch.object(fill_api.fill, "execute", return_value=typed):
        assert fill_api.start({"max_24h_gb": 0})["ok"]
        for _ in range(100):
            if not worker.running():
                break
            time.sleep(0.01)
    status = worker.status()
    persisted = fill_api.last_terminal()
    assert status["status"] == "error" and status["code"] == "HF_REPO_GATED", status
    assert persisted["code"] == "HF_REPO_GATED" and persisted["evidence"] == {"repo": "org/gated"}
    assert persisted["actions"] == ["open_huggingface", "retry_fill"]


if __name__ == "__main__":
    import tempfile
    test_persist_last_ack(Path(tempfile.mkdtemp()))
    print("ok  test_persist_last_ack")
    print("all passed")
