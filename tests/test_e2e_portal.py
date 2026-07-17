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
    con.executemany(
        "INSERT INTO selection(repo_id,finalized_at) VALUES(?,'2026-07-15')",
        [("demo/tiny-llm",), ("demo/pickle-only",), ("demo/replica-blocked",)],
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
            assert "CAPACITY_" in advisory
            pg.wait_for_selector("#fillQueue .telq.blocked")
            blocked = pg.inner_text("#fillQueue")
            assert "demo/pickle-only" in blocked and "MANIFEST_POLICY" in blocked
            assert "demo/replica-blocked" in blocked and "CAPACITY_" in blocked
            assert pg.locator("#fillQueue .telq.blocked").count() == 2
            fill_note = pg.inner_text("#fillNote")
            assert "1 to place" in fill_note and "2 blocked" in fill_note, fill_note
            assert pg.locator("#fillStart").is_disabled()
            print("  policy + capacity blockers rendered with disjoint totals; Start fill disabled")

            # The established drive's planned segment remains left-aligned; the durable archived
            # occupancy trails in grey instead of pushing the useful planned colors to the right.
            segments = pg.locator("#dc-drive-00 .dcbarfill > .seg")
            assert segments.count() >= 2
            assert "segarch" not in (segments.first.get_attribute("class") or "")
            assert "segarch" in (segments.last.get_attribute("class") or "")
            assert segments.first.bounding_box()["x"] < segments.last.bounding_box()["x"]
            print("  drive progress segments remain left-aligned")

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

            # 4. open Catalog, wait for rows, confirm the giant is there
            pg.click("button[data-view='catalog']")
            time.sleep(2)
            pg.wait_for_selector("#tbody tr")
            assert pg.query_selector("tr[data-id='demo/giant-llm']"), "giant row missing from catalog"
            print("  catalog rendered")
            # 5. tick the giant -> the over-cap banner should appear
            pg.check("tr[data-id='demo/giant-llm'] input[type=checkbox]")
            time.sleep(3)                                # selection round-trip + renderBudget
            pg.wait_for_selector("#capWarn", state="visible")
            msg = pg.inner_text("#capWarnMsg")
            assert "24-hour" in msg and "considerate" in msg, f"unexpected banner text: {msg!r}"
            print("  over-cap banner shown")
            # 6. dismiss hides it
            pg.click("#capWarnDismiss")
            time.sleep(1)
            pg.wait_for_selector("#capWarn", state="hidden")
            print("  banner dismissed")

            # 7. A bounded transport retry must be visibly identified as network work, with its
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

            # 8. the same public hook used by the live Fill poll must show typed terminals without a
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
        print("  seeded 6 models (1 giant, policy + capacity blockers) in an isolated catalog")

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
