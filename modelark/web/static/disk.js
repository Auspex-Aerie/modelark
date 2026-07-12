// Disk Health view: render SMART status per attached drive.
window.loadDisk = async function () {
  const {api} = window.MA;
  const body = document.getElementById("diskBody");
  const note = document.getElementById("diskNote");
  body.innerHTML = '<div class="stub">Reading SMART…</div>';
  let d;
  try { d = await api("/api/disk"); }
  catch (e) { body.innerHTML = '<div class="stub">Could not read disks.</div>'; return; }

  if (d.tool_missing) {
    note.innerHTML = '<span class="disknote">smartmontools isn\'t installed — run <b>sudo apt-get install -y smartmontools</b>, then reopen this page.</span>';
    body.innerHTML = '<div class="stub">Install smartmontools to read drive SMART data.</div>';
    return;
  }
  if (d.platform_unsupported) {
    note.innerHTML = '<span class="disknote">' + (d.os || "This OS") + ' drives aren\'t health-checked in-system.</span>';
    body.innerHTML = '<div class="stub">' + (d.message || "Run your platform's preferred health tracking against the drive first before use.") + '</div>';
    return;
  }
  note.innerHTML = d.needs_privilege
    ? '<span class="disknote">SMART needs root — grant passwordless sudo for <b>smartctl</b> (see README Setup) to read drive health; do not run the portal as root.</span>'
    : "SMART status for attached drives — vet your library volumes before they hold archive data.";

  if (!d.drives.length) { body.innerHTML = '<div class="stub">No physical disks detected.</div>'; return; }

  const cell = (k, v, cls) => v == null || v === "" ? "" :
    `<div class="k">${k}</div><div class="v ${cls || ''}">${v}</div>`;
  body.innerHTML = d.drives.map(x => {
    const st = x.status || "unknown";
    const realloc = cell("Reallocated", x.reallocated, x.reallocated >= 100 ? "bad" : x.reallocated > 0 ? "warn" : "");
    const pend = cell("Pending sectors", x.pending, x.pending > 0 ? "bad" : "");
    const off = cell("Offline uncorrectable", x.offline_uncorrectable, x.offline_uncorrectable > 0 ? "bad" : "");
    const crc = cell("UDMA CRC errors", x.crc_errors, x.crc_errors > 0 ? "warn" : "");
    const media = cell("Media errors (NVMe)", x.media_errors, x.media_errors > 0 ? "bad" : "");
    const wear = cell("Endurance used", x.percentage_used != null ? x.percentage_used + "%" : null,
      x.percentage_used >= 85 ? "warn" : "");
    const spare = cell("Available spare", x.available_spare != null ? x.available_spare + "%" : null,
      x.available_spare != null && x.available_spare < 20 ? "bad" : "");
    const unsafe = cell("Unsafe shutdowns", x.unsafe_shutdowns);
    const smart = x.smart_passed == null ? "" :
      cell("SMART overall", x.smart_passed ? "PASSED" : "FAILED", x.smart_passed ? "" : "bad");
    const drv = x.dtype ? ` · <span title="smartctl driver">-d ${x.dtype}</span>` : "";
    return `<div class="drive ${st}">
      <span class="pill ${st}">${st}</span>
      <h3>${x.dev}</h3>
      <div class="sub">${x.model} · ${x.size || '?'} · ${x.bus || '?'}${x.spinning ? ' · spinning' : ' · ssd'}${drv}<br>SN ${x.serial}</div>
      ${x.note ? `<div class="sub" style="color:var(--warn)">${x.note}</div>${x.quirk_cmd ? `<code class="fixcmd" title="click to select">${x.quirk_cmd}</code>` : ''}` : `
      <div class="attrs">
        ${smart}
        ${cell("Power-on hours", x.power_on_hours)}
        ${cell("Temp °C", x.temp_c)}
        ${realloc}${pend}${off}${crc}${wear}${spare}${unsafe}${media}
      </div>`}
    </div>`;
  }).join("");
};
