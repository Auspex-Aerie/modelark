"""PR-01 portal mutation guard — tests-first contract (RFC-002 / DEC-049, issue #39 slice 1).

Gate 1: these express the reviewed guard contract BEFORE production code exists. Characterization
tests pass on today's behavior; change-contract tests are RED until the guard lands, and fail for the
reviewed reason (missing guard), not a broken fixture.

Reviewed contract (Gate 0 + Gate 1 corrections):
  * One shared server-side primitive (``FillWorker.guarded_mutation``) refuses portal selection
    ``finalize`` and every removal path (``toggle(..., on=False)``, ``bulk(..., on=False)``,
    ``clear()``) while the process-local Fill controller is live. Additions (``on=True``),
    ``oversize``, reads, and exports stay allowed.
  * Liveness is keyed on ``FillWorker.running()`` (live controller ownership), NEVER the retained
    status string, which keeps terminal values (e.g. ``done``) and never resets to ``idle``.
  * A distinct lifecycle gate ``FillWorker._gate`` linearizes ``start()`` against the guarded
    mutation. The mutation holds ``_gate`` across its DB op but NOT the state lock ``_lock``; worker
    progress/``_emit``/``status``/stop/terminal handling never touch ``_gate`` (Gate 0 correction 1).
  * Race invariant (Gate 1 correction 1/2): a mutation that wins the gate first may commit and THEN
    Fill may start — valid serialization. The forbidden case is a guarded mutation committing after
    Fill has claimed the gate and become live. ``start()`` MUST contend on the same gate.
  * Refusal contract: the ``REFUSAL`` dict below, surfaced by the server as HTTP 409 Conflict.
  * Semantic comparison (correction 6): snapshot the complete ordered selection-row state.

Out of scope for PR-01 (documented residual): external CLI writers, discover/manifest refresh,
protect/numcopies, plan/drive edits, the old executor's batch-boundary re-planning, and the pre-existing
idle deselect-of-finalized-row behavior.
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager

from modelark.core import db
from modelark.web import data, fill_worker, selection_api, server

# The approved typed refusal (Gate 0 correction 3), returned by the guarded selection paths while a
# Fill is live and surfaced by the server as HTTP 409 Conflict.
REFUSAL = {
    "ok": False,
    "refused": True,
    "code": "FILL_SESSION_ACTIVE",
    "error": "Selection finalization and removal are blocked while Fill is running.",
    "actions": ["wait_for_fill", "stop_fill"],
}

_HAS_PRIMITIVE = hasattr(fill_worker.FillWorker, "guarded_mutation")


def _require_primitive():
    """Fail fast and clearly (not via a swallowed thread AttributeError + timeout) when the reviewed
    primitive is still absent — the intended Gate-1 red for the threaded tests."""
    if not _HAS_PRIMITIVE:
        raise AssertionError("FillWorker.guarded_mutation is not implemented yet (expected Gate-1 red)")


# --------------------------------------------------------------------------- worker helpers

def _noop_work(should_stop, emit):
    return None


def _join(worker, timeout=5):
    """Bounded join on the worker's thread, then a final liveness assertion (no drain sleep-loop)."""
    thread = worker._thread
    if thread is not None:
        thread.join(timeout)
    assert not worker.running(), "worker thread did not exit within the join timeout"


@contextmanager
def _live(worker):
    """Make ``worker`` deterministically live for the block: the work body signals ``started`` (so we
    never sleep-poll to observe liveness) and blocks on ``release`` until teardown."""
    started = threading.Event()
    release = threading.Event()

    def work(should_stop, emit):
        started.set()
        release.wait(5)
        return None

    assert not worker.running(), "worker must be idle before a live block"
    assert worker.start(work)["ok"], "worker failed to start"
    assert started.wait(5), "worker work body never entered"
    try:
        assert worker.running(), "worker should be live inside the block"
        yield worker
    finally:
        release.set()
        _join(worker)


class _ProbeLock:
    """A lock that records acquisition attempts so a second (``start()``) acquire is observable
    without sleeps. Substituted for ``_gate`` to prove ``start()`` contends on the same gate."""

    def __init__(self):
        self._lock = threading.Lock()
        self.acquisitions = 0
        self.second_acquire = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        self.acquisitions += 1
        if self.acquisitions == 2:
            self.second_acquire.set()            # fires BEFORE blocking on the held real lock
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


class _Trap:
    """Any access raises — substituted for ``_gate`` to prove state ops never touch it (correction 2).
    Raising (instead of holding a real lock) turns a would-be deadlock into a deterministic failure."""

    def _boom(self, *a, **k):
        raise AssertionError("worker state operation must not touch the lifecycle gate `_gate`")

    __enter__ = __exit__ = acquire = release = _boom

    def __getattr__(self, name):
        raise AssertionError(f"worker state operation touched `_gate` (accessed .{name})")


# --------------------------------------------------------------------------- catalog fixture

def _apply_schema(con):
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)


@contextmanager
def _catalog(finalized=(), cart=(), extra_models=()):
    """An in-memory catalog wired into ``modelark.web.data`` with the given selection rows.

    ``finalized`` rows carry a ``finalized_at`` (the committed wishlist); ``cart`` rows are staged
    (``finalized_at IS NULL``). ``extra_models`` seeds repos that exist in ``models`` (for FK) but are
    not yet selected — used to exercise additions while live. Restores ``data._con``/``data.total`` and
    closes the connection on exit (correction: fixture hardening)."""
    con = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    _apply_schema(con)
    repos = sorted(set(finalized) | set(cart) | set(extra_models))
    for repo in repos:
        con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
    for repo in sorted(finalized):
        con.execute("INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-01-01 00:00:00')", [repo])
    for repo in sorted(cart):
        con.execute("INSERT INTO selection(repo_id) VALUES(?)", [repo])
    saved_con, saved_total = data._con, data.total
    data._con = con
    try:
        data.build_cache()                       # materialize ui_cache so summary() can run
        yield con
    finally:
        data._con, data.total = saved_con, saved_total
        con.close()


def _rows(con):
    """The complete ordered selection-row state — a semantic snapshot, not the SQLite file bytes."""
    return con.execute(
        "SELECT repo_id, added_at, finalized_at FROM selection ORDER BY repo_id"
    ).fetchall()


# =========================================================================== Part A: the primitive
# These target the reviewed FillWorker primitive directly (no DB). They are RED until `_gate` and
# `guarded_mutation` exist — the reviewed reason.

def test_state_operations_do_not_touch_gate():
    """`_gate` (lifecycle) is a distinct object from `_lock` (state), and state operations never touch
    `_gate`, so a held gate can never block progress/status/stop (correction 1). A trap makes an
    incorrect gate-reacquiring implementation fail deterministically instead of deadlocking (correction 2)."""
    w = fill_worker.FillWorker()
    assert w._gate is not w._lock
    w._gate = _Trap()                            # any access to the gate now raises
    w._emit({"status": "running", "message": "mid-flight"})
    assert w.status()["status"] == "running"
    assert w.request_stop()["ok"] is True


def test_guarded_mutation_runs_when_idle():
    w = fill_worker.FillWorker()
    ran = []
    result = w.guarded_mutation(lambda: (ran.append(1), "committed")[1])
    assert result == "committed" and ran == [1]


def test_guarded_mutation_refuses_when_live():
    w = fill_worker.FillWorker()
    with _live(w):
        ran = []
        result = w.guarded_mutation(lambda: (ran.append(1), "committed")[1])
    assert result is None and ran == [], "a live worker must refuse the mutation and not run it"


def test_finished_worker_is_idle_for_the_guard_though_status_is_terminal():
    """Liveness, not the retained status string: after the work returns, running() is false and the
    status is a terminal value (`done`, never reset to `idle`), so the guarded mutation runs."""
    _require_primitive()
    w = fill_worker.FillWorker()
    assert w.start(_noop_work)["ok"]
    _join(w)
    assert w.running() is False
    assert w.status()["status"] == "done", "terminal status must be retained, never reset to idle"
    ran = []
    assert w.guarded_mutation(lambda: (ran.append(1), "ok")[1]) == "ok"
    assert ran == [1]


def test_stop_requested_but_still_alive_keeps_refusing():
    """A stop-requested but still-alive worker (stopping-but-not-terminal) remains live and keeps
    refusing mutations."""
    w = fill_worker.FillWorker()
    with _live(w):
        assert w.request_stop()["ok"] is True
        assert w.running() is True               # thread still alive at its boundary
        ran = []
        assert w.guarded_mutation(lambda: ran.append(1)) is None
        assert ran == []


def test_mutation_first_then_start_succeeds():
    """Valid serialization: a mutation that wins the gate commits, and Fill may then start."""
    w = fill_worker.FillWorker()
    ran = []
    assert w.guarded_mutation(lambda: (ran.append(1), "ok")[1]) == "ok"
    assert ran == [1]
    assert w.start(_noop_work)["ok"] is True
    _join(w)


def test_start_first_then_mutation_refused():
    """Once Fill has claimed the gate and gone live, a subsequent mutation is refused and writes nothing."""
    w = fill_worker.FillWorker()
    with _live(w):
        ran = []
        result = w.guarded_mutation(lambda: (ran.append(1), "committed")[1])
        assert result is None and ran == [], "mutation must not commit after Fill is live"


def test_write_happens_under_the_gate():
    """No check/write gap: the mutation body executes while `_gate` is held. Deterministic — asserted
    from inside the body via a non-blocking acquire, with no timing wait."""
    w = fill_worker.FillWorker()
    gate_free_during_write = []

    def mutate():
        acquired = w._gate.acquire(blocking=False)
        gate_free_during_write.append(acquired)
        if acquired:
            w._gate.release()
        return "committed"

    assert w.guarded_mutation(mutate) == "committed"
    assert gate_free_during_write == [False], "the DB write must run while `_gate` is held"


def test_start_blocks_on_the_gate_until_mutation_commits():
    """Concurrency (Gate 1 correction 1): while a guarded mutation holds `_gate`, a concurrent
    ``start()`` MUST contend on that same gate and cannot go live until the mutation commits and
    releases it. Deterministic via an instrumented gate + events — no sleeps.

    This is the case a broken implementation (mutation gates, start() ignores the gate) would pass
    under the earlier tests: there, ``start()`` would never touch the probe and would go live while the
    mutation is parked."""
    _require_primitive()
    w = fill_worker.FillWorker()
    probe = _ProbeLock()
    w._gate = probe
    order = []
    in_mutate = threading.Event()
    release = threading.Event()
    worker_started = threading.Event()
    worker_release = threading.Event()
    mut_result, start_result = [], []

    def mutate():
        in_mutate.set()
        assert release.wait(5)
        order.append("commit")                   # the write completes here, still under `_gate`
        return "ok"

    def start_work(should_stop, emit):
        order.append("start")
        worker_started.set()
        worker_release.wait(5)
        return None

    mt = threading.Thread(target=lambda: mut_result.append(w.guarded_mutation(mutate)))
    mt.start()
    assert in_mutate.wait(5), "guarded mutation never entered its body"

    st = threading.Thread(target=lambda: start_result.append(w.start(start_work)))
    st.start()
    assert probe.second_acquire.wait(5), "start() did not contend on `_gate` (it ignores the gate)"
    # start() is now blocked inside the real acquire (mutation holds it); it cannot have gone live.
    assert order == [] and start_result == [], "Fill started before the mutation released the gate"

    release.set()                                # mutation commits, releases the gate, then start proceeds
    mt.join(5)
    assert not mt.is_alive() and mut_result == ["ok"]
    st.join(5)
    assert not st.is_alive() and start_result and start_result[0]["ok"] is True
    assert worker_started.wait(5)
    assert order == ["commit", "start"], "the mutation must commit before Fill goes live"

    worker_release.set()
    _join(w)


# =========================================================================== Part B: selection_api
# Endpoint-level semantics against a real (in-memory) catalog, driving the real process-local worker.

def test_idle_paths_mutate_as_before():
    """Characterization: with no Fill live, every selection path mutates exactly as today."""
    assert not fill_worker.WORKER.running()
    with _catalog(finalized=("a/one",), cart=("b/two",), extra_models=("c/three",)):
        selection_api.finalize()
        finalized = {r[0] for r in data.q("SELECT repo_id FROM selection WHERE finalized_at IS NOT NULL")}
        assert finalized == {"a/one", "b/two"}
        selection_api.toggle("a/one", False)
        assert not data.q("SELECT 1 FROM selection WHERE repo_id='a/one'")
        selection_api.bulk(["b/two"], False)
        assert not data.q("SELECT 1 FROM selection WHERE repo_id='b/two'")
        selection_api.toggle("c/three", True)
        selection_api.clear()
        assert not data.q("SELECT 1 FROM selection")


def test_removal_and_finalize_refused_while_live_preserve_rows():
    """Each guarded path returns the approved refusal contract and leaves the ordered selection-row
    state identical (semantic snapshot, correction 6)."""
    assert not fill_worker.WORKER.running()
    with _catalog(finalized=("a/one",), cart=("b/two", "c/three")) as con:
        before = _rows(con)
        with _live(fill_worker.WORKER):
            assert selection_api.finalize() == REFUSAL
            assert selection_api.clear() == REFUSAL
            assert selection_api.toggle("b/two", False) == REFUSAL
            assert selection_api.bulk(["c/three"], False) == REFUSAL
            assert _rows(con) == before, "a refused mutation must change no selection rows"
        assert _rows(con) == before


def test_additions_and_reads_allowed_while_live():
    """Do not broaden the guard: additions, oversize, and reads stay allowed while Fill is live
    (staged additions never alter the running wishlist, which reads finalized_at IS NOT NULL)."""
    assert not fill_worker.WORKER.running()
    with _catalog(finalized=("a/one",), extra_models=("d/new", "e/new")) as con:
        with _live(fill_worker.WORKER):
            assert selection_api.toggle("d/new", True).get("refused") is not True
            assert selection_api.bulk(["e/new"], True).get("refused") is not True
            assert selection_api.oversize({"selected_gb": 10, "cap_gb": 5}) == {"ok": True}
            assert selection_api.summary().get("refused") is not True
            assert selection_api.export_ids() == ["a/one", "d/new", "e/new"]
        staged = {r[0] for r in data.q("SELECT repo_id FROM selection WHERE finalized_at IS NULL")}
        assert {"d/new", "e/new"} <= staged
        assert con.execute("SELECT finalized_at FROM selection WHERE repo_id='a/one'").fetchone()[0]


# =========================================================================== Part C: HTTP contract


@contextmanager
def _portal():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    httpd.csrf_token = "test-csrf-token"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _post(httpd, path, body):
    con = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
    try:
        con.request("POST", path, body=json.dumps(body), headers={
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{httpd.server_port}",
            server.CSRF_HEADER: httpd.csrf_token,
        })
        resp = con.getresponse()
        return resp.status, json.loads(resp.read() or "{}")
    finally:
        con.close()


def test_http_refusal_is_409_with_contract_body():
    """The server maps a guarded refusal to HTTP 409 Conflict carrying the approved contract body,
    and no selection row changes."""
    assert not fill_worker.WORKER.running()
    with _catalog(finalized=("a/one",), cart=("b/two",)) as con:
        before = _rows(con)
        with _portal() as httpd, _live(fill_worker.WORKER):
            status, body = _post(httpd, "/api/selection/finalize", {})
            assert status == 409, f"expected 409 Conflict, got {status}"
            assert body == REFUSAL
            status, _ = _post(httpd, "/api/selection/clear", {})
            assert status == 409
        assert _rows(con) == before


# --------------------------------------------------------------------------- script runner

def main():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    passed, failed = [], []
    for name, fn in tests:
        try:
            fn()
            passed.append(name)
            print(f"PASS  {name}")
        except Exception as exc:                 # noqa: BLE001 — Gate-1 wants the full red/green map
            failed.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL  {name}  -> {type(exc).__name__}: {exc}")
    print(f"\n{len(passed)} passed, {len(failed)} failed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
