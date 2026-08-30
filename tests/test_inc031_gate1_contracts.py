"""INC-031 Gate-1 contracts — Bound Dismiss error envelope.

Contracts only. Production is unchanged, so c01 must be red until Gate 2
translates a DB-level ``proposal.Refusal("FILL_SESSION_ACTIVE")`` into the
existing typed 409 body. Browser residual c02 lives in
``tests/test_e2e_portal.py`` (``error500`` Dismiss mode).
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager

from modelark import proposal as prop
from modelark.core import db
from modelark.web import data, fill_worker, selection_api, server


_FILL_ACTIVE = {
    "ok": False,
    "refused": True,
    "code": "FILL_SESSION_ACTIVE",
    "error": "Selection finalization and removal are blocked while Fill is running.",
    "actions": ["wait_for_fill", "stop_fill"],
}


def _apply_schema(con):
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)


def _catalog(*, revision: int = 5) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    _apply_schema(con)
    n = con.execute("SELECT count(*) FROM planner_state WHERE singleton_id=1").fetchone()[0]
    if n == 0:
        con.execute(
            "INSERT INTO planner_state(singleton_id,planner_revision,"
            "active_approved_proposal_id,next_fencing_token) VALUES(1,?,?,0)",
            [revision, None],
        )
    else:
        con.execute(
            "UPDATE planner_state SET planner_revision=? WHERE singleton_id=1",
            [revision],
        )
    con.execute(
        "INSERT OR IGNORE INTO plans(plan_id,name,capacity_mode,is_active) "
        "VALUES('ark','Ark','guaranteed',1)")
    con.execute("UPDATE plans SET is_active=0")
    con.execute("UPDATE plans SET is_active=1 WHERE plan_id='ark'")
    for repo in ("org/a", "org/b"):
        con.execute("INSERT INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
        con.execute(
            "INSERT INTO selection(repo_id,finalized_at) VALUES(?, '2026-01-01')",
            [repo])
    return con


def _seed_live_db_session(con) -> None:
    """CLI/external live session: a starting row, no portal WORKER."""
    con.execute(
        "INSERT INTO placement_proposals("
        "proposal_id,plan_id,based_on_revision,lifecycle,canonical_hash,"
        "mutation_kind,serializer_version) "
        "VALUES('p-live','ark',0,'approved',?, 'replan','1')",
        ["a" * 64],
    )
    con.execute(
        "INSERT INTO execution_sessions("
        "session_id,plan_id,approved_proposal_id,controller_identity,state,"
        "bound_planner_revision,fencing_token) "
        "VALUES('sess-cli','ark','p-live','cli','starting',0,1)"
    )


def _rev(con) -> int:
    return int(con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])


def _sel_ids(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT repo_id FROM selection").fetchall()}


@contextmanager
def _portal_catalog(con):
    saved_con, saved_total = data._con, getattr(data, "total", None)
    data._con = con
    try:
        try:
            data.build_cache()
        except Exception:
            pass
        yield con
    finally:
        data._con, data.total = saved_con, saved_total


@contextmanager
def _http_server():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    httpd.csrf_token = "inc031-gate1"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _http_post(httpd, path: str, body: dict) -> tuple[int, dict]:
    client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    try:
        payload = json.dumps(body).encode()
        client.request(
            "POST", path, body=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "X-ModelArk-CSRF": "inc031-gate1",
                "Host": f"127.0.0.1:{httpd.server_port}",
                "Origin": f"http://127.0.0.1:{httpd.server_port}",
            },
        )
        resp = client.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        client.close()


def test_c01_db_live_session_bound_dismiss_is_typed_409():
    """DB live session + WORKER off must be HTTP 409 FILL_SESSION_ACTIVE, not 500.

    Today ``bulk`` lets ``proposal.Refusal`` escape to ``server.py`` ``except Exception``,
    so the client sees ``{"error": "FILL_SESSION_ACTIVE: …"}`` without ``refused``.
    """
    assert not fill_worker.WORKER.running(), (
        "c01 must exercise the DB-session path; a live portal WORKER would hide it"
    )
    con = _catalog(revision=5)
    _seed_live_db_session(con)
    with _portal_catalog(con), _http_server() as httpd:
        before_sel = _sel_ids(con)
        before_rev = _rev(con)
        status, body = _http_post(httpd, "/api/selection/bulk", {
            "ids": ["org/a"],
            "on": False,
            "expected_revision": before_rev,
            "expected_selection_hash": prop._selection_hash(con),
        })
    assert not fill_worker.WORKER.running()
    assert status == 409, (
        f"c01: DB-session bound Dismiss must be HTTP 409 typed refusal, "
        f"got {status} {body!r}"
    )
    assert body == _FILL_ACTIVE, f"c01: exact FILL_SESSION_ACTIVE body required, got {body!r}"
    assert _sel_ids(con) == before_sel
    assert _rev(con) == before_rev


def test_c03_api_typed_worker_refusal_still_exact():
    """Regression: process-local WORKER path stays the existing typed body."""
    con = _catalog(revision=4)
    with _portal_catalog(con):
        before_sel = _sel_ids(con)
        before_rev = _rev(con)
        started = threading.Event()
        release = threading.Event()

        def work(should_stop, emit):
            started.set()
            release.wait(5)
            return None

        w = fill_worker.WORKER
        assert not w.running()
        assert w.start(work)["ok"]
        assert started.wait(5)
        try:
            result = selection_api.bulk({
                "ids": ["org/a"],
                "on": False,
                "expected_revision": before_rev,
                "expected_selection_hash": prop._selection_hash(con),
            })
        finally:
            release.set()
            if w._thread is not None:
                w._thread.join(timeout=5)
            assert not w.running()
    assert result == _FILL_ACTIVE, f"c03: WORKER path must stay exact, got {result!r}"
    assert _sel_ids(con) == before_sel
    assert _rev(con) == before_rev
