// DEF-036 — operator-facing review and approval of one immutable placement proposal.
// The browser presents backend-authored evidence only; it never constructs planning authority.
(function () {
  const byId = id => document.getElementById(id);
  let status = null;
  let review = null;
  let planFeasible = false;
  let fillRunning = false;
  let requestPending = false;
  let successorOptions = null;

  function refusalText(result) {
    if (!result) return "The proposal request failed.";
    const bits = [result.code || result.error || "PROPOSAL_REFUSED"];
    if (result.actions && result.actions.length) bits.push(`next: ${result.actions.join(" · ")}`);
    return bits.join(" — ");
  }

  function setStartGate() {
    const start = byId("fillStart");
    if (!start) return;
    const approved = !!(status && status.state === "approved_current");
    start.disabled = !planFeasible || !approved;
    if (!planFeasible) start.title = "Plan admission is not feasible; resolve the displayed blockers.";
    else if (!approved) start.title = "Review and approve the exact placement before starting Fill.";
    else start.title = "";
  }

  function renderDocket() {
    const seal = byId("proposalSeal");
    const state = byId("proposalState");
    const button = byId("proposalReview");
    const successor = byId("proposalSuccessor");
    if (!seal || !state || !button || !successor) return;

    const kind = status && status.state;
    seal.textContent = kind === "approved_current" ? "Ready to fill" : "Placement approval";
    if (requestPending) {
      state.textContent = "Preparing exact placement…";
      button.textContent = "Preparing…";
      button.disabled = true;
    } else if (kind === "approved_current") {
      const active = status.active_proposal || {};
      state.textContent = `Revision ${status.planner_revision} approved · ${active.proposal_id || "current proposal"}`;
      button.textContent = "Placement approved";
      button.disabled = true;
    } else if (kind === "approved_stale") {
      state.textContent = "Approval is stale · review the current placement again";
      button.textContent = "Review current placement";
      button.disabled = !planFeasible || fillRunning;
    } else if (kind === "missing") {
      state.textContent = planFeasible
        ? "Approval required · review the exact assignment before Fill"
        : "Approval unavailable · resolve the plan blockers first";
      button.textContent = "Review exact placement";
      button.disabled = !planFeasible || fillRunning;
    } else if (status && status.refused) {
      state.textContent = refusalText(status);
      button.textContent = "Review exact placement";
      button.disabled = !planFeasible || fillRunning;
    } else {
      state.textContent = "Checking approval…";
      button.textContent = "Review exact placement";
      button.disabled = true;
    }
    successor.hidden = kind !== "approved_current";
    successor.disabled = requestPending || fillRunning || !planFeasible ||
      kind !== "approved_current";
    setStartGate();
  }

  function applyStatus(next) {
    status = next || null;
    renderDocket();
    return status;
  }

  function refresh() {
    return window.MA.api("/api/proposal/status")
      .then(applyStatus)
      .catch(error => applyStatus({
        refused: true,
        code: "PROPOSAL_STATUS_UNAVAILABLE",
        error: String((error && error.message) || error || "status unavailable"),
      }));
  }

  function clearChildren(element) {
    while (element && element.firstChild) element.removeChild(element.firstChild);
  }

  function addFact(host, label, value) {
    const item = document.createElement("div");
    const key = document.createElement("span");
    const val = document.createElement("b");
    key.textContent = label;
    val.textContent = value;
    item.append(key, val);
    host.appendChild(item);
  }

  function renderTotals(value) {
    const host = byId("proposalTotals");
    clearChildren(host);
    const totals = value || {};
    addFact(host, "Requirements", String(totals.requirements || 0));
    addFact(host, "Executable", String(totals.executable || 0));
    addFact(host, "Already satisfied", String(totals.baseline_satisfied || 0));
    addFact(host, "Repositories", String(totals.repositories || 0));
    addFact(host, "Guaranteed", window.MA.gb(totals.guaranteed_bytes || 0));
    addFact(host, "Expected", window.MA.gb(totals.expected_bytes || 0));
  }

  function renderDrives(drives) {
    const host = byId("proposalDrives");
    clearChildren(host);
    for (const drive of drives || []) {
      const card = document.createElement("div");
      card.className = "proposal-drive";
      const head = document.createElement("b");
      const detail = document.createElement("span");
      head.textContent = drive.drive_label || "unknown drive";
      detail.textContent = `${drive.requirements || 0} requirements · ${drive.executable || 0} executable · ${window.MA.gb(drive.guaranteed_bytes || 0)} guaranteed`;
      card.append(head, detail);
      host.appendChild(card);
    }
  }

  function assignmentCell(row, value, className) {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = value == null || value === "" ? "—" : String(value);
    row.appendChild(cell);
  }

  function renderAssignments(assignments) {
    const host = byId("proposalAssignments");
    clearChildren(host);
    for (const assignment of assignments || []) {
      const row = document.createElement("tr");
      row.dataset.requirementId = String(assignment.requirement_id || "");
      row.dataset.filterText = [
        assignment.requirement_id, assignment.repo_id, assignment.target_drive,
        assignment.source_drive, assignment.previous_target, assignment.row_kind,
      ].filter(Boolean).join(" ").toLowerCase();
      assignmentCell(row, assignment.row_kind);
      assignmentCell(row, assignment.repo_id, "proposal-repo");
      const target = assignment.target_changed
        ? `${assignment.previous_target} → ${assignment.target_drive}`
        : assignment.target_drive;
      assignmentCell(row, target);
      assignmentCell(row, assignment.source_drive);
      assignmentCell(row, window.MA.gb(assignment.guaranteed_durable || 0), "num");
      assignmentCell(row, assignment.identity_epoch, "num");
      host.appendChild(row);
    }
    byId("proposalAssignmentCount").textContent = `${(assignments || []).length} rows`;
  }

  function showReview(nextReview) {
    review = nextReview;
    byId("proposalGate").textContent = review.gate_b_code || "UNKNOWN";
    byId("proposalPlan").textContent = review.plan_id || "";
    byId("proposalRevision").textContent = String(review.based_on_revision ?? "");
    byId("proposalCapacityMode").textContent = review.capacity_mode || "";
    byId("proposalDerivation").textContent = review.derivation_mode || "";
    byId("proposalCanonical").textContent = review.canonical_hash || "";
    byId("proposalPhrase").textContent = review.confirmation_phrase || "";
    if (review.successor) {
      const value = review.successor;
      const lane = value.lane_bytes == null ? "its recorded" : window.MA.gb(value.lane_bytes);
      byId("proposalMessage").textContent =
        `${value.successor_drive} inherits ${lane} planning place from ` +
        `${value.predecessor_drive}. ${value.moved_to_successor} requirements ` +
        `move to the replacement; ${value.unchanged_requirements} stay put. ` +
        "No archive bytes move in this dialog.";
    } else {
      byId("proposalMessage").textContent =
        "Review every assignment before approval. No archive bytes move in this dialog.";
    }
    byId("proposalRefusal").textContent = "";
    byId("proposalConfirm").value = "";
    byId("proposalApprove").disabled = true;
    byId("proposalConfirmation").hidden = !review.approvable;
    renderTotals(review.totals);
    renderDrives(review.drives);
    renderAssignments(review.assignments);
    byId("proposalModal").hidden = false;
    byId("proposalAssignmentFilter").value = "";
    byId("proposalConfirm").focus();
  }

  function showRefusal(result) {
    const text = refusalText(result);
    const refusal = byId("proposalRefusal");
    if (refusal && !byId("proposalModal").hidden) refusal.textContent = text;
    else window.MA.toast(text);
  }

  function openReview() {
    if (requestPending || fillRunning || !planFeasible) return;
    requestPending = true;
    renderDocket();
    window.MA.post("/api/proposal/draft", {}).then(result => {
      if (!result || result.ok !== true || !result.review) {
        applyStatus(result || {refused: true, code: "PROPOSAL_DRAFT_FAILED"});
        showRefusal(result);
        return;
      }
      showReview(result.review);
    }).catch(error => {
      const result = {refused: true, code: "PROPOSAL_DRAFT_UNAVAILABLE", error: String(error)};
      applyStatus(result);
      showRefusal(result);
    }).finally(() => {
      requestPending = false;
      renderDocket();
    });
  }

  function closeReview() {
    byId("proposalModal").hidden = true;
    byId("proposalConfirm").value = "";
    byId("proposalRefusal").textContent = "";
  }

  function clearSelect(select) {
    while (select.firstChild) select.removeChild(select.firstChild);
  }

  function driveOption(item) {
    const option = document.createElement("option");
    option.value = item.drive_label;
    option.textContent = `${item.drive_label} · ${window.MA.gb(item.capacity_bytes || 0)} · ` +
      `${item.assigned_requirements || 0} current requirements`;
    option.dataset.role = item.role || "";
    option.dataset.capacity = String(item.capacity_bytes || 0);
    option.dataset.assigned = String(item.assigned_requirements || 0);
    return option;
  }

  function updateSuccessorChoices() {
    if (!successorOptions) return;
    const predecessor = byId("successorPredecessor");
    const successor = byId("successorDrive");
    const old = (successorOptions.predecessors || []).find(
      item => item.drive_label === predecessor.value
    );
    const prior = successor.value;
    clearSelect(successor);
    const choices = (successorOptions.successors || [])
      .filter(item => !old || item.role === old.role)
      .sort((a, b) =>
        (a.assigned_requirements - b.assigned_requirements) ||
        (b.capacity_bytes - a.capacity_bytes) ||
        a.drive_label.localeCompare(b.drive_label)
      );
    choices.forEach(item => successor.appendChild(driveOption(item)));
    if (choices.some(item => item.drive_label === prior)) successor.value = prior;
    const selected = choices.find(item => item.drive_label === successor.value);
    byId("successorNote").textContent = old && selected
      ? `${selected.drive_label} will inherit a lane bounded by ${old.drive_label}'s ` +
        `safe capacity. Existing content reuse still wins; remaining assignments stay where ` +
        "they are when feasible."
      : "No identity-proven replacement with a matching role is available.";
    byId("successorCreate").disabled = !old || !selected || requestPending;
  }

  function showSuccessorOptions(result) {
    successorOptions = result;
    const predecessor = byId("successorPredecessor");
    clearSelect(predecessor);
    const predecessors = result.predecessors || [];
    predecessors.forEach(item => predecessor.appendChild(driveOption(item)));
    const drive2 = predecessors.find(item => item.drive_label === "drive-02");
    if (drive2) predecessor.value = drive2.drive_label;
    byId("successorRefusal").textContent = "";
    byId("successorModal").hidden = false;
    updateSuccessorChoices();
    predecessor.focus();
  }

  function openSuccessor() {
    if (requestPending || fillRunning || !planFeasible ||
        !status || status.state !== "approved_current") return;
    requestPending = true;
    renderDocket();
    window.MA.api("/api/proposal/successor-options").then(result => {
      if (!result || result.ok !== true) {
        showRefusal(result);
        return;
      }
      showSuccessorOptions(result);
    }).catch(error => {
      showRefusal({code: "SUCCESSOR_OPTIONS_UNAVAILABLE", error: String(error)});
    }).finally(() => {
      requestPending = false;
      renderDocket();
      updateSuccessorChoices();
    });
  }

  function closeSuccessor() {
    byId("successorModal").hidden = true;
    byId("successorRefusal").textContent = "";
  }

  function createSuccessor() {
    if (requestPending) return;
    const predecessor_drive = byId("successorPredecessor").value;
    const successor_drive = byId("successorDrive").value;
    if (!predecessor_drive || !successor_drive) return;
    requestPending = true;
    byId("successorCreate").disabled = true;
    byId("successorRefusal").textContent = "";
    window.MA.post("/api/proposal/draft", {
      mode: "successor",
      predecessor_drive,
      successor_drive,
    }).then(result => {
      if (!result || result.ok !== true || !result.review) {
        byId("successorRefusal").textContent = refusalText(result);
        return;
      }
      closeSuccessor();
      showReview(result.review);
    }).catch(error => {
      byId("successorRefusal").textContent =
        refusalText({code: "SUCCESSOR_DRAFT_UNAVAILABLE", error: String(error)});
    }).finally(() => {
      requestPending = false;
      renderDocket();
      updateSuccessorChoices();
    });
  }

  function approve() {
    if (requestPending || !review || !review.approvable) return;
    const confirmation = byId("proposalConfirm").value;
    if (confirmation !== review.confirmation_phrase) return;
    requestPending = true;
    byId("proposalApprove").disabled = true;
    byId("proposalRefusal").textContent = "";
    window.MA.post("/api/proposal/approve", {
      proposal_id: review.proposal_id,
      confirmation,
    }).then(result => {
      if (!result || result.ok !== true || result.state !== "approved_current") {
        showRefusal(result);
        return refresh();
      }
      applyStatus(result);
      closeReview();
      window.MA.toast("exact placement approved — Fill remains stopped");
    }).catch(error => {
      showRefusal({code: "PROPOSAL_APPROVAL_UNAVAILABLE", error: String(error)});
    }).finally(() => {
      requestPending = false;
      renderDocket();
      const confirm = byId("proposalConfirm");
      if (confirm && review) {
        byId("proposalApprove").disabled = confirm.value !== review.confirmation_phrase;
      }
    });
  }

  function filterAssignments() {
    const query = byId("proposalAssignmentFilter").value.trim().toLowerCase();
    const rows = byId("proposalAssignments").querySelectorAll("tr");
    rows.forEach(row => { row.hidden = !!query && !row.dataset.filterText.includes(query); });
  }

  function setPlanState(plan) {
    if (plan && typeof plan.feasible === "boolean") planFeasible = plan.feasible;
    else if (plan && plan.gate_b_code) planFeasible = plan.gate_b_code === "FEASIBLE";
    renderDocket();
  }

  function resetPlan() {
    planFeasible = false;
    renderDocket();
  }

  function setRunning(running) {
    fillRunning = !!running;
    renderDocket();
  }

  function wire() {
    const button = byId("proposalReview");
    if (!button) return;
    button.onclick = openReview;
    byId("proposalSuccessor").onclick = openSuccessor;
    byId("proposalCancel").onclick = closeReview;
    byId("proposalApprove").onclick = approve;
    byId("proposalConfirm").addEventListener("input", event => {
      byId("proposalApprove").disabled = requestPending || !review ||
        event.target.value !== review.confirmation_phrase;
    });
    byId("proposalAssignmentFilter").addEventListener("input", filterAssignments);
    byId("successorCancel").onclick = closeSuccessor;
    byId("successorCreate").onclick = createSuccessor;
    byId("successorPredecessor").addEventListener("change", updateSuccessorChoices);
    byId("successorDrive").addEventListener("change", updateSuccessorChoices);
    window.MA.proposal = {refresh, resetPlan, setPlanState, setRunning};
    renderDocket();
  }

  if (document.readyState !== "loading") wire();
  else document.addEventListener("DOMContentLoaded", wire);
})();
