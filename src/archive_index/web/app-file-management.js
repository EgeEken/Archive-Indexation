let fileManagement = {profiles: [], rulesets: [], presets: [], current: null, draft: [], dirty: false, planSession: null, planToken: 0, planTimer: null, plan: null, execution: null, executionTimer: null};

const assetSelectors = [["all", "All"], ["selected", "Selected"], ["undecided", "Undecided"], ["rejected", "Rejected"]];
const representationSelectors = [["all", "All"], ["raw", "RAW"], ["conventional-image", "JPEG/PNG"], ["jpeg", "JPEG"], ["png", "PNG"], ["jxl", "JXL"], ["avif", "AVIF"], ["webp", "WebP"], ["video", "Video"]];
const operationSelectors = [["copy", "Copy"], ["move", "Move"], ["compress", "Compress"], ["delete", "Delete"]];

function fileManagementOptionList(values, selected) { return values.map(([value, label]) => `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`).join(""); }
function fileManagementStatus(message = "", error = false) { const node = $("file-management-status"); if (!node) return; node.textContent = message; node.classList.toggle("error", error); node.classList.toggle("hidden", !message); }
function fileManagementMutationActive() { return ["running", "cancelling"].includes(fileManagement.execution?.status); }
function renderFileManagementLockState() {
  const locked = fileManagementMutationActive();
  document.querySelectorAll("#file-management-rules-section input, #file-management-rules-section select, #file-management-rules-section button, #file-management-profiles-section input, #file-management-profiles-section select, #file-management-profiles-section button").forEach(control => { control.disabled = locked; });
  document.querySelectorAll("[data-file-management-lock-note]").forEach(node => node.classList.toggle("hidden", !locked));
}
function clearDraftReview() {
  if (fileManagement.execution?.status === "draft") {
    const id = fileManagement.execution.id;
    fileManagement.execution = null;
    fileManagement.plan = null;
    stopExecutionPolling();
    if (id) void api(`/api/file-management/executions/${encodeURIComponent(id)}/discard`, {method: "POST", headers: {"Content-Type": "application/json"}}).catch(() => {});
    if ($("file-management-execution-panel")) { $("file-management-execution-panel").innerHTML = ""; $("file-management-execution-panel").classList.add("hidden"); }
  }
}
function renderPlanProgress(session) {
  const panel = $("file-management-plan-progress");
  panel.classList.toggle("hidden", !session);
  if (!session) return;
  $("file-management-plan-progress").querySelector("[data-plan-phase]").textContent = session.phase || "Preparing plan";
  const progress = panel.querySelector("[data-plan-progress]");
  progress.max = Math.max(1, Number(session.total || 0));
  progress.value = Math.min(progress.max, Number(session.completed || 0));
  const count = session.total ? `${Number(session.completed || 0).toLocaleString()} / ${Number(session.total).toLocaleString()} files` : "Preparing indexed files";
  panel.querySelector("[data-plan-progress-detail]").textContent = `${count} · ${Number(session.elapsed_seconds || 0).toFixed(1)} s elapsed`;
  panel.querySelector("[data-plan-cancel]").classList.toggle("hidden", session.status !== "running");
}
async function cancelFileManagementPlan(showCancelled = false) {
  const sessionId = fileManagement.planSession;
  fileManagement.planToken++;
  clearTimeout(fileManagement.planTimer);
  fileManagement.planSession = null;
  if (sessionId) {
    try { await api("/api/file-management/plan/cancel", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({session_id: sessionId})}); } catch {}
  }
  if (showCancelled) {
    renderPlanProgress({status: "cancelled"});
    $("file-management-plan-progress").classList.remove("hidden");
    $("file-management-plan-progress").querySelector("[data-plan-phase]").textContent = "Analysis cancelled";
    $("file-management-plan-progress").querySelector("[data-plan-progress-detail]").textContent = "No changes were made.";
  } else renderPlanProgress(null);
}
function currentRuleset() { return fileManagement.rulesets.find(item => item.id === fileManagement.current) || null; }
function newId() { return globalThis.crypto?.randomUUID?.() || `rule-${Date.now()}-${Math.random().toString(16).slice(2)}`; }

function ruleToUi(rule = {}) {
  const match = rule.match || {};
  const action = rule.action || {};
  let representation = match.representation_class || match.format || "all";
  if (match.formats?.length === 2 && match.formats.includes("jpeg") && match.formats.includes("png")) representation = "conventional-image";
  return {id: rule.id || newId(), enabled: true, asset: match.selection_state || "all", representation, operation: action.operation || "delete", profileId: action.profile_id || fileManagement.profiles.find(profile => profile.codec === "jpeg-xl")?.id || "", disposition: action.source_disposition !== "replace", inPlace: action.compress_in_place !== false, destination: action.destination_dir || action.target_template || "", preserve: action.preserve_relative_structure === true, conflictPolicy: action.conflict_policy || (action.rename_on_conflict === false ? "skip" : "rename")};
}

function uiToRule(rule) {
  const match = {};
  if (rule.asset !== "all") match.selection_state = rule.asset;
  if (["raw", "conventional-image", "compressed-image", "video"].includes(rule.representation)) match.representation_class = rule.representation;
  if (["jpeg", "png", "jxl", "avif", "webp"].includes(rule.representation)) match.format = rule.representation;
  if (rule.representation === "conventional-image") match.formats = ["jpeg", "png"];
  const action = {operation: rule.operation};
  if (rule.operation === "compress") { action.profile_id = rule.profileId || null; action.source_disposition = rule.disposition ? "keep" : "replace"; action.compress_in_place = rule.inPlace; action.conflict_policy = rule.conflictPolicy; if (!rule.inPlace) action.destination_dir = rule.destination; }
  if (["copy", "move"].includes(rule.operation)) { action.destination_dir = rule.destination; action.preserve_relative_structure = rule.preserve; action.conflict_policy = rule.conflictPolicy; }
  return {id: rule.id, enabled: true, match, action};
}

function ruleHelperMarkup(rule) {
  const destination = (rule.destination || "destination").replace(/^\/+|\/+$/g, "") || "destination";
  const source = "photos/day1/file.jpg";
  const target = rule.preserve ? `${destination}/${source}` : `${destination}/file.jpg`;
  return `Source <code>${escapeHtml(source)}</code> → <code>${escapeHtml(target)}</code>`;
}

function setFileManagementTab(tab) {
  document.querySelectorAll("[data-file-management-tab]").forEach(button => button.classList.toggle("active", button.dataset.fileManagementTab === tab));
  document.querySelectorAll("[data-file-management-section]").forEach(section => section.classList.toggle("hidden", section.dataset.fileManagementSection !== tab));
  if (tab === "plan") loadFileManagementPlan();
}

function renderRulesetSelector() {
  const select = $("file-management-ruleset-select");
  const customDraft = fileManagement.dirty && (!currentRuleset() || currentRuleset()?.is_builtin);
  select.innerHTML = `${fileManagement.rulesets.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("")}${customDraft ? `<option value="custom-draft">Custom</option>` : ""}`;
  select.value = customDraft ? "custom-draft" : fileManagement.current || "";
  renderFileManagementLockState();
}

function markRulesDirty() {
  clearDraftReview();
  fileManagement.dirty = true;
  $("file-management-ruleset-kind").textContent = "Custom Ruleset";
  $("file-management-rename").classList.toggle("hidden", !currentRuleset() || currentRuleset().is_builtin);
  $("file-management-delete").classList.toggle("hidden", !currentRuleset() || currentRuleset().is_builtin);
  renderRulesetSelector();
  renderFileManagementLockState();
}

function renderFileManagementRules() {
  const profiles = fileManagement.profiles.filter(profile => profile.codec === "jpeg-xl" || profile.codec === "av1" || profile.codec === "avif");
  $("file-management-rules").innerHTML = fileManagement.draft.map((rule, index) => {
    const profileOptions = profiles.map(profile => `<option value="${escapeHtml(profile.id)}"${profile.id === rule.profileId ? " selected" : ""}>${escapeHtml(profile.name)}${profile.codec === "av1" ? " · pending" : ""}</option>`).join("");
    const conflictPolicy = `<fieldset class="conflict-policy"><legend>Destination conflict</legend>${[["rename", "Rename"], ["skip", "Skip"], ["overwrite", "Overwrite"]].map(([value, label]) => `<label><input type="radio" name="conflict-${index}" data-rule-field="conflictPolicy" value="${value}"${rule.conflictPolicy === value ? " checked" : ""}> ${label}</label>`).join("")}</fieldset>`;
    const actionFields = rule.operation === "compress"
      ? `<div class="rule-options rule-options-compress"><label class="rule-field">Profile <select data-rule-field="profileId">${profileOptions}</select></label><label class="rule-option-toggle rule-checkbox"><input type="checkbox" data-rule-field="disposition"${rule.disposition ? " checked" : ""}> <span>Keep source</span></label><label class="rule-option-toggle rule-checkbox"><input type="checkbox" data-rule-field="inPlace"${rule.inPlace ? " checked" : ""}> <span>Compress in place</span></label>${conflictPolicy}<label class="rule-field">Destination <input data-rule-field="destination" value="${escapeHtml(rule.destination)}" placeholder="compressed/"${rule.inPlace ? " disabled" : ""}></label></div>`
      : ["copy", "move"].includes(rule.operation)
        ? `<div class="rule-options rule-options-copy"><label class="rule-field">Destination <input data-rule-field="destination" value="${escapeHtml(rule.destination)}" placeholder="raws/"></label><label class="rule-option-toggle rule-checkbox"><input type="checkbox" data-rule-field="preserve"${rule.preserve ? " checked" : ""}> <span>Recreate source folders inside destination</span></label>${conflictPolicy}<p class="muted rule-help" data-rule-help>${ruleHelperMarkup(rule)}</p></div>`
        : "";
    return `<article class="file-rule-card" data-rule-index="${index}"><div class="rule-card-header"><span class="rule-number">Rule ${index + 1}</span><button class="icon" type="button" data-rule-remove aria-label="Remove rule">×</button></div><div class="rule-line">For <select data-rule-field="representation">${fileManagementOptionList(representationSelectors, rule.representation)}</select> representations of <select data-rule-field="asset">${fileManagementOptionList(assetSelectors, rule.asset)}</select> assets → <select data-rule-field="operation">${fileManagementOptionList(operationSelectors, rule.operation)}</select></div>${actionFields}</article>`;
  }).join("") || `<p class="muted">No rules yet. Add a rule to define the plan.</p>`;
  $("file-management-rules").querySelectorAll("[data-rule-index]").forEach(card => {
    const index = Number(card.dataset.ruleIndex);
    card.querySelectorAll("[data-rule-field]").forEach(control => {
      const update = () => {
        if (control.type === "radio" && !control.checked) return;
        fileManagement.draft[index][control.dataset.ruleField] = control.type === "checkbox" ? control.checked : control.value;
        markRulesDirty();
        if (control.dataset.ruleField === "destination") {
          const help = card.querySelector("[data-rule-help]");
          if (help) help.innerHTML = ruleHelperMarkup(fileManagement.draft[index]);
        } else {
          renderFileManagementRules();
        }
      };
      control.addEventListener(control.dataset.ruleField === "destination" ? "input" : "change", update);
    });
    card.querySelector("[data-rule-remove]").addEventListener("click", () => { fileManagement.draft.splice(index, 1); markRulesDirty(); renderFileManagementRules(); });
  });
  renderFileManagementLockState();
}

function selectRuleset(id, {dirty = false} = {}) {
  const ruleset = fileManagement.rulesets.find(item => item.id === id);
  if (!ruleset) return;
  fileManagement.current = ruleset.id;
  fileManagement.draft = (ruleset.rules || []).filter(rule => rule.enabled !== false).map(ruleToUi);
  fileManagement.dirty = dirty;
  $("file-management-ruleset-kind").textContent = ruleset.is_builtin && !dirty ? "Built-in" : dirty ? "Custom Ruleset" : "Custom";
  $("file-management-rename").classList.toggle("hidden", ruleset.is_builtin);
  $("file-management-delete").classList.toggle("hidden", ruleset.is_builtin);
  renderRulesetSelector();
  renderFileManagementRules();
}

function renderFileManagementProfiles() {
  $("file-management-profiles").innerHTML = fileManagement.profiles.map(profile => {
    const settings = profile.settings || {};
    const av1Available = profile.capability?.available;
    const detail = profile.codec === "jpeg-xl" ? `Quality ${settings.quality ?? "—"}${settings.effort ? ` · Effort ${settings.effort}` : ""}` : profile.codec.toUpperCase();
    const status = profile.codec === "av1" ? ` · ${av1Available ? "encoder available" : "encoder unavailable"} · pending` : "";
    const preview = ["jpeg-xl", "avif"].includes(profile.codec) ? `<button class="profile-preview-button${profile.is_builtin ? "" : " custom"}" type="button" data-profile-preview="${escapeHtml(profile.id)}" aria-label="Preview ${escapeHtml(profile.name)} compression" title="Preview ${escapeHtml(profile.name)} compression">?</button>` : "";
    const edit = profile.is_builtin ? `<span class="muted">Built-in</span>` : `<button class="secondary" type="button" data-profile-edit="${escapeHtml(profile.id)}">Edit</button>`;
    return `<article class="profile-card"><div><h3>${escapeHtml(profile.name)}</h3><p class="muted">${escapeHtml(detail)}${status}</p></div><div class="profile-card-actions">${preview}${edit}</div></article>`;
  }).join("");
  $("file-management-profiles").querySelectorAll("[data-profile-edit]").forEach(button => button.addEventListener("click", () => editProfile(button.dataset.profileEdit)));
  $("file-management-profiles").querySelectorAll("[data-profile-preview]").forEach(button => button.addEventListener("click", () => globalThis.openCompressionProfilePreview?.(fileManagement.profiles.find(profile => profile.id === button.dataset.profilePreview))));
  renderFileManagementLockState();
}

async function refreshFileManagementEditor(preferredId = null) {
  const [profiles, rulesets, presets] = await Promise.all([api("/api/file-management/profiles"), api("/api/file-management/rulesets"), api("/api/file-management/presets")]);
  fileManagement.profiles = profiles.profiles || [];
  fileManagement.rulesets = rulesets.rulesets || [];
  fileManagement.presets = presets.presets || [];
  const preset = preferredId ? fileManagement.presets.find(item => item.ruleset_id === preferredId) : fileManagement.presets.find(item => item.name === "Archive cleanup") || fileManagement.presets[0];
  selectRuleset(preset?.ruleset_id || fileManagement.rulesets[0]?.id);
  renderFileManagementProfiles();
}

async function saveDraftRuleset(name = "Custom Ruleset", {forceNew = false} = {}) {
  const existing = forceNew ? null : currentRuleset();
  const ruleset = await api("/api/file-management/rulesets", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: existing && !existing.is_builtin ? existing.id : undefined, name, rules: fileManagement.draft.map(uiToRule)})});
  const index = fileManagement.rulesets.findIndex(item => item.id === ruleset.id);
  if (index >= 0) fileManagement.rulesets[index] = ruleset; else fileManagement.rulesets.push(ruleset);
  const existingPreset = fileManagement.presets.find(item => item.ruleset_id === ruleset.id);
  if (existingPreset || existing?.is_builtin || !existing) {
    const savedPreset = await api("/api/file-management/presets", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, ruleset_id: ruleset.id})});
    if (existingPreset) fileManagement.presets[fileManagement.presets.indexOf(existingPreset)] = savedPreset; else fileManagement.presets.push(savedPreset);
  }
  fileManagement.current = ruleset.id;
  fileManagement.dirty = false;
  selectRuleset(ruleset.id);
  fileManagementStatus("");
  return ruleset;
}

async function loadFileManagementPlan() {
  await cancelFileManagementPlan();
  const token = fileManagement.planToken;
  renderPlanProgress({status: "running", phase: "Loading indexed files", completed: 0, total: 0, elapsed_seconds: 0});
  try {
    if (fileManagement.dirty) await saveDraftRuleset("Custom Ruleset");
    if (token !== fileManagement.planToken || !$("file-management-dialog").open) return;
    const started = await api("/api/file-management/plan/start", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ruleset_id: fileManagement.current})});
    if (token !== fileManagement.planToken) return;
    fileManagement.planSession = started.session_id;
    while (token === fileManagement.planToken && $("file-management-dialog").open) {
      const session = await api(`/api/file-management/plan/status?session_id=${encodeURIComponent(started.session_id)}`);
      if (token !== fileManagement.planToken) return;
      renderPlanProgress(session);
      if (session.status === "complete") { fileManagement.planSession = null; renderFileManagementPlan(session.result); return; }
      if (session.status === "failed") { fileManagementStatus(`Plan analysis failed: ${session.error}`, true); return; }
      if (session.status === "cancelled") { renderPlanProgress(session); $("file-management-plan-progress").querySelector("[data-plan-phase]").textContent = "Analysis cancelled"; return; }
      await new Promise(resolve => { fileManagement.planTimer = setTimeout(resolve, 200); });
    }
  } catch (error) { if (token === fileManagement.planToken) fileManagementStatus(`Plan analysis failed: ${error.message}`, true); }
}

function planOperationRow(item) { return `<div class="plan-operation-row" data-plan-operation-row><span><strong>${escapeHtml(item.filename || "File")}</strong><small>${escapeHtml(item.source_relative_path || "")}</small></span><span>${escapeHtml(item.target_relative_path || item.profile_name || "Delete")}${item.renamed_to_avoid_conflict ? " · Renamed to avoid conflict" : item.replaces_source_in_place ? " · Replaces source in place" : ""}</span><span>${formatBytes(item.bytes)}${["already_satisfied", "skipped"].includes(item.destination_status) ? ` · ${item.destination_status === "skipped" ? "Skipped" : "Already satisfied"}` : ""}${item.conflicts?.length ? ` · <em>${escapeHtml(item.conflicts.join("; "))}</em>` : ""}${item.blockers?.length ? ` · <em>Blocked: ${escapeHtml(item.blockers.join("; "))}</em>` : ""}</span></div>`; }
function renderPlanOperationGroup(operation, rows) {
  const label = operation[0].toUpperCase() + operation.slice(1);
  const initial = rows.slice(0, 1).map(planOperationRow).join("");
  const remaining = rows.length - 1;
  return `<details class="plan-operation"><summary>${label} · ${rows.length}</summary><div data-plan-operation-rows>${initial}</div>${remaining > 0 ? `<button type="button" class="secondary" data-plan-show-more data-operation="${operation}" data-offset="1">Show more (${remaining})</button>` : ""}</details>`;
}
function bindPlanOperationDetails(data) {
  document.querySelectorAll("[data-plan-show-more]").forEach(button => button.addEventListener("click", () => {
    const rows = (data.operations || []).filter(item => item.operation === button.dataset.operation);
    const offset = Number(button.dataset.offset || 0);
    const batch = rows.slice(offset, offset + 50);
    button.parentElement.querySelector("[data-plan-operation-rows]").insertAdjacentHTML("beforeend", batch.map(planOperationRow).join(""));
    const next = offset + batch.length;
    button.dataset.offset = String(next);
    const left = rows.length - next;
    button.textContent = left ? `Show more (${left})` : "Show all shown";
    if (!left) button.disabled = true;
  }));
}
function renderFileManagementPlan(data) {
  fileManagement.plan = data;
  const summary = data.summary || {};
  const cards = [["DELETE", summary.delete, "bytes"], ["COMPRESS", summary.compress, "source_bytes"], ["COPY", summary.copy, "bytes_added"], ["MOVE", summary.move, "bytes_moved"]];
  const delta = Number(summary.estimated_storage_delta_bytes || 0);
  const deltaLabel = delta < 0 ? `Estimated total change <strong class="net-reduction">−${formatBytes(-delta)}</strong>` : delta > 0 ? `Estimated total change <strong class="net-increase">+${formatBytes(delta)}</strong>` : `Estimated total change <strong class="net-neutral">0 B</strong>`;
  const currentFree = summary.available_space_bytes;
  const afterPlan = currentFree == null ? null : Number(currentFree) - delta;
  $("file-management-plan-summary").innerHTML = `<div class="plan-summary-grid">${cards.map(([label, value, bytes]) => { const storageDelta = Number(value?.estimated_storage_delta_bytes || 0); const amount = label === "COMPRESS" ? `${formatBytes(value?.source_bytes || 0)} → ${formatBytes(value?.estimated_output_bytes || 0)}` : formatBytes(value?.[bytes] || 0); const deltaLine = storageDelta ? `<em class="${storageDelta < 0 ? "storage-free" : "storage-add"}">Total change ${storageDelta < 0 ? "−" : "+"}${formatBytes(Math.abs(storageDelta))}</em>` : ""; return `<article class="plan-card"><strong>${label}</strong><span>${Number(value?.file_count || 0).toLocaleString()} ${Number(value?.file_count || 0) === 1 ? "file" : "files"}</span><small>${amount}</small>${deltaLine}</article>`; }).join("")}</div><div class="plan-summary-callouts"><span>${deltaLabel}</span><span>Temporary space upper bound <strong>${formatBytes(summary.temporary_space_upper_bound_bytes || 0)}</strong></span><span>Current free disk space <strong>${formatDiskSpace(currentFree)}</strong></span>${afterPlan == null ? "" : `<span>Estimated free disk space after plan <strong>${formatDiskSpace(afterPlan)}</strong></span>`}<span>Logical assets with no remaining archive representation <strong>${Number(summary.assets_with_no_surviving_representation || 0).toLocaleString()}</strong></span></div>`;
  const blockers = data.blockers || summary.capability_blockers || [];
  $("file-management-plan-blockers").innerHTML = blockers.length ? `<section class="plan-blockers"><h3>Plan blockers</h3>${blockers.map(blocker => `<article class="plan-blocker"><strong>${escapeHtml(blocker.profile_name || "Capability")}</strong><span>${escapeHtml(blocker.reason)}</span><small>${Number(blocker.affected_count || 0).toLocaleString()} planned ${Number(blocker.affected_count || 0) === 1 ? "operation" : "operations"} affected.</small></article>`).join("")}</section>` : "";
  $("file-management-plan-conflicts").innerHTML = data.conflicts?.length ? `<section class="plan-conflicts"><h3>${data.conflicts.length} conflicts must be resolved</h3>${data.conflicts.map(conflict => `<article class="plan-conflict"><strong>${escapeHtml(conflict.filename || "File")}</strong><span>${escapeHtml(conflict.source_relative_path || "")}${conflict.target_relative_path ? ` → ${escapeHtml(conflict.target_relative_path)}` : ""}</span><p>${escapeHtml(conflict.reason)}</p><small>Rule ${escapeHtml(conflict.rule_id || "")}</small></article>`).join("")}</section>` : `<p class="plan-ok">No conflicts detected.</p>`;
  const operations = data.operations || [];
  $("file-management-plan-details").innerHTML = ["delete", "compress", "copy", "move"].map(operation => { const rows = operations.filter(item => item.operation === operation); return rows.length ? renderPlanOperationGroup(operation, rows) : ""; }).join("");
  bindPlanOperationDetails(data);
  $("file-management-plan-note").textContent = data.executor?.message || "";
  const executable = (data.operations || []).some(item => ["copy", "move", "delete"].includes(item.operation) && !item.conflicts?.length && !item.blockers?.length && !["already_satisfied", "skipped"].includes(item.destination_status));
  $("file-management-execution-panel").innerHTML = executable ? `<div class="execution-review-actions"><button type="button" class="primary-action" data-review-execution>Review execution</button><small>Execution will revalidate this plan before creating a frozen snapshot.</small></div>` : `<p class="muted">No safe Copy, Move, or Delete operations are available to execute.</p>`;
  $("file-management-execution-panel").classList.remove("hidden");
  $("file-management-execution-panel").querySelector("[data-review-execution]")?.addEventListener("click", () => { void prepareFileManagementExecution(); });
}

function executionRows(execution) { return execution?.operations || []; }
function formatDuration(seconds) { const value = Math.max(0, Math.round(Number(seconds) || 0)); const hours = Math.floor(value / 3600); const minutes = Math.floor((value % 3600) / 60); const rest = value % 60; return hours ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`; }
function executionDestructive(execution) { return executionRows(execution).some(row => ["move", "delete"].includes(row.operation) && row.status === "pending"); }
function executionChangeMarkup(written, removed, delta) { const value = Number(delta || 0); const sign = value > 0 ? "+" : value < 0 ? "−" : ""; const className = value > 0 ? "net-increase" : value < 0 ? "net-reduction" : "net-neutral"; return `Written ${formatBytes(written || 0)} · Removed ${formatBytes(removed || 0)} · Total change <strong class="${className}">${sign}${formatBytes(Math.abs(value))}</strong>`; }
function renderExecution(execution) {
  fileManagement.execution = execution;
  const panel = $("file-management-execution-panel");
  if (!execution) { panel.classList.add("hidden"); return; }
  const rows = executionRows(execution);
  const pending = rows.filter(row => row.status === "pending");
  const destructive = executionDestructive(execution);
  const active = ["running", "cancelling"].includes(execution.status);
  const canStart = execution.status === "draft";
  const canResume = ["cancelled", "interrupted"].includes(execution.status);
  const canRetry = rows.some(row => row.status === "failed") && !active;
  const canReanalyze = ["completed", "completed_with_errors"].includes(execution.status);
  const counts = execution.counts || {};
  if (canStart) {
    const copies = pending.filter(row => row.operation === "copy").length;
    const moves = pending.filter(row => row.operation === "move").length;
    const deletes = pending.filter(row => row.operation === "delete").length;
    const compressed = rows.filter(row => row.operation === "compress").length;
    const movedBytes = pending.filter(row => row.operation === "move").reduce((sum, row) => sum + Number(row.source_size_bytes || 0), 0);
    const targetFolders = [...new Set(pending.map(row => String(row.target_relative_path || "").split("/").slice(0, -1).join("/")).filter(Boolean))];
    panel.innerHTML = `<section class="execution-review"><h3>Review execution</h3><p>This frozen review contains the exact operations that will be attempted.</p><div class="execution-counts"><span>Copy <strong>${copies}</strong></span><span>Move <strong>${moves}</strong></span><span>Delete <strong>${deletes}</strong></span><span>Compress excluded <strong>${compressed}</strong></span><span>Excluded <strong>${counts.excluded || 0}</strong></span><span>Skipped <strong>${counts.skipped || 0}</strong></span></div><p class="muted">Bytes copied ${formatBytes(execution.estimated_bytes_written || 0)} · Bytes moved ${formatBytes(movedBytes)} · Bytes permanently deleted ${formatBytes(execution.estimated_bytes_removed || 0)} · Total change ${formatBytes(execution.estimated_storage_delta || 0)}</p><p class="muted">Temporary space required ${formatBytes(execution.temporary_space_upper_bound_bytes || 0)}${targetFolders.length ? ` · Target folders ${escapeHtml(targetFolders.join(", "))}` : ""}</p>${compressed ? `<p class="muted">${compressed} Compress operation${compressed === 1 ? " is" : "s are"} excluded: production compression remains disabled until Phase 10C.</p>` : ""}${destructive ? `<p class="execution-warning">Move and Delete operations will modify source files. Delete operations permanently remove the confirmed files.</p><label class="checkbox-line"><input type="checkbox" data-execution-ack> I understand that this execution will modify source files.</label>` : ""}<div class="form-actions"><button type="button" class="primary-action" data-execution-start ${destructive ? "disabled" : ""}>Execute safe operations</button><button type="button" class="secondary" data-execution-discard>Discard review</button></div></section>`;
    panel.querySelector("[data-execution-ack]")?.addEventListener("change", event => { panel.querySelector("[data-execution-start]").disabled = !event.target.checked; });
    panel.querySelector("[data-execution-start]")?.addEventListener("click", () => { void startFileManagementExecution(); });
    panel.querySelector("[data-execution-discard]")?.addEventListener("click", () => { void executionAction("discard"); });
    renderFileManagementLockState();
    return;
  }
  const current = execution.current_operation;
  const statusLabel = execution.status === "running" ? "Executing file plan" : execution.status === "cancelling" ? "Cancelling execution" : `Execution ${execution.status}`;
  const byteLine = execution.total_bytes ? `${formatBytes(execution.bytes_processed)} / ${formatBytes(execution.total_bytes)}` : "No byte-copy work";
  const throughput = execution.throughput_bytes_per_second ? `${formatBytes(execution.throughput_bytes_per_second)}/s` : "—";
  const eta = execution.eta_seconds == null ? "—" : formatDuration(execution.eta_seconds);
  panel.innerHTML = `<section class="execution-progress"><h3>${escapeHtml(statusLabel)}</h3><p>${current ? `${escapeHtml(current.operation)} · ${escapeHtml(current.filename)}<br>${byteLine}<br>${throughput} · Elapsed ${formatDuration(execution.elapsed_seconds || 0)} · ETA ${eta}` : byteLine}</p><div class="execution-counts"><span>Completed <strong>${counts.completed || 0}</strong></span><span>Failed <strong>${counts.failed || 0}</strong></span><span>Skipped <strong>${counts.skipped || 0}</strong></span><span>Pending <strong>${counts.pending || 0}</strong></span><span>${counts.completed || 0} / ${counts.total || 0} operations</span></div><p class="muted">${executionChangeMarkup(execution.actual_bytes_written, execution.actual_bytes_removed, execution.actual_storage_delta)}</p>${execution.error ? `<p class="status error">${escapeHtml(execution.error)}</p>` : ""}${execution.warning ? `<p class="status">${escapeHtml(execution.warning)}</p>` : ""}<div class="form-actions">${active ? `<button type="button" class="danger-button" data-execution-cancel>Cancel</button>` : ""}${canResume ? `<button type="button" class="primary-action" data-execution-resume>Resume</button>` : ""}${execution.status === "interrupted" ? `<button type="button" class="secondary" data-execution-abandon>Abandon</button>` : ""}${canRetry ? `<button type="button" class="secondary" data-execution-retry>Retry failed</button>` : ""}${canReanalyze ? `<button type="button" class="secondary" data-execution-reanalyze>Re-analyze plan</button>` : ""}</div></section>`;
  panel.querySelector("[data-execution-cancel]")?.addEventListener("click", () => { void executionAction("cancel"); });
  panel.querySelector("[data-execution-resume]")?.addEventListener("click", () => { void executionAction("resume"); });
  panel.querySelector("[data-execution-abandon]")?.addEventListener("click", () => { void executionAction("abandon"); });
  panel.querySelector("[data-execution-retry]")?.addEventListener("click", () => { void executionAction("retry-failed"); });
  panel.querySelector("[data-execution-reanalyze]")?.addEventListener("click", () => { setFileManagementTab("plan"); });
  renderFileManagementLockState();
}
function stopExecutionPolling() { clearTimeout(fileManagement.executionTimer); fileManagement.executionTimer = null; }
async function pollFileManagementExecution() {
  if (!fileManagement.execution?.id) return;
  try {
    const result = await api(`/api/file-management/executions/${encodeURIComponent(fileManagement.execution.id)}`);
    renderExecution(result.execution);
    if (["running", "cancelling"].includes(result.execution.status)) fileManagement.executionTimer = setTimeout(() => { void pollFileManagementExecution(); }, 300);
    else stopExecutionPolling();
  } catch (error) { stopExecutionPolling(); fileManagementStatus(`Execution status failed: ${error.message}`, true); }
}
async function prepareFileManagementExecution() {
  const panel = $("file-management-execution-panel");
  panel.classList.remove("hidden");
  panel.innerHTML = `<section class="execution-review-busy" aria-busy="true"><h3>Preparing execution review…</h3><p class="muted">Rechecking the analyzed plan and freezing its safe operations.</p><progress></progress></section>`;
  try {
    const result = await api("/api/file-management/executions", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ruleset_id: fileManagement.current, plan_digest: fileManagement.plan?.plan_digest})});
    renderExecution(result.execution);
  } catch (error) {
    panel.innerHTML = `<section class="execution-review-actions"><p class="status error">${escapeHtml(error.message)}</p><button type="button" class="secondary" data-review-execution>Review execution</button></section>`;
    panel.querySelector("[data-review-execution]").addEventListener("click", () => { void prepareFileManagementExecution(); });
  }
}
async function startFileManagementExecution() { try { const result = await executionAction("start"); if (result) void pollFileManagementExecution(); } catch {} }
async function executionAction(action) {
  if (!fileManagement.execution?.id) return null;
  try {
    const result = await api(`/api/file-management/executions/${encodeURIComponent(fileManagement.execution.id)}/${action}`, {method: "POST", headers: {"Content-Type": "application/json"}});
    renderExecution(result.execution);
    if (["running", "cancelling"].includes(result.execution.status)) void pollFileManagementExecution();
    return result.execution;
  } catch (error) { fileManagementStatus(`Execution action failed: ${error.message}`, true); return null; }
}

function editProfile(id) { const profile = fileManagement.profiles.find(item => item.id === id); if (!profile) return; const form = $("file-management-profile-form"); form.dataset.profileId = id; $("file-management-profile-name").value = profile.name; $("file-management-profile-codec").value = profile.codec; $("file-management-profile-quality").value = profile.settings?.quality ?? 60; $("file-management-profile-effort").value = profile.settings?.effort ?? 7; form.classList.remove("hidden"); }

$("file-management-button").addEventListener("click", async () => { try { await refreshFileManagementEditor(); const active = await api("/api/file-management/executions/active"); renderExecution(active.execution); $("file-management-dialog").showModal(); if (active.execution && ["running", "cancelling"].includes(active.execution.status)) void pollFileManagementExecution(); } catch (error) { fileManagementStatus(`File Management failed: ${error.message}`, true); } });
$("file-management-close").addEventListener("click", () => $("file-management-dialog").close());
$("file-management-dialog").addEventListener("close", () => { void cancelFileManagementPlan(); stopExecutionPolling(); });
document.querySelectorAll("[data-file-management-tab]").forEach(button => button.addEventListener("click", () => setFileManagementTab(button.dataset.fileManagementTab)));
$("file-management-ruleset-select").addEventListener("change", event => { if (event.target.value === "custom-draft") return; selectRuleset(event.target.value); });
$("file-management-add-rule").addEventListener("click", () => { fileManagement.draft.push(ruleToUi({})); markRulesDirty(); renderFileManagementRules(); });
$("file-management-new-ruleset").addEventListener("click", () => { fileManagement.current = null; fileManagement.draft = []; fileManagement.dirty = true; $("file-management-ruleset-kind").textContent = "Custom Ruleset"; $("file-management-rename").classList.add("hidden"); $("file-management-delete").classList.add("hidden"); renderRulesetSelector(); renderFileManagementRules(); });
$("file-management-analyze").addEventListener("click", () => { setFileManagementTab("plan"); });
$("file-management-plan-progress").querySelector("[data-plan-cancel]").addEventListener("click", () => { void cancelFileManagementPlan(true); });
$("file-management-save-as").addEventListener("click", async () => { const name = window.prompt("Save ruleset as", "Custom Ruleset"); if (!name) return; try { await saveDraftRuleset(name, {forceNew: true}); } catch (error) { fileManagementStatus(`Ruleset save failed: ${error.message}`, true); } });
$("file-management-rename").addEventListener("click", async () => { const name = window.prompt("Rename ruleset", currentRuleset()?.name || "Custom Ruleset"); if (!name) return; try { await saveDraftRuleset(name); } catch (error) { fileManagementStatus(`Rename failed: ${error.message}`, true); } });
$("file-management-delete").addEventListener("click", async () => { const ruleset = currentRuleset(); if (!ruleset || ruleset.is_builtin || !window.confirm(`Delete ${ruleset.name}?`)) return; try { await api("/api/file-management/rulesets/delete", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: ruleset.id})}); await refreshFileManagementEditor(); } catch (error) { fileManagementStatus(`Delete failed: ${error.message}`, true); } });
$("file-management-profile-cancel").addEventListener("click", () => $("file-management-profile-form").classList.add("hidden"));
$("file-management-new-profile").addEventListener("click", () => { const form = $("file-management-profile-form"); form.dataset.profileId = ""; $("file-management-profile-name").value = ""; $("file-management-profile-codec").value = "jpeg-xl"; $("file-management-profile-quality").value = 60; $("file-management-profile-effort").value = 7; form.classList.remove("hidden"); });
$("file-management-profile-form").addEventListener("submit", async event => { event.preventDefault(); try { await api("/api/file-management/profiles", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: event.currentTarget.dataset.profileId || undefined, name: $("file-management-profile-name").value, codec: $("file-management-profile-codec").value, container: $("file-management-profile-codec").value === "jpeg-xl" ? "jxl" : $("file-management-profile-codec").value === "av1" ? "mp4" : "avif", settings: {quality: Number($("file-management-profile-quality").value), effort: Number($("file-management-profile-effort").value)}})}); $("file-management-profile-form").classList.add("hidden"); await refreshFileManagementEditor(); } catch (error) { fileManagementStatus(`Profile save failed: ${error.message}`, true); } });
