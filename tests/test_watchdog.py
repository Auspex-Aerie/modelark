"""DEC-023 stage 2 (#27): the no-progress watchdog + killable download child.

Covers: _run_monitored kills a stalled/stopped child (real subprocesses) and returns clean exits;
download_worker classifies hf errors without network; _download_shard reconstructs the terminal hf
exceptions from the child's result so run() still classifies gated/429/not-found exactly as before.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest import mock

from modelark import download_worker, fetch

PY = sys.executable


# ---- _run_monitored (real child processes) ---------------------------------------------------

def test_monitored_clean_exit(tmp_path):
    mon = fetch._run_monitored([PY, "-c", "print('hi')"], lambda: 0, stall_secs=999, should_stop=lambda: False)
    assert mon["outcome"] == "exited" and mon["rc"] == 0, mon


def test_monitored_kills_stall(tmp_path):
    # a child that runs long but never makes progress → killed by the watchdog, not left for 60s
    t0 = time.monotonic()
    mon = fetch._run_monitored([PY, "-c", "import time; time.sleep(60)"],
                               lambda: 0, stall_secs=1, should_stop=lambda: False)
    assert mon["outcome"] == "stalled", mon
    assert time.monotonic() - t0 < 30, "watchdog should kill promptly, not wait out the sleep"


def test_monitored_stop(tmp_path):
    mon = fetch._run_monitored([PY, "-c", "import time; time.sleep(60)"],
                               lambda: 0, stall_secs=999, should_stop=lambda: True)
    assert mon["outcome"] == "stopped", mon


def test_monitored_progress_prevents_kill(tmp_path):
    # progress() keeps growing → NOT killed for stalling; the child finishes on its own
    counter = {"n": 0}
    def progress():
        counter["n"] += 1_000_000
        return counter["n"]
    mon = fetch._run_monitored([PY, "-c", "import time; time.sleep(7)"],
                               progress, stall_secs=2, should_stop=lambda: False)
    assert mon["outcome"] == "exited" and mon["rc"] == 0, mon


# ---- download_worker error classification (no network) ---------------------------------------

def test_worker_success(tmp_path):
    with mock.patch("modelark.download_worker.hf_hub_download", return_value=str(tmp_path / "x.bin")):
        r = download_worker.run({"repo_id": "a", "rfilename": "x.bin", "local_dir": str(tmp_path)})
    assert r["ok"] is True and r["path"] == str(tmp_path / "x.bin")


def test_worker_transient(tmp_path):
    with mock.patch("modelark.download_worker.hf_hub_download", side_effect=ConnectionError("reset")):
        r = download_worker.run({"repo_id": "a", "rfilename": "b", "local_dir": str(tmp_path)})
    assert r["ok"] is False and r["error_type"] == "transient", r


def test_worker_local_io_is_not_transient(tmp_path):
    error = FileNotFoundError(2, "missing annex object", str(tmp_path / "broken"))
    with mock.patch("modelark.download_worker.hf_hub_download", side_effect=error):
        r = download_worker.run({"repo_id": "a", "rfilename": "b", "local_dir": str(tmp_path)})
    assert r["ok"] is False and r["error_type"] == "local_io", r
    assert r["errno"] == 2


def test_worker_gated(tmp_path):
    from huggingface_hub.errors import GatedRepoError
    exc = GatedRepoError("gated", response=_Resp(403))
    with mock.patch("modelark.download_worker.hf_hub_download", side_effect=exc):
        r = download_worker.run({"repo_id": "a", "rfilename": "b", "local_dir": str(tmp_path)})
    assert r["error_type"] == "gated", r


# ---- _download_shard: reconstruct terminal exceptions from the child's result ----------------

def _monitored_writes(result: dict, outcome: str = "exited", rc: int = 0):
    """A _run_monitored stand-in that writes `result` to the child's result file, then returns `outcome`."""
    def side_effect(cmd, progress, stall_secs, should_stop):
        req = json.loads(cmd[-1])
        Path(req["result"]).write_text(json.dumps(result))
        return {"outcome": outcome, "rc": rc, "stderr": ""}
    return side_effect


def test_download_success(tmp_path):
    (tmp_path / "f.bin").write_bytes(b"data")
    result = {"ok": True, "path": str(tmp_path / "f.bin")}
    with mock.patch("modelark.fetch._run_monitored", side_effect=_monitored_writes(result)):
        p = fetch._download_shard(fetch.RunCtx(con=None), "repo", "f.bin", tmp_path, {})
    assert p == tmp_path / "f.bin"


def test_download_gated_reraises(tmp_path):
    from huggingface_hub.errors import GatedRepoError
    result = {"ok": False, "error_type": "gated", "status_code": None, "retry_after": None, "detail": "gated"}
    with mock.patch("modelark.fetch._run_monitored", side_effect=_monitored_writes(result)):
        try:
            fetch._download_shard(fetch.RunCtx(con=None), "repo", "f.bin", tmp_path, {})
        except GatedRepoError:
            return
    raise AssertionError("expected GatedRepoError")


def test_download_429_reraises(tmp_path):
    from huggingface_hub.errors import HfHubHTTPError
    result = {"ok": False, "error_type": "http", "status_code": 429, "retry_after": 30, "detail": "rate limited"}
    with mock.patch("modelark.fetch._run_monitored", side_effect=_monitored_writes(result)):
        try:
            fetch._download_shard(fetch.RunCtx(con=None), "repo", "f.bin", tmp_path, {})
        except HfHubHTTPError as e:
            assert getattr(getattr(e, "response", None), "status_code", None) == 429      # run() reads this
            return
    raise AssertionError("expected HfHubHTTPError 429")


def test_download_transient_exhausts(tmp_path):
    result = {"ok": False, "error_type": "transient", "status_code": None, "retry_after": None, "detail": "conn reset"}
    with mock.patch("modelark.fetch._run_monitored", side_effect=_monitored_writes(result)), \
         mock.patch("modelark.fetch.time.sleep"):                    # skip the real backoff
        try:
            fetch._download_shard(fetch.RunCtx(con=None), "repo", "f.bin", tmp_path, {})
        except RuntimeError as e:
            assert "conn reset" in str(e)
            return
    raise AssertionError("expected RuntimeError after exhausting retries")


def test_download_local_io_fails_once_without_network_cooldown(tmp_path):
    result = {"ok": False, "error_type": "local_io", "status_code": None,
              "retry_after": None, "errno": 2, "detail": "broken annex placeholder"}
    with mock.patch("modelark.fetch._run_monitored", side_effect=_monitored_writes(result)) as monitored, \
         mock.patch("modelark.fetch.time.sleep") as sleep:
        try:
            fetch._download_shard(fetch.RunCtx(con=None), "repo", "f.bin", tmp_path, {})
        except fetch.DownloadLocalError as exc:
            assert exc.code == "DOWNLOAD_LOCAL_IO"
        else:
            raise AssertionError("expected typed local I/O failure")
    assert monitored.call_count == 1
    sleep.assert_not_called()


def test_download_staging_enospc_is_typed_before_worker_start(tmp_path):
    import errno

    with mock.patch("modelark.fetch.tempfile.mkstemp", side_effect=OSError(errno.ENOSPC, "full")), \
         mock.patch("modelark.fetch._run_monitored") as monitored:
        try:
            fetch._download_shard(fetch.RunCtx(con=None), "repo", "f.bin", tmp_path, {})
        except fetch.DownloadLocalError as exc:
            assert exc.code == "DOWNLOAD_LOCAL_IO" and exc.evidence["errno"] == errno.ENOSPC
        else:
            raise AssertionError("staging ENOSPC must be a typed local failure")
    monitored.assert_not_called()


def test_hf_auth_preflight_types_only_rejected_credentials(tmp_path):
    response = _Resp(401)
    rejected = fetch.HfHubHTTPError("invalid", response=response)
    api = mock.Mock()
    api.whoami.side_effect = rejected
    with mock.patch("modelark.fetch.get_token", return_value="hf_oauth_test"), \
         mock.patch("modelark.fetch.HfApi", return_value=api):
        failure = fetch.hf_auth_preflight(fetch.RunCtx(con=None))
    assert failure["code"] == "HF_AUTH_INVALID" and failure["gate"] == "A", failure

    with mock.patch("modelark.fetch.get_token", return_value=None), \
         mock.patch("modelark.fetch.HfApi") as hf_api:
        assert fetch.hf_auth_preflight(fetch.RunCtx(con=None)) is None
    hf_api.assert_not_called()


class _Resp:                       # minimal response so an hf error constructs in a test
    def __init__(self, code):
        self.status_code = code
        self.headers = {}
        self.request = None


if __name__ == "__main__":
    import tempfile
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(Path(tempfile.mkdtemp()))
            print(f"ok  {name}")
    print("all passed")
