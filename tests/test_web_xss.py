"""Frontend HTML-escaping and CSRF-header regression tests (standalone + pytest)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "modelark" / "web" / "static"


def test_shared_encoder_and_post_contract() -> None:
    """Exercise the actual app.js helpers in Node with a minimal browser facade."""
    script = f"""
const fs = require("fs");
const vm = require("vm");
let request;
const document = {{
  readyState: "loading",
  querySelectorAll: () => [],
  querySelector: selector => selector === 'meta[name="modelark-csrf-token"]'
    ? {{content: "csrf-<&token"}} : null,
  getElementById: () => null,
  addEventListener: () => {{}},
}};
const context = {{
  window: {{}}, document, sessionStorage: {{getItem: () => null, setItem: () => {{}}}},
  clearTimeout: () => {{}}, setTimeout: () => 0,
  fetch: async (path, options) => {{ request = {{path, options}}; return {{json: async () => ({{ok: true}})}}; }},
}};
vm.runInNewContext(fs.readFileSync({json.dumps(str(STATIC / 'app.js'))}, "utf8"), context);
const payload = `<img src=x onerror="globalThis.pwned=1"> & 'quoted'`;
if (context.window.MA.esc(payload) !== "&lt;img src=x onerror=&quot;globalThis.pwned=1&quot;&gt; &amp; &#39;quoted&#39;")
  throw new Error("escapeHTML did not encode an executable payload");
if (context.window.MA.hfRepoURL("org/model with spaces") !== "https://huggingface.co/org/model%20with%20spaces")
  throw new Error("Hugging Face repository URL was not safely encoded");
context.window.MA.post("/api/mutate", {{value: payload}}).then(() => {{
  if (request.options.headers["Content-Type"] !== "application/json") throw new Error("missing JSON content type");
  if (request.options.headers["X-ModelArk-CSRF"] !== "csrf-<&token") throw new Error("missing CSRF token");
  if (JSON.parse(request.options.body).value !== payload) throw new Error("POST body changed");
}}).catch(error => {{ console.error(error); process.exitCode = 1; }});
"""
    subprocess.run(["node", "-e", script], check=True, cwd=ROOT)


def test_api_text_fields_use_the_shared_encoder() -> None:
    """Keep the previously exploitable and highest-risk API fields behind MA.esc()."""
    expected = {
        "plans.js": (
            "${esc(p.name || p.plan_id)}",
            'data-id="${esc(p.plan_id)}"',
        ),
        "catalog.js": (
            "${esc(m.id)}",
            "${esc(m.lic)}",
            "${esc(g.recent.split('/').pop())}",
        ),
        "disk.js": (
            "${esc(x.serial)}",
            "${esc(x.quirk_cmd)}",
        ),
        "library.js": (
            "${esc(m.repo_id)}",
            "${esc((m.drives || []).join(\", \"))}",
            'data-drive="${esc(x.label)}"',
        ),
        "fill.js": (
            "${esc(a.msg)}",
            "${esc(s.repo || \"—\")}",
            "${esc(s.awaiting_drive)}",
            "${esc(t.plan_id)}",
        ),
        "verify.js": (
            "${esc(s.repo)}",
            "${esc(c.file)}",
            "${esc(r.detail || \"\")}",
        ),
    }
    for filename, needles in expected.items():
        source = (STATIC / filename).read_text()
        assert "esc" in source, filename
        for needle in needles:
            assert needle in source, f"{filename} lost escaping around {needle}"


def test_no_direct_script_insertion_apis() -> None:
    for path in STATIC.glob("*.js"):
        source = path.read_text()
        assert "document.write" not in source, path.name
        assert ".outerHTML" not in source, path.name
        assert "insertAdjacentHTML" not in source, path.name


def test_hugging_face_url_builder_is_shared() -> None:
    app = (STATIC / "app.js").read_text()
    fill = (STATIC / "fill.js").read_text()
    verify = (STATIC / "verify.js").read_text()
    assert app.count("hfRepoURL:") == 1
    assert "MA.hfRepoURL(p.repo)" in fill
    assert "const { api, post, toast, esc, hfRepoURL } = window.MA" in verify
    assert "const hfRepoURL" not in fill
    assert "const hfRepoURL" not in verify


def test_fill_poll_uses_shared_terminal_classifier_and_modal() -> None:
    app = (STATIC / "app.js").read_text()
    fill = (STATIC / "fill.js").read_text()
    assert '"plan-capacity-stop": "🟠 capacity changed"' in app
    assert "window.MA.showFillTerminal = show" in app
    assert "MA.isFillTerminal(s.status)" in fill
    call_sites = [line.strip() for line in fill.splitlines() if line.strip() == "announceTerminal(s);"]
    assert len(call_sites) == 2, "both poll and refresh/load must announce terminals"
    assert "transient network retry" in fill
    assert "retry_attempt" in fill and "retry_limit" in fill
    assert "publish:" in fill


if __name__ == "__main__":
    test_shared_encoder_and_post_contract()
    test_api_text_fields_use_the_shared_encoder()
    test_no_direct_script_insertion_apis()
    test_hugging_face_url_builder_is_shared()
    test_fill_poll_uses_shared_terminal_classifier_and_modal()
    print("web XSS tests passed")
