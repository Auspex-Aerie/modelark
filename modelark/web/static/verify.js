// Verify view (DEF-021): auto-surfaced disruption suspects + on-demand re-verify of archived copies.
// Record consistency is checked offline; the decompress-canary runs server-side when a drive is mounted.
(function () {
  const { api, post, toast, esc, hfRepoURL } = window.MA;
  const $ = id => document.getElementById(id);
  let suspects = [];

  const suspectRow = s => {
    const types = s.types || ["integrity"];
    const integrity = types.includes("integrity"), access = types.includes("access-gated");
    const badges = types.map(t => `<span class="vfbadge ${t === "access-gated" ? "mut" : "bad"}">${esc(t)}</span>`).join(" ");
    const actions = (access ? `<a class="modal-link vfaccess" href="${esc(hfRepoURL(s.repo))}" target="_blank" rel="noopener noreferrer">get access</a>` : "")
      + (integrity ? `<button class="vfone" data-repo="${esc(s.repo)}">re-verify</button>` : "");
    return `<div class="vfsus">
      <div class="vfsusmain"><span class="vfrepo">${esc(s.repo)}</span>
        <span>${badges}</span><span class="vfreasons">${esc(s.reasons.join(" · "))}</span></div>
      <div class="vfsusmeta"><span>${esc((s.drives || []).join(", "))}${(s.followup_at || s.verified_at) ? " · " + esc(s.followup_at || s.verified_at) : ""}</span>
        <span>${actions}</span></div>
    </div>`;
  };

  function verdictRow(r) {
    const status = r.status || (r.ok ? "verified" : "failed");
    const cls = !r.archived || status === "unknown" ? "mut" : status === "verified" ? "ok" : "bad";
    const badge = !r.archived ? "not archived" : status === "verified" ? "verified"
      : status === "unknown" ? "not fully checked" : "FAIL";
    const checks = (r.deep_checks && r.deep_checks.length)
      ? '<div class="vfchecks">' + r.deep_checks.map(c => `${c.ok === true ? "✓" : c.ok === false ? "✗" : "?"} ${esc(c.file)}${c.err ? " (" + esc(c.err) + ")" : ""}`).join("<br>") + "</div>" : "";
    return `<div class="vfres ${cls}"><div class="vfresh"><span class="vfrepo">${esc(r.repo)}</span>
      <span class="vfbadge ${cls}">${badge}</span></div><div class="vfresd">${esc(r.detail || "")}</div>${checks}</div>`;
  }

  async function runVerify(repos) {
    if (!repos.length) { toast("no models to verify"); return; }
    $("vfResults").innerHTML = '<div class="pcmut" style="padding:12px">verifying ' + repos.length + ' model(s)…</div>';
    let r;
    try { r = await post("/api/verify/run", { repos }); } catch (e) { r = { error: String(e) }; }
    if (!r || !r.ok) { $("vfResults").innerHTML = '<div class="pcmut" style="padding:12px">error: ' + esc((r && r.error) || "failed") + "</div>"; return; }
    $("vfResults").innerHTML = '<p class="psub" style="margin:18px 0 8px"><b>Results</b></p>' + r.results.map(verdictRow).join("");
  }

  window.loadVerify = async function () {
    const host = $("vfSuspects");
    if (!host) return;
    host.innerHTML = '<div class="pcmut" style="padding:12px">scanning for suspects…</div>';
    let d;
    try { d = await api("/api/verify/suspects"); } catch (e) { host.textContent = "error: " + e; return; }
    suspects = (d && d.suspects) || [];
    const integrity = suspects.filter(s => (s.types || ["integrity"]).includes("integrity"));
    const access = suspects.filter(s => (s.types || []).includes("access-gated"));
    $("vfNote").textContent = suspects.length
      ? `${integrity.length} integrity suspect(s) · ${access.length} access follow-up(s)`
      : "no follow-ups — no disrupted copies or deferred access";
    $("vfReverifyAll").disabled = !integrity.length;
    host.innerHTML = suspects.length ? suspects.map(suspectRow).join("")
      : '<div class="pcmut" style="padding:12px">No follow-ups. 🎉</div>';
    host.querySelectorAll(".vfone").forEach(b => b.onclick = () => runVerify([b.dataset.repo]));
  };

  function wire() {
    const all = $("vfReverifyAll"), run = $("vfRun");
    if (all) all.onclick = () => runVerify(suspects
      .filter(s => (s.types || ["integrity"]).includes("integrity")).map(s => s.repo));
    if (run) run.onclick = () => { const v = ($("vfRepo").value || "").trim(); if (v) runVerify([v]); };
  }
  if (document.readyState !== "loading") wire(); else document.addEventListener("DOMContentLoaded", wire);
})();
