"""E2E smoke test: the portal boots on a throwaway seeded catalog and the 24h-cap plumbing is live.

Standalone harness (no pytest). Needs .venv-dev (playwright installs come in V2):
    python3 -m venv .venv-dev && .venv-dev/bin/pip install -e . playwright && .venv-dev/bin/playwright install chromium
    .venv-dev/bin/python tests/test_e2e_portal.py

V1 (this file): back up any real catalog, seed a tiny one (a few small models + one 1.5 TB giant),
start `modelark serve` headless on a test port, and assert over HTTP that the cap is 1 TB and the
giant is in the catalog. V2 adds the Playwright browser flow (select plan -> tick giant -> banner).
Cleans up + restores the real catalog in `finally`.
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
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
        except Exception:
            pg.screenshot(path="/tmp/e2e-fail.png")
            print("  (screenshot saved to /tmp/e2e-fail.png)")
            raise
        finally:
            browser.close()


def main() -> None:
    backup = None
    if db.DB_PATH.exists():                                  # never clobber a real catalog
        backup = db.DB_PATH.with_name("catalog.sqlite.e2e-bak")
        shutil.move(str(db.DB_PATH), str(backup))
    proc = None
    try:
        con = db.connect(_bootstrapping=True)               # creates catalog.sqlite + applies schema.sql
        _seed(con)
        con.close()
        print("  seeded 4 models (1 giant)")

        serve = Path(sys.executable).with_name("modelark")  # .venv-dev/bin/modelark
        proc = subprocess.Popen([str(serve), "serve", "--no-open", "--port", str(PORT)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert _wait_port(PORT), f"portal did not come up on :{PORT}"
        time.sleep(2)                                       # patience: ui_cache build + ark bootstrap
        print(f"  portal up on {BASE}")

        sel = _get("/api/selection")
        assert sel["cap_24h_gb"] == 1000, f"cap should be 1000 GB, got {sel['cap_24h_gb']}"
        ids = [m["id"] for m in _get("/api/models")["rows"]]
        assert "demo/giant-llm" in ids, f"giant model missing from catalog: {ids}"
        print(f"  api ok: cap={sel['cap_24h_gb']} GB · {len(ids)} models incl. the giant")
        _browser_flow()
        print("all passed")
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=10)
        db.DB_PATH.unlink(missing_ok=True)
        if backup:
            shutil.move(str(backup), str(db.DB_PATH))


if __name__ == "__main__":
    main()
