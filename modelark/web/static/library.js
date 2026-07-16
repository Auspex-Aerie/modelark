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
  const searchEl = document.getElementById("libSearch");
  const filtersEl = document.getElementById("libDriveFilters");
  const shownEl = document.getElementById("libShown");
  fleet.innerHTML = '<div class="stub">Reading library…</div>'; totals.innerHTML = ""; bodyEl.innerHTML = "";
  filtersEl.innerHTML = ""; shownEl.textContent = ""; searchEl.value = "";

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
    return `<button type="button" class="drive libdrive ${esc(h)}" data-drive="${esc(x.label)}" aria-pressed="false" title="Filter archived models by ${esc(x.label)}">
      <span class="pill ${esc(h)}">${esc(h)}</span>
      <h3>${esc(x.label)}</h3>
      <div class="sub">${esc(x.model || "—")}${x.serial ? " · SN " + esc(x.serial) : ""}${x.location ? " · " + esc(x.location) : ""}</div>
      <div class="attrs">
        <div class="k">Models</div><div class="v">${esc(x.n_models)}</div>
        <div class="k">Files</div><div class="v">${esc(x.n_files)}</div>
        <div class="k">On disk</div><div class="v">${esc(sz(x.on_disk))}</div>
        <div class="k">Free</div><div class="v">${esc(sz(x.free))} / ${esc(sz(x.capacity))}</div>
      </div>
    </button>`;
  }).join("") || '<div class="stub">No drives registered yet.</div>';

  if (!d.models.length) {
    bodyEl.innerHTML = '<div class="stub">Nothing archived yet — finalize a set and run <b>modelark fetch</b>.</div>';
    return;
  }
  const selectedDrives = new Set();

  const render = () => {
    const query = searchEl.value.trim().toLowerCase();
    const models = d.models.filter(m => {
      const matchesRepo = !query || String(m.repo_id || "").toLowerCase().includes(query);
      const matchesDrive = !selectedDrives.size || (m.drives || []).some(label => selectedDrives.has(label));
      return matchesRepo && matchesDrive;
    });
    shownEl.textContent = `${models.length} of ${d.models.length} models`;
    filtersEl.innerHTML = '<span class="flabel">drives</span>' + d.fleet.map(x =>
      `<button type="button" class="chip toggle${selectedDrives.has(x.label) ? " on" : ""}" data-drive="${esc(x.label)}">${esc(x.label)} · ${esc(x.n_models)}</button>`
    ).join("");
    fleet.querySelectorAll(".libdrive").forEach(card => {
      const on = selectedDrives.has(card.dataset.drive);
      card.classList.toggle("on", on);
      card.setAttribute("aria-pressed", String(on));
    });
    filtersEl.querySelectorAll("button[data-drive]").forEach(button => {
      button.onclick = () => toggleDrive(button.dataset.drive);
    });
    if (!models.length) {
      bodyEl.innerHTML = '<div class="stub">No archived models match these filters.</div>';
      return;
    }
    const rows = models.map(m => `<tr data-repo="${esc(m.repo_id)}">
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

  const toggleDrive = label => {
    if (selectedDrives.has(label)) selectedDrives.delete(label); else selectedDrives.add(label);
    render();
  };
  fleet.querySelectorAll(".libdrive").forEach(card => {
    card.onclick = () => toggleDrive(card.dataset.drive);
  });
  searchEl.oninput = render;
  render();
};
