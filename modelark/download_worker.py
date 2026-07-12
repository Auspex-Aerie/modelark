"""Isolated single-file download child (DEC-023 stage 2 — a hang-KILLABLE download).

`hf_hub_download` can block indefinitely on a stalled/half-open connection (INC-004 residual — the
2026-07-09 Falcon-H1 hang, where `socket.setdefaulttimeout` did NOT fire, likely hf_xet native I/O).
A blocking call inside a thread can't be interrupted; inside a CHILD process it can be SIGKILL'd. So
the fetch pipeline downloads HERE, and the parent (`fetch._download_shard` via `_run_monitored`) kills
this child when the on-disk `.incomplete` stops growing, then retries — hf resumes the partial.

Protocol: argv[1] = JSON {repo_id, rfilename, local_dir, result}. Writes a JSON result and exits 0:
    {"ok": true,  "path": "<downloaded file>"}
    {"ok": false, "error_type": "gated|not_found|http|transient",
                  "status_code": int|null, "retry_after": float|null, "detail": "..."}
A stall/kill exits via signal WITHOUT writing the result; the parent reads the negative return code.
The result travels by file (not stdout) — hf/its deps may write to stdout and would corrupt it.
"""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

from huggingface_hub import hf_hub_download
from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError

_SOCKET_TIMEOUT = 120       # belt-and-suspenders; the parent's no-progress watchdog is the real guard


def _retry_after(e) -> float | None:
    resp = getattr(e, "response", None)
    raw = resp.headers.get("Retry-After") if resp is not None else None
    try:
        return float(raw) if raw else None       # seconds form; HTTP-date form ignored (v1)
    except (TypeError, ValueError):
        return None


def run(req: dict) -> dict:
    socket.setdefaulttimeout(_SOCKET_TIMEOUT)
    try:
        path = hf_hub_download(req["repo_id"], req["rfilename"], local_dir=req["local_dir"])
        return {"ok": True, "path": str(path)}
    except GatedRepoError as e:
        return {"ok": False, "error_type": "gated", "status_code": None, "retry_after": None, "detail": str(e)[:300]}
    except RepositoryNotFoundError as e:
        return {"ok": False, "error_type": "not_found", "status_code": None, "retry_after": None, "detail": str(e)[:300]}
    except HfHubHTTPError as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        return {"ok": False, "error_type": "http", "status_code": code,
                "retry_after": _retry_after(e), "detail": str(e)[:300]}
    except Exception as e:      # timeout / connection reset / chunked-encoding → transient; parent retries
        return {"ok": False, "error_type": "transient", "status_code": None, "retry_after": None,
                "detail": f"{type(e).__name__}: {e}"[:300]}


def main(argv: list[str]) -> int:
    req = json.loads(argv[1])
    Path(req["result"]).write_text(json.dumps(run(req)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
