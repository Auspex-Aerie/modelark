// Drive lifecycle + health view. Passive inventory is advisory; SMART and catalog mutation each
// require a separate explicit operator action.
(function () {
  const {api, post, esc, gb, toast} = window.MA;
  const $ = id => document.getElementById(id);
  let lossPreview = null;
  let onboardingPreview = null;

  const OBS = {
    attached_exact_serial: ["attached", "ok", "Exact registered serial observed."],
    not_attached: ["not attached", "watch", "Not observed now. This means offline or missing only."],
    ambiguous_serial: ["ambiguous", "watch", "More than one attached device reports this serial."],
    identity_unproven: ["identity unproven", "unknown", "No stored serial can prove attachment."],
    inventory_unavailable: ["inventory unavailable", "unknown", "Attached inventory could not be read."],
  };

  function registeredCard(item, index) {
    const obs = OBS[item.observation] || [item.observation || "unknown", "unknown", ""];
    const lifecycle = item.lifecycle + " · " + item.eligibility;
    const plans = (item.plans || []).map(p => p.plan_id + (p.is_active ? " (active)" : "")).join(", ") || "none";
    const device = item.device
      ? `<br>observed ${esc(item.device.dev || "device")} · ${esc(item.device.model || "unknown model")} · ${esc(item.device.size || "?")}`
      : "";
    const review = item.lifecycle === "active" && item.observation === "not_attached"
      ? `<div class="driveacts"><button class="driveproblem" data-drive-index="${index}">Uh oh — review this drive</button></div>`
      : "";
    return `<div class="drive ${esc(obs[1])}">
      <span class="pill ${esc(obs[1])}">${esc(obs[0])}</span>
      <h3>${esc(item.drive_label)}</h3>
      <div class="sub">${esc(item.hw_model || "registered drive")} · ${esc(item.capacity_bytes ? gb(item.capacity_bytes) : "capacity unknown")}<br>
        ${esc(lifecycle)} · identity epoch ${esc(item.identity_epoch)} · plan ${esc(plans)}${device}</div>
      <div class="attrs">
        <div class="k">Observation</div><div class="v">${esc(obs[2])}</div>
        <div class="k">Stored serial</div><div class="v">${esc(item.serial || "unrecorded")}</div>
      </div>${review}
    </div>`;
  }

  function unregisteredCard(item, index) {
    return `<div class="drive unknown">
      <span class="pill unknown">unregistered</span>
      <h3>${esc(item.dev || "attached device")}</h3>
      <div class="sub">${esc(item.model || "unknown model")} · ${esc(item.size || "?")} · ${esc(item.bus || "?")}<br>
        serial ${esc(item.serial || "unreported")}</div>
      <div class="disknote">No action taken. This hardware has no ModelArk label, role, plan
        membership, capacity authority, or inherited residency history.</div>
      <div class="driveacts"><button class="driveonboard" data-drive-index="${index}">Review onboarding</button></div>
    </div>`;
  }

  async function loadInventory() {
    const body = $("driveBody");
    const note = $("driveNote");
    body.innerHTML = '<div class="stub">Reading passive attached inventory…</div>';
    let result;
    try { result = await api("/api/drives"); }
    catch (e) { body.innerHTML = '<div class="stub">Could not read drive inventory.</div>'; return; }
    if (!result || !result.ok) {
      body.innerHTML = '<div class="stub">Drive inventory is unavailable.</div>';
      return;
    }
    note.textContent = result.message + " Planner revision " + result.planner_revision + ".";
    const registered = result.registered || [];
    const unregistered = result.unregistered || [];
    body.innerHTML = registered.map(registeredCard).join("") + unregistered.map(unregisteredCard).join("");
    if (!registered.length && !unregistered.length) {
      body.innerHTML = '<div class="stub">No registered or attached drives reported.</div>';
    }
    body.querySelectorAll(".driveproblem").forEach(button => {
      button.onclick = () => openLoss(registered[Number(button.dataset.driveIndex)]);
    });
    body.querySelectorAll(".driveonboard").forEach(button => {
      button.onclick = () => openOnboarding(unregistered[Number(button.dataset.driveIndex)]);
    });
  }

  async function openOnboarding(item) {
    let result;
    try {
      result = await api("/api/drive/onboarding-preview?dev=" + encodeURIComponent(item.dev) +
        "&serial=" + encodeURIComponent(item.serial || ""));
    } catch (e) {
      toast("could not prepare onboarding preview");
      return;
    }
    if (!result || !result.ok) {
      toast(result?.refused?.code || "onboarding preview refused");
      return;
    }
    onboardingPreview = result.preview;
    const volume = onboardingPreview.volume;
    const lost = onboardingPreview.separate_lost_identities || [];
    $("driveOnboardingHead").textContent = "Review onboarding — " + onboardingPreview.suggested_label;
    $("driveOnboardingMsg").textContent =
      "Read-only preview. No SMART, formatting, initialization, registration, plan change, or reconciliation has run.";
    $("driveOnboardingIdentity").textContent =
      "observed " + (item.dev || "device") + " · " + (item.model || "unknown model") +
      " · serial " + (item.serial || "unreported") + " · proposed new label " +
      onboardingPreview.suggested_label;
    $("driveOnboardingVolume").textContent = volume
      ? "filesystem " + volume.dev + " · " + volume.fstype + " · UUID " +
        (volume.fs_uuid || "unproven") + " · " + (volume.mounted ? "mounted" : "not mounted") +
        " · archive namespace " + (volume.archive_state || "unproven") +
        (volume.archive_path ? " at " + volume.archive_path : "") +
        (volume.mounted ? " · registration parent " +
          (volume.archive_parent_writable === true ? "writable" :
            volume.archive_parent_writable === false ? "not writable" : "write access unproven") : "")
      : "No single registration filesystem was identified.";
    const registration = onboardingPreview.registration_preview || {};
    $("driveOnboardingPlan").textContent =
      "new identity role " + (registration.role || "unproven") + " · active plan " +
      (registration.adds_to_active_plan || "none") + " · reconciliation " +
      (registration.requires_reconcile_after_registration ? "required after registration" : "not required");
    const remediation = onboardingPreview.permission_remediation;
    const remediationBox = $("driveOnboardingRemediation");
    remediationBox.hidden = !remediation;
    $("driveOnboardingRemediationSummary").textContent = remediation
      ? "This dedicated filesystem root is not writable by the ModelArk service account. Run these exact commands outside ModelArk, then refresh this preview."
      : "";
    $("driveOnboardingCommands").textContent = remediation
      ? (remediation.commands || []).map(command => command.display).join("\n") : "";
    $("driveOnboardingGuardrails").textContent = remediation
      ? (remediation.guardrails || []).join(" ") : "";
    const layout = registration.directory_layout;
    $("driveOnboardingDirectory").hidden = !layout;
    $("driveOnboardingLayout").textContent = layout?.display || "";
    $("driveOnboardingHistory").textContent = lost.length
      ? "Separate lost history (never inherited): " + lost.map(old =>
          old.drive_label + " epoch " + old.identity_epoch + " · " + old.archived_rows +
          " archived rows · " + old.replica_rows + " replica rows").join("; ")
      : "No lost identity is being replaced or inherited by this preview.";
    const actions = {
      mount_volume: "Next operator gate: mount " + (volume?.dev || "the filesystem") +
        ", then refresh this preview.",
      review_registration: "Filesystem is mounted and the archive namespace is empty. The separately confirmed registration action is available below.",
      refuse_system_device: "Refused: this device backs the running system.",
      select_active_plan: "Select one active plan before registering a new drive.",
      choose_volume: "More than one filesystem exists. Choose the intended volume outside ModelArk first.",
      review_filesystem: "No supported existing filesystem is ready. Formatting remains a separate destructive workflow.",
      establish_filesystem_identity: "The filesystem UUID is unproven; do not register it.",
      review_archive_namespace: "The modelark path is already occupied and was not recognized as a safe fresh target.",
      review_existing_annex: "An existing git-annex identity is present. Use the recovery/re-registration workflow; do not treat it as fresh media.",
      review_prepared_registration: "A prepared registration receipt does not match this review. Stop and inspect it; do not adopt it automatically.",
      prepare_archive_permissions: "Registration is blocked: ModelArk cannot create its staging and archive directories at the mounted filesystem root. Prepare dedicated ownership or an ACL outside ModelArk, then refresh; ModelArk will not use sudo or loosen permissions automatically.",
      prove_archive_permissions: "Registration is blocked because write access to the mounted filesystem root could not be proven. Inspect the mount and permissions outside ModelArk, then refresh.",
      refresh_topology: "Topology could not be proven; refresh attached inventory.",
    };
    $("driveOnboardingNext").textContent = actions[onboardingPreview.next_action] ||
      "Preview is blocked; refresh and review the evidence.";
    const mayRegister = Boolean(onboardingPreview.ready_for_registration);
    $("driveOnboardingRegistration").hidden = !mayRegister;
    $("driveOnboardingApply").hidden = !mayRegister;
    $("driveOnboardingPhrase").textContent = onboardingPreview.confirmation || "";
    $("driveOnboardingConfirm").value = "";
    $("driveOnboardingRefusal").textContent = "";
    $("driveOnboardingApply").disabled = true;
    $("driveOnboardingModal").hidden = false;
    (mayRegister ? $("driveOnboardingConfirm") : $("driveOnboardingClose")).focus();
  }

  function closeOnboarding() {
    $("driveOnboardingModal").hidden = true;
    onboardingPreview = null;
  }

  async function applyOnboarding() {
    if (!onboardingPreview || !onboardingPreview.ready_for_registration) return;
    const button = $("driveOnboardingApply");
    button.disabled = true;
    let result;
    try {
      result = await post("/api/drive/register-new", {
        ...onboardingPreview.registration_binding,
        confirmation: $("driveOnboardingConfirm").value,
      });
    } catch (e) {
      $("driveOnboardingRefusal").textContent =
        "Network outcome unknown. Refresh attached inventory before retrying; an exact completed registration will be shown as registered, while an interrupted preparation remains separately reviewable.";
      return;
    }
    if (!result || !result.ok) {
      const refusal = result?.refused || {};
      $("driveOnboardingRefusal").textContent = (refusal.code || "registration refused") +
        (refusal.evidence ? " · " + JSON.stringify(refusal.evidence) : "");
      button.disabled = refusal.code === "DRIVE_REGISTRATION_CONFIRMATION_MISMATCH"
        ? $("driveOnboardingConfirm").value !== onboardingPreview.confirmation : true;
      return;
    }
    const registration = result.registration;
    const event = $("driveEvent");
    event.hidden = false;
    event.className = "driveevent blocked";
    event.innerHTML = `<b>${esc(registration.drive_label)} is registered as a new identity at revision ${esc(registration.planner_revision)}.</b>` +
      `<div class="sub">joined plan ${esc(registration.plan_id)} · capacity evidence remains unknown · reconciliation not run · inherited lost-drive facts 0</div>`;
    closeOnboarding();
    toast("new drive identity registered; reconciliation still required");
    await loadInventory();
    if (window.loadPlans) window.loadPlans();
  }

  async function openLoss(item) {
    let result;
    try {
      result = await api("/api/drive/loss-preview?drive_label=" + encodeURIComponent(item.drive_label));
    } catch (e) {
      toast("could not prepare drive review");
      return;
    }
    if (!result || !result.ok) {
      toast(result?.refused?.code || "could not prepare drive review");
      return;
    }
    lossPreview = result.preview;
    $("driveLossHead").textContent = "Uh oh — " + lossPreview.drive_label + " is not attached";
    $("driveLossMsg").textContent = "Review the old registered identity. The attached observation has not changed its state.";
    $("driveLossEvidence").textContent =
      "revision " + lossPreview.planner_revision + " · identity epoch " + lossPreview.identity_epoch +
      " · fingerprint " + (lossPreview.identity_fingerprint || "unproven") +
      " · " + lossPreview.archived_rows + " archived rows across " + lossPreview.archived_repositories +
      " repositories · " + lossPreview.replica_rows + " replica rows";
    $("driveLossWarning").textContent = lossPreview.warning;
    $("driveLossPhrase").textContent = lossPreview.confirmation;
    $("driveLossConfirm").value = "";
    $("driveLossRefusal").textContent = "";
    $("driveLossApply").disabled = true;
    $("driveLossModal").hidden = false;
    $("driveLossConfirm").focus();
  }

  function closeLoss() {
    $("driveLossModal").hidden = true;
    lossPreview = null;
  }

  async function applyLoss() {
    if (!lossPreview) return;
    const button = $("driveLossApply");
    button.disabled = true;
    let result;
    try {
      result = await post("/api/drive/declare-lost", {
        drive_label: lossPreview.drive_label,
        expected_revision: lossPreview.planner_revision,
        expected_identity_epoch: lossPreview.identity_epoch,
        expected_identity_fingerprint: lossPreview.identity_fingerprint,
        confirmation: $("driveLossConfirm").value,
      });
    } catch (e) {
      $("driveLossRefusal").textContent =
        "Network outcome unknown. Refresh attached inventory and review the catalog state before retrying.";
      return;
    }
    if (!result || !result.ok) {
      const refusal = result?.refused || {};
      $("driveLossRefusal").textContent = (refusal.code || "transition refused") +
        (refusal.evidence ? " · " + JSON.stringify(refusal.evidence) : "");
      button.disabled = refusal.code === "DRIVE_LOSS_CONFIRMATION_MISMATCH"
        ? $("driveLossConfirm").value !== lossPreview.confirmation : true;
      return;
    }
    const transition = result.transition;
    const after = result.after || {};
    const replan = after.replan;
    const replanRevision = replan?.planner_revision ?? transition.planner_revision;
    const event = $("driveEvent");
    event.hidden = false;
    event.className = "driveevent" + (replan && !replan.feasible ? " blocked" : "");
    event.innerHTML = `<b>${esc(transition.drive_label)} is now lost + excluded; replanned at revision ${esc(replanRevision)}.</b>` +
      (replan
        ? `<div class="sub">canonical result: ${esc(replan.root_code)} · ${esc(replan.executable_tasks)} executable tasks · lost-drive targets 0 · active capacity ${esc(after.totals ? gb(after.totals.capacity) : "unknown")}</div>`
        : `<div class="sub">Lifecycle transition committed, but the replan summary failed: ${esc(after.replan_error || "unknown error")}</div>`);
    closeLoss();
    toast("drive excluded and plan recalculated");
    await loadInventory();
    if (window.loadPlans) window.loadPlans();
  }

  async function runSmartChecks() {
    const body = $("diskBody");
    const note = $("diskNote");
    body.innerHTML = '<div class="stub">Reading SMART…</div>';
    let d;
    try { d = await api("/api/disk"); }
    catch (e) { body.innerHTML = '<div class="stub">Could not read disks.</div>'; return; }

    if (d.tool_missing) {
      note.innerHTML = '<span class="disknote">smartmontools isn\'t installed — run <b>sudo apt-get install -y smartmontools</b>, then retry.</span>';
      body.innerHTML = '<div class="stub">Install smartmontools to read drive SMART data.</div>';
      return;
    }
    if (d.platform_unsupported) {
      note.innerHTML = '<span class="disknote">' + esc(d.os || "This OS") + ' drives aren\'t health-checked in-system.</span>';
      body.innerHTML = '<div class="stub">' + esc(d.message || "Use the platform health tools.") + '</div>';
      return;
    }
    note.innerHTML = d.needs_privilege
      ? '<span class="disknote">SMART needs root — grant passwordless sudo for <b>smartctl</b>; do not run the portal as root.</span>'
      : "SMART status for attached drives.";
    if (!d.drives.length) { body.innerHTML = '<div class="stub">No physical disks detected.</div>'; return; }

    const cell = (k, v, cls) => v == null || v === "" ? "" :
      `<div class="k">${esc(k)}</div><div class="v ${esc(cls || '')}">${esc(v)}</div>`;
    body.innerHTML = d.drives.map(x => {
      const st = x.status || "unknown";
      const drv = x.dtype ? ` · <span title="smartctl driver">-d ${esc(x.dtype)}</span>` : "";
      return `<div class="drive ${esc(st)}">
        <span class="pill ${esc(st)}">${esc(st)}</span><h3>${esc(x.dev)}</h3>
        <div class="sub">${esc(x.model)} · ${esc(x.size || "?")} · ${esc(x.bus || "?")}${x.spinning ? " · spinning" : " · ssd"}${drv}<br>SN ${esc(x.serial)}</div>
        ${x.note ? `<div class="sub" style="color:var(--warn)">${esc(x.note)}</div>${x.quirk_cmd ? `<code class="fixcmd">${esc(x.quirk_cmd)}</code>` : ""}` : `<div class="attrs">
          ${x.smart_passed == null ? "" : cell("SMART overall", x.smart_passed ? "PASSED" : "FAILED", x.smart_passed ? "" : "bad")}
          ${cell("Power-on hours", x.power_on_hours)}${cell("Temp °C", x.temp_c)}
          ${cell("Reallocated", x.reallocated, x.reallocated >= 100 ? "bad" : x.reallocated > 0 ? "warn" : "")}
          ${cell("Pending sectors", x.pending, x.pending > 0 ? "bad" : "")}
          ${cell("Offline uncorrectable", x.offline_uncorrectable, x.offline_uncorrectable > 0 ? "bad" : "")}
          ${cell("UDMA CRC errors", x.crc_errors, x.crc_errors > 0 ? "warn" : "")}
          ${cell("Endurance used", x.percentage_used != null ? x.percentage_used + "%" : null, x.percentage_used >= 85 ? "warn" : "")}
          ${cell("Available spare", x.available_spare != null ? x.available_spare + "%" : null, x.available_spare != null && x.available_spare < 20 ? "bad" : "")}
          ${cell("Unsafe shutdowns", x.unsafe_shutdowns)}${cell("Media errors (NVMe)", x.media_errors, x.media_errors > 0 ? "bad" : "")}
        </div>`}
      </div>`;
    }).join("");
  }

  window.loadDisk = loadInventory;
  function wire() {
    $("refreshDriveInventory").onclick = loadInventory;
    $("runHealthChecks").onclick = runSmartChecks;
    $("driveLossCancel").onclick = closeLoss;
    $("driveLossConfirm").oninput = () => {
      $("driveLossApply").disabled = !lossPreview || $("driveLossConfirm").value !== lossPreview.confirmation;
    };
    $("driveLossApply").onclick = applyLoss;
    $("driveOnboardingClose").onclick = closeOnboarding;
    $("driveOnboardingConfirm").oninput = () => {
      $("driveOnboardingApply").disabled = !onboardingPreview ||
        $("driveOnboardingConfirm").value !== onboardingPreview.confirmation;
    };
    $("driveOnboardingApply").onclick = applyOnboarding;
  }
  if (document.readyState !== "loading") wire(); else document.addEventListener("DOMContentLoaded", wire);
})();
