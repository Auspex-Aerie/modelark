"""Blocked-selection Gate-1 contracts — expected red until Gate-2 production.

Locked design (Gate 0 at 06fd604; DEC-058):
  - GET /api/plan/preview → plan_api.preview() on a dedicated read-only SQLite snapshot
  - Coherent read: BEGIN (not IMMEDIATE) before active-plan resolve + preview_pure; end before close
  - Exact reduced key set; exact gate_b_refusal from pure preview
  - Bound Dismiss CAS inside same BEGIN IMMEDIATE before first DELETE
  - Exact PREVIEW_STALE bodies; FILL_SESSION_ACTIVE exact equality
  - Successful bound Dismiss returns existing selection-summary shape (no invented ok=True)
  - Browser behavior is covered by tests/test_e2e_portal.py (Playwright), not marker strings

No production or UI implementation in this gate.
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from contextlib import contextmanager
from unittest import mock

from modelark import proposal as prop
from modelark.core import db
from modelark.web import data, fill_worker, plan_api, selection_api, server


_Q2_ACTIONS = ["review_manifest_policy", "trim_selection", "replan"]

# Exact reduced success key set (DEC-058) — no additional fields.
_REDUCED_KEYS = frozenset({
    "ok",
    "plan_id",
    "based_on_revision",
    "selection_before_hash",
    "gate_b_code",
    "gate_b_refusal",
})

# Exact no-active-plan response (no bootstrap). Sibling of plan_api.shadow_explain.
_NO_ACTIVE_PLAN = {
    "ok": False,
    "error": "no active plan",
}

_FILL_ACTIVE = {
    "ok": False,
    "refused": True,
    "code": "FILL_SESSION_ACTIVE",
    "error": "Selection finalization and removal are blocked while Fill is running.",
    "actions": ["wait_for_fill", "stop_fill"],
}

_PREVIEW_STALE_ERROR = "Selection changed since this preview. Replan before dismissing."


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


# ---------------------------------------------------------------------------
# Connection instrumentation
# ---------------------------------------------------------------------------


class _ConnProxy:
    """Delegate to a real connection; optional event log + optional close swallow."""

    def __init__(self, real: sqlite3.Connection, *, swallow_close: bool = False):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "events", [])
        object.__setattr__(self, "closed", False)
        object.__setattr__(self, "_swallow_close", swallow_close)

    def close(self):
        object.__getattribute__(self, "events").append("CLOSE")
        object.__setattr__(self, "closed", True)
        if not object.__getattribute__(self, "_swallow_close"):
            object.__getattribute__(self, "_real").close()

    def execute(self, sql, parameters=()):
        text = sql if isinstance(sql, str) else str(sql)
        u = " ".join(text.upper().split())
        events = object.__getattribute__(self, "events")
        if u.startswith("BEGIN IMMEDIATE"):
            events.append("BEGIN_IMMEDIATE")
        elif u.startswith("BEGIN"):
            events.append("BEGIN")
        if u.startswith("COMMIT") or u == "END" or u.startswith("END "):
            events.append("END_SNAPSHOT")
        if u.startswith("ROLLBACK"):
            events.append("END_SNAPSHOT")
        if "DELETE FROM SELECTION" in u:
            events.append("DELETE")
        if u.startswith("SELECT") and "PLANNER_REVISION" in u:
            events.append("CAS_REV_READ")
        # Canonical selection-hash read (proposal._selection_hash).
        if (
            u.startswith("SELECT")
            and "FROM SELECTION" in u
            and "REPO_ID" in u
            and "FINALIZED_AT" in u
        ):
            events.append("CAS_SEL_READ")
        real = object.__getattribute__(self, "_real")
        if parameters == () or parameters is None:
            try:
                return real.execute(sql)
            except TypeError:
                return real.execute(sql, parameters)
        return real.execute(sql, parameters)

    def executemany(self, sql, seq):
        text = sql if isinstance(sql, str) else str(sql)
        u = " ".join(text.upper().split())
        if "DELETE FROM SELECTION" in u:
            object.__getattribute__(self, "events").append("DELETE")
        return object.__getattribute__(self, "_real").executemany(sql, seq)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_real"), name)


def _proxy(con, *, swallow_close: bool = False) -> _ConnProxy:
    return _ConnProxy(con, swallow_close=swallow_close)


def _assert_exact_refusal_container(refusal, *, label: str):
    assert isinstance(refusal, dict), f"{label}: gate_b_refusal must be dict, got {refusal!r}"
    assert refusal.get("code") == "MANIFEST_POLICY"
    assert refusal.get("gate") == "B"
    evidence = refusal.get("evidence")
    assert isinstance(evidence, dict)
    blocked = evidence.get("blocked_repositories")
    assert isinstance(blocked, list)
    assert refusal.get("actions") == _Q2_ACTIONS
    return blocked


def _assert_reduced_preview(body, *, label: str, expect_refusal: bool):
    assert isinstance(body, dict), f"{label}: reduced preview must be a dict"
    assert set(body.keys()) == _REDUCED_KEYS, (
        f"{label}: reduced key set must be exactly {sorted(_REDUCED_KEYS)}, "
        f"got {sorted(body)}")
    assert body["ok"] is True
    assert body["plan_id"]
    assert isinstance(body["based_on_revision"], int)
    assert isinstance(body["selection_before_hash"], str) and body["selection_before_hash"]
    assert "gate_b_code" in body
    if expect_refusal:
        assert body["gate_b_code"] == "INFEASIBLE"
        blocked = _assert_exact_refusal_container(body["gate_b_refusal"], label=label)
        by_id = {r["repo_id"]: r["reason"] for r in blocked}
        assert set(by_id) == {"org/pickle", "org/noweights"}
        assert "org/ok" not in by_id
        assert by_id["org/pickle"] != by_id["org/noweights"]
    else:
        assert body["gate_b_refusal"] is None


def _assert_preview_stale(result, *, current_revision, based_on_revision, selection_changed, label):
    """Exact PREVIEW_STALE body — complete key set, message, evidence, actions."""
    expected = {
        "ok": False,
        "refused": True,
        "code": "PREVIEW_STALE",
        "error": _PREVIEW_STALE_ERROR,
        "evidence": {
            "current_revision": current_revision,
            "based_on_revision": based_on_revision,
            "selection_changed": selection_changed,
        },
        "actions": ["replan"],
    }
    assert result == expected, f"{label}: PREVIEW_STALE body mismatch:\n got {result!r}\n want {expected!r}"


def _assert_cas_before_delete(events, *, label: str, expect_delete: bool):
    """Both CAS reads inside the same BEGIN IMMEDIATE and before the first DELETE."""
    assert "BEGIN_IMMEDIATE" in events, f"{label}: bound dismiss requires BEGIN IMMEDIATE, got {events}"
    bi = events.index("BEGIN_IMMEDIATE")
    # No second write-begin before CAS completes.
    assert "BEGIN" not in events[bi + 1:], (
        f"{label}: no non-IMMEDIATE BEGIN after BEGIN IMMEDIATE, got {events}")
    if expect_delete:
        assert "DELETE" in events, f"{label}: expected DELETE, got {events}"
        di = events.index("DELETE")
        assert bi < di, f"{label}: BEGIN IMMEDIATE must precede DELETE"
        window = events[bi:di]
        assert "CAS_REV_READ" in window, (
            f"{label}: planner_revision read must occur after BEGIN IMMEDIATE and before DELETE; "
            f"events={events}")
        assert "CAS_SEL_READ" in window, (
            f"{label}: selection-hash read must occur after BEGIN IMMEDIATE and before DELETE; "
            f"events={events}")
    else:
        assert "DELETE" not in events, f"{label}: must not DELETE on refusal, got {events}"
        # Comparisons still run inside the transaction before abort.
        after = events[bi:]
        assert "CAS_REV_READ" in after, (
            f"{label}: revision compare requires CAS_REV_READ after BEGIN IMMEDIATE; events={events}")
        assert "CAS_SEL_READ" in after, (
            f"{label}: selection-hash compare requires CAS_SEL_READ after BEGIN IMMEDIATE; events={events}")


# ===========================================================================
# Preview endpoint
# ===========================================================================


def test_bs01_preview_reduced_response_exact_keys_and_nested_refusal():
    con = _proxy(_policy_blocked_catalog(revision=11), swallow_close=True)
    pure = prop.preview_pure(con, "ark", ("adopt_current", ()))
    pure_refusal = pure["gate_b_refusal"]
    expected_sel_hash = pure["header"]["selection_before_hash"]
    preview_fn = _require_preview()
    forbidden_lock = mock.MagicMock()
    forbidden_lock.__enter__.side_effect = AssertionError("must not hold data._lock")
    with mock.patch("modelark.core.db.connect", return_value=con) as connect, \
            mock.patch.object(data, "_lock", forbidden_lock), \
            mock.patch.object(data, "conn",
                              side_effect=AssertionError("must not use data.conn()")):
        result = preview_fn()
    connect.assert_called_with(read_only=True)
    _assert_reduced_preview(result, label="bs01", expect_refusal=True)
    assert result["based_on_revision"] == 11
    assert result["selection_before_hash"] == expected_sel_hash
    assert result["gate_b_refusal"] == pure_refusal


def test_bs02_coherent_read_snapshot_order():
    """BEGIN before active-plan + preview_pure; snapshot ends before close; no IMMEDIATE/shared lock."""
    real = _policy_blocked_catalog()
    con = _proxy(real, swallow_close=True)
    preview_fn = _require_preview()
    order: list[str] = []
    from modelark import plan as plan_mod
    real_active = plan_mod.active
    real_pure = prop.preview_pure

    def track_active(c, *a, **k):
        order.append("ACTIVE")
        return real_active(c, *a, **k)

    def track_pure(c, *a, **k):
        order.append("PREVIEW_PURE")
        return real_pure(c, *a, **k)

    forbidden_lock = mock.MagicMock()
    forbidden_lock.__enter__.side_effect = AssertionError("must not hold data._lock")
    with mock.patch("modelark.core.db.connect", return_value=con) as connect, \
            mock.patch.object(data, "_lock", forbidden_lock), \
            mock.patch.object(data, "conn",
                              side_effect=AssertionError("must not use data.conn()")), \
            mock.patch("modelark.plan.active", side_effect=track_active), \
            mock.patch("modelark.proposal.preview_pure", side_effect=track_pure):
        result = preview_fn()

    connect.assert_called_with(read_only=True)
    forbidden_lock.__enter__.assert_not_called()
    assert "BEGIN_IMMEDIATE" not in con.events, (
        f"bs02: read snapshot must not use BEGIN IMMEDIATE, got {con.events}")
    assert "BEGIN" in con.events, f"bs02: coherent read requires BEGIN, got {con.events}"
    bi = con.events.index("BEGIN")
    # ACTIVE and PREVIEW_PURE are logical steps; SQL BEGIN must precede both.
    assert "ACTIVE" in order and "PREVIEW_PURE" in order
    assert order.index("ACTIVE") < order.index("PREVIEW_PURE")
    # Snapshot ends before close.
    assert "END_SNAPSHOT" in con.events, f"bs02: snapshot must end before close, got {con.events}"
    assert "CLOSE" in con.events
    assert con.events.index("BEGIN") < con.events.index("END_SNAPSHOT") < con.events.index("CLOSE")
    # BEGIN is the first recorded transaction event.
    assert bi == 0 or all(e not in ("END_SNAPSHOT", "CLOSE") for e in con.events[:bi])
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


def test_bs04_no_active_plan_exact_response_without_bootstrap():
    con = _proxy(_no_active_plan_catalog(), swallow_close=True)
    preview_fn = _require_preview()
    with mock.patch("modelark.core.db.connect", return_value=con), \
            mock.patch("modelark.plan.bootstrap") as bootstrap:
        result = preview_fn()
    bootstrap.assert_not_called()
    assert result == _NO_ACTIVE_PLAN, (
        f"bs04: exact no-active-plan response required, got {result!r}")


def test_bs05_preview_is_side_effect_free():
    con = _proxy(_policy_blocked_catalog(revision=5), swallow_close=True)
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
    con = _proxy(_policy_blocked_catalog(), swallow_close=True)
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
    assert pure_m.call_count == 1
    args, kwargs = pure_calls[0]
    mut = kwargs.get("mutation")
    if mut is None and len(args) >= 3:
        mut = args[2]
    assert mut == ("adopt_current", ()) or list(mut) == ["adopt_current", []]
    plan_view.assert_not_called()
    create_draft.assert_not_called()
    _assert_reduced_preview(result, label="bs06", expect_refusal=True)


def test_bs07_http_get_plan_preview_route():
    con = _proxy(_policy_blocked_catalog(revision=9), swallow_close=True)
    with mock.patch("modelark.core.db.connect", return_value=con), \
            _http_server() as httpd:
        status, body = _http_get(httpd, "/api/plan/preview")
    assert status == 200, f"bs07: GET /api/plan/preview must exist, got {status} {body!r}"
    _assert_reduced_preview(body, label="bs07", expect_refusal=True)


def test_bs08_feasible_preview_null_refusal():
    con = _proxy(_feasible_catalog(revision=2), swallow_close=True)
    pure = prop.preview_pure(con, "ark", ("adopt_current", ()))
    assert pure.get("gate_b_refusal") is None
    preview_fn = _require_preview()
    with mock.patch("modelark.core.db.connect", return_value=con):
        result = preview_fn()
    _assert_reduced_preview(result, label="bs08", expect_refusal=False)
    assert result["gate_b_code"] == pure["header"].get("gate_b_code")


def test_bs21_preview_pure_authority_not_reconcile_diagnostic_shape():
    con = _proxy(_policy_blocked_catalog(), swallow_close=True)
    pure = prop.preview_pure(con, "ark", ("adopt_current", ()))
    pure_refusal = pure["gate_b_refusal"]
    from modelark import librarian
    with mock.patch("modelark.wishlist.exclude_pickle_only", return_value=True):
        plan = librarian.plan_view(con, plan_id="ark", capacity_mode="guaranteed")
    assert "gate_b_refusal" not in plan
    assert "blocking_diagnostics" in plan
    preview_fn = _require_preview()
    with mock.patch("modelark.core.db.connect", return_value=con):
        result = preview_fn()
    assert result["gate_b_refusal"] == pure_refusal
    assert set(result.keys()) == _REDUCED_KEYS


# ===========================================================================
# Dismiss CAS
# ===========================================================================


def test_bs09_revision_mismatch_exact_preview_stale_no_mutation():
    """Exactly one canonical bound call; revision mismatch → selection_changed false."""
    real = _policy_blocked_catalog(revision=10)
    con = _proxy(real, swallow_close=True)
    with _portal_catalog(con):
        before_sel = _sel_rows(con)
        before_rev = _rev(con)
        sel_hash = _sel_hash(con)
        stale_rev = before_rev - 1
        con.events.clear()
        result = selection_api.bulk({
            "ids": ["org/pickle", "org/noweights"],
            "on": False,
            "expected_revision": stale_rev,
            "expected_selection_hash": sel_hash,
        })
    _assert_preview_stale(
        result,
        current_revision=before_rev,
        based_on_revision=stale_rev,
        selection_changed=False,
        label="bs09",
    )
    _assert_cas_before_delete(con.events, label="bs09", expect_delete=False)
    assert _sel_rows(con) == before_sel
    assert _rev(con) == before_rev


def test_bs10_selection_hash_mismatch_exact_preview_stale_no_mutation():
    """Exactly one canonical bound call; hash mismatch → selection_changed true."""
    real = _policy_blocked_catalog(revision=10)
    con = _proxy(real, swallow_close=True)
    with _portal_catalog(con):
        before_sel = _sel_rows(con)
        before_rev = _rev(con)
        con.events.clear()
        result = selection_api.bulk({
            "ids": ["org/pickle"],
            "on": False,
            "expected_revision": before_rev,
            "expected_selection_hash": "0" * 64,
        })
    _assert_preview_stale(
        result,
        current_revision=before_rev,
        based_on_revision=before_rev,
        selection_changed=True,
        label="bs10",
    )
    _assert_cas_before_delete(con.events, label="bs10", expect_delete=False)
    assert _sel_rows(con) == before_sel
    assert _rev(con) == before_rev


def test_bs11_http_bound_dismiss_revision_mismatch_is_409_exact_body():
    real = _policy_blocked_catalog(revision=6)
    con = _proxy(real, swallow_close=True)
    with _portal_catalog(con), _http_server() as httpd:
        before = _sel_rows(con)
        before_rev = _rev(con)
        status, body = _http_post(httpd, "/api/selection/bulk", {
            "ids": ["org/pickle", "org/noweights"],
            "on": False,
            "expected_revision": 1,
            "expected_selection_hash": _sel_hash(con),
        })
    assert status == 409, f"bs11: expected HTTP 409, got {status} {body!r}"
    _assert_preview_stale(
        body,
        current_revision=before_rev,
        based_on_revision=1,
        selection_changed=False,
        label="bs11",
    )
    assert _sel_rows(con) == before


def test_bs12_live_fill_bound_dismiss_fill_session_active_exact():
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
    assert result == _FILL_ACTIVE, f"bs12: exact FILL_SESSION_ACTIVE required, got {result!r}"
    assert _sel_rows(con) == before
    assert _rev(con) == before_rev


def test_bs13_successful_bound_dismiss_summary_shape_exact_removals_cas_order():
    """Matching bindings: existing selection-summary shape; exact removals; one bump; CAS order."""
    real = _policy_blocked_catalog(revision=8)
    con = _proxy(real, swallow_close=True)
    with _portal_catalog(con):
        before_rev = _rev(con)
        sel_hash = _sel_hash(con)
        con.events.clear()
        result = selection_api.bulk({
            "ids": ["org/pickle", "org/noweights"],
            "on": False,
            "expected_revision": before_rev,
            "expected_selection_hash": sel_hash,
        })
        after_sel = {r[0] for r in _sel_rows(con)}
        after_rev = _rev(con)
    assert result.get("refused") is not True, f"bs13: must succeed, got {result!r}"
    # Existing selection-summary shape — no invented ok=True (DEC-058 clarification).
    assert "ok" not in result, (
        f"bs13: successful bound Dismiss must preserve selection-summary shape without ok=True, "
        f"got {result!r}")
    for key in ("n", "bytes", "finalized"):
        assert key in result, f"bs13: summary missing {key!r}: {result!r}"
    assert after_sel == {"org/ok"}, f"bs13: exact blocked removals only, got {after_sel}"
    assert after_rev == before_rev + 1
    _assert_cas_before_delete(con.events, label="bs13", expect_delete=True)


def test_bs14_unbound_catalog_bulk_still_works_without_bindings():
    """Compat: ordinary Catalog bulk-off without CAS fields continues to work (may be green)."""
    con = _policy_blocked_catalog(revision=3)
    with _portal_catalog(con):
        before_rev = _rev(con)
        result = selection_api.bulk(ids=["org/pickle"], on=False)
        after = {r[0] for r in _sel_rows(con)}
    assert result.get("refused") is not True
    assert "org/pickle" not in after
    assert _rev(con) == before_rev + 1


def test_bs_bg_unbound_bulk_http_still_200_without_cas():
    con = _policy_blocked_catalog(revision=2)
    with _portal_catalog(con), _http_server() as httpd:
        status, body = _http_post(httpd, "/api/selection/bulk", {
            "ids": ["org/noweights"],
            "on": False,
        })
    assert status == 200
    assert body.get("refused") is not True
