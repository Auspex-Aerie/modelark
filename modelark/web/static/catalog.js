// Catalog view: filter/search/sort, build the cart, finalize the wishlist.
(function () {
  const {api, post, gb, dl, toast} = window.MA;
  const $ = id => document.getElementById(id);
  const S = {q: "", cat: new Set(), v: new Set(), bucket: new Set(),
             hide_quant: 1, hide_gated: 0, sel: "", sort: "dl", dir: "desc"};
  let BUD = +(localStorage.getItem("modelark_budget") || 27);
  let lastSel = {n: 0, bytes: 0, finalized: 0, by_cat: []};
  let gatePrevent = false;                     // #38: cart's compressed footprint would exceed plan capacity
  const catChips = {};

  const chip = (t, cls) => { const b = document.createElement("button");
    b.className = "chip " + (cls || ""); b.textContent = t; return b; };
  const tog = (set, val, btn) => { set.has(val) ? set.delete(val) : set.add(val); btn.classList.toggle("on"); };

  async function init() {
    const f = await api("/api/facets");
    if (!localStorage.getItem("modelark_budget")) BUD = f.budget;
    $("budget").value = BUD;
    const cf = $("catf"); cf.innerHTML = '<span class="flabel">category</span>';
    f.categories.forEach(c => { const b = chip(c.name + " " + c.n); catChips[c.name] = b;
      b.onclick = () => { tog(S.cat, c.name, b); load(); }; cf.appendChild(b); });
    const vf = $("varf"); vf.innerHTML = '<span class="flabel">variant</span>';
    f.variants.forEach(v => { const b = chip(v, "v t-" + v); b.style.color = "var(--" + v + ")";
      b.onclick = () => { tog(S.v, v, b); b.style.background = b.classList.contains("on") ? "var(--" + v + ")" : ""; load(); };
      vf.appendChild(b); });
    const bf = $("bucf"); bf.innerHTML = '<span class="flabel">size</span>';
    f.buckets.forEach(bk => { const b = chip(bk);
      b.onclick = () => { tog(S.bucket, bk, b); load(); }; bf.appendChild(b); });
    load(); refreshTally();
  }

  function qstr() {
    const p = new URLSearchParams({q: S.q, sort: S.sort, dir: S.dir,
      hide_quant: S.hide_quant, hide_gated: S.hide_gated});
    if (S.sel) p.set("sel", S.sel);
    if (S.cat.size) p.set("cat", [...S.cat].join(","));
    if (S.v.size) p.set("v", [...S.v].join(","));
    if (S.bucket.size) p.set("bucket", [...S.bucket].join(","));
    return p.toString();
  }

  async function load() {
    const d = await api("/api/models?" + qstr());
    $("tbody").innerHTML = d.rows.map(m => `
      <tr class="${m.sel ? 'sel' : ''}" data-id="${m.id}">
      <td class="cb"><input type="checkbox" ${m.sel ? 'checked' : ''}></td>
      <td class="idcell">${m.id}${m.g ? ' <span class="lock" title="gated">&#128274;</span>' : ''}</td>
      <td class="num">${m.p != null ? m.p + 'B' : '—'}</td><td>${m.bucket}</td><td>${m.cat}</td>
      <td><span class="tag t-${m.v}">${m.v}</span></td><td class="num">${gb(m.bytes)}</td>
      <td class="num">${dl(m.dl)}</td><td>${m.lic}</td></tr>`).join("");
    $("empty").style.display = d.rows.length ? "none" : "block";
    $("shown").textContent = d.matched + " of " + d.total + (d.capped ? " (showing " + d.rows.length + ")" : "") + " shown";
    $("sizeScreen").textContent = "· " + gb(d.filtered_bytes || 0) + " on screen";
  }

  function renderBudget() {
    const tb = lastSel.bytes / 1e12;
    $("selTB").textContent = tb.toFixed(2);
    $("selN").textContent = lastSel.n;
    const pct = Math.min(100, tb / BUD * 100), bar = $("barfill");
    bar.style.width = pct + "%";
    bar.style.background = tb > BUD ? "var(--crit)" : tb > BUD * 0.85 ? "var(--warn)" : "var(--ok)";
    $("bnote").textContent = tb > BUD ? ("over by " + (tb - BUD).toFixed(1) + " TB")
      : ((BUD - tb).toFixed(1) + " TB left · ZipNN ~30% off bf16 on disk");
    $("finnote").innerHTML = lastSel.finalized ? `<b>${lastSel.finalized}</b> finalized (ready for fetch)` : "nothing finalized yet";
  }

  function renderTally(s) {
    lastSel = s; renderBudget();
    $("tally").innerHTML = '<h2>your set · by category</h2>' + (s.by_cat.length ? s.by_cat.map(g => `
      <div class="crow" data-cat="${g.cat}"><div class="cc">${g.cat}</div><div class="cn">${g.n} · ${gb(g.bytes)}</div>
      ${g.recent ? `<div class="rec">+ ${g.recent.split('/').pop()}</div>` : ''}</div>`).join("")
      : '<div class="rec" style="padding:10px">Tick rows to build your set. Hit Finish to commit it as the wishlist.</div>');
    $("tally").querySelectorAll(".crow").forEach(el => el.onclick = () => {
      const cat = el.dataset.cat;                       // clicking a tally category filters the table to it
      Object.values(catChips).forEach(b => b.classList.remove("on"));
      S.cat = new Set([cat]); if (catChips[cat]) catChips[cat].classList.add("on");
      load();
    });
    refreshPlanGate();                                  // #38: recompute the cart footprint vs plan capacity
  }
  const refreshTally = async () => renderTally(await api("/api/selection"));

  // #38 graduated catalog gate: the cart's live footprint vs the active plan's capacity. Tiers on the
  // COMPRESSED estimate (what actually lands): ok → soft (approaching) → warn (near full) → prevent
  // (over capacity — block adding more). Uncompressed-over-capacity is flagged too (fits only if
  // compression holds). Read-only; refreshed after every selection change (renderTally).
  async function refreshPlanGate() {
    const el = $("planGate");
    if (!el) return;
    let g;
    try { g = await api("/api/plan/cart"); } catch { return; }
    if (!g || g.error) { el.innerHTML = ""; gatePrevent = false; return; }
    gatePrevent = g.tier === "prevent";
    const cap = g.capacity || 1;
    const compPct = Math.min(100, 100 * g.compressed / cap);
    const badge = {ok: "ok", soft: "watch", warn: "near full", prevent: "over capacity"}[g.tier] || g.tier;
    const barColor = g.tier === "ok" ? "var(--ok)" : g.tier === "prevent" ? "var(--crit)" : "var(--warn)";
    let note;
    if (g.tier === "prevent")
      note = `<span class="pgnote crit"><b>Over capacity.</b> Compressed ${gb(g.compressed)} exceeds the plan's ${gb(cap)} — adding is blocked. Remove models or add a drive to the plan.</span>`;
    else if (g.tier === "warn")
      note = `<span class="pgnote"><b>Near full.</b> Compressed ${gb(g.compressed)} of ${gb(cap)}${g.over_uncompressed ? ` · uncompressed ${gb(g.uncompressed)} over raw (fits only if compression holds)` : ""}.</span>`;
    else if (g.tier === "soft")
      note = `<span class="pgnote">Compressed ${gb(g.compressed)} of ${gb(cap)} — approaching capacity · uncompressed ${gb(g.uncompressed)}.</span>`;
    else
      note = `<span class="pgnote">Compressed ${gb(g.compressed)} · uncompressed ${gb(g.uncompressed)} of ${gb(cap)} capacity.</span>`;
    el.innerHTML =
      `<div class="pgh"><span class="pgtitle">Plan capacity · ${g.plan_id}</span><span class="pgbadge ${g.tier}">${badge}</span></div>` +
      `<div class="pgbar"><div style="width:${compPct.toFixed(1)}%;background:${barColor}"></div></div>` + note;
  }

  // selection events
  $("tbody").addEventListener("change", async e => {
    if (e.target.type !== "checkbox") return;
    const tr = e.target.closest("tr");
    if (e.target.checked && gatePrevent) {              // #38 prevent: over compressed capacity → block ADDS (un-checking always allowed)
      e.target.checked = false;
      toast("Over plan capacity — remove models or add a drive before adding more.");
      return;
    }
    tr.classList.toggle("sel", e.target.checked);
    renderTally(await post("/api/selection", {id: tr.dataset.id, on: e.target.checked}));
  });
  function visibleIds() { return [...document.querySelectorAll("#tbody tr")].map(tr => tr.dataset.id); }
  function markVisible(on) {
    document.querySelectorAll("#tbody tr").forEach(tr => {
      tr.classList.toggle("sel", on); const cb = tr.querySelector("input"); if (cb) cb.checked = on; });
  }
  $("selAll").onclick = async () => {
    if (gatePrevent) { toast("Over plan capacity — can't add more. Remove models or add a drive to the plan."); return; }
    const ids = visibleIds(); markVisible(true);
    renderTally(await post("/api/selection/bulk", {ids, on: true})); toast(ids.length + " added"); };
  $("deselAll").onclick = async () => { const ids = visibleIds(); markVisible(false);
    renderTally(await post("/api/selection/bulk", {ids, on: false})); toast("deselected shown"); };
  $("clear").onclick = async () => { if (!confirm("Clear the entire set (cart + finalized)?")) return;
    markVisible(false); renderTally(await post("/api/selection/clear", {})); };
  $("finish").onclick = async () => { const s = await post("/api/selection/finalize", {});
    renderTally(s); toast(s.finalized + " models finalized → wishlist"); };
  $("export").onclick = () => { location = "/api/export"; toast("selection downloaded"); };

  // controls
  let dt; $("search").addEventListener("input", e => { S.q = e.target.value; clearTimeout(dt); dt = setTimeout(load, 180); });
  $("budget").addEventListener("change", e => { BUD = +e.target.value || 27; localStorage.setItem("modelark_budget", BUD); renderBudget(); });
  $("hideQuant").onclick = e => { S.hide_quant ^= 1; e.target.classList.toggle("on"); load(); };
  $("hideGated").onclick = e => { S.hide_gated ^= 1; e.target.classList.toggle("on"); load(); };
  function setSel(mode) {                              // only-checked / only-unchecked (mutually exclusive)
    S.sel = (S.sel === mode) ? "" : mode;
    $("showChecked").classList.toggle("on", S.sel === "checked");
    $("showUnchecked").classList.toggle("on", S.sel === "unchecked");
    load();
  }
  $("showChecked").onclick = () => setSel("checked");
  $("showUnchecked").onclick = () => setSel("unchecked");
  document.querySelectorAll("thead th[data-k]").forEach(th => th.onclick = () => {
    const k = th.dataset.k;
    if (S.sort === k) S.dir = S.dir === "asc" ? "desc" : "asc";
    else { S.sort = k; S.dir = (["id", "cat", "v", "lic", "bucket"].includes(k)) ? "asc" : "desc"; }
    document.querySelectorAll("thead th .ar").forEach(a => a.remove());
    th.insertAdjacentHTML("beforeend", ' <span class="ar">' + (S.dir === "asc" ? "▲" : "▼") + '</span>');
    load();
  });

  init();
})();
