// Library view: what's archived, on which drive — from the SQLite mirror (DEF-006).
window.loadLibrary = async function () {
  const {api, esc} = window.MA;
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
  if (d.error) { fleet.innerHTML = '<div class="stub">' + esc(d.error) + '</div>'; return; }

  const t = d.totals;
  totals.innerHTML =
    `<b>${esc(t.n_models)}</b> model${t.n_models === 1 ? "" : "s"} · <b>${esc(t.n_files)}</b> files · `
    + `${esc(sz(t.raw))} → <b>${esc(sz(t.on_disk))}</b> on disk (${esc(pctSaved(t.raw, t.on_disk))}% saved) · `
    + `footprint ${esc(sz(t.physical))} across ${esc(t.n_drives)} drive${t.n_drives === 1 ? "" : "s"} · `
    + `fleet ${esc(sz(t.capacity - t.free))} / ${esc(sz(t.capacity))} used`;

  fleet.innerHTML = d.fleet.map(x => {
    const h = x.health || "unknown";
    return `<div class="drive ${esc(h)}">
      <span class="pill ${esc(h)}">${esc(h)}</span>
      <h3>${esc(x.label)}</h3>
      <div class="sub">${esc(x.model || "—")}${x.serial ? " · SN " + esc(x.serial) : ""}${x.location ? " · " + esc(x.location) : ""}</div>
      <div class="attrs">
        <div class="k">Models</div><div class="v">${esc(x.n_models)}</div>
        <div class="k">Files</div><div class="v">${esc(x.n_files)}</div>
        <div class="k">On disk</div><div class="v">${esc(sz(x.on_disk))}</div>
        <div class="k">Free</div><div class="v">${esc(sz(x.free))} / ${esc(sz(x.capacity))}</div>
      </div>
    </div>`;
  }).join("") || '<div class="stub">No drives registered yet.</div>';

  if (!d.models.length) {
    bodyEl.innerHTML = '<div class="stub">Nothing archived yet — finalize a set and run <b>modelark fetch</b>.</div>';
    return;
  }
  const rows = d.models.map(m => `<tr>
      <td>${esc(m.repo_id)}</td>
      <td>${esc(m.category || "—")}</td>
      <td class="num">${esc(m.n_files)}</td>
      <td class="num">${esc(sz(m.raw))}</td>
      <td class="num">${esc(sz(m.on_disk))}${m.n_compressed ? ` (${esc(pctSaved(m.raw, m.on_disk))}%)` : ""}</td>
      <td>${esc((m.drives || []).join(", "))}</td>
      <td class="num">${esc(m.min_copies || 1)}</td>
      <td>${esc(m.verified_at ? String(m.verified_at).slice(0, 10) : "—")}</td>
    </tr>`).join("");
  bodyEl.innerHTML = `<table><thead><tr>
      <th>Repo</th><th>Category</th><th class="num">Files</th>
      <th class="num">Raw</th><th class="num">On&nbsp;disk</th><th>Drive(s)</th>
      <th class="num">Copies</th><th>Verified</th>
    </tr></thead><tbody>${rows}</tbody></table>`;
};
