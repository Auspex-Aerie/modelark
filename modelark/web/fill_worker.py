"""A single, SAFE background worker for the Library Fill (task #22, DEC-019/020).

Guarantees (the reason this is its own tiny, dependency-free module — it's unit-tested in isolation):
  • ONE at a time      — start() refuses if a fill is already running (guarded).
  • stops cleanly       — request_stop() sets an Event; the work fn checks should_stop() at file/
                          repo boundaries and returns, so no write is ever half-done.
  • dies correctly      — the thread is a daemon: on portal shutdown the process sets the stop Event
                          (clean exit at the next boundary) and, if mid-download, the daemon dies with
                          the process. The DB is per-file transactional, so an abrupt death loses only
                          the in-flight file, which DEC-019 resume re-schedules — never corruption.
  • never crashes host  — the run body is wrapped; a worker exception becomes status='error', the
                          thread exits, and the portal keeps serving.
  • thread-safe state    — all state reads/writes go through one lock.

The actual work is INJECTED as `work(should_stop, emit)` so this class has no DB/portal coupling
and can be tested with a mock. The portal supplies a fill runner that uses the shared connection.
"""
from __future__ import annotations

import threading
import time


class FillWorker:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._state: dict = {"status": "idle", "message": ""}
        self._decision_event = threading.Event()
        self._decision_id: str | None = None
        self._decision_response: str | None = None

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, work) -> dict:
        """work(should_stop, emit): the fill body. should_stop() -> bool (check at boundaries);
        emit(dict) merges fields into the live status. work MAY return a terminal status dict
        (e.g. {"status": "paused"|"blocked", ...}) to classify a clean, non-completion end — the
        worker emits it verbatim instead of the default "done"/"fill complete". Refuses a second
        concurrent fill."""
        with self._lock:
            if self.running():
                return {"ok": False, "error": "a fill is already running"}
            self._stop.clear()
            self._decision_event.clear()
            self._decision_id = None
            self._decision_response = None
            self._state = {"status": "running", "message": "starting…"}
            self._thread = threading.Thread(target=self._run, args=(work,), name="modelark-fill", daemon=True)
            self._thread.start()
        return {"ok": True}

    def request_stop(self) -> dict:
        """Ask the worker to stop at the next safe boundary (idempotent; safe to call on shutdown)."""
        self._stop.set()
        self._decision_event.set()                  # wake an operator prompt so Stop is immediate
        with self._lock:
            if self._state.get("status") == "running":
                self._state["message"] = "stopping after the current file…"
        return {"ok": True, "stopping": self.running()}

    def await_action(self, prompt: dict, timeout_seconds: float) -> str:
        """Publish one operator prompt and wait boundedly for its matching response.

        Only the fill thread calls this method. HTTP request threads call :meth:`resolve_action`;
        the id check prevents a stale tab from answering a later prompt. A timeout is an explicit
        result so the scheduler can park the work as a typed follow-up and continue safely.
        """
        decision_id = str(prompt["id"])
        deadline = time.time() + max(0.0, timeout_seconds)
        with self._lock:
            self._decision_event.clear()
            self._decision_id = decision_id
            self._decision_response = None
            self._state["operator_prompt"] = dict(prompt, deadline=deadline)
        while not self._stop.is_set():
            remaining = deadline - time.time()
            if remaining <= 0 or not self._decision_event.wait(min(0.25, remaining)):
                if remaining > 0:
                    continue
                break
            break
        with self._lock:
            response = self._decision_response if self._decision_id == decision_id else None
            self._decision_id = None
            self._decision_response = None
            self._state["operator_prompt"] = None
        self._decision_event.clear()
        if self._stop.is_set():
            return "stopped"
        return response or "timeout"

    def resolve_action(self, decision_id: str, action: str) -> dict:
        """Resolve the current prompt exactly once; reject stale ids and unsupported actions."""
        if action not in {"retry", "skip"}:
            return {"ok": False, "error": "action must be retry or skip"}
        with self._lock:
            if not self.running() or self._decision_id != decision_id:
                return {"ok": False, "error": "that fill prompt is no longer active"}
            if self._decision_response is not None:
                return {"ok": False, "error": "that fill prompt was already answered"}
            self._decision_response = action
            self._decision_event.set()
        return {"ok": True, "action": action}

    def status(self) -> dict:
        with self._lock:
            return dict(self._state, running=self.running())

    def _emit(self, ev: dict) -> None:
        with self._lock:
            self._state.update(ev)

    def _run(self, work) -> None:
        try:
            outcome = work(self._stop.is_set, self._emit)
            if self._stop.is_set():                              # a user Stop takes priority over any outcome
                self._emit({"status": "stopped", "message": "stopped by request"})
            elif isinstance(outcome, dict) and outcome.get("status"):
                self._emit(outcome)                              # work classified it (paused / blocked / done)
            else:
                self._emit({"status": "done", "message": "fill complete"})
        except Exception as e:                       # isolate: a worker crash must never take the portal down
            self._emit({"status": "error", "message": str(e)[:300]})


WORKER = FillWorker()          # one per portal process


def shutdown() -> None:
    """Called from the portal's shutdown path so the worker exits cleanly with the process."""
    WORKER.request_stop()
