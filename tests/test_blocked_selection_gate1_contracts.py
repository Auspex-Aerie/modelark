"""Blocked-selection Gate-1 contracts — expected red until Gate-2 production.

Locked design (Gate 0 accepted at 06fd604; DEC-058):
  - GET /api/plan/preview → plan_api.preview() on a dedicated read-only SQLite snapshot
  - Calls proposal.preview_pure exactly once; returns reduced response with exact gate_b_refusal
  - Never uses portal data.conn()/data._lock; never bootstraps; never draft/approve/mutate/start
  - Fill tab owns MANIFEST_POLICY notice; capacity-only stays on plan_view advisories
  - Bound Dismiss: bulk {ids, on:false, expected_revision, expected_selection_hash} with
    atomic PREVIEW_STALE CAS inside BEGIN IMMEDIATE before any deletion
  - FILL_SESSION_ACTIVE takes precedence when Fill owns the mutation gate
  - Replan = GET preview only; successful Dismiss auto-re-previews
  - _admission_terminal deleted in Gate 2 (no brittle source-absence pin here)

No production or UI implementation in this gate. Background green pins (INC-024 Q2,
selection-guard FILL_SESSION_ACTIVE without bindings) must remain green separately.
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import _pr09_gate1_fixtures as f
from modelark import proposal as prop
from modelark.core import db
from modelark.web import data, fill_worker, plan_api, selection_api, server


# Exact Q2 action tuple (INC-024 / DEC-058) — must match proposal._MANIFEST_POLICY_ACTIONS.
_Q2_ACTIONS = ["review_manifest_policy", "trim_selection", "replan"]

_PREVIEW_STALE = {
    "ok": False,
    "refused": True,
    "code": "PREVIEW_STALE",
    "error": "Selection changed since this preview. Replan before dismissing.",
    "actions": ["replan"],
}

_FILL_ACTIVE = {
    "ok": False,
    "refused": True,
    "code": "FILL_SESSION_ACTIVE",
    "error": "Selection finalization and removal are blocked while Fill is running.",
    "actions": ["wait_for_fill", "stop_fill"],
}

_STATIC = Path(__file__).resolve().parents[1] / "modelark" / "web" / "static"
_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _apply_schema(con):
    for statement in db._statements(db.SCHEMA_PATH.read_text()):
        con.execute(statement)


def _seed_planner(con, *, revision: int = 7):
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


def _seed_active_plan(con, plan_id="ark"):
    con.execute(
        "INSERT OR IGNORE INTO plans(plan_id,name,capacity_mode,is_active) "
        "VALUES(?,?, 'guaranteed', 1)",
        [plan_id, plan_id.title()],
    )
    con.execute("UPDATE plans SET is_active=0")
    con.execute("UPDATE plans SET is_active=1 WHERE plan_id=?", [plan_id])


def _add_drive(con, label="d0", *, free=10**12):
    con.execute(
        "INSERT OR IGNORE INTO drives(drive_label,capacity_bytes,free_bytes,role,raid_backed,"
        "lifecycle,eligibility,identity_epoch,write_generation,identity_fingerprint,"
        "write_authority,filesystem_capacity_bytes) "
        "VALUES(?,?,?,'primary',0,'active','enabled',1,1,?,'dedicated_local',?)",
        [label, free, free, "a" * 64, free],
    )
    con.execute(
        "INSERT OR IGNORE INTO plan_drives(plan_id,drive_label) VALUES('ark',?)", [label])


def _add_repo(con, repo, files, *, finalized=True):
    con.execute("INSERT OR IGNORE INTO models(repo_id,numcopies) VALUES(?,1)", [repo])
    if finalized:
        con.execute(
            "INSERT OR IGNORE INTO selection(repo_id,finalized_at) VALUES(?, '2026-01-01')",
            [repo],
        )
    else:
        con.execute("INSERT OR IGNORE INTO selection(repo_id) VALUES(?)", [repo])
    for rfilename, size, fmt, quant, sha in files:
        con.execute(
            "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant,sha256) "
            "VALUES(?,?,?,?,?,?)",
            [repo, rfilename, size, fmt, quant, sha],
        )


def _policy_blocked_catalog(*, revision: int = 7) -> sqlite3.Connection:
    """Active plan + one placeable repo + two acquisition-policy blockers (Q2 shape)."""
    con = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    _apply_schema(con)
    _seed_planner(con, revision=revision)
    _seed_active_plan(con)
    _add_drive(con)
    _add_repo(con, "org/ok", [
        ("model.safetensors", 1000, "safetensors", "bf16", "a" * 64),
        ("config.json", 10, "aux", None, "b" * 64),
    ])
    _add_repo(con, "org/pickle", [
        ("pytorch_model.bin", 200, "pytorch", "fp16", "c" * 64),
    ])
    _add_repo(con, "org/noweights", [
        ("tokenizer.json", 10, "aux", None, "d" * 64),
        ("weird.dat", 10, "other", None, "e" * 64),
    ])
    return con


def _feasible_catalog(*, revision: int = 3) -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    _apply_schema(con)
    _seed_planner(con, revision=revision)
    _seed_active_plan(con)
    _add_drive(con)
    _add_repo(con, "org/ok", [
        ("model.safetensors", 1000, "safetensors", "bf16", "a" * 64),
    ])
    return con


def _no_active_plan_catalog() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    _apply_schema(con)
    _seed_planner(con, revision=1)
    # plans table empty — no active plan; must not bootstrap
    return con


def _rev(con) -> int:
    return int(con.execute(
        "SELECT planner_revision FROM planner_state WHERE singleton_id=1").fetchone()[0])


def _sel_rows(con):
    return con.execute(
        "SELECT repo_id, finalized_at FROM selection ORDER BY repo_id").fetchall()


def _sel_hash(con) -> str:
    return prop._selection_hash(con)


def _require_preview():
    if not hasattr(plan_api, "preview"):
        raise AssertionError(
            "plan_api.preview is not implemented yet (blocked-selection Gate-1 expected red; "
            "DEC-058 / GET /api/plan/preview)")
    return plan_api.preview


def _wire_portal_catalog(con):
    """Point web.data at an in-memory catalog (selection mutations + HTTP)."""
    saved_con, saved_total = data._con, getattr(data, "total", None)
    data._con = con
    try:
        data.build_cache()
    except Exception:
        pass
    return saved_con, saved_total


def _unwire_portal_catalog(saved):
    data._con, data.total = saved[0], saved[1]


@contextmanager
def _portal_catalog(con):
    saved = _wire_portal_catalog(con)
    try:
        yield con
    finally:
        _unwire_portal_catalog(saved)


@contextmanager
def _http_server():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    httpd.csrf_token = "blocked-selection-gate1"
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _http_get(httpd, path: str) -> tuple[int, dict | list | str]:
    client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    try:
        client.request("GET", path)
        resp = client.getresponse()
        raw = resp.read()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = raw.decode("utf-8", errors="replace")
        return resp.status, body
    finally:
        client.close()


def _http_post(httpd, path: str, body: dict) -> tuple[int, dict]:
    client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
    try:
        payload = json.dumps(body).encode()
        client.request(
            "POST", path, body=payload,
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
                "X-ModelArk-CSRF": "blocked-selection-gate1",
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


def _assert_exact_refusal_container(refusal, *, label: str):
    assert isinstance(refusal, dict), f"{label}: gate_b_refusal must be dict, got {refusal!r}"
    assert refusal.get("code") == "MANIFEST_POLICY", (
        f"{label}: code must be MANIFEST_POLICY, got {refusal.get('code')!r}")
    assert refusal.get("gate") == "B", f"{label}: gate must be 'B', got {refusal.get('gate')!r}"
    evidence = refusal.get("evidence")
    assert isinstance(evidence, dict), f"{label}: evidence dict required"
    blocked = evidence.get("blocked_repositories")
    assert isinstance(blocked, list), f"{label}: blocked_repositories list required"
    assert refusal.get("actions") == _Q2_ACTIONS, (
        f"{label}: actions must be exactly {_Q2_ACTIONS}, got {refusal.get('actions')!r}")
    return blocked


def _assert_reduced_preview(body, *, label: str, expect_refusal: bool):
    assert isinstance(body, dict), f"{label}: reduced preview must be a dict"
    assert body.get("ok") is True, f"{label}: ok must be True, got {body!r}"
    assert "plan_id" in body and body["plan_id"], f"{label}: plan_id required"
    assert isinstance(body.get("based_on_revision"), int), (
        f"{label}: based_on_revision int required, got {body.get('based_on_revision')!r}")
    assert isinstance(body.get("selection_before_hash"), str) and body["selection_before_hash"], (
        f"{label}: selection_before_hash required")
    assert "gate_b_code" in body, f"{label}: gate_b_code required"
    # Full pure payload must not leak.
    for banned in ("tasks", "files", "canonical_hash", "header", "mutation"):
        assert banned not in body, (
            f"{label}: reduced response must not expose {banned!r}; keys={sorted(body)}")
    if expect_refusal:
        assert body.get("gate_b_code") == "INFEASIBLE", (
            f"{label}: gate_b_code must be INFEASIBLE when refusal present")
        blocked = _assert_exact_refusal_container(body.get("gate_b_refusal"), label=label)
        by_id = {r["repo_id"]: r["reason"] for r in blocked}
        assert set(by_id) == {"org/pickle", "org/noweights"}, (
            f"{label}: blocked set must be exactly policy blockers, got {set(by_id)}")
        assert "org/ok" not in by_id
        assert by_id["org/pickle"] and by_id["org/noweights"]
        assert by_id["org/pickle"] != by_id["org/noweights"]
    else:
        assert body.get("gate_b_refusal") is None, (
            f"{label}: gate_b_refusal must be null when absent, got {body.get('gate_b_refusal')!r}")


def _pure_preview_refusal(con):
    """Ground truth from proposal.preview_pure (canonical authority)."""
    pure = prop.preview_pure(con, "ark", ("adopt_current", ()))
    return pure


# ===========================================================================
# Preview endpoint — reduced response + read-only snapshot
# ===========================================================================


class _ConnProxy:
    """Delegate to a real sqlite3 connection; swallow close() so fixtures stay inspectable."""

    def __init__(self, real: sqlite3.Connection):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "closed", False)

    def close(self):
        object.__setattr__(self, "closed", True)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


def _suppress_close(con):
    """Keep the fixture connection inspectable after production close()."""
    return _ConnProxy(con)


def test_bs01_preview_reduced_response_and_exact_nested_refusal():
    """Reduced GET preview carries exact gate_b_refusal from pure preview."""
    con = _suppress_close(_policy_blocked_catalog(revision=11))
    pure = _pure_preview_refusal(con)
    assert pure.get("gate_b_refusal") is not None, "fixture must produce gate_b_refusal"
    pure_refusal = pure["gate_b_refusal"]
    expected_sel_hash = pure["header"]["selection_before_hash"]

    preview_fn = _require_preview()
    forbidden_lock = mock.MagicMock()
    forbidden_lock.__enter__.side_effect = AssertionError(
        "plan_api.preview must not hold data._lock (DEC-058)")
    with mock.patch("modelark.core.db.connect", return_value=con) as connect, \
            mock.patch.object(data, "_lock", forbidden_lock), \
            mock.patch.object(data, "conn",
                              side_effect=AssertionError(
                                  "plan_api.preview must not use data.conn()")):
        result = preview_fn()

    connect.assert_called_with(read_only=True)
    _assert_reduced_preview(result, label="bs01", expect_refusal=True)
    assert result["based_on_revision"] == 11
    assert result["selection_before_hash"] == expected_sel_hash, (
        f"bs01: selection_before_hash must match pure header, "
        f"got {result['selection_before_hash']!r} vs {expected_sel_hash!r}")
    assert result["gate_b_refusal"] == pure_refusal, (
        "bs01: gate_b_refusal must be the exact preview_pure container, not reconstructed")


def test_bs02_preview_uses_dedicated_readonly_snapshot_not_shared_portal_conn():
    con = _suppress_close(_policy_blocked_catalog())
    preview_fn = _require_preview()
    forbidden_lock = mock.MagicMock()
    forbidden_lock.__enter__.side_effect = AssertionError(
        "must not hold data._lock while planning (DEC-058)")
    shared_conn = mock.Mock(side_effect=AssertionError(
        "must not use portal data.conn() for preview (DEC-058)"))

    with mock.patch("modelark.core.db.connect", return_value=con) as connect, \
            mock.patch.object(data, "_lock", forbidden_lock), \
            mock.patch.object(data, "conn", shared_conn):
        result = preview_fn()

    connect.assert_called_with(read_only=True)
    forbidden_lock.__enter__.assert_not_called()
    shared_conn.assert_not_called()
    _assert_reduced_preview(result, label="bs02", expect_refusal=True)


def test_bs03_preview_closes_connection():
    con = _policy_blocked_catalog()
    preview_fn = _require_preview()
    with mock.patch("modelark.core.db.connect", return_value=con):
        preview_fn()
    try:
        con.execute("SELECT 1")
        raise AssertionError("bs03: preview must close its dedicated connection")
    except sqlite3.ProgrammingError:
        pass


def test_bs04_no_active_plan_fails_closed_without_bootstrap():
    con = _suppress_close(_no_active_plan_catalog())
    preview_fn = _require_preview()
    with mock.patch("modelark.core.db.connect", return_value=con), \
            mock.patch("modelark.plan.bootstrap") as bootstrap:
        result = preview_fn()
    bootstrap.assert_not_called()
    assert result.get("ok") is False, (
        f"bs04: no active plan must fail closed without bootstrap, got {result!r}")
    err = result.get("error") or result.get("code") or ""
    assert err, f"bs04: typed error required, got {result!r}"


def test_bs05_preview_is_side_effect_free():
    """No draft, approval, revision bump, selection change, or Fill start."""
    con = _suppress_close(_policy_blocked_catalog(revision=5))
    before_rev = _rev(con)
    before_sel = _sel_rows(con)
    before_props = con.execute("SELECT count(*) FROM placement_proposals").fetchone()[0]
    preview_fn = _require_preview()

    with mock.patch("modelark.core.db.connect", return_value=con), \
            mock.patch("modelark.proposal.publish_draft") as publish, \
            mock.patch("modelark.proposal.create_draft") as create, \
            mock.patch("modelark.proposal.approve") as approve, \
            mock.patch("modelark.execution_service.start_fill") as start_fill:
        result = preview_fn()

    publish.assert_not_called()
    create.assert_not_called()
    approve.assert_not_called()
    start_fill.assert_not_called()
    assert _rev(con) == before_rev
    assert _sel_rows(con) == before_sel
    assert con.execute("SELECT count(*) FROM placement_proposals").fetchone()[0] == before_props
    _assert_reduced_preview(result, label="bs05", expect_refusal=True)


def test_bs06_preview_calls_preview_pure_not_plan_view():
    con = _suppress_close(_policy_blocked_catalog())
    preview_fn = _require_preview()
    pure_calls = []

    real_pure = prop.preview_pure

    def spy_pure(*a, **k):
        pure_calls.append((a, k))
        return real_pure(*a, **k)

    with mock.patch("modelark.core.db.connect", return_value=con), \
            mock.patch("modelark.proposal.preview_pure", side_effect=spy_pure) as pure_m, \
            mock.patch("modelark.librarian.plan_view") as plan_view, \
            mock.patch("modelark.proposal.create_draft") as create_draft:
        result = preview_fn()

    assert pure_m.call_count == 1, (
        f"bs06: must call preview_pure exactly once, got {pure_m.call_count}")
    args, kwargs = pure_calls[0]
    mut = kwargs.get("mutation")
    if mut is None and len(args) >= 3:
        mut = args[2]
    assert mut == ("adopt_current", ()) or list(mut) == ["adopt_current", []], (
        f"bs06: mutation must be adopt_current, got {mut!r}")
    plan_view.assert_not_called()
    create_draft.assert_not_called()
    _assert_reduced_preview(result, label="bs06", expect_refusal=True)


def test_bs07_http_get_plan_preview_route():
    con = _suppress_close(_policy_blocked_catalog(revision=9))
    with mock.patch("modelark.core.db.connect", return_value=con), \
            _http_server() as httpd:
        status, body = _http_get(httpd, "/api/plan/preview")
    assert status == 200, f"bs07: GET /api/plan/preview must exist, got {status} {body!r}"
    _assert_reduced_preview(body, label="bs07", expect_refusal=True)


def test_bs08_feasible_preview_null_refusal():
    con = _suppress_close(_feasible_catalog(revision=2))
    pure = prop.preview_pure(con, "ark", ("adopt_current", ()))
    assert pure.get("gate_b_refusal") is None
    preview_fn = _require_preview()
    with mock.patch("modelark.core.db.connect", return_value=con):
        result = preview_fn()
    _assert_reduced_preview(result, label="bs08", expect_refusal=False)
    assert result.get("gate_b_code") == pure["header"].get("gate_b_code")


# ===========================================================================
# Dismiss CAS on bulk removal
# ===========================================================================


def test_bs09_revision_mismatch_preview_stale_no_mutation():
    con = _policy_blocked_catalog(revision=10)
    with _portal_catalog(con):
        before_sel = _sel_rows(con)
        before_rev = _rev(con)
        sel_hash = _sel_hash(con)
        # Bound dismiss with stale revision
        result = selection_api.bulk(
            ids=["org/pickle", "org/noweights"],
            on=False,
            expected_revision=before_rev - 1,
            expected_selection_hash=sel_hash,
        )
        # Also accept dict body form used by HTTP
        if not (isinstance(result, dict) and result.get("code") == "PREVIEW_STALE"):
            result = selection_api.bulk({
                "ids": ["org/pickle", "org/noweights"],
                "on": False,
                "expected_revision": before_rev - 1,
                "expected_selection_hash": sel_hash,
            })
    assert result.get("code") == "PREVIEW_STALE", (
        f"bs09: revision mismatch must refuse PREVIEW_STALE, got {result!r}")
    assert result.get("refused") is True
    assert result.get("ok") is False
    assert result.get("error") == _PREVIEW_STALE["error"]
    assert result.get("actions") == ["replan"]
    ev = result.get("evidence") or {}
    assert ev.get("current_revision") == before_rev
    assert ev.get("based_on_revision") == before_rev - 1
    assert _sel_rows(con) == before_sel, "bs09: must not delete on PREVIEW_STALE"
    assert _rev(con) == before_rev, "bs09: must not bump revision on PREVIEW_STALE"


def test_bs10_selection_hash_mismatch_preview_stale_no_mutation():
    con = _policy_blocked_catalog(revision=10)
    with _portal_catalog(con):
        before_sel = _sel_rows(con)
        before_rev = _rev(con)
        result = selection_api.bulk({
            "ids": ["org/pickle"],
            "on": False,
            "expected_revision": before_rev,
            "expected_selection_hash": "0" * 64,  # wrong hash, revision matches
        })
    assert result.get("code") == "PREVIEW_STALE", (
        f"bs10: selection-hash mismatch must refuse PREVIEW_STALE, got {result!r}")
    assert result.get("refused") is True
    assert result.get("ok") is False
    ev = result.get("evidence") or {}
    assert ev.get("selection_changed") is True
    assert _sel_rows(con) == before_sel
    assert _rev(con) == before_rev


def test_bs11_http_bound_dismiss_revision_mismatch_is_409():
    con = _policy_blocked_catalog(revision=6)
    with _portal_catalog(con), _http_server() as httpd:
        before = _sel_rows(con)
        status, body = _http_post(httpd, "/api/selection/bulk", {
            "ids": ["org/pickle", "org/noweights"],
            "on": False,
            "expected_revision": 1,
            "expected_selection_hash": _sel_hash(con),
        })
    assert status == 409, f"bs11: PREVIEW_STALE must be HTTP 409, got {status} {body!r}"
    assert body.get("code") == "PREVIEW_STALE"
    assert body.get("refused") is True
    assert _sel_rows(con) == before


def test_bs12_live_fill_bound_dismiss_fill_session_active():
    """FILL_SESSION_ACTIVE takes precedence; bindings do not bypass the live guard."""
    con = _policy_blocked_catalog(revision=4)
    with _portal_catalog(con):
        before = _sel_rows(con)
        before_rev = _rev(con)
        with _live_fill():
            result = selection_api.bulk({
                "ids": ["org/pickle"],
                "on": False,
                "expected_revision": before_rev,
                "expected_selection_hash": _sel_hash(con),
            })
    assert result == _FILL_ACTIVE or (
        result.get("code") == "FILL_SESSION_ACTIVE" and result.get("refused") is True
    ), f"bs12: live Fill must yield FILL_SESSION_ACTIVE, got {result!r}"
    assert _sel_rows(con) == before
    assert _rev(con) == before_rev


def test_bs13_successful_bound_dismiss_atomic_exact_bump_once():
    con = _policy_blocked_catalog(revision=8)
    with _portal_catalog(con):
        before_rev = _rev(con)
        sel_hash = _sel_hash(con)
        result = selection_api.bulk({
            "ids": ["org/pickle", "org/noweights"],
            "on": False,
            "expected_revision": before_rev,
            "expected_selection_hash": sel_hash,
        })
        after_sel = {r[0] for r in _sel_rows(con)}
        after_rev = _rev(con)
    assert result.get("refused") is not True, f"bs13: matching bindings must succeed, got {result!r}"
    # Bound success must be explicit (today's summary lacks ok/planner_revision — expected red).
    assert result.get("ok") is True, (
        f"bs13: bound success must set ok=True (DEC-058), got {result!r}")
    assert after_sel == {"org/ok"}, f"bs13: must remove exact blocked ids only, got {after_sel}"
    assert "org/pickle" not in after_sel and "org/noweights" not in after_sel
    assert after_rev == before_rev + 1, (
        f"bs13: must bump planner_revision exactly once, {before_rev} → {after_rev}")


def test_bs14_unbound_catalog_bulk_still_works_without_bindings():
    """Compatible extension: ordinary Catalog bulk-off without CAS fields continues to work.

    This pin may be green at the parent tip (existing behavior). It must not regress when CAS lands.
    """
    con = _policy_blocked_catalog(revision=3)
    with _portal_catalog(con):
        before_rev = _rev(con)
        result = selection_api.bulk(ids=["org/pickle"], on=False)
        after = {r[0] for r in _sel_rows(con)}
    assert result.get("refused") is not True, f"bs14: unbound bulk must succeed, got {result!r}"
    assert "org/pickle" not in after
    assert _rev(con) == before_rev + 1


# ===========================================================================
# Notice / UI wiring contracts (source + pure client rules)
# ===========================================================================


def _escape_html(value: str) -> str:
    """Mirror MA.esc / app.js escapeHTML — notice must apply this to repo_id and reason."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _notice_rows_from_preview(preview: dict) -> list[dict]:
    """Client contract: rows come only from MANIFEST_POLICY gate_b_refusal."""
    refusal = preview.get("gate_b_refusal")
    if not isinstance(refusal, dict) or refusal.get("code") != "MANIFEST_POLICY":
        return []
    evidence = refusal.get("evidence") or {}
    blocked = evidence.get("blocked_repositories") or []
    return [
        {"repo_id": r["repo_id"], "reason": r["reason"],
         "repo_id_esc": _escape_html(r["repo_id"]),
         "reason_esc": _escape_html(r["reason"])}
        for r in blocked
        if isinstance(r, dict) and r.get("repo_id")
    ]


def _dismiss_body_from_preview(preview: dict) -> dict:
    rows = _notice_rows_from_preview(preview)
    return {
        "ids": [r["repo_id"] for r in rows],
        "on": False,
        "expected_revision": preview["based_on_revision"],
        "expected_selection_hash": preview["selection_before_hash"],
    }


def test_bs15_notice_lists_every_repo_reason_and_escapes():
    """Notice model lists every blocked repo/reason and HTML-escapes both."""
    # Production Gate-2 wires this into Fill; contract pins the data→row transform
    # against a real pure-preview ground truth once plan_api.preview exists.
    con = _policy_blocked_catalog()
    pure = prop.preview_pure(con, "ark", ("adopt_current", ()))
    pure_refusal = pure["gate_b_refusal"]
    # Hostile strings: ensure escape contract is non-trivial when reasons contain markup.
    # Use pure container shape with injected special chars via a synthetic reduced preview.
    synthetic = {
        "ok": True,
        "plan_id": "ark",
        "based_on_revision": 1,
        "selection_before_hash": "ab" * 32,
        "gate_b_code": "INFEASIBLE",
        "gate_b_refusal": {
            "code": "MANIFEST_POLICY",
            "gate": "B",
            "evidence": {
                "blocked_repositories": [
                    {"repo_id": "org/<script>", "reason": 'pickle & "only" <x>'},
                    {"repo_id": "org/noweights", "reason": pure_refusal["evidence"]
                     ["blocked_repositories"][1]["reason"]
                     if pure_refusal else "no weights"},
                ],
            },
            "actions": list(_Q2_ACTIONS),
        },
    }
    rows = _notice_rows_from_preview(synthetic)
    assert len(rows) == 2
    assert rows[0]["repo_id_esc"] == "org/&lt;script&gt;"
    assert "&amp;" in rows[0]["reason_esc"] and "&lt;" in rows[0]["reason_esc"]
    assert rows[0]["reason_esc"] != rows[0]["reason"]
    # Production Fill must actually perform this escaping (source pin).
    app_js = (_STATIC / "app.js").read_text()
    fill_js = (_STATIC / "fill.js").read_text()
    assert "esc" in app_js or "escapeHTML" in app_js
    assert "/api/plan/preview" in fill_js, (
        "bs15: Fill tab must fetch GET /api/plan/preview for the blocked-selection notice "
        "(Gate-1 expected red until Gate-2 UI)")
    assert "expected_revision" in fill_js and "expected_selection_hash" in fill_js, (
        "bs15: Dismiss must send both CAS bindings (Gate-1 expected red until Gate-2 UI)")


def test_bs16_dismiss_sends_exact_displayed_ids_plus_bindings():
    preview = {
        "ok": True,
        "plan_id": "ark",
        "based_on_revision": 42,
        "selection_before_hash": "f" * 64,
        "gate_b_code": "INFEASIBLE",
        "gate_b_refusal": {
            "code": "MANIFEST_POLICY",
            "gate": "B",
            "evidence": {
                "blocked_repositories": [
                    {"repo_id": "org/pickle", "reason": "pickle"},
                    {"repo_id": "org/noweights", "reason": "none"},
                ],
            },
            "actions": list(_Q2_ACTIONS),
        },
    }
    body = _dismiss_body_from_preview(preview)
    assert body == {
        "ids": ["org/pickle", "org/noweights"],
        "on": False,
        "expected_revision": 42,
        "expected_selection_hash": "f" * 64,
    }
    # Must not invent extra ids (e.g. from plan_view capacity rows).
    assert "org/ok" not in body["ids"]
    fill_js = (_STATIC / "fill.js").read_text()
    assert "/api/selection/bulk" in fill_js, (
        "bs16: Fill Dismiss must POST /api/selection/bulk (Gate-1 expected red until Gate-2 UI)")


def test_bs17_successful_dismiss_automatically_repreviews():
    """After successful bound Dismiss, UI must re-call GET /api/plan/preview (not create_draft)."""
    fill_js = (_STATIC / "fill.js").read_text()
    assert "/api/plan/preview" in fill_js, "bs17: re-preview requires /api/plan/preview in Fill"
    # Gate-2 must not wire replan/dismiss to draft publication or fill start as substitute.
    # Presence of preview path after dismiss is required; absence of create_draft is required.
    assert "create_draft" not in fill_js
    assert "/api/fill/start" in fill_js  # start remains for Start button — orthogonal
    # Marker: auto re-preview after dismiss (Gate-2 should keep a clear call site).
    # Until Gate 2, this fails on missing preview path already; strengthen with both markers:
    assert "PREVIEW_STALE" in fill_js or "expected_revision" in fill_js, (
        "bs17: Fill must handle bound dismiss / PREVIEW_STALE (expected red until Gate-2)")
    # Explicit re-preview after dismiss success is required production wiring.
    # Without a dedicated helper name, require both bulk and preview in fill.js and that
    # a dismiss path exists as a named concern — pin a required comment/marker Gate-2 adds:
    assert "blocked-selection" in fill_js or "gate_b_refusal" in fill_js or (
        "/api/plan/preview" in fill_js and "expected_selection_hash" in fill_js
    ), "bs17: Fill must wire blocked-selection dismiss → re-preview (expected red)"


def test_bs18_replan_is_preview_only():
    fill_js = (_STATIC / "fill.js").read_text()
    assert "/api/plan/preview" in fill_js, (
        "bs18: Replan must GET /api/plan/preview only (expected red until Gate-2 UI)")
    # Replan must not publish drafts via portal APIs that do not exist / must not call approve.
    for banned in ("/api/proposal/approve", "create_draft", "publish_draft"):
        assert banned not in fill_js, f"bs18: Replan must not reference {banned}"


def test_bs19_network_and_refusal_paths_retain_evidence_restore_controls():
    """Control-state contract: in-flight disables controls; error restores; evidence retained.

    Pure state rules the Fill notice controller must implement (Gate-2). Source pin ensures
    Fill owns disabled-while-in-flight behavior for the new controls.
    """
    def controls(*, in_flight: bool, last_error: str | None, evidence: dict | None):
        return {
            "dismiss_disabled": in_flight,
            "replan_disabled": in_flight,
            "evidence": evidence,  # retained even on error
            "error": last_error,
        }

    evidence = {"blocked_repositories": [{"repo_id": "org/pickle", "reason": "x"}]}
    inflight = controls(in_flight=True, last_error=None, evidence=evidence)
    assert inflight["dismiss_disabled"] and inflight["replan_disabled"]
    assert inflight["evidence"] is evidence
    failed = controls(in_flight=False, last_error="network", evidence=evidence)
    assert not failed["dismiss_disabled"] and not failed["replan_disabled"]
    assert failed["evidence"] is evidence and failed["error"] == "network"

    fill_js = (_STATIC / "fill.js").read_text()
    # Gate-2 notice controls must exist; until then this is red via missing preview wiring.
    assert "/api/plan/preview" in fill_js, (
        "bs19: Fill notice controls require /api/plan/preview (expected red until Gate-2)")


def test_bs20_capacity_only_diagnostics_never_populate_notice():
    """Capacity-only blockers must not invent a MANIFEST_POLICY notice."""
    # Synthetic plan_view-like capacity diagnostic must yield zero notice rows.
    capacity_only_preview = {
        "ok": True,
        "plan_id": "ark",
        "based_on_revision": 1,
        "selection_before_hash": "a" * 64,
        "gate_b_code": "INFEASIBLE",
        "gate_b_refusal": None,  # pure preview: no policy refusal
    }
    assert _notice_rows_from_preview(capacity_only_preview) == []

    # Even if a non-MANIFEST_POLICY container were wrongly attached, notice ignores it.
    wrong_code = {
        **capacity_only_preview,
        "gate_b_refusal": {
            "code": "REQUIREMENT_EXCEEDS_USABLE_MAX",
            "gate": "B",
            "evidence": {"blocked_repositories": [
                {"repo_id": "org/huge", "reason": "too big"},
            ]},
            "actions": ["replan"],
        },
    }
    assert _notice_rows_from_preview(wrong_code) == []

    # plan_view advisories are a sibling surface — not authority for this notice.
    fill_js = (_STATIC / "fill.js").read_text()
    # Until Gate 2, capacity continues via plan_view; notice must key on gate_b_refusal.
    assert "gate_b_refusal" in fill_js or "/api/plan/preview" in fill_js, (
        "bs20: notice must key on gate_b_refusal / plan preview, not capacity advisories alone")


def test_bs21_preview_pure_authority_not_reconcile_diagnostic_shape():
    """Behavioral pin: portal preview uses preview_pure container, not reconcile diagnostics."""
    con = _suppress_close(_policy_blocked_catalog())
    pure = prop.preview_pure(con, "ark", ("adopt_current", ()))
    pure_refusal = pure["gate_b_refusal"]
    assert pure_refusal and "blocked_repositories" in pure_refusal["evidence"]
    from modelark import librarian
    with mock.patch("modelark.wishlist.exclude_pickle_only", return_value=True):
        plan = librarian.plan_view(con, plan_id="ark", capacity_mode="guaranteed")
    assert "gate_b_refusal" not in plan
    assert "blocking_diagnostics" in plan
    preview_fn = _require_preview()
    with mock.patch("modelark.core.db.connect", return_value=con):
        result = preview_fn()
    assert result["gate_b_refusal"] == pure_refusal
    assert "blocking_diagnostics" not in (result.get("gate_b_refusal") or {})


# ===========================================================================
# Background characterization (may be green at parent tip)
# ===========================================================================


def test_bs_bg_unbound_bulk_http_still_200_without_cas():
    """Ordinary Catalog bulk without bindings remains 200 when idle (compat)."""
    con = _policy_blocked_catalog(revision=2)
    with _portal_catalog(con), _http_server() as httpd:
        status, body = _http_post(httpd, "/api/selection/bulk", {
            "ids": ["org/noweights"],
            "on": False,
        })
    assert status == 200, f"unbound bulk should stay 200, got {status} {body!r}"
    assert body.get("refused") is not True
