// Verify view (DEF-021): auto-surfaced disruption suspects + on-demand re-verify of archived copies.
// Record consistency is checked offline; the decompress-canary runs server-side when a drive is mounted.
(function () {
  const { api, post, toast } = window.MA;
  const $ = id => document.getElementById(id);
  let suspects = [];

  const suspectRow = s => `<div class="vfsus">
      <div class="vfsusmain"><span class="vfrepo">${s.repo}</span>
        <span class="vfreasons">${s.reasons.join(" · ")}</span></div>
      <div class="vfsusmeta"><span>${(s.drives || []).join(", ")}${s.verified_at ? " · " + s.verified_at : ""}</span>
        <button class="vfone" data-repo="${s.repo}">re-verify</button></div>
    </div>`;

  function verdictRow(r) {
    const status = r.status || (r.ok ? "verified" : "failed");
    const cls = !r.archived || status === "unknown" ? "mut" : status === "verified" ? "ok" : "bad";
    const badge = !r.archived ? "not archived" : status === "verified" ? "verified"
      : status === "unknown" ? "not fully checked" : "FAIL";
    const checks = (r.deep_checks && r.deep_checks.length)
      ? '<div class="vfchecks">' + r.deep_checks.map(c => `${c.ok === true ? "✓" : c.ok === false ? "✗" : "?"} ${c.file}${c.err ? " (" + c.err + ")" : ""}`).join("<br>") + "</div>" : "";
    return `<div class="vfres ${cls}"><div class="vfresh"><span class="vfrepo">${r.repo}</span>
      <span class="vfbadge ${cls}">${badge}</span></div><div class="vfresd">${r.detail || ""}</div>${checks}</div>`;
  }

  async function runVerify(repos) {
    if (!repos.length) { toast("no models to verify"); return; }
    $("vfResults").innerHTML = '<div class="pcmut" style="padding:12px">verifying ' + repos.length + ' model(s)…</div>';
    let r;
    try { r = await post("/api/verify/run", { repos }); } catch (e) { r = { error: String(e) }; }
    if (!r || !r.ok) { $("vfResults").innerHTML = '<div class="pcmut" style="padding:12px">error: ' + ((r && r.error) || "failed") + "</div>"; return; }
    $("vfResults").innerHTML = '<p class="psub" style="margin:18px 0 8px"><b>Results</b></p>' + r.results.map(verdictRow).join("");
  }

  window.loadVerify = async function () {
    const host = $("vfSuspects");
    if (!host) return;
    host.innerHTML = '<div class="pcmut" style="padding:12px">scanning for suspects…</div>';
    let d;
    try { d = await api("/api/verify/suspects"); } catch (e) { host.innerHTML = "error: " + e; return; }
    suspects = (d && d.suspects) || [];
    $("vfNote").textContent = suspects.length
      ? suspects.length + " suspect(s) found — archiving overlapped a disruption"
      : "no suspects — every archived copy is clear of a disruption boundary";
    $("vfReverifyAll").disabled = !suspects.length;
    host.innerHTML = suspects.length ? suspects.map(suspectRow).join("")
      : '<div class="pcmut" style="padding:12px">Nothing looks disrupted. 🎉</div>';
    host.querySelectorAll(".vfone").forEach(b => b.onclick = () => runVerify([b.dataset.repo]));
  };

  function wire() {
    const all = $("vfReverifyAll"), run = $("vfRun");
    if (all) all.onclick = () => runVerify(suspects.map(s => s.repo));
    if (run) run.onclick = () => { const v = ($("vfRepo").value || "").trim(); if (v) runVerify([v]); };
  }
  if (document.readyState !== "loading") wire(); else document.addEventListener("DOMContentLoaded", wire);
})();
