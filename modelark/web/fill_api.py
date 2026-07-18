"""Portal endpoints for the guided Library Fill (task #22): start / stop / status / confirm-drive.

The heavy work runs in the single safe FillWorker thread (web/fill_worker.py). The worker injects a
`fetch.RunCtx` bound to the portal's SHARED connection + `data._lock`, so its per-file DB writes are
brief-locked (the multi-day download/compress stays lock-free → the portal never freezes), its
progress feeds the worker's live status via `emit`, and it stops cooperatively at file/drive
boundaries. `fill.execute` returns a result dict; a non-ok, non-stopped result is re-raised inside
the worker so the FillWorker wrapper flips status to 'error' (the emitted 'say' already explains why).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from modelark.core import db, telemetry
from modelark import fetch, fill, register, wishlist
from modelark.web import data, fill_worker

# DEF-023: persist a NON-DONE terminal fill outcome so the portal can surface it LOUDLY on open — it
# survives a page reload AND a portal restart until the operator acknowledges it (INC-009 sat silent
# overnight). A clean 'done' or a user 'stopped' clears it.
_TERMINAL_PATH = db.CATALOG_DIR / "last_fill.json"
_OOPSIE = {"error", "blocked", "plan-capacity-stop", "paused"}


def _persist_terminal(term: dict) -> None:
    try:
        if term.get("status") in _OOPSIE:
            payload = {
                "version": 2,
                "status": term["status"], "message": term.get("message", ""),
                "when": datetime.now().isoformat(sep=" ", timespec="seconds"),
                "code": term.get("code"), "gate": term.get("gate"),
                "evidence": term.get("evidence") or {},
                "actions": list(term.get("actions") or []),
                "failed": (term.get("failed") or [])[:12],
            }
            _TERMINAL_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _TERMINAL_PATH.with_name(_TERMINAL_PATH.name + ".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True))
            os.replace(tmp, _TERMINAL_PATH)
        else:
            _TERMINAL_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def last_terminal() -> dict:
    """The persisted last non-DONE terminal (DEF-023), or {} — polled once on portal open for the modal."""
    try:
        return json.loads(_TERMINAL_PATH.read_text()) if _TERMINAL_PATH.exists() else {}
    except (OSError, ValueError):
        return {}


def ack_terminal(body: dict | None = None) -> dict:
    """Operator acknowledged the oopsie → clear it so it stops popping."""
    try:
        _TERMINAL_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True}


_net_lock = threading.Lock()
_net = {"t": None, "rx": None}          # last (monotonic time, cumulative RX bytes) sample


def _shard(ev: dict) -> str | None:
    return f"{ev['shard_no']}/{ev.get('n_shards')}" if ev.get("shard_no") else None


def _log_event(log, ev: dict) -> None:
    """Surface notable fill progress to the log. Logs the START of each download/compress at INFO so a
    HANG leaves a line that says WHAT it's stuck on (the 2026-07-09 blindness); skips the ~1.5s rate
    ticks that carry no phase. Event fields are genuinely optional per phase, hence .get()."""
    fp, ph = ev.get("file_phase"), ev.get("phase")
    if fp == "download":
        log.info("downloading", repo=ev.get("repo"), file=ev.get("file"), shard=_shard(ev))
    elif fp == "compress":
        log.info("compressing", repo=ev.get("repo"), file=ev.get("file"), codec=ev.get("codec"))
    elif fp == "compress-crashed":
        log.warning("compressor crashed → stored raw", repo=ev.get("repo"), file=ev.get("file"))
    elif fp == "download-retry":
        log.warning("transient download retry", repo=ev.get("repo"), file=ev.get("file"),
                    attempt=ev.get("retry_attempt"), limit=ev.get("retry_limit"),
                    reason=ev.get("retry_reason"), cooldown=bool(ev.get("stall_cooldown")))
    elif fp == "stored" and "session_bytes" in ev:                 # the per-file completion (carries stats)
        log.info("stored", repo=ev.get("repo"), file=ev.get("file"),
                 ratio=ev.get("ratio"), session_gb=round((ev.get("session_bytes") or 0) / 1e9, 1))
    elif ph == "awaiting-drive":
        log.info("awaiting drive", drive=ev.get("awaiting_drive"))
    elif ph in ("planning", "throttled", "rate_limited"):
        log.info(ph, repo=ev.get("repo"))


def _rx_bytes() -> int | None:
    """System-wide bytes received across all NICs except loopback (Linux /sys). None if unreadable
    (off-Linux) — the download rate then just shows '—'. This is a whole-host figure by design
    (per-socket attribution isn't worth it): during a fill it's dominated by the HF download."""
    base = "/sys/class/net"
    try:
        total = 0
        for nic in os.listdir(base):
            if nic == "lo":
                continue
            p = os.path.join(base, nic, "statistics", "rx_bytes")
            if os.path.exists(p):
                with open(p) as fh:
                    total += int(fh.read().strip())
        return total
    except OSError:
        return None


def start(body: dict) -> dict:
    """Launch the guided fill. Optional body {"max_24h_gb": <float>} overrides the config cap
    (wishlist.yaml `download.max_24h_gb`; 0 = unlimited, default 1 TB/day)."""
    max_24h_gb = float(body["max_24h_gb"]) if "max_24h_gb" in body else wishlist.download()["max_24h_gb"]

    def work(should_stop, emit):
        log = telemetry.get_logger("fill")

        def logged_emit(ev: dict) -> None:                  # tee every progress event into the log, then the UI
            _log_event(log, ev)
            emit(ev)

        log.info("fill starting", max_24h_gb=max_24h_gb)
        try:
            logged_emit({"phase": "planning", "say": "resolving the active plan + placement…"})
            # fill.execute now resolves the active Plan (#33) + re-plans internally per drive-batch,
            # so there is no single up-front plan to compute here.
            ctx = fetch.RunCtx(
                con=data.conn(), lock=data._lock, on_progress=logged_emit,
                should_stop=should_stop,
                read_connection_factory=lambda: db.connect(read_only=True),
                check_hf_auth=True,
                request_action=fill_worker.WORKER.await_action,
            )
            res = fill.execute(ctx, max_24h_gb=max_24h_gb, guided=True)
            logged_emit({"result": res})
            log.info("fill finished", ok=res["ok"], stopped=res["stopped"],
                     state=res.get("state"), detail=res["message"])
            # Classify the terminal, PERSIST a non-DONE one for the loud on-open surface (DEF-023),
            # then translate to the worker's return (None = user Stop; a typed status dict covers
            # paused/blocked/plan-capacity-stop/done/error). Expected executor errors are returned,
            # not raised: raising would make the outer crash guard overwrite their evidence and
            # actions as UNHANDLED_FILL_ERROR.
            if res["ok"]:
                terminal = {"status": "done", "message": res["message"], "code": res.get("code")}
            elif res["stopped"] and should_stop():
                terminal = {"status": "stopped", "message": "stopped by request",
                            "code": "OPERATOR_STOP"}
            else:
                terminal = {"status": res.get("state", "error"), "message": res["message"],
                            "failed": res.get("failed"), "gate": res.get("gate"),
                            "code": res.get("code"), "evidence": res.get("evidence"),
                            "actions": res.get("actions")}
            _persist_terminal(terminal)
            return None if terminal["status"] == "stopped" else terminal
        except Exception as e:
            _persist_terminal({"status": "error", "message": str(e)[:300],
                               "code": "UNHANDLED_FILL_ERROR",
                               "actions": ["inspect_logs", "report_bug"]})
            log.exception("fill worker error", error=str(e)[:200])
            raise

    return fill_worker.WORKER.start(work)


def stop(body: dict | None = None) -> dict:
    """Request a clean stop at the next file boundary (idempotent)."""
    return fill_worker.WORKER.request_stop()


def gated_decision(body: dict) -> dict:
    """Resolve the currently displayed gated-access prompt; stale tabs cannot answer a new one."""
    decision_id = str(body.get("id") or "")
    action = str(body.get("action") or "")
    if not decision_id:
        return {"ok": False, "error": "prompt id is required"}
    return fill_worker.WORKER.resolve_action(decision_id, action)


def status() -> dict:
    """The worker's live status (phase, current drive/repo/file, rolling ratio, per-drive bytes,
    awaiting-drive prompt, terminal result) plus a live system-wide network download rate
    (`net_rx_bps`) sampled between polls. Cheap — polled by the Fill tab."""
    s = fill_worker.WORKER.status()
    rx, now = _rx_bytes(), time.monotonic()
    if rx is not None:
        with _net_lock:
            if _net["rx"] is not None and now > _net["t"]:
                s = dict(s, net_rx_bps=max(0.0, (rx - _net["rx"]) / (now - _net["t"])))
            _net["rx"], _net["t"] = rx, now
    return s


def confirm_drive(body: dict) -> dict:
    """The operator says they inserted a drive — report whether it now resolves to a live mount.
    The worker polls `archive_path` on its own, so this is just an immediate confirmation for the UI."""
    label = body["label"]
    with data._lock:
        mounted = register.archive_path(data.conn(), label) is not None
    return {"label": label, "mounted": mounted}
