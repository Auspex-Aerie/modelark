// Fill view — the librarian's placement plan: drives grouped by tier, stacked fill bars,
// a type/category toggle, and dotted copy#1→copy#2 links. Consumes /api/library/plan
// (the same librarian.plan_view the CLI's `library plan --json` emits).
(function () {
  const {esc} = window.MA;
  const CAT = {
    "generative-llm": "#2c5f8f", "embedding": "#0f766e", "encoder": "#0e7490",
    "reranker": "#7c3aed", "qa": "#b45309", "classifier": "#a16207", "compression": "#be123c",
    "asr": "#15803d", "tts": "#4d7c0f", "speech-lm": "#0d9488", "audio-gen": "#ca8a04",
    "image-gen": "#c026d3", "world-model": "#4338ca", "?": "#7a8496",
  };
  const TYPE = { "1": "#0f766e", "bulk": "#2c5f8f", "2": "#a8620a" };
  const TYPE_LABEL = { "1": "copy #1 (RAID)", "bulk": "bulk", "2": "copy #2 (replica)" };
  const PHASE_COLOR = { download: "#2c5f8f", verify: "#7a8496", compress: "#b45309", canary: "#7c3aed", annex: "#15803d", stored: "#0f766e", store: "#0f766e" };
  const TIERS = [
    ["raid", "RAID · safe home (must-have copy #1)"],
    ["primary", "Primary · bulk (re-fetchable, 1 copy)"],
    ["replica", "Replication · must-have copy #2"],
  ];
  let mode = "type", last = null, statusTimer = null, plannedBy = {}, lastStatus = null, archivedBy = {}, usableBy = {}, pollFails = 0, queueSig = null, queueCentered = false;
  let queueModels = null, queueDrives = null, placedMap = {}, lastQueueRepo = null, placedLoaded = false;   // one-row-per-model queue state

  const hashColor = s => { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0; return `hsl(${h % 360} 42% 50%)`; };
  const color = k => mode === "type" ? (TYPE[k] || "#7a8496") : (CAT[k] || hashColor(k));
  const keyOf = m => mode === "type" ? m.copy : m.category;
  const keyLabel = k => mode === "type" ? (TYPE_LABEL[k] || k) : k;

  function ensureStyle() {
    if (document.getElementById("fill-style")) return;
    const s = document.createElement("style");
    s.id = "fill-style";
    s.textContent = `
      .fillctl{display:flex;align-items:center;gap:8px;margin:8px 0 18px}
      .fillnote{color:#5c6675;font-size:13px;margin-left:10px;font-variant-numeric:tabular-nums}
      .fillgraph{position:relative;display:flex;flex-direction:column;gap:26px;padding-left:26px}
      .tierhead{font:600 12px/1.4 ui-monospace,Menlo,monospace;letter-spacing:.1em;text-transform:uppercase;color:#5c6675;margin:0 0 10px}
      .tierrow{display:flex;flex-wrap:wrap;gap:14px}
      .drivecard{flex:1 1 258px;max-width:340px;background:#fff;border:1px solid #d5dbe4;border-radius:6px;padding:13px 14px}
      .drivecard.empty{opacity:.55;border-style:dashed}
      .dchead{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px}
      .dclabel{font:600 14px ui-monospace,Menlo,monospace}
      .dcbadge{font:600 10px/1 ui-monospace,monospace;letter-spacing:.06em;padding:3px 7px;border-radius:3px;color:#fff}
      .dcbadge.raid{background:#0f766e}.dcbadge.primary{background:#2c5f8f}.dcbadge.replica{background:#a8620a}
      .dcbar{height:22px;background:#eef1f6;border-radius:4px;overflow:hidden;border:1px solid #e0e5ec}
      .dcbarfill{display:flex;height:100%}
      .seg{height:100%;min-width:1px}
      .seg.segarch{background:#c7cfd9}
      .dcfoot{display:flex;justify-content:space-between;margin-top:8px;font:500 12px ui-monospace,monospace;color:#5c6675;font-variant-numeric:tabular-nums}
      .filllegend{display:flex;flex-wrap:wrap;gap:12px;margin:22px 0 4px;font-size:12px;color:#333}
      .lgi{display:flex;align-items:center;gap:6px}
      .lgsw{width:12px;height:12px;border-radius:3px;flex:none}
      svg.fill-links{position:absolute;inset:0;pointer-events:none;overflow:visible}
      .linkpath{fill:none;stroke:#a8620a;stroke-width:2;stroke-dasharray:5 4;opacity:.8}
      .linkdot{fill:#a8620a;opacity:.9}
      #fillAdvisories{margin-top:20px;display:flex;flex-direction:column;gap:6px}
      .fadv{font-size:14px;padding:9px 12px;border-radius:4px;border:1px solid}
      .fadv.error{background:#fbf1f0;border-color:#e7cdca;color:#a4342c}
      .fadv.warn{background:#fdf6e3;border-color:#eadfb8;color:#8a6d1a}
      .fadv.ok{background:#d8ece9;border-color:#bcdcd7;color:#0b5b54}
      .fadv.info{background:#eef1f6;border-color:#dde3ec;color:#5c6675}
      .fillloading{color:#5c6675;padding:30px;font:500 14px ui-monospace,monospace}
      .fillrun{display:flex;align-items:center;gap:12px;margin:14px 0 4px}
      #fillStop{background:#a4342c;color:#fff;border:none;border-radius:5px;padding:7px 14px;font-weight:600;cursor:pointer}
      .fillstatus{font:500 13px ui-monospace,Menlo,monospace;color:#333;font-variant-numeric:tabular-nums}
      #view-fill .page{max-width:none}
      .fillmain{display:grid;grid-template-columns:minmax(0,1fr) clamp(540px,40%,780px);gap:0;align-items:start;margin-top:8px}
      @media(max-width:1100px){.fillmain{grid-template-columns:1fr}}
      .fillcol-graph{min-width:0;padding-right:30px;border-right:1px solid var(--line2)}
      @media(max-width:1100px){.fillcol-graph{border-right:none;padding-right:0}}
      .fillcol-run{position:sticky;top:12px;padding-left:30px;display:flex;flex-direction:column;gap:16px}
      @media(max-width:1100px){.fillcol-run{padding-left:0;margin-top:22px}}
      .telpanel{background:#fff;border:1px solid #d7dde5;border-radius:11px;padding:18px 20px;box-shadow:0 1px 3px rgba(6,14,22,.35)}
      .telpanel.idle{background:#f6f8fb;border-style:dashed;color:#5c6675;font-size:14px;box-shadow:none}
      .telcap{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;gap:10px}
      .telhead{font:700 11px/1 ui-monospace,monospace;letter-spacing:.14em;text-transform:uppercase;color:#7a8592}
      .telpill{font:700 10px/1 ui-monospace,monospace;letter-spacing:.06em;text-transform:uppercase;padding:4px 9px;border-radius:5px;color:#fff;flex:none}
      .telcur{font:700 17px/1.25 ui-monospace,Menlo,monospace;color:#141b26;word-break:break-all;margin-bottom:8px}
      .telphase{display:inline-block;font:700 10px/1 ui-monospace,monospace;letter-spacing:.05em;text-transform:uppercase;padding:3px 8px;border-radius:4px;color:#fff;margin-right:8px;vertical-align:middle}
      .telfile{font:500 12.5px ui-monospace,monospace;color:#66707d;word-break:break-all}
      .telrow{margin:14px 0 0}
      .tellabel{display:flex;justify-content:space-between;font:600 11px ui-monospace,monospace;color:#66707d;margin-bottom:5px}
      .telbar{height:10px;background:#edf1f6;border-radius:5px;overflow:hidden}
      .telbarfill{height:100%;background:#2c5f8f;border-radius:5px;transition:width .4s ease}
      .telbarfill.file{background:#0f766e}
      .telstats{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;margin-top:17px;background:#e7ecf2;border:1px solid #e7ecf2;border-radius:9px;overflow:hidden}
      @media(max-width:1400px){.telstats{grid-template-columns:repeat(2,1fr)}}
      .telstat{display:flex;flex-direction:column;gap:5px;background:#fff;padding:12px 13px}
      .telstat .k{font:600 9.5px ui-monospace,monospace;letter-spacing:.07em;text-transform:uppercase;color:#93a0af}
      .telstat .v{font:700 19px ui-monospace,monospace;color:#141b26;font-variant-numeric:tabular-nums;line-height:1}
      .telstat .v.accent{color:#2c5f8f}
      .telqueue{display:flex;flex-direction:column;gap:1px;max-height:calc(100vh - 440px);min-height:190px;overflow-y:auto}
      .telq{display:flex;align-items:center;gap:9px;padding:6px 8px;border-radius:6px;font:500 12.5px ui-monospace,monospace;color:#2a333f}
      .telqdot{width:8px;height:8px;border-radius:50%;flex:none}
      .telq .qname{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .telq .qsz{color:#93a0af;flex:none;font-variant-numeric:tabular-nums;font-size:11.5px}
      .telq.done{color:#aab3bf}.telq.done .qname{text-decoration:line-through}.telq.done .telqdot{opacity:.35}
      .telq.partial{background:#fdf2e4;color:#9a6f30;box-shadow:inset 3px 0 0 #d98a2b}
      .telq.partial .qname{color:#8a6528}
      .telq.blocked{background:#fbf1f0;color:#a4342c;box-shadow:inset 3px 0 0 #a4342c}
      .telq.blocked .qname{color:#8f2d27}
      .qrem{font:700 9.5px/1 ui-monospace,monospace;letter-spacing:.02em;color:#b9701f;background:#f6e0c2;padding:3px 6px;border-radius:4px;flex:none}
      .qblock{font:700 9.5px/1 ui-monospace,monospace;letter-spacing:.02em;color:#8f2d27;background:#f1d5d2;padding:3px 6px;border-radius:4px;flex:none}
      .telq.cur{background:#e9f1f9;color:#12447a;font-weight:700;box-shadow:inset 3px 0 0 #2c5f8f}
      .telqdrive{display:flex;justify-content:space-between;align-items:baseline;gap:8px;font:700 9.5px/1 ui-monospace,monospace;letter-spacing:.09em;text-transform:uppercase;color:#8792a0;padding:10px 8px 5px;border-top:1px solid #edf1f6;margin-top:2px}
      .telqdrive:first-child{border-top:none;margin-top:0;padding-top:2px}
      .telqdrive .qsub{color:#aeb7c2;font-weight:600;letter-spacing:.02em}
      .fillprompt{font-size:14px;padding:10px 13px;border-radius:5px;margin:6px 0 10px;background:#fdf6e3;border:1px solid #eadfb8;color:#8a6d1a}
      .fillprompt button{margin-left:10px;padding:5px 11px;border:1px solid #d8c98f;border-radius:4px;background:#fff;cursor:pointer}
      .drivecard.active{border-color:#2c5f8f;box-shadow:0 0 0 2px #2c5f8f33}
      .dcdone{margin-top:7px;display:flex;align-items:center;gap:7px;font:500 11px ui-monospace,monospace;color:#0b5b54}
      .dcdonebar{flex:1;height:5px;background:#eef1f6;border-radius:3px;overflow:hidden}
      .dcdonefill{height:100%;background:#0f766e;transition:width .4s ease}
      .planbars{background:#fff;border:1px solid #d7dde5;border-radius:11px;padding:15px 20px;margin:8px 0 14px;box-shadow:0 1px 3px rgba(6,14,22,.15)}
      .pbhead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:11px;gap:12px;flex-wrap:wrap}
      .pbtitle{font:700 11px ui-monospace,monospace;letter-spacing:.11em;text-transform:uppercase;color:#5c6675}
      .pbcap{font:600 12.5px ui-monospace,monospace;color:#141b26;font-variant-numeric:tabular-nums}
      .pbrow{margin:9px 0}
      .pblabel{display:flex;justify-content:space-between;font:600 11.5px ui-monospace,monospace;color:#66707d;margin-bottom:4px;font-variant-numeric:tabular-nums}
      .pbtrack{height:16px;background:#eef1f6;border:1px solid #e0e5ec;border-radius:5px;overflow:hidden;position:relative}
      .pbfill{height:100%;border-radius:4px;transition:width .4s ease}
      .pbfill.unc{background:#2c5f8f}.pbfill.comp{background:#0f766e}.pbfill.over{background:#c0392b}
      .pbnote{margin-top:9px;font:500 12px ui-monospace,monospace;color:#5c6675}
    `;
    document.head.appendChild(s);
  }

  // "archived" = durable bytes on the drive (+ live session delta), filling toward its usable space.
  const doneRowHTML = (archived, usable) => {
    const pct = usable ? Math.min(100, 100 * archived / usable) : 0;
    return `<div class="dcdonebar"><div class="dcdonefill" style="width:${pct.toFixed(1)}%"></div></div><span>${MA.gb(archived)} archived</span>`;
  };

  function driveCard(d) {
    const groups = {};
    for (const m of d.models) { const k = keyOf(m); groups[k] = (groups[k] || 0) + m.size; }
    const usable = d.usable || 1;
    const archived = d.archived_bytes || 0;
    // True fill = what's ALREADY archived (grey base) + what's PLANNED (category segments). The old
    // fill_pct counted only new-planned bytes, so a near-full drive (lots archived) read ~half-empty.
    const archSeg = archived > 0
      ? `<div class="seg segarch" style="width:${(100 * archived / usable).toFixed(2)}%" title="already archived: ${MA.gb(archived)}"></div>`
      : "";
    const segs = Object.entries(groups).sort((a, b) => b[1] - a[1]).map(([k, b]) =>
      `<div class="seg" style="width:${(100 * b / usable).toFixed(2)}%;background:${color(k)}" title="${esc(keyLabel(k))} (planned): ${esc(MA.gb(b))}"></div>`).join("");
    const badge = { raid: "RAID", primary: "PRIMARY", replica: "REPLICA" }[d.tier] || d.tier;
    const total = archived + (d.planned_bytes || 0);
    const totalPct = usable ? Math.min(100, Math.round(100 * total / usable)) : 0;
    return `<div class="drivecard${(d.n_models || archived) ? "" : " empty"}" id="dc-${esc(d.label)}">
      <div class="dchead"><span class="dclabel">${esc(d.label)}</span><span class="dcbadge ${esc(d.tier)}">${esc(badge)}</span></div>
      <div class="dcbar"><div class="dcbarfill">${archSeg}${segs}</div></div>
      <div class="dcfoot"><span>${totalPct}% · ${MA.gb(total)}/${MA.gb(usable)}</span><span>${d.n_models} planned</span></div>
      <div class="dcdone" id="done-${esc(d.label)}"${archived ? "" : " hidden"}>${archived ? doneRowHTML(archived, usable) : ""}</div>
    </div>`;
  }

  function legend(data) {
    const keys = new Set();
    data.drives.forEach(d => d.models.forEach(m => keys.add(keyOf(m))));
    return `<div class="filllegend">` + [...keys].sort().map(k =>
      `<span class="lgi"><span class="lgsw" style="background:${color(k)}"></span>${esc(keyLabel(k))}</span>`).join("") + `</div>`;
  }

  // Route each copy#1→copy#2 link down the left gutter (orthogonal), so it never crosses the
  // drive cards in the rows between the source and target.
  function drawLinks(data) {
    const graph = document.getElementById("fillGraph");
    const old = graph.querySelector("svg.fill-links"); if (old) old.remove();
    if (!data.links || !data.links.length) return;
    const NS = "http://www.w3.org/2000/svg";
    const gr = graph.getBoundingClientRect();
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "fill-links");
    let i = 0, drew = false;
    for (const ln of data.links) {
      const a = document.getElementById("dc-" + ln.from), b = document.getElementById("dc-" + ln.to);
      if (!a || !b) continue;
      const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
      const y1 = ar.top + ar.height / 2 - gr.top, y2 = br.top + br.height / 2 - gr.top;
      const sx = ar.left - gr.left, tx = br.left - gr.left;   // card left edges
      const gx = 13 - (i % 3) * 4;                            // vertical bus in the left gutter (offset per link)
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", `M${sx},${y1} H${gx} V${y2} H${tx}`);
      p.setAttribute("class", "linkpath");
      svg.appendChild(p);
      const dot = document.createElementNS(NS, "circle");
      dot.setAttribute("cx", tx); dot.setAttribute("cy", y2); dot.setAttribute("r", "3");
      dot.setAttribute("class", "linkdot");
      svg.appendChild(dot);
      drew = true; i++;
    }
    if (drew) graph.appendChild(svg);
  }

  function render(data) {
    last = data;
    ensureStyle();
    plannedBy = {}; archivedBy = {}; usableBy = {};
    data.drives.forEach(d => {
      plannedBy[d.label] = d.planned_bytes;
      archivedBy[d.label] = d.archived_bytes || 0;   // durable: what's actually on the drive (survives restarts)
      usableBy[d.label] = d.usable || 1;
    });
    const graph = document.getElementById("fillGraph");
    let html = "";
    for (const [tier, label] of TIERS) {
      const ds = data.drives.filter(d => d.tier === tier);
      if (!ds.length) continue;
      html += `<div class="tiergroup"><div class="tierhead">${label}</div><div class="tierrow">${ds.map(driveCard).join("")}</div></div>`;
    }
    graph.innerHTML = html + legend(data);
    requestAnimationFrame(() => drawLinks(data));
    document.getElementById("fillAdvisories").innerHTML =
      (data.advisories || []).map(a => `<div class="fadv ${esc(a.level)}">${esc(a.msg)}</div>`).join("");
    const t = data.totals || {};
    document.getElementById("fillNote").textContent =
      `${t.n_planned} to place · ${t.n_must} must-have · ${t.n_bulk} bulk` +
      (t.n_blocked ? ` · ${t.n_blocked} blocked` : "") + (t.n_done ? ` · ${t.n_done} done` : "");
    const start = document.getElementById("fillStart");
    if (start) {
      start.disabled = data.feasible === false;
      start.title = data.feasible === false
        ? `Plan admission blocked: ${(data.blocking_diagnostics || []).join(", ")}` : "";
    }
    if (lastStatus) renderRun(lastStatus);   // re-apply live overlays/telemetry after the cards are rebuilt
  }

  // ---- run surface: start / stop / live status (task #22) ----
  const shortFile = f => (f && f.length > 34) ? "…" + f.slice(-33) : (f || "");
  const TERMINAL = MA.fillTerminals;
  let announcedTerminal = null;

  function announceTerminal(s) {
    if (!s || !MA.isFillTerminal(s.status) || ["done", "stopped"].includes(s.status)) return;
    const signature = `${s.status}|${s.code || ""}|${s.message || ""}`;
    if (signature === announcedTerminal) return;
    announcedTerminal = signature;
    if (MA.showFillTerminal) MA.showFillTerminal(s);
  }

  function statusLine(s) {
    if (!s || s.status === "idle") return "idle — not running";
    if (s.status === "running") {
      const bits = [];
      if (s.phase) bits.push(s.phase);
      if (s.drive) bits.push(s.drive + (s.n_drives ? ` (${s.drive_index}/${s.n_drives})` : ""));
      if (s.repo) bits.push(s.repo + (s.n_repos ? ` [${s.repo_index || "?"}/${s.n_repos}]` : ""));
      if (s.file) bits.push((s.file_phase || "") + " " + shortFile(s.file));
      return "▶ " + bits.join(" · ");
    }
    return (TERMINAL[s.status] || s.status) + (s.message ? " — " + s.message : "");
  }

  function setRunUI(s) {
    const running = !!(s && s.running);
    const start = document.getElementById("fillStart"), stop = document.getElementById("fillStop");
    if (start) start.hidden = running;
    if (stop) stop.hidden = !running;
    const st = document.getElementById("fillStatus");
    if (st) st.textContent = statusLine(s);
  }

  function renderRun(s) { lastStatus = s; setRunUI(s); renderTelemetry(s); renderQueue(s); maybeRefreshQueueState(s); renderCards(s); renderPrompt(s); }

  // Build the queue as ONE row per model, from queue_view (structure: size, numcopies, copy#1/#2
  // drives) + live placed counts (queue_state). Per model: state = done (all copies placed) |
  // partial (some, ≥1 left) | upcoming (none) | current (fetching now); positioned under the drive
  // of its NEXT unfinished copy — done rows settle at their copy#1 home. Ordered by drive
  // (RAID → primary by capacity → replica), then size desc. Nothing vanishes: finished models stay,
  // struck-through, so you can always see what's complete — not just what's left.
  const TIER_RANK = { raid: 0, primary: 1, replica: 2 };
  function buildQueueRows() {
    if (!queueModels || !queueModels.length) return null;
    if (!placedLoaded) return null;                       // don't paint a false "all upcoming" before done-state lands
    const curRepo = lastStatus && lastStatus.repo;
    const items = queueModels.map(m => {
      const p = placedMap[m.repo] || 0, N = m.numcopies || 1;
      const blockers = m.blocking_diagnostics || [];
      let state = blockers.length ? "blocked" : (p >= N ? "done" : (p > 0 ? "partial" : "upcoming"));
      const drive = state === "blocked" ? "blocked" : ((state === "partial" ? (m.copy2 || m.copy1) : m.copy1) || "?");
      if (m.repo === curRepo) state = "current";
      return { repo: m.repo, size: m.size || 0, category: m.category, numcopies: N,
               remaining: Math.max(0, N - p), state, drive, blockers };
    });
    const order = [...(queueDrives || [])].sort((a, b) =>
      (TIER_RANK[a.tier] - TIER_RANK[b.tier]) || (b.capacity - a.capacity) || String(a.label).localeCompare(b.label))
      .map(d => d.label);
    const rank = {}; order.forEach((d, i) => rank[d] = i);
    const rk = d => (d in rank ? rank[d] : 99);
    items.sort((a, b) => (rk(a.drive) - rk(b.drive)) || (b.size - a.size) || a.repo.localeCompare(b.repo));
    return items;
  }

  function renderTelemetry(s) {
    const el = document.getElementById("fillTelemetry");
    if (!el) return;
    if (!s || s.status === "idle" || (s.status !== "running" && !s.session_bytes)) {
      const t = (last && last.totals) || {};
      el.innerHTML = `<div class="telpanel idle"><div class="telhead" style="margin-bottom:9px">Run</div>` +
        (s && s.status in TERMINAL ? esc(statusLine(s)) : "Not running. Press <b>Start fill</b> to begin.") +
        (t.n_planned != null ? `<div style="margin-top:11px;font:500 13px ui-monospace,monospace">${esc(t.n_planned)} to place · ${esc(t.n_must)} must-have · ${esc(t.n_bulk)} bulk</div>` : "") +
        `</div>`;
      return;
    }
    const repoPct = s.n_repos ? 100 * (s.repo_index || 0) / s.n_repos : 0;
    const shardPct = s.n_shards ? 100 * (s.shard_no || 0) / s.n_shards : 0;   // true shard progress (safetensors only)
    const rate = s.rate_bps ? (s.rate_bps / 1e6).toFixed(0) + " MB/s" : "—";
    // net_rx_bps is a LIVE, system-wide NIC gauge resampled every poll — only meaningful while this
    // fill is actually downloading. In verify/compress/annex nothing's being fetched, so show "—"
    // instead of ambient network noise (or a near-zero) under a "download" label.
    const downloading = (s.file_phase || "").toLowerCase().startsWith("download");
    const dlrate = (downloading && s.net_rx_bps != null) ? (s.net_rx_bps / 1e6).toFixed(0) + " MB/s" : "—";
    const saved = (s.ratio != null) ? Math.round((1 - s.ratio) * 100) + "%" : "—";   // ratio = stored/orig → saved = 1−ratio
    const fetched = s.session_bytes ? MA.gb(s.session_bytes) : "—";
    const phase = (s.file_phase || "").toLowerCase();

    let html = `<div class="telpanel">`;
    html += `<div class="telcap"><span class="telhead">Now fetching</span>${s.drive ? `<span class="telpill" style="background:#2c5f8f">${esc(s.drive)}</span>` : ""}</div>`;
    html += `<div class="telcur">${esc(s.repo || "—")}</div>`;
    if (s.file) html += `<div><span class="telphase" style="background:${PHASE_COLOR[phase] || "#7a8496"}">${esc(phase || "…")}</span><span class="telfile">${esc(shortFile(s.file))}</span></div>`;
    if (s.n_repos) html += `<div class="telrow"><div class="tellabel"><span>model</span><span>${esc(s.repo_index || 0)} / ${esc(s.n_repos)}</span></div><div class="telbar"><div class="telbarfill" style="width:${repoPct.toFixed(1)}%"></div></div></div>`;
    if (s.shard_no) html += `<div class="telrow"><div class="tellabel"><span>shard</span><span>${esc(s.shard_no)} / ${esc(s.n_shards)}</span></div><div class="telbar"><div class="telbarfill file" style="width:${shardPct.toFixed(1)}%"></div></div></div>`;
    html += `<div class="telstats">`
      + `<div class="telstat"><span class="k">download · live</span><span class="v accent">${dlrate}</span></div>`
      + `<div class="telstat"><span class="k">avg · effective</span><span class="v">${rate}</span></div>`
      + `<div class="telstat"><span class="k">ZipNN saved</span><span class="v">${saved}</span></div>`
      + `<div class="telstat"><span class="k">fetched</span><span class="v">${fetched}</span></div>`
      + `</div>`;
    if (s.cap_24h) {
      const capPct = Math.min(100, 100 * (s.used_24h || 0) / s.cap_24h);
      html += `<div class="telrow" style="margin-top:17px"><div class="tellabel"><span>24h download cap</span><span>${MA.gb(s.used_24h || 0)} / ${MA.gb(s.cap_24h)}</span></div>`
        + `<div class="telbar"><div class="telbarfill" style="width:${capPct.toFixed(1)}%;background:${capPct >= 100 ? "#c0392b" : "#c9a24b"}"></div></div></div>`;
    }
    html += `</div>`;   // /telpanel (now fetching)
    el.innerHTML = html;
  }

  // The whole-fleet queue lives in its OWN element and is rebuilt ONLY when its content changes
  // (plan reshaped or the current model advanced) — never on the 1.5s status poll. That is the fix
  // for the list "popping back" to the current row and never rendering past it: between changes the
  // DOM and your scroll are left untouched. It auto-centres on the current model once (first open),
  // then hands scrolling to you; a later rebuild preserves where you had scrolled to.
  function renderQueue(s) {
    const host = document.getElementById("fillQueue");
    if (!host) return;
    const items = buildQueueRows();
    if (!items) {
      host.innerHTML = (queueModels && !placedLoaded) ? '<div class="telpanel idle">syncing queue…</div>' : "";
      queueSig = null;
      return;
    }
    const doneN = items.filter(i => i.state === "done").length;
    const partN = items.filter(i => i.state === "partial").length;
    const blockedN = items.filter(i => i.state === "blocked").length;
    const curRepo = (lastStatus && lastStatus.repo) || "";
    const sig = `${items.length}|${doneN}|${partN}|${blockedN}|${curRepo}`;   // rebuild only when this changes, not per poll
    if (sig === queueSig) return;
    const wasBuilt = queueSig !== null;
    const prevEl = document.getElementById("telQueue");
    const prevScroll = prevEl ? prevEl.scrollTop : 0;

    const dstat = {};                                     // per-drive subtotal: rows parked there + their bytes
    for (const it of items) { const d = dstat[it.drive] || (dstat[it.drive] = { n: 0, b: 0 }); d.n++; d.b += it.size; }
    let prevDrive = null;
    const rows = items.map(it => {
      let div = "";
      if (it.drive !== prevDrive) {
        prevDrive = it.drive;
        const st = dstat[it.drive];
        div = `<div class="telqdrive"><span>${esc(it.drive)}</span><span class="qsub">${esc(st.n)} · ${esc(MA.gb(st.b))}</span></div>`;
      }
      const cls = { done: "done", partial: "partial", blocked: "blocked", current: "cur" }[it.state] || "";
      const rem = it.state === "partial"
        ? `<span class="qrem">${it.remaining} cop${it.remaining === 1 ? "y" : "ies"} left</span>` : "";
      const block = it.state === "blocked"
        ? `<span class="qblock">${esc(it.blockers.join(", "))}</span>` : "";
      return div + `<div class="telq ${cls}"><span class="telqdot" style="background:${CAT[it.category] || hashColor(it.category || "?")}"></span><span class="qname">${esc(it.repo)}</span>${rem}${block}<span class="qsz">${esc(MA.gb(it.size))}</span></div>`;
    }).join("");
    const left = items.filter(it => it.state !== "blocked").reduce((a, it) => a + it.remaining * it.size, 0);
    const head = `${doneN} / ${items.length} done · ${MA.gb(left)} schedulable left` +
      (blockedN ? ` · ${blockedN} blocked` : "");
    host.innerHTML = `<div class="telpanel"><div class="telcap"><span class="telhead">Queue · whole fleet</span><span class="telhead" style="letter-spacing:.04em">${head}</span></div><div class="telqueue" id="telQueue">${rows}</div></div>`;
    queueSig = sig;

    const q = document.getElementById("telQueue");
    if (!q) return;
    if (!queueCentered) {                                 // centre on the current (or first unfinished) ONCE, on open
      const target = q.querySelector(".telq.cur") || q.querySelector(".telq:not(.done):not(.partial)");
      if (target) q.scrollTop = Math.max(0, target.offsetTop - q.offsetTop - 70);
      queueCentered = true;
    } else if (wasBuilt) {                                // a later rebuild (state changed) → keep the operator's scroll
      q.scrollTop = prevScroll;
    }
  }

  // Refresh the cheap per-model completion counts when the fill crosses a model boundary (repo
  // changes) — that's when a copy just finished. Avoids re-running the heavy plan every poll.
  function maybeRefreshQueueState(s) {
    const repo = (s && s.repo) || null;
    if (repo === lastQueueRepo) return;
    lastQueueRepo = repo;
    refreshPlanBars();                                    // #36: the footprint shrinks/lands per model
    if (!queueModels) return;                             // structure not loaded yet; loadFill will pull state
    MA.api("/api/library/queue-state").then(st => {
      if (st && !st.error) { placedMap = st; placedLoaded = true; renderQueue(lastStatus); }
    }).catch(() => {});
  }

  function renderCards(s) {
    const done = (s && s.done_by_drive) || {};    // live delta THIS session, added on top of the durable base
    Object.keys(archivedBy).forEach(label => {
      const row = document.getElementById("done-" + label);
      if (!row) return;
      const total = (archivedBy[label] || 0) + (done[label] || 0);
      if (!total) { row.hidden = true; row.innerHTML = ""; return; }
      row.hidden = false;
      row.innerHTML = doneRowHTML(total, usableBy[label] || 1);
    });
    document.querySelectorAll(".drivecard").forEach(c => c.classList.remove("active"));
    if (s && s.drive) { const c = document.getElementById("dc-" + s.drive); if (c) c.classList.add("active"); }
  }

  function renderPrompt(s) {
    const el = document.getElementById("fillPrompt");
    if (!el) return;
    if (s && s.status === "running" && s.awaiting_drive) {
      el.hidden = false;
      el.innerHTML = `⏳ Insert drive <b>${esc(s.awaiting_drive)}</b> — the fill continues automatically once it mounts. ` +
        `<button id="fillConfirm">I inserted it</button>`;
      const b = document.getElementById("fillConfirm");
      if (b) b.onclick = () => MA.post("/api/fill/confirm-drive", { label: s.awaiting_drive })
        .then(r => MA.toast(r.mounted ? r.label + " is mounted — continuing" : r.label + " not detected yet"));
    } else { el.hidden = true; el.innerHTML = ""; }
  }

  function showLost() {   // portal poll failed repeatedly → it likely stopped/crashed; don't freeze on the last frame
    const st = document.getElementById("fillStatus");
    if (st) st.textContent = "⚠ lost connection to the portal — it may have stopped or crashed. Restart it, then Start.";
    const start = document.getElementById("fillStart"), stop = document.getElementById("fillStop");
    if (start) start.hidden = false;
    if (stop) stop.hidden = true;
    const prompt = document.getElementById("fillPrompt");
    if (prompt) { prompt.hidden = true; prompt.innerHTML = ""; }
  }

  function poll() {
    MA.api("/api/fill/status").then(s => {
      pollFails = 0;
      renderRun(s);
      if (s && s.status === "running") { statusTimer = setTimeout(poll, 1500); return; }
      statusTimer = null;
      if (s && s.status in TERMINAL) {
        announceTerminal(s);
        MA.toast(statusLine(s)); window.loadFill();
      }   // plan shrinks as copies land
    }).catch(() => {
      if (++pollFails >= 2) showLost();       // say "portal gone" instead of silently freezing on the last frame
      statusTimer = setTimeout(poll, 3000);   // keep trying so the view recovers if the portal comes back
    });
  }

  function refreshStatus(resumePolling) {
    MA.api("/api/fill/status").then(s => {
      pollFails = 0;
      renderRun(s);
      announceTerminal(s);
      if (resumePolling && s && s.status === "running" && !statusTimer) poll();
    }).catch(() => { if (++pollFails >= 2) showLost(); if (!statusTimer) poll(); });   // detect a down portal + keep checking
  }

  function startFill() {
    MA.post("/api/fill/start", {}).then(r => {
      if (r && r.ok) { MA.toast("fill started"); if (!statusTimer) poll(); }
      else MA.toast((r && r.error) || "could not start the fill");
    });
  }
  function stopFill() { MA.post("/api/fill/stop", {}).then(() => MA.toast("stopping after the current file…")); }

  // #36: the two always-on capacity bars — fully-UNCOMPRESSED (the boundary currency) + fully-COMPRESSED
  // (what lands on disk), both vs the plan's capacity line. The gap = the compression dividend in drive
  // terms. Refreshed on open + at every model boundary (plan.totals is live).
  function renderPlanBars(t) {
    const el = document.getElementById("planBars");
    if (!el) return;
    if (!t || t.error) { el.innerHTML = ""; return; }
    const cap = t.capacity || 1;
    const bar = (label, bytes, cls) => {
      const ratio = bytes / cap, pct = Math.min(100, 100 * ratio), over = bytes > cap;
      const tail = over ? `${(100 * ratio).toFixed(0)}% — over by ${MA.gb(bytes - cap)}` : `${(100 * ratio).toFixed(0)}%`;
      return `<div class="pbrow"><div class="pblabel"><span>${label}</span><span>${MA.gb(bytes)} / ${MA.gb(cap)} · ${tail}</span></div>` +
        `<div class="pbtrack"><div class="pbfill ${cls}${over ? " over" : ""}" style="width:${pct.toFixed(1)}%"></div></div></div>`;
    };
    const dividend = Math.max(0, t.uncompressed - t.compressed);
    el.innerHTML =
      `<div class="pbhead"><span class="pbtitle">Plan '${esc(t.plan_id)}' · ${esc(t.capacity_mode)} capacity</span>` +
      `<span class="pbcap">${esc(t.n_selection)} finalized · capacity ${esc(MA.gb(cap))} · ${esc(t.n_drives)} drive${t.n_drives === 1 ? "" : "s"}</span></div>` +
      bar("Raw forecast (conservative)", t.uncompressed, "unc") +
      bar("Expected stored forecast", t.compressed, "comp") +
      `<div class="pbnote">compression dividend ≈ ${MA.gb(dividend)} — the drive space compression is expected to reclaim` +
      (t.over_uncompressed
        ? `</div><div class="fadv warn" style="margin-top:9px">⚠ Raw forecast exceeds capacity — this plan depends on compression savings. Add a drive, trim the set, or explicitly choose compression_aware capacity.</div>`
        : "</div>");
  }
  function refreshPlanBars() { MA.api("/api/plan/totals").then(renderPlanBars).catch(() => {}); }

  window.loadFill = function () {
    refreshPlanBars();
    queueSig = null; queueCentered = false; lastQueueRepo = null;   // fresh load → rebuild + re-centre the queue once
    const graph = document.getElementById("fillGraph");
    graph.innerHTML = '<div class="fillloading">planning…</div>';
    document.getElementById("fillAdvisories").innerHTML = "";
    MA.api("/api/library/plan").then(d => {
      if (!d || d.error) { graph.innerHTML = '<div class="fillloading">error: ' + esc((d && d.error) || "no data") + '</div>'; return; }
      render(d);
    }).catch(e => { graph.innerHTML = '<div class="fillloading">error: ' + esc((e && e.message) || e) + '</div>'; });
    // one-row-per-model queue: pull the heavy structure once, then the cheap live per-model state
    MA.api("/api/library/queue").then(d => {
      if (d && !d.error) { queueModels = d.models; queueDrives = d.drives; }
      return MA.api("/api/library/queue-state");
    }).then(st => {
      if (st && !st.error) { placedMap = st; placedLoaded = true; }
      renderQueue(lastStatus);
    }).catch(() => {});
    refreshStatus(true);        // reflect any in-flight fill and resume polling if one is running
  };

  function wire() {
    const st = document.getElementById("stackType"), sc = document.getElementById("stackCat");
    if (!st || !sc) return;
    st.onclick = () => { mode = "type"; st.classList.add("on"); sc.classList.remove("on"); if (last) render(last); };
    sc.onclick = () => { mode = "category"; sc.classList.add("on"); st.classList.remove("on"); if (last) render(last); };
    const fstart = document.getElementById("fillStart"), fstop = document.getElementById("fillStop");
    if (fstart) fstart.onclick = startFill;
    if (fstop) fstop.onclick = stopFill;
    window.addEventListener("resize", () => { if (last) drawLinks(last); });
  }
  if (document.readyState !== "loading") wire(); else document.addEventListener("DOMContentLoaded", wire);
})();
