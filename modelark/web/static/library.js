// Library view: what's archived, on which drive — from the SQLite mirror (DEF-006).
window.loadLibrary = async function () {
  const {api} = window.MA;
  const sz = b => b == null ? "—"
    : b >= 1e12 ? (b / 1e12).toFixed(2) + " TB"
    : b >= 1e9  ? (b / 1e9).toFixed(1) + " GB"
    : b >= 1e6  ? (b / 1e6).toFixed(0) + " MB"
    : (b / 1e3).toFixed(0) + " KB";
  const pctSaved = (raw, disk) => raw ? Math.round(100 * (raw - disk) / raw) : 0;
  const totals = document.getElementById("libTotals");
  const fleet = document.getElementById("libFleet");
  const bodyEl = document.getElementById("libBody");
  fleet.innerHTML = '<div class="stub">Reading library…</div>'; totals.innerHTML = ""; bodyEl.innerHTML = "";

  let d;
  try { d = await api("/api/library"); }
  catch (e) { fleet.innerHTML = '<div class="stub">Could not read the library.</div>'; return; }
  if (d.error) { fleet.innerHTML = '<div class="stub">' + d.error + '</div>'; return; }

  const t = d.totals;
  totals.innerHTML =
    `<b>${t.n_models}</b> model${t.n_models === 1 ? "" : "s"} · <b>${t.n_files}</b> files · `
    + `${sz(t.raw)} → <b>${sz(t.on_disk)}</b> on disk (${pctSaved(t.raw, t.on_disk)}% saved) · `
    + `footprint ${sz(t.physical)} across ${t.n_drives} drive${t.n_drives === 1 ? "" : "s"} · `
    + `fleet ${sz(t.capacity - t.free)} / ${sz(t.capacity)} used`;

  fleet.innerHTML = d.fleet.map(x => {
    const h = x.health || "unknown";
    return `<div class="drive ${h}">
      <span class="pill ${h}">${h}</span>
      <h3>${x.label}</h3>
      <div class="sub">${x.model || "—"}${x.serial ? " · SN " + x.serial : ""}${x.location ? " · " + x.location : ""}</div>
      <div class="attrs">
        <div class="k">Models</div><div class="v">${x.n_models}</div>
        <div class="k">Files</div><div class="v">${x.n_files}</div>
        <div class="k">On disk</div><div class="v">${sz(x.on_disk)}</div>
        <div class="k">Free</div><div class="v">${sz(x.free)} / ${sz(x.capacity)}</div>
      </div>
    </div>`;
  }).join("") || '<div class="stub">No drives registered yet.</div>';

  if (!d.models.length) {
    bodyEl.innerHTML = '<div class="stub">Nothing archived yet — finalize a set and run <b>modelark fetch</b>.</div>';
    return;
  }
  const rows = d.models.map(m => `<tr>
      <td>${m.repo_id}</td>
      <td>${m.category || "—"}</td>
      <td class="num">${m.n_files}</td>
      <td class="num">${sz(m.raw)}</td>
      <td class="num">${sz(m.on_disk)}${m.n_compressed ? ` (${pctSaved(m.raw, m.on_disk)}%)` : ""}</td>
      <td>${(m.drives || []).join(", ")}</td>
      <td class="num">${m.min_copies || 1}</td>
      <td>${m.verified_at ? String(m.verified_at).slice(0, 10) : "—"}</td>
    </tr>`).join("");
  bodyEl.innerHTML = `<table><thead><tr>
      <th>Repo</th><th>Category</th><th class="num">Files</th>
      <th class="num">Raw</th><th class="num">On&nbsp;disk</th><th>Drive(s)</th>
      <th class="num">Copies</th><th>Verified</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
};
