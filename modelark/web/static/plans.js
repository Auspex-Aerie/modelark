// Plans view (#35): pick a plan to unlock the app; create + recall (no delete yet). Selecting a plan
// sets it active server-side and reloads, so the whole app re-reads against the chosen drive set. The
// gate (app.js) forces this view until a plan is picked; `ark` (the whole fleet) is always available.
(function () {
  const { api, post, gb, toast, esc } = window.MA;
  const $ = id => document.getElementById(id);

  function planCard(p, totals) {
    const t = p.is_active && totals ? totals : null;
    const nums = t
      ? `<div class="pcnums"><span>unc <b>${gb(t.uncompressed)}</b></span><span>comp <b>${gb(t.compressed)}</b></span><span>cap <b>${gb(t.capacity)}</b></span></div>`
      : `<div class="pcnums pcmut">select to see live capacity numbers</div>`;
    const other = p.provisioning === "uncompressed" ? "compressed" : "uncompressed";
    const drives = (p.drives && p.drives.length) ? " (" + p.drives.join(", ") + ")" : " (none)";
    return `<div class="plancard${p.is_active ? " active" : ""}">
      <div class="pcheadrow"><div><span class="pcname">${esc(p.name || p.plan_id)}</span> <span class="pcid">${esc(p.plan_id)}</span></div>
        ${p.is_active ? '<span class="pcactive">active</span>' : ""}</div>
      <div class="pcmeta">${esc(p.provisioning)} · ${esc(p.n_drives)} drive${p.n_drives === 1 ? "" : "s"}${esc(drives)}</div>
      ${nums}
      <div class="pcacts"><button class="pcuse" data-id="${esc(p.plan_id)}">${p.is_active ? "Use this plan →" : "Select + use →"}</button>
        <button class="pcprov" data-id="${esc(p.plan_id)}" data-mode="${other}">switch to ${other}</button></div>
    </div>`;
  }

  async function selectAndReload(id) {
    const r = await post("/api/plan/select", { plan_id: id });
    if (r && r.ok) {
      window.MA.planChosen();
      toast("plan '" + id + "' selected — reloading");
      setTimeout(() => location.reload(), 300);
    } else toast((r && r.error) || "could not select the plan");
  }

  window.loadPlans = async function () {
    const host = $("plansList");
    if (!host) return;
    host.innerHTML = '<div class="pcmut" style="padding:14px">loading…</div>';
    let ov;
    try { ov = await api("/api/plan"); } catch (e) { host.textContent = "error: " + e; return; }
    if (!ov || ov.error) { host.textContent = "error: " + ((ov && ov.error) || "no data"); return; }
    const gate = $("plansGateMsg");
    if (gate) {
      const chosen = sessionStorage.getItem("modelark_plan_chosen");
      gate.hidden = !!chosen;
      if (!chosen) gate.innerHTML = "Choose a plan to unlock Catalog, Library, and Fill. <b>ark</b> owns the whole registered fleet and is always available.";
    }
    host.innerHTML = ov.plans.map(p => planCard(p, ov.totals)).join("");
    host.querySelectorAll(".pcuse").forEach(b => b.onclick = () => selectAndReload(b.dataset.id));
    host.querySelectorAll(".pcprov").forEach(b => b.onclick = async () => {
      const r = await post("/api/plan/provisioning", { plan_id: b.dataset.id, mode: b.dataset.mode });
      if (r && r.ok) { toast("provisioning → " + r.provisioning); window.loadPlans(); }
      else toast((r && r.error) || "failed");
    });
  };

  function wire() {
    const btn = $("newPlanBtn");
    if (!btn) return;
    btn.onclick = async () => {
      const id = ($("newPlanId").value || "").trim().toLowerCase();
      if (!id) { toast("enter a plan id"); return; }
      const r = await post("/api/plan/create",
        { plan_id: id, name: $("newPlanName").value, provisioning: $("newPlanProv").value });
      if (r && r.ok) { toast("created plan '" + r.plan_id + "'"); $("newPlanId").value = ""; $("newPlanName").value = ""; window.loadPlans(); }
      else toast((r && r.error) || "could not create the plan");
    };
  }
  if (document.readyState !== "loading") wire(); else document.addEventListener("DOMContentLoaded", wire);
})();
