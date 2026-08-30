"""Blocked-selection Gate-2 remediation contracts — CAS binding fail-closed.

Frozen Gate-1 contracts remain in test_blocked_selection_gate1_contracts.py.
This file only pins incomplete-binding refusal (PREVIEW_BINDINGS_REQUIRED).
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager

from modelark.core import db
from modelark.web import data, fill_worker, selection_api, server


_PREVIEW_BINDINGS_REQUIRED = {
    "ok": False,
    "refused": True,
    "code": "PREVIEW_BINDINGS_REQUIRED",
    "error": (
        "expected_revision and expected_selection_hash "
        "must be provided together."
    ),
    "actions": ["replan"],
}

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
    # Schema may seed planner_state; set revision explicitly.
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
    httpd.csrf_token = "blocked-selection-gate2-remediation"
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
                "X-ModelArk-CSRF": "blocked-selection-gate2-remediation",
                "Host": f"127.0.0.1:{httpd.server_port}",
                "Origin": f"http://127.0.0.1:{httpd.server_port}",
            },
        )
        resp = client.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        client.close()


@contextmanager
def _live_fill():
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
        yield w
    finally:
        release.set()
        if w._thread is not None:
            w._thread.join(timeout=5)
        assert not w.running()


def test_g2r01_revision_only_refuses_without_mutation():
    con = _catalog(revision=9)
    with _portal_catalog(con):
        before_sel = _sel_ids(con)
        before_rev = _rev(con)
        result = selection_api.bulk({
            "ids": ["org/a"],
            "on": False,
            "expected_revision": before_rev,
            # expected_selection_hash intentionally omitted
        })
    assert result == _PREVIEW_BINDINGS_REQUIRED, (
        f"g2r01: exact PREVIEW_BINDINGS_REQUIRED required, got {result!r}")
    assert _sel_ids(con) == before_sel
    assert _rev(con) == before_rev


def test_g2r02_hash_only_refuses_without_mutation():
    con = _catalog(revision=9)
    with _portal_catalog(con):
        before_sel = _sel_ids(con)
        before_rev = _rev(con)
        result = selection_api.bulk({
            "ids": ["org/a"],
            "on": False,
            "expected_selection_hash": "a" * 64,
            # expected_revision intentionally omitted
        })
    assert result == _PREVIEW_BINDINGS_REQUIRED
    assert _sel_ids(con) == before_sel
    assert _rev(con) == before_rev


def test_g2r03_http_incomplete_bindings_is_409_exact_body():
    con = _catalog(revision=4)
    with _portal_catalog(con), _http_server() as httpd:
        before_sel = _sel_ids(con)
        before_rev = _rev(con)
        status, body = _http_post(httpd, "/api/selection/bulk", {
            "ids": ["org/a", "org/b"],
            "on": False,
            "expected_revision": 4,
        })
    assert status == 409, f"g2r03: expected HTTP 409, got {status} {body!r}"
    assert body == _PREVIEW_BINDINGS_REQUIRED
    assert _sel_ids(con) == before_sel
    assert _rev(con) == before_rev


def test_g2r04_unbound_bulk_without_cas_keys_still_works():
    con = _catalog(revision=3)
    with _portal_catalog(con):
        before_rev = _rev(con)
        result = selection_api.bulk(ids=["org/a"], on=False)
        after = _sel_ids(con)
    assert result.get("refused") is not True, f"g2r04: unbound bulk must succeed, got {result!r}"
    assert "org/a" not in after
    assert "org/b" in after
    assert _rev(con) == before_rev + 1


def test_g2r05_fill_session_active_precedes_incomplete_bindings():
    con = _catalog(revision=2)
    with _portal_catalog(con):
        before_sel = _sel_ids(con)
        before_rev = _rev(con)
        with _live_fill():
            result = selection_api.bulk({
                "ids": ["org/a"],
                "on": False,
                "expected_revision": before_rev,
                # incomplete pair
            })
    assert result == _FILL_ACTIVE, (
        f"g2r05: FILL_SESSION_ACTIVE must precede PREVIEW_BINDINGS_REQUIRED, got {result!r}")
    assert _sel_ids(con) == before_sel
    assert _rev(con) == before_rev


def test_g2r06_null_pair_member_is_incomplete():
    """Key present with null still expresses bound intent and must refuse."""
    con = _catalog(revision=6)
    with _portal_catalog(con):
        before_sel = _sel_ids(con)
        before_rev = _rev(con)
        result = selection_api.bulk({
            "ids": ["org/b"],
            "on": False,
            "expected_revision": before_rev,
            "expected_selection_hash": None,
        })
    assert result == _PREVIEW_BINDINGS_REQUIRED
    assert _sel_ids(con) == before_sel
    assert _rev(con) == before_rev
