"""E2E smoke test: the portal boots on an isolated temporary catalog and drives the real UI.

Standalone harness (no pytest). Needs the development extra and a browser:
    python3 -m venv .venv-dev && .venv-dev/bin/pip install -e '.[dev]'
    .venv-dev/bin/playwright install chromium
    .venv-dev/bin/python tests/test_e2e_portal.py

The test injects a temporary data/state directory into both this process and the portal subprocess.
It never reads, moves, replaces, or deletes the user's default catalog (including its WAL/SHM files).
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from modelark.core import db

PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"
GIANT_BYTES = int(1.5e12)          # 1.5 TB of safetensors > the 1 TB default cap -> selecting it warns

_MODELS = [   # repo_id, author, params_b, category, variant, license, downloads_30d, safetensors bytes
    ("demo/tiny-llm",  "demo",   1.0, "generative-llm", "instruct", "apache-2.0", 5000, int(2e9)),
    ("demo/small-llm", "demo",   7.0, "generative-llm", "base",     "mit",        3000, int(3e9)),
    ("demo/embed",     "demo",   0.1, "embedding",      "base",     "apache-2.0", 8000, int(1e9)),
    ("demo/giant-llm", "demo", 400.0, "generative-llm", "instruct", "apache-2.0",  100, GIANT_BYTES),
]


def _seed(con) -> None:
    for repo, author, p, cat, var, lic, dl, size in _MODELS:
        con.execute("INSERT INTO models(repo_id,author,params_b,category,variant,license,downloads_30d,"
                    "gated,status) VALUES(?,?,?,?,?,?,?, 'false', 'discovered')",
                    (repo, author, p, cat, var, lic, dl))
        con.execute("INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
                    "VALUES(?, 'model.safetensors', ?, 'safetensors', 'bf16')", (repo, size))
    con.execute(
        "INSERT INTO models(repo_id,author,params_b,category,variant,license,downloads_30d,"
        "gated,status) VALUES('demo/pickle-only','demo',2.0,'generative-llm','base','mit',10,"
        "'false','discovered')"
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('demo/pickle-only','pytorch_model.bin',2000000000,'pytorch','fp16')"
    )
    con.execute(
        "INSERT INTO models(repo_id,author,params_b,category,variant,license,downloads_30d,"
        "gated,status,numcopies) VALUES('demo/replica-blocked','demo',2.0,'generative-llm',"
        "'base','mit',10,'false','discovered',2)"
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('demo/replica-blocked','model.safetensors',2000000000,'safetensors','bf16')"
    )
    # Hostile id/reason carrier for blocked-selection XSS contract (pickle-only → MANIFEST_POLICY).
    con.execute(
        "INSERT INTO models(repo_id,author,params_b,category,variant,license,downloads_30d,"
        "gated,status) VALUES('demo/<script>alert(1)</script>','demo',1.0,'generative-llm',"
        "'base','mit',5,'false','discovered')"
    )
    con.execute(
        "INSERT INTO files(repo_id,rfilename,size_bytes,format,quant) "
        "VALUES('demo/<script>alert(1)</script>','pytorch_model.bin',1000000,'pytorch','fp16')"
    )
    con.executemany(
        "INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-07-15')",
        [("demo/tiny-llm",), ("demo/pickle-only",), ("demo/replica-blocked",),
         ("demo/<script>alert(1)</script>",)],
    )
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes) "
        "VALUES('drive-00','primary',0,10000000000000,10000000000000)"
    )
    con.execute(
        "INSERT INTO drives(drive_label,role,raid_backed,capacity_bytes,free_bytes) "
        "VALUES('drive-replica','replica',0,1000000000,1000000000)"
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_bytes,stored_bytes,compressed,annex_key) VALUES("
        "'demo/small-llm','model.safetensors','model.safetensors','model.safetensors',"
        "'drive-00',3000000000,2000000000,1,'KEY-small')"
    )
    con.execute(
        "INSERT INTO archived(repo_id,rfilename,stored_name,stored_relpath,drive_label,"
        "orig_bytes,stored_bytes,compressed,annex_key) VALUES("
        "'demo/embed','model.safetensors','model.safetensors','model.safetensors',"
        "'drive-replica',1000000000,600000000,1,'KEY-embed')"
    )
    # Reconcile both drives (proven identity + a matching clean anchor) so admission evidence is
    # available offline (#35-C). A migrated drive would be `unknown` and capacity-block everything —
    # the fail-closed migration default — masking the intended policy/replica-capacity blockers.
    for label, cap, fp in (("drive-00", 10000000000000, "a" * 64), ("drive-replica", 1000000000, "b" * 64)):
        con.execute("UPDATE drives SET identity_epoch=1, write_generation=1, filesystem_capacity_bytes=?, "
                    "identity_fingerprint=?, write_authority='dedicated_local' WHERE drive_label=?",
                    (cap, fp, label))
        con.execute("INSERT INTO drive_dirty_generations(drive_label,identity_epoch,generation,"
                    "operation_code) VALUES(?,1,1,'reconcile')", (label,))
        con.execute("INSERT INTO drive_clean_anchors(drive_label,identity_epoch,generation,"
                    "anchor_free_bytes,filesystem_capacity_bytes,identity_fingerprint,write_authority,"
                    "identity_proof,fence_proof,observed_at) "
                    "VALUES(?,1,1,?,?,?, 'dedicated_local','proof','fence','2026-07-15 00:00:00')",
                    (label, cap, cap, fp))


def _wait_port(port: int, timeout: int = 40) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _get(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        return json.load(r)


_POLICY_BLOCKED_IDS = {
    "demo/pickle-only",
    "demo/<script>alert(1)</script>",
}
_DISMISS_BODY_KEYS = {"ids", "on", "expected_revision", "expected_selection_hash"}
_MUTATION_ROUTE_SPECS = (
    ("**/api/selection", "selection"),
    ("**/api/selection/bulk", "selection_bulk"),
    ("**/api/selection/clear", "selection_clear"),
    ("**/api/selection/finalize", "selection_finalize"),
    ("**/api/fill/start", "fill_start"),
    ("**/api/proposal/**", "proposal"),
)


def _blocked_selection_flow(pg) -> None:
    """Fill-tab blocked-selection notice (DEC-058). Fail closed if the real UI is absent.

    Stable selectors (Gate-2 production must provide them):
      #blockedSelection #blockedSelectionList #blockedDismiss #blockedReplan
      #blockedSelectionList [data-repo-id]
    """
    # Exactly one notice container (no tautological count fallback).
    pg.wait_for_selector("#blockedSelection", timeout=8000)
    assert pg.locator("#blockedSelection").count() == 1
    pg.wait_for_selector("#blockedSelectionList")

    rows = pg.locator("#blockedSelectionList [data-repo-id]")
    assert rows.count() == 2, (
        f"exactly two policy-blocked rows required, got {rows.count()}")
    # Both current policy-blocker fixtures are pickle-only; pin the canonical reason
    # independently of the repository ID (name-only rows with "pickle" in the id fail).
    expected_reason = "pickle-only weights are blocked by exclude.pickle_only=true"
    row_ids = []
    for i in range(rows.count()):
        row = rows.nth(i)
        rid = row.get_attribute("data-repo-id")
        assert rid, f"row {i} missing data-repo-id"
        row_ids.append(rid)
        text = row.inner_text()
        assert text.strip(), f"row {rid!r} must render non-empty text"
        assert expected_reason in text.lower(), (
            f"row {rid!r} must contain canonical policy reason "
            f"{expected_reason!r}, got {text!r}")
    assert set(row_ids) == _POLICY_BLOCKED_IDS, (
        f"exact policy-blocked row set required, got {set(row_ids)}")
    # Capacity-only blocker must not appear as a blocked-selection row.
    assert pg.locator(
        '#blockedSelectionList [data-repo-id="demo/replica-blocked"]').count() == 0
    list_text = pg.inner_text("#blockedSelectionList")
    assert "REQUIREMENT_EXCEEDS_USABLE_MAX" not in list_text
    # Hostile id is literal text; no injected elements.
    hostile = pg.locator(
        '#blockedSelectionList [data-repo-id="demo/<script>alert(1)</script>"]')
    assert hostile.count() == 1
    assert "demo/<script>alert(1)</script>" in hostile.inner_text() or \
        "demo/<script>alert(1)</script>" in (hostile.get_attribute("data-repo-id") or "")
    assert pg.locator("#blockedSelectionList script").count() == 0
    assert pg.locator("#blockedSelectionList img").count() == 0
    # No other injected tags under the list that could execute from the reason/id.
    assert pg.locator("#blockedSelectionList iframe").count() == 0
    assert pg.locator("#blockedSelectionList object").count() == 0

    assert pg.locator("#blockedDismiss").count() == 1
    assert pg.locator("#blockedReplan").count() == 1
    assert pg.is_enabled("#blockedDismiss") and pg.is_enabled("#blockedReplan")

    traffic = {
        "preview_get": 0,
        "bulk_post": [],
        "mutations": [],
    }
    # Single handler mode switch avoids Playwright unroute/LIFO surprises across steps.
    route_mode = {"preview": "pass", "bulk": "ban"}

    def record_mutation(route, label):
        traffic["mutations"].append({
            "label": label,
            "method": route.request.method,
            "url": route.request.url,
        })
        # Never let accidental mutation hit the isolated server during these contracts.
        route.fulfill(status=500, content_type="application/json",
                      body=json.dumps({"ok": False, "error": "test-blocked-mutation"}))

    # Intercept mutation-capable routes BEFORE Replan (and keep them for the suite).
    # Bulk is handled separately via route_mode so Dismiss can switch stale→ok.
    for pattern, label in _MUTATION_ROUTE_SPECS:
        if label == "selection_bulk":
            continue
        pg.route(pattern, lambda route, lab=label: record_mutation(route, lab))

    def preview_router(route):
        if route.request.method != "GET":
            traffic["mutations"].append({
                "label": "preview_non_get",
                "method": route.request.method,
                "url": route.request.url,
            })
            route.fulfill(status=500, body="preview-non-get")
            return
        mode = route_mode["preview"]
        if mode == "pass":
            traffic["preview_get"] += 1
            route.continue_()
        elif mode == "hold":
            traffic["preview_get"] += 1
            held.append(route)
        elif mode == "after_dismiss":
            preview_after_dismiss["count"] += 1
            traffic["preview_get"] += 1
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({
                    "ok": True, "plan_id": "ark", "based_on_revision": 99,
                    "selection_before_hash": "a" * 64, "gate_b_code": "FEASIBLE",
                    "gate_b_refusal": None,
                }),
            )
        else:
            route.continue_()

    def bulk_router(route):
        if route.request.method != "POST":
            route.continue_()
            return
        mode = route_mode["bulk"]
        traffic.setdefault("bulk_modes", []).append(mode)
        if mode == "ban":
            record_mutation(route, "selection_bulk")
            return
        traffic["bulk_post"].append(route.request.post_data_json)
        if mode == "stale":
            route.fulfill(
                status=409, content_type="application/json",
                body=json.dumps({
                    "ok": False, "refused": True, "code": "PREVIEW_STALE",
                    "error": "Selection changed since this preview. Replan before dismissing.",
                    "evidence": {
                        "current_revision": 2, "based_on_revision": 1,
                        "selection_changed": False,
                    },
                    "actions": ["replan"],
                }),
            )
        elif mode == "ok":
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({
                    "n": 2, "bytes": 0, "finalized": 2, "budget": 27,
                    "cap_24h_gb": 1000, "by_cat": [],
                    "refused": False,
                }),
            )
        elif mode == "error500":
            # INC-031 c02: HTTP 500 {error} without refused — must not re-preview.
            route.fulfill(
                status=500, content_type="application/json",
                body=json.dumps({
                    "error": "FILL_SESSION_ACTIVE: {'session_id': 'sess-cli'}",
                }),
            )
        else:
            record_mutation(route, "selection_bulk")

    held = []
    preview_after_dismiss = {"count": 0}
    pg.route("**/api/plan/preview", preview_router)
    pg.route("**/api/selection/bulk", bulk_router)

    # --- Replan: preview GET only; zero mutation-route calls ---
    route_mode["preview"] = "pass"
    route_mode["bulk"] = "ban"
    before_preview = traffic["preview_get"]
    before_mut = len(traffic["mutations"])
    pg.click("#blockedReplan")
    for _ in range(40):
        if traffic["preview_get"] > before_preview:
            break
        time.sleep(0.05)
    assert traffic["preview_get"] == before_preview + 1, (
        f"Replan must issue exactly one additional preview GET, "
        f"before={before_preview} after={traffic['preview_get']}")
    assert len(traffic["mutations"]) == before_mut, (
        f"Replan must not call mutation routes, got {traffic['mutations'][before_mut:]}")
    for _ in range(40):
        if pg.is_enabled("#blockedDismiss") and pg.is_enabled("#blockedReplan"):
            break
        time.sleep(0.05)
    assert pg.is_enabled("#blockedDismiss") and pg.is_enabled("#blockedReplan")
    assert set(
        pg.locator("#blockedSelectionList [data-repo-id]").evaluate_all(
            "els => els.map(e => e.getAttribute('data-repo-id'))")
    ) == _POLICY_BLOCKED_IDS
    print("  blocked-selection Replan: preview GET only, zero mutations")

    # --- Network failure: hold request in flight, assert disabled, abort, restore ---
    route_mode["preview"] = "hold"
    held.clear()
    before_list = pg.inner_text("#blockedSelectionList")
    before_ids = set(
        pg.locator("#blockedSelectionList [data-repo-id]").evaluate_all(
            "els => els.map(e => e.getAttribute('data-repo-id'))"))
    pg.click("#blockedReplan")
    for _ in range(40):
        if held:
            break
        time.sleep(0.05)
    assert held, "Replan must produce a held preview request"
    assert not pg.is_enabled("#blockedDismiss"), "Dismiss disabled while preview in flight"
    assert not pg.is_enabled("#blockedReplan"), "Replan disabled while preview in flight"
    held[0].abort("failed")
    for _ in range(40):
        if pg.is_enabled("#blockedDismiss") and pg.is_enabled("#blockedReplan"):
            break
        time.sleep(0.05)
    assert pg.inner_text("#blockedSelectionList") == before_list, (
        "network failure must retain displayed blocked evidence")
    assert set(
        pg.locator("#blockedSelectionList [data-repo-id]").evaluate_all(
            "els => els.map(e => e.getAttribute('data-repo-id'))")
    ) == before_ids
    assert pg.is_enabled("#blockedDismiss") and pg.is_enabled("#blockedReplan")
    toast = pg.inner_text("#toast") if pg.locator("#toast").count() else ""
    assert toast.strip(), f"network error must surface to the operator, toast={toast!r}"
    print("  blocked-selection held-request network failure: disabled → abort → restore")

    # --- PREVIEW_STALE 409: retain evidence, restore controls, exact Dismiss body ---
    route_mode["preview"] = "pass"
    route_mode["bulk"] = "stale"
    traffic["bulk_post"].clear()
    before_list = pg.inner_text("#blockedSelectionList")
    pg.click("#blockedDismiss")
    for _ in range(40):
        t = pg.inner_text("#toast") if pg.locator("#toast").count() else ""
        if "Replan before dismissing" in t or "PREVIEW_STALE" in t:
            break
        time.sleep(0.05)
    toast = pg.inner_text("#toast")
    assert "Replan before dismissing" in toast or "PREVIEW_STALE" in toast, (
        f"PREVIEW_STALE must surface, toast={toast!r}")
    assert pg.inner_text("#blockedSelectionList") == before_list
    assert pg.is_enabled("#blockedDismiss") and pg.is_enabled("#blockedReplan")
    assert traffic["bulk_post"], "Dismiss must POST /api/selection/bulk"
    body = traffic["bulk_post"][-1]
    assert set(body.keys()) == _DISMISS_BODY_KEYS, (
        f"Dismiss body exact keys required, got {sorted(body)}")
    assert body["on"] is False
    assert isinstance(body["expected_revision"], int)
    assert isinstance(body["expected_selection_hash"], str) and body["expected_selection_hash"]
    assert set(body["ids"]) == _POLICY_BLOCKED_IDS, (
        f"Dismiss ids must be exact policy-blocked set, got {body['ids']!r}")
    assert len(body["ids"]) == 2, "no duplicates or extras in Dismiss ids"
    print("  blocked-selection PREVIEW_STALE + exact Dismiss CAS body")

    # --- INC-031 c02: 500 {error} without refused must toast and not re-preview ---
    route_mode["preview"] = "pass"
    route_mode["bulk"] = "error500"
    traffic["bulk_post"].clear()
    before_preview = traffic["preview_get"]
    before_list = pg.inner_text("#blockedSelectionList")
    before_ids = set(
        pg.locator("#blockedSelectionList [data-repo-id]").evaluate_all(
            "els => els.map(e => e.getAttribute('data-repo-id'))"))
    pg.click("#blockedDismiss")
    for _ in range(40):
        t = pg.inner_text("#toast") if pg.locator("#toast").count() else ""
        if "FILL_SESSION_ACTIVE" in t or "sess-cli" in t:
            break
        time.sleep(0.05)
    toast = pg.inner_text("#toast") if pg.locator("#toast").count() else ""
    assert "FILL_SESSION_ACTIVE" in toast or "sess-cli" in toast, (
        f"INC-031 c02: 500 error body must toast, toast={toast!r}")
    assert pg.inner_text("#blockedSelectionList") == before_list, (
        "INC-031 c02: 500 error body must retain blocked-notice evidence")
    assert set(
        pg.locator("#blockedSelectionList [data-repo-id]").evaluate_all(
            "els => els.map(e => e.getAttribute('data-repo-id'))")
    ) == before_ids
    # Wait for the error branch to restore controls, then drain late GETs
    # before asserting no re-preview (Codex Gate-1 MEDIUM / Gate-2 required).
    for _ in range(40):
        if pg.is_enabled("#blockedDismiss") and pg.is_enabled("#blockedReplan"):
            break
        time.sleep(0.05)
    assert pg.is_enabled("#blockedDismiss") and pg.is_enabled("#blockedReplan")
    for _ in range(10):
        time.sleep(0.05)
    assert traffic["preview_get"] == before_preview, (
        f"INC-031 c02: 500 error body must not auto re-preview, "
        f"before={before_preview} after={traffic['preview_get']}")
    print("  blocked-selection INC-031 500 error body: toast, retain, no re-preview")

    # --- Successful Dismiss: auto re-preview; notice clears; capacity remains ---
    traffic["bulk_post"].clear()
    preview_after_dismiss["count"] = 0
    route_mode["bulk"] = "ok"
    route_mode["preview"] = "after_dismiss"
    with pg.expect_request(
            lambda r: r.method == "GET" and "/api/plan/preview" in r.url,
            timeout=8000) as preview_req:
        pg.click("#blockedDismiss")
    assert preview_req.value is not None
    for _ in range(40):
        if preview_after_dismiss["count"] >= 1:
            break
        time.sleep(0.05)
    for _ in range(40):
        if pg.locator("#blockedSelection").count() == 0:
            break
        if pg.locator("#blockedSelection").count() and pg.is_hidden("#blockedSelection"):
            break
        if pg.locator("#blockedSelectionList [data-repo-id]").count() == 0:
            break
        time.sleep(0.05)
    assert traffic["bulk_post"], "successful Dismiss must POST bulk"
    assert traffic.get("bulk_modes", [])[-1:] == ["ok"], (
        f"success Dismiss bulk mode should be ok, got {traffic.get('bulk_modes')!r}")
    # Route handler alone measures the automatic re-preview GET (no manual increment).
    assert preview_after_dismiss["count"] == 1, (
        f"successful Dismiss must issue exactly one automatic preview GET, "
        f"got {preview_after_dismiss['count']}")
    # Notice cleared of policy blockers.
    remaining_ids = set()
    if pg.locator("#blockedSelectionList [data-repo-id]").count():
        remaining_ids = set(
            pg.locator("#blockedSelectionList [data-repo-id]").evaluate_all(
                "els => els.map(e => e.getAttribute('data-repo-id'))"))
    assert remaining_ids.isdisjoint(_POLICY_BLOCKED_IDS), (
        f"policy blockers must leave the notice, still have {remaining_ids}")
    # Capacity blocker remains on the existing advisory/queue surface.
    advisory = pg.inner_text("#fillAdvisories")
    queue = pg.inner_text("#fillQueue")
    assert "REQUIREMENT_EXCEEDS_USABLE_MAX" in advisory or "demo/replica-blocked" in queue
    # No draft/approve/fill-start across Dismiss/Replan (mutation intercept retained).
    assert not any(m["label"] == "fill_start" for m in traffic["mutations"]), traffic["mutations"]
    assert not any(m["label"] == "proposal" for m in traffic["mutations"]), traffic["mutations"]

    for pattern, _label in _MUTATION_ROUTE_SPECS:
        try:
            pg.unroute(pattern)
        except Exception:
            pass
    try:
        pg.unroute("**/api/plan/preview")
    except Exception:
        pass
    try:
        pg.unroute("**/api/selection/bulk")
    except Exception:
        pass
    print("  blocked-selection notice + Dismiss/Replan contracts exercised")


def _drive_loss_flow(pg) -> None:
    """Exercise DEC-069 entirely through mocked observations; never enumerate test-host disks."""
    inventory = {
        "ok": True,
        "planner_revision": 11,
        "inventory_available": True,
        "observation_authority": "advisory_only",
        "message": "Attached inventory is observation only.",
        "registered": [{
            "drive_label": "drive-02", "lifecycle": "active", "eligibility": "enabled",
            "identity_epoch": 3, "identity_fingerprint": "b" * 64,
            "serial": "FAILED-SERIAL", "hw_model": "old disk",
            "capacity_bytes": 4_000_000_000_000, "last_seen": "2026-08-01",
            "observation": "not_attached", "device": None,
            "plans": [{"plan_id": "ark", "is_active": True}],
        }],
        "unregistered": [{
            "dev": "/dev/mock-seagate", "size": "7.3T", "model": "Seagate 8TB",
            "serial": "NEW-SEAGATE", "bus": "usb", "spinning": True,
            "observation": "unregistered", "action_taken": False,
        }],
    }
    preview = {
        "ok": True,
        "preview": {
            "drive_label": "drive-02", "planner_revision": 11,
            "identity_epoch": 3, "identity_fingerprint": "b" * 64,
            "archived_rows": 120, "archived_repositories": 40,
            "replica_rows": 120, "confirmation": "DECLARE LOST drive-02",
            "warning": "Not currently observed means offline or missing only.",
        },
    }
    response = {
        "ok": True,
        "transition": {
            "drive_label": "drive-02", "lifecycle": "lost", "eligibility": "excluded",
            "planner_revision": 12,
        },
        "after": {
            "totals": {"capacity": 5_000_000_000_000},
            "replan": {
                "root_code": "CAPACITY_EVIDENCE_UNKNOWN", "feasible": False,
                "executable_tasks": 0, "target_counts": {}, "planner_revision": 12,
            },
            "replan_error": None,
        },
    }
    onboarding = {
        "ok": True,
        "preview": {
            "planner_revision": 11,
            "observation_authority": "read_only",
            "device": inventory["unregistered"][0],
            "volume": {
                "dev": "/dev/mock-seagate1", "type": "part",
                "size_bytes": 8_000_000_000_000, "fstype": "ext4",
                "fs_uuid": "NEW-FS-UUID", "mountpoints": [], "mounted": False,
            },
            "suggested_label": "drive-07",
            "label_policy": "new_label_required",
            "blockers": ["MOUNT_REQUIRED"],
            "ready_for_registration": False,
            "next_action": "mount_volume",
            "registration_preview": {
                "dev": "/dev/mock-seagate1", "label": "drive-07", "mount": None,
                "format": None, "role": "primary", "adds_to_active_plan": "ark",
                "requires_reconcile_after_registration": True,
                "inherited_from_lost_identity": [],
            },
            "separate_lost_identities": [{
                "drive_label": "drive-02", "identity_epoch": 3,
                "identity_fingerprint": "b" * 64, "archived_rows": 120,
                "replica_rows": 120,
                "plans": [{"plan_id": "ark", "is_active": True}],
                "relationship": "not_inherited",
            }],
        },
    }
    submitted = []
    smart_requests = []
    onboarding_requests = []

    pg.route("**/api/drives", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(inventory)))
    pg.route("**/api/drive/loss-preview**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(preview)))

    def onboarding_preview(route):
        onboarding_requests.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(onboarding))

    pg.route("**/api/drive/onboarding-preview**", onboarding_preview)

    def declare(route):
        submitted.append(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(response))

    pg.route("**/api/drive/declare-lost", declare)
    pg.route("**/api/disk", lambda route: (
        smart_requests.append(True),
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"drives": []})),
    ))

    pg.click("button[data-view='disk']")
    pg.wait_for_selector(".driveproblem")
    text = pg.inner_text("#driveBody")
    assert "drive-02" in text and "not attached" in text.lower()
    assert "Seagate 8TB" in text and "unregistered" in text.lower()
    assert smart_requests == [], "opening Drives must not run SMART"

    pg.click(".driveonboard")
    pg.wait_for_selector("#driveOnboardingModal", state="visible")
    onboarding_text = pg.inner_text("#driveOnboardingModal")
    assert "drive-07" in onboarding_text
    assert "/dev/mock-seagate1" in onboarding_text
    assert "not mounted" in onboarding_text.lower()
    assert "drive-02" in onboarding_text and "never inherited" in onboarding_text.lower()
    assert len(onboarding_requests) == 1
    assert "dev=%2Fdev%2Fmock-seagate" in onboarding_requests[0]
    assert "serial=NEW-SEAGATE" in onboarding_requests[0]
    assert smart_requests == [], "onboarding preview must not run SMART"
    pg.click("#driveOnboardingClose")

    pg.click(".driveproblem")
    pg.wait_for_selector("#driveLossModal", state="visible")
    assert "offline or missing only" in pg.inner_text("#driveLossWarning")
    pg.click("#driveLossCancel")
    assert submitted == [], "cancel must be a no-op"

    pg.click(".driveproblem")
    pg.fill("#driveLossConfirm", "DECLARE LOST drive-02")
    assert pg.locator("#driveLossApply").is_enabled()
    pg.click("#driveLossApply")
    pg.wait_for_selector("#driveEvent", state="visible")
    assert submitted == [{
        "drive_label": "drive-02", "expected_revision": 11,
        "expected_identity_epoch": 3, "expected_identity_fingerprint": "b" * 64,
        "confirmation": "DECLARE LOST drive-02",
    }]
    event = pg.inner_text("#driveEvent")
    assert "replanned at revision 12" in event
    assert "CAPACITY_EVIDENCE_UNKNOWN" in event and "lost-drive targets 0" in event
    assert smart_requests == [], "loss/replan must not implicitly run SMART"

    for pattern in (
        "**/api/drives", "**/api/drive/loss-preview**", "**/api/drive/declare-lost",
        "**/api/drive/onboarding-preview**", "**/api/disk",
    ):
        pg.unroute(pattern)
    print("  advisory drive discovery + cancel + exact loss/replan UI exercised without SMART")


def _browser_flow() -> None:
    """Drive the portal in a headless browser: clear the #35 plan-gate by selecting `ark`, open the
    Catalog, tick the giant, and confirm the over-cap banner shows + dismisses. Patient waits per step
    (the app reloads after a plan is selected); screenshots to /tmp on failure for debugging."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = browser.new_page()
        pg.set_default_timeout(20000)                    # generous per-action wait
        try:
            pg.goto(BASE, wait_until="networkidle")
            time.sleep(2)
            plan_text = pg.inner_text("#view-plans")
            assert "guaranteed" in plan_text and "raw forecast" in plan_text
            assert "expected stored" in plan_text and "provisioning mode" not in plan_text
            assert pg.locator("#newPlanProv option").evaluate_all(
                "els => els.map(el => el.value)"
            ) == ["guaranteed", "compression_aware"]
            print("  canonical capacity-mode labels rendered")
            # 1. select the `ark` plan (the #35 gate forces this before anything unlocks) -> app reloads
            pg.wait_for_selector(".pcuse[data-id='ark']")
            pg.click(".pcuse[data-id='ark']")
            time.sleep(3)                                # reload fires ~300ms after select; let it settle
            pg.wait_for_load_state("networkidle")
            print("  selected the ark plan")
            # 2. The migrated-cart shape includes both a policy blocker and a valid manifest whose
            # replica cannot fit. It must render typed, disjoint blockers rather than HTTP 500s,
            # omitted rows, or inflated "to place" totals.
            pg.click("button[data-view='fill']")
            pg.wait_for_selector("#fillAdvisories .fadv.error")
            advisory = pg.inner_text("#fillAdvisories")
            assert "MANIFEST_POLICY" in advisory and "demo/pickle-only" in advisory
            # #38 tiered_v2: structural exceed-max projects REQUIREMENT_EXCEEDS_USABLE_MAX (not
            # a CAPACITY_*_SHORT proven free short, and not a CAPACITY_ string prefix).
            assert "REQUIREMENT_EXCEEDS_USABLE_MAX" in advisory
            pg.wait_for_selector("#fillQueue .telq.blocked")
            blocked = pg.inner_text("#fillQueue")
            assert "demo/pickle-only" in blocked and "MANIFEST_POLICY" in blocked
            assert "demo/replica-blocked" in blocked and "REQUIREMENT_EXCEEDS_USABLE_MAX" in blocked
            # Two policy blockers (pickle + hostile XSS id) + one capacity blocker.
            assert pg.locator("#fillQueue .telq.blocked").count() == 3
            fill_note = pg.inner_text("#fillNote")
            assert "1 to place" in fill_note and "3 blocked" in fill_note, fill_note
            assert pg.locator("#fillStart").is_disabled()
            print("  policy + capacity blockers rendered with disjoint totals; Start fill disabled")

            # ------------------------------------------------------------------
            # Blocked-selection workflow (DEC-058 / Gate 1) — Fill-tab notice.
            # Selectors: #blockedSelection #blockedSelectionList #blockedDismiss #blockedReplan
            # Expected red until Gate-2 UI exists; e2e must not stay green without it.
            # ------------------------------------------------------------------
            _blocked_selection_flow(pg)

            # Drive chart still renders under a blocked cart. With tiered_v2 whole-plan Gate-B,
            # a structural blocker (replica exceed-max) yields 0 planned tasks on the bar while
            # archived occupancy remains as a grey segarch trail. When planned segs also exist,
            # they stay left of archived (pre-#38 layout contract).
            pg.wait_for_selector("#dc-drive-00 .dcbarfill > .seg")
            segments = pg.locator("#dc-drive-00 .dcbarfill > .seg")
            n_seg = segments.count()
            assert n_seg >= 1, "drive-00 bar must show at least archived occupancy"
            classes = [(segments.nth(i).get_attribute("class") or "") for i in range(n_seg)]
            assert any("segarch" in c for c in classes), f"expected segarch among {classes}"
            foot = pg.inner_text("#dc-drive-00 .dcfoot")
            assert "0 planned" in foot or "planned" in foot
            if n_seg >= 2:
                assert "segarch" not in (segments.first.get_attribute("class") or "")
                assert "segarch" in (segments.last.get_attribute("class") or "")
                assert segments.first.bounding_box()["x"] < segments.last.bounding_box()["x"]
            print("  drive progress bar renders archived segment under blocked cart")

            # 3. Library search and multi-drive filters operate over every archived model. Clicking
            # a fleet card toggles the same filter chip, while multiple drives use OR semantics.
            pg.click("button[data-view='library']")
            pg.wait_for_selector("#libBody tbody tr")
            assert pg.locator("#libBody tbody tr").count() == 2
            assert pg.inner_text("#libShown") == "2 of 2 models"
            pg.click("#libFleet .libdrive[data-drive='drive-00']")
            assert pg.locator("#libBody tbody tr").count() == 1
            assert "demo/small-llm" in pg.inner_text("#libBody")
            assert pg.locator("#libDriveFilters [data-drive='drive-00'].on").count() == 1
            pg.click("#libDriveFilters [data-drive='drive-replica']")
            assert pg.locator("#libBody tbody tr").count() == 2
            pg.click("#libFleet .libdrive[data-drive='drive-00']")
            assert pg.locator("#libBody tbody tr").count() == 1
            assert "demo/embed" in pg.inner_text("#libBody")
            pg.fill("#libSearch", "small")
            pg.wait_for_selector("#libBody .stub")
            assert pg.inner_text("#libShown") == "0 of 2 models"
            assert pg.inner_text("#libDriveFilters [data-drive='drive-00']") == "drive-00 · 1"
            assert pg.inner_text("#libDriveFilters [data-drive='drive-replica']") == "drive-replica · 0"
            pg.click("#libDriveFilters [data-drive='drive-replica']")
            assert pg.locator("#libBody tbody tr").count() == 1
            assert "demo/small-llm" in pg.inner_text("#libBody")
            print("  library repository search + clickable multi-drive filters rendered")

            # 4. Rehearse drive discovery and the operator-confirmed loss flow with browser-level
            # mock hardware evidence. No request reaches the host inventory or SMART endpoint.
            _drive_loss_flow(pg)

            # 5. open Catalog, wait for rows, confirm the giant is there
            pg.click("button[data-view='catalog']")
            time.sleep(2)
            pg.wait_for_selector("#tbody tr")
            assert pg.query_selector("tr[data-id='demo/giant-llm']"), "giant row missing from catalog"
            print("  catalog rendered")
            # 6. tick the giant -> the over-cap banner should appear
            pg.check("tr[data-id='demo/giant-llm'] input[type=checkbox]")
            time.sleep(3)                                # selection round-trip + renderBudget
            pg.wait_for_selector("#capWarn", state="visible")
            msg = pg.inner_text("#capWarnMsg")
            assert "24-hour" in msg and "considerate" in msg, f"unexpected banner text: {msg!r}"
            print("  over-cap banner shown")
            # 7. dismiss hides it
            pg.click("#capWarnDismiss")
            time.sleep(1)
            pg.wait_for_selector("#capWarn", state="hidden")
            print("  banner dismissed")

            # 8. A bounded transport retry must be visibly identified as network work, with its
            # attempt count, instead of looking like rapid model churn or an unexplained stall.
            retry_status = {
                "status": "running", "running": True, "phase": "primary",
                "drive": "drive-00", "repo": "demo/small-llm",
                "file": "model.safetensors", "file_phase": "download-retry",
                "retry_attempt": 2, "retry_limit": 4, "retry_reason": "transient_network",
            }
            pg.route(
                "**/api/fill/status",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(retry_status)
                ),
            )
            pg.evaluate("window.loadFill()")
            for _ in range(40):
                if "network attempt 2/4" in pg.inner_text("#fillStatus"):
                    break
                time.sleep(0.25)
            else:
                raise AssertionError(f"retry attempt was not rendered: {pg.inner_text('#fillStatus')!r}")
            assert "transient network retry" in pg.inner_text("#fillTelemetry")
            assert "2 / 4" in pg.inner_text("#fillTelemetry")
            pg.unroute("**/api/fill/status")
            print("  transient retry reason + attempt count rendered")

            # 9. the same public hook used by the live Fill poll must show typed terminals without a
            # reload; verify the operator-facing evidence/action surface, not merely DOM presence.
            pg.evaluate("""
                window.MA.showFillTerminal({
                  status: "plan-capacity-stop",
                  message: "remaining work no longer fits",
                  code: "CAPACITY_WORKSPACE_SHORT",
                  gate: "B",
                  evidence: {shortfall_bytes: 123},
                  actions: ["add_capacity", "start_fill"],
                  failed: [{repo: "demo/giant-llm"}],
                })
            """)
            pg.wait_for_selector("#oopsieModal", state="visible")
            assert "CAPACITY_WORKSPACE_SHORT" in pg.inner_text("#oopsieCode")
            assert "shortfall_bytes" in pg.inner_text("#oopsieEvidence")
            assert "add_capacity" in pg.inner_text("#oopsieActions")
            print("  live typed fill terminal shown")

            # 10. A gated repository is first-class interactive state: the retained notice toasts,
            # the second encounter displays the bounded prompt and a fixed-origin HF link, and the
            # operator decision is posted with the prompt id (stale tabs cannot answer a later one).
            pg.evaluate("document.getElementById('oopsieModal').hidden = true")
            gated_status = {
                "status": "running", "running": True, "phase": "primary",
                "notice": {
                    "id": "access-gated:org/model:1", "type": "access-gated",
                    "repo": "org/model", "message": "Access is required; continuing other work.",
                },
                "operator_prompt": {
                    "id": "access-gated:org/model:2", "type": "access-gated",
                    "repo": "org/model", "title": "Hugging Face access required",
                    "message": "Obtain access, then retry, or skip for this run.",
                    "deadline": time.time() + 300,
                },
            }
            decisions = []
            pg.route(
                "**/api/fill/status",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(gated_status)
                ),
            )
            def gated_decision(route):
                decisions.append(route.request.post_data_json)
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps({"ok": True, "action": "retry"}))
            pg.route("**/api/fill/gated-decision", gated_decision)
            pg.evaluate("window.loadFill()")
            pg.wait_for_selector("#gatedModal", state="visible")
            assert "Access is required" in pg.inner_text("#toast")
            assert pg.get_attribute("#gatedLink", "href") == "https://huggingface.co/org/model"
            assert "continuing in" in pg.inner_text("#gatedCountdown")
            pg.click("#gatedRetry")
            for _ in range(40):
                if decisions:
                    break
                time.sleep(0.05)
            assert decisions == [{"id": "access-gated:org/model:2", "action": "retry"}]
            pg.unroute("**/api/fill/gated-decision")
            pg.unroute("**/api/fill/status")
            print("  gated toast + retry/skip prompt rendered and resolved")

            # 11. PR-01 portal mutation guard (RFC-002/DEC-049 #39 slice 1), tests-first: a selection
            # removal the server refuses with a typed HTTP 409 must hold the prior checked/selected
            # appearance and surface the refusal — WITHOUT issuing any rollback GET (no /api/models
            # reload, no selection-summary refetch), since a refused mutation changed nothing canonical
            # and a rollback fetch could clobber a concurrent allowed addition. The 409 is faked at the
            # client boundary, so the server catalog is never changed.
            refusal = {
                "ok": False, "refused": True, "code": "FILL_SESSION_ACTIVE",
                "error": "Selection finalization and removal are blocked while Fill is running.",
                "actions": ["wait_for_fill", "stop_fill"],
            }
            rollback = {"models": 0, "selection": 0}

            def selection_route(route):
                if route.request.method == "POST":
                    route.fulfill(status=409, content_type="application/json", body=json.dumps(refusal))
                else:                                          # GET summary during a refusal = forbidden rollback
                    rollback["selection"] += 1
                    route.continue_()

            def models_route(route):
                rollback["models"] += 1                        # GET rows during a refusal = forbidden rollback
                route.continue_()

            pg.click("button[data-view='catalog']")
            pg.wait_for_selector("#tbody tr")
            row = "tr[data-id='demo/tiny-llm']"                # seeded finalized -> rendered selected
            pg.wait_for_selector(f"{row} input[type=checkbox]:checked")
            before_n = pg.inner_text("#selN")
            pg.route("**/api/selection", selection_route)
            pg.route("**/api/models**", models_route)
            rollback["models"] = rollback["selection"] = 0     # count only the refused interaction
            pg.click(f"{row} input[type=checkbox]")            # optimistic uncheck -> refused removal
            for _ in range(40):
                if "blocked while Fill is running" in pg.inner_text("#toast"):
                    break
                time.sleep(0.05)
            toast = pg.inner_text("#toast")
            assert "blocked while Fill is running" in toast, f"refusal toast missing: {toast!r}"
            assert pg.is_checked(f"{row} input[type=checkbox]"), "refused row must stay checked"
            assert "sel" in (pg.get_attribute(row, "class") or ""), "refused row must stay selected"
            assert "deselected" not in toast and "finalized" not in toast, f"unexpected success toast: {toast!r}"
            assert pg.inner_text("#selN") == before_n, "tally must be preserved (no refusal render)"
            assert rollback == {"models": 0, "selection": 0}, f"refusal issued rollback GET(s): {rollback}"
            pg.unroute("**/api/models**")
            pg.unroute("**/api/selection")
            print("  selection-removal 409 refusal held the row + tally with no rollback GET")
        except Exception:
            pg.screenshot(path="/tmp/e2e-fail.png")
            print("  (screenshot saved to /tmp/e2e-fail.png)")
            raise
        finally:
            browser.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="modelark-e2e-") as td:
        root = Path(td)
        data_dir, state_dir = root / "data", root / "state"
        db.configure(data_dir, state_dir)
        con = db.connect(_bootstrapping=True)
        _seed(con)
        con.close()
        assert db.DB_PATH.parent == data_dir and db.DB_PATH.is_file()
        print("  seeded models (giant, policy + capacity + hostile blockers) in an isolated catalog")

        serve = Path(sys.executable).with_name("modelark")  # .venv-dev/bin/modelark
        proc = subprocess.Popen(
            [str(serve), "--data-dir", str(data_dir), "--state-dir", str(state_dir),
             "serve", "--no-open", "--port", str(PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            assert _wait_port(PORT), f"portal did not come up on :{PORT}"
            time.sleep(2)                                   # patience: ui_cache build + ark bootstrap
            print(f"  portal up on {BASE}")

            sel = _get("/api/selection")
            assert sel["cap_24h_gb"] == 1000, f"cap should be 1000 GB, got {sel['cap_24h_gb']}"
            plans = _get("/api/plan")
            assert plans["plans"][0]["capacity_mode"] == "guaranteed"
            assert plans["plans"][0]["provisioning"] == "uncompressed"  # one-release alias
            ids = [m["id"] for m in _get("/api/models")["rows"]]
            assert "demo/giant-llm" in ids, f"giant model missing from catalog: {ids}"
            print(f"  api ok: cap={sel['cap_24h_gb']} GB · {len(ids)} models incl. giant + blockers")
            _browser_flow()
            print("all passed")
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    main()
