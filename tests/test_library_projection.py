"""Reconciled CLI/portal projections retain mixed-cart policy blockers without crashing."""
from __future__ import annotations

import http.client
import io
import json
import sqlite3
import threading
from argparse import Namespace
from contextlib import contextmanager, redirect_stdout
from unittest import mock

from modelark import cli, librarian
from modelark.core import db
from modelark.web import data, server


def _catalog() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)
    con.execute(
        "INSERT INTO plans(plan_id,name,capacity_mode,is_active) "
        "VALUES('ark','Ark','guaranteed',1)"
    )
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes) "
        "VALUES('drive-00','primary',0,1000000,1000000)"
    )
    con.execute("INSERT INTO plan_drives(plan_id,drive_label) VALUES('ark','drive-00')")
    con.executemany(
        "INSERT INTO models(repo_id,category,numcopies) VALUES(?,?,1)",
        [("demo/safe", "generative-llm"), ("demo/pickle-only", "generative-llm")],
    )
    con.executemany(
        "INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-07-15')",
        [("demo/safe",), ("demo/pickle-only",)],
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('demo/safe','model.safetensors',100,'safetensors','bf16')"
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('demo/pickle-only','pytorch_model.bin',200,'pytorch','fp16')"
    )
    return con


def _assert_mixed_cart(plan: dict, queue: dict) -> None:
    assert plan["feasible"] is False
    assert plan["blocking_diagnostics"] == ["MANIFEST_POLICY"]
    assert plan["capacity_failures"] == []
    assert plan["totals"]["n_selected"] == 2
    assert plan["totals"]["n_planned"] == 1
    assert plan["totals"]["n_blocked"] == 1
    assert [item["repo"] for item in plan["drives"][0]["models"]] == ["demo/safe"]
    policy = next(item for item in plan["diagnostics"] if item["code"] == "MANIFEST_POLICY")
    assert policy["detail"]["repo_id"] == "demo/pickle-only"
    assert any(item["code"] == "MANIFEST_POLICY" for item in plan["advisories"])

    assert queue["feasible"] is False
    assert [item["repo"] for item in queue["models"]] == ["demo/pickle-only", "demo/safe"]
    by_repo = {item["repo"]: item for item in queue["models"]}
    assert by_repo["demo/safe"]["copy1"] == "drive-00"
    assert by_repo["demo/safe"]["blocking_diagnostics"] == []
    assert by_repo["demo/pickle-only"]["copy1"] is None
    assert by_repo["demo/pickle-only"]["size_known"] is False
    assert by_repo["demo/pickle-only"]["blocking_diagnostics"] == ["MANIFEST_POLICY"]


def test_library_views_adapt_reconciled_mixed_cart_without_omission():
    con = _catalog()
    with mock.patch("modelark.wishlist.exclude_pickle_only", return_value=True):
        plan = librarian.plan_view(con, plan_id="ark", capacity_mode="guaranteed")
        queue = librarian.queue_view(con, plan_id="ark", capacity_mode="guaranteed")
    _assert_mixed_cart(plan, queue)
    con.close()


def test_library_plan_json_cli_returns_typed_policy_result():
    con = _catalog()
    args = Namespace(explain=False, apply=False, json=True, repo=None)
    output = io.StringIO()
    with mock.patch.object(db, "connect", return_value=con), \
         mock.patch("modelark.wishlist.exclude_pickle_only", return_value=True), \
         redirect_stdout(output):
        cli.cmd_library_plan(args)
    payload = json.loads(output.getvalue())
    assert payload["feasible"] is False
    assert payload["blocking_diagnostics"] == ["MANIFEST_POLICY"]
    assert payload["totals"]["n_selected"] == 2


@contextmanager
def _portal():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    httpd.csrf_token = "projection-test-token"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _get(httpd, path: str) -> tuple[int, dict]:
    client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
    try:
        client.request("GET", path)
        response = client.getresponse()
        return response.status, json.loads(response.read())
    finally:
        client.close()


def test_http_plan_and_queue_return_typed_mixed_cart_instead_of_500():
    con = _catalog()
    with mock.patch.object(data, "conn", return_value=con), \
         mock.patch("modelark.wishlist.exclude_pickle_only", return_value=True), \
         _portal() as httpd:
        plan_status, plan = _get(httpd, "/api/library/plan")
        queue_status, queue = _get(httpd, "/api/library/queue")
    assert plan_status == 200 and queue_status == 200
    _assert_mixed_cart(plan, queue)
    con.close()
