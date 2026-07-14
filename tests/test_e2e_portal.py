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
            # 1. select the `ark` plan (the #35 gate forces this before anything unlocks) -> app reloads
            pg.wait_for_selector(".pcuse[data-id='ark']")
            pg.click(".pcuse[data-id='ark']")
            time.sleep(3)                                # reload fires ~300ms after select; let it settle
            pg.wait_for_load_state("networkidle")
            print("  selected the ark plan")
            # 2. open Catalog, wait for rows, confirm the giant is there
            pg.click("button[data-view='catalog']")
            time.sleep(2)
            pg.wait_for_selector("#tbody tr")
            assert pg.query_selector("tr[data-id='demo/giant-llm']"), "giant row missing from catalog"
            print("  catalog rendered")
            # 3. tick the giant -> the over-cap banner should appear
            pg.check("tr[data-id='demo/giant-llm'] input[type=checkbox]")
            time.sleep(3)                                # selection round-trip + renderBudget
            pg.wait_for_selector("#capWarn", state="visible")
            msg = pg.inner_text("#capWarnMsg")
            assert "24-hour" in msg and "considerate" in msg, f"unexpected banner text: {msg!r}"
            print("  over-cap banner shown")
            # 4. dismiss hides it
            pg.click("#capWarnDismiss")
            time.sleep(1)
            pg.wait_for_selector("#capWarn", state="hidden")
            print("  banner dismissed")
            # 5. the same public hook used by the live Fill poll must show typed terminals without a
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
        print("  seeded 4 models (1 giant) in an isolated catalog")

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
            ids = [m["id"] for m in _get("/api/models")["rows"]]
            assert "demo/giant-llm" in ids, f"giant model missing from catalog: {ids}"
            print(f"  api ok: cap={sel['cap_24h_gb']} GB · {len(ids)} models incl. the giant")
            _browser_flow()
            print("all passed")
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    main()
