// Shared helpers + view navigation.
const escapeHTML = value => String(value == null ? "" : value).replace(/[&<>"']/g, ch => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[ch]);

window.MA = {
  api: (p, o) => fetch(p, o).then(r => r.json()),
  post: (p, body) => {
    const token = document.querySelector('meta[name="modelark-csrf-token"]')?.content;
    const headers = {"Content-Type": "application/json"};
    if (token) headers["X-ModelArk-CSRF"] = token;
    return fetch(p, {method: "POST", headers, body: JSON.stringify(body)}).then(r => r.json());
  },
  // Any API/operator value interpolated into an HTML template must pass through this helper.
  // Prefer textContent for plain text; esc() exists for the structured views below.
  esc: escapeHTML,
  gb: b => b >= 1e12 ? (b / 1e12).toFixed(2) + "TB" : (b / 1e9).toFixed(0) + "GB",
  dl: n => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : n,
  toast(m) {
    const t = document.getElementById("toast");
    t.textContent = m; t.classList.add("show");
    clearTimeout(this._tt); this._tt = setTimeout(() => t.classList.remove("show"), 1700);
  },
};

window.MA.fillTerminals = {
  done: "✅ done", stopped: "■ stopped", error: "🔴 error", paused: "⏸ paused",
  blocked: "⚠ blocked", "plan-capacity-stop": "🟠 capacity changed",
};
window.MA.isFillTerminal = status => !!window.MA.fillTerminals[status];

let diskLoaded = false;
document.querySelectorAll(".navbtn").forEach(b => b.onclick = () => {
  if (b.disabled) return;
  const v = b.dataset.view;
  document.querySelectorAll(".navbtn").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach(s => s.hidden = s.id !== "view-" + v);
  if (v === "plans") { window.loadPlans && window.loadPlans(); }
  if (v === "disk" && !diskLoaded) { diskLoaded = true; window.loadDisk && window.loadDisk(); }
  if (v === "library") { window.loadLibrary && window.loadLibrary(); }
  if (v === "fill") { window.loadFill && window.loadFill(); }
  if (v === "verify") { window.loadVerify && window.loadVerify(); }
});

// #35 plan gate: force an explicit plan pick per session before Catalog/Library/Fill unlock. `ark` is
// always bootstrapped + selectable, so this can never lock you out. Selecting a plan (plans.js) sets
// sessionStorage + reloads → this then leaves the nav enabled and the default Catalog view shows.
const PLAN_KEY = "modelark_plan_chosen";
window.MA.planChosen = () => sessionStorage.setItem(PLAN_KEY, "1");
function applyPlanGate() {
  const chosen = sessionStorage.getItem(PLAN_KEY);
  document.querySelectorAll(".navbtn").forEach(b => {
    const isPlans = b.dataset.view === "plans";
    b.disabled = !chosen && !isPlans;
    b.classList.toggle("gated", !chosen && !isPlans);
  });
  if (!chosen) {                                     // force the Plans view until a plan is chosen
    document.querySelectorAll(".navbtn").forEach(x => x.classList.toggle("on", x.dataset.view === "plans"));
    document.querySelectorAll(".view").forEach(s => s.hidden = s.id !== "view-plans");
    window.loadPlans && window.loadPlans();
  }
}
if (document.readyState !== "loading") applyPlanGate();
else document.addEventListener("DOMContentLoaded", applyPlanGate);

// DEF-023: a fill that fell over must ANNOUNCE itself on open — an unmissable modal that survives a
// reload/restart until acknowledged (INC-009 sat silent overnight). Shown regardless of the plan gate.
(function () {
  const TITLE = {
    error: "🔴 The fill errored", blocked: "⚠ The fill is blocked",
    "plan-capacity-stop": "🟠 A drive filled up — add capacity", paused: "⏸ The fill paused",
  };
  const overlay = document.getElementById("oopsieModal");
  if (!overlay) return;
  function show(t) {
    document.getElementById("oopsieHead").textContent = TITLE[t.status] || ("Fill: " + t.status);
    document.getElementById("oopsieMsg").textContent = t.message || "";
    document.getElementById("oopsieCode").textContent = t.code
      ? `code: ${t.code}${t.gate ? ` · gate ${t.gate}` : ""}` : "";
    const fel = document.getElementById("oopsieFailed");
    fel.textContent = (t.failed && t.failed.length)
      ? "affected: " + t.failed.map(f => f.repo || f.requirement_id || f.code || "unknown").join(" · ") : "";
    const evidence = t.evidence || {};
    document.getElementById("oopsieEvidence").textContent = Object.keys(evidence).length
      ? "evidence: " + JSON.stringify(evidence) : "";
    document.getElementById("oopsieActions").textContent = (t.actions && t.actions.length)
      ? "next: " + t.actions.join(" · ") : "";
    document.getElementById("oopsieWhen").textContent = t.when ? ("when: " + t.when) : "";
    overlay.hidden = false;
  }
  window.MA.showFillTerminal = show;
  document.getElementById("oopsieAck").onclick = () =>
    window.MA.post("/api/fill/ack-terminal", {}).finally(() => { overlay.hidden = true; });
  document.getElementById("oopsieView").onclick = () => {
    overlay.hidden = true;
    const b = document.querySelector('.navbtn[data-view="fill"]');
    if (b && !b.disabled) b.click();
  };
  window.MA.api("/api/fill/last-terminal").then(t => { if (t && t.status) show(t); }).catch(() => {});
})();

// Non-Linux: drive health is punted to the OS — surface a small nav note.
window.MA.api("/api/meta").then(m => {
  window.MA.meta = m || {};
  if (m && m.smart_supported === false) {
    const n = document.getElementById("navnote");
    if (n) n.textContent = (m.os || "This OS") + " drives aren't health-checked in-system";
  }
}).catch(() => {});
