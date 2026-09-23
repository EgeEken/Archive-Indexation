let fileManagement = {profiles: [], rulesets: [], presets: [], current: null, draft: [], dirty: false};

const assetSelectors = [["all", "All"], ["selected", "Selected"], ["undecided", "Undecided"], ["rejected", "Rejected"]];
const representationSelectors = [["all", "All representations"], ["raw", "RAW"], ["rendered-image", "Rendered image"], ["jpeg", "JPEG"], ["png", "PNG"], ["jxl", "JXL"], ["avif", "AVIF"], ["video", "Video"]];
const operationSelectors = [["copy", "Copy"], ["move", "Move"], ["compress", "Compress"], ["delete", "Delete"]];

function fileManagementOptionList(values, selected) {
  return values.map(([value, label]) => `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function ruleToUi(rule) {
  const match = rule.match || {};
  const action = rule.action || {};
  return {id: rule.id || crypto.randomUUID(), enabled: rule.enabled !== false, asset: match.selection_state || "all", representation: match.representation_class || match.format || match.formats?.[0] || "all", operation: action.operation || "delete", profileId: action.profile_id || fileManagement.profiles.find(profile => profile.codec === "jpeg-xl")?.id || "", disposition: action.source_disposition === "replace" ? "replace" : "keep", destination: action.target_template || "", preserve: action.preserve_relative_structure !== false};
}

function uiToRule(rule) {
  const match = {};
  if (rule.asset !== "all") match.selection_state = rule.asset;
  if (["raw", "rendered-image", "video"].includes(rule.representation)) match.representation_class = rule.representation;
  if (["jpeg", "png", "jxl", "avif"].includes(rule.representation)) match.format = rule.representation;
  const action = {operation: rule.operation};
  if (rule.operation === "compress") { action.profile_id = rule.profileId || null; action.source_disposition = rule.disposition; }
  if (["copy", "move"].includes(rule.operation)) { action.target_template = rule.destination; action.preserve_relative_structure = rule.preserve; }
  return {id: rule.id, enabled: rule.enabled, match, action};
}

function currentRuleset() { return fileManagement.rulesets.find(item => item.id === fileManagement.current) || null; }

function setFileManagementTab(tab) {
  document.querySelectorAll("[data-file-management-tab]").forEach(button => button.classList.toggle("active", button.dataset.fileManagementTab === tab));
  document.querySelectorAll("[data-file-management-section]").forEach(section => section.classList.toggle("hidden", section.dataset.fileManagementSection !== tab));
  if (tab === "plan") loadFileManagementPlan();
}

function markRulesDirty() {
  fileManagement.dirty = true;
  $("file-management-ruleset-kind").textContent = "Custom Ruleset";
  const custom = currentRuleset() && !currentRuleset().is_builtin;
  $("file-management-rename").classList.toggle("hidden", !custom);
  $("file-management-delete").classList.toggle("hidden", !custom);
}

function renderFileManagementRules() {
  $("file-management-rules").innerHTML = fileManagement.draft.map((rule, index) => {
    const profileOptions = fileManagement.profiles.map(profile => `<option value="${escapeHtml(profile.id)}"${profile.id === rule.profileId ? " selected" : ""}>${escapeHtml(profile.name)}${profile.codec === "av1" ? " · pending" : ""}</option>`).join("");
    const actionFields = rule.operation === "compress" ? `<div class="rule-options"><label>Profile <select data-rule-field="profileId">${profileOptions}</select></label><label>Source <select data-rule-field="disposition"><option value="keep"${rule.disposition === "keep" ? " selected" : ""}>Keep source</option><option value="replace"${rule.disposition === "replace" ? " selected" : ""}>Replace source after validation</option></select></label></div>` : ["copy", "move"].includes(rule.operation) ? `<div class="rule-options"><label>Destination <input data-rule-field="destination" value="${escapeHtml(rule.destination)}" placeholder="raws/"></label><label class="checkbox-line"><input type="checkbox" data-rule-field="preserve"${rule.preserve ? " checked" : ""}> Preserve relative folder structure</label></div>` : "";
    return `<article class="file-rule-card${rule.enabled ? "" : " disabled"}" data-rule-index="${index}"><div class="rule-card-header"><label class="checkbox-line"><input type="checkbox" data-rule-field="enabled"${rule.enabled ? " checked" : ""}> Enabled</label><button class="icon" type="button" data-rule-remove aria-label="Remove rule">×</button></div><div class="rule-line">When <select data-rule-field="asset">${fileManagementOptionList(assetSelectors, rule.asset)}</select> assets with <select data-rule-field="representation">${fileManagementOptionList(representationSelectors, rule.representation)}</select> representations → <select data-rule-field="operation">${fileManagementOptionList(operationSelectors, rule.operation)}</select></div>${actionFields}</article>`;
  }).join("") || `<p class="muted">No rules yet. Add a rule to define the plan.</p>`;
  $("file-management-rules").querySelectorAll("[data-rule-index]").forEach(card => {
    const index = Number(card.dataset.ruleIndex);
    card.querySelectorAll("[data-rule-field]").forEach(control => control.addEventListener("change", () => { fileManagement.draft[index][control.dataset.ruleField] = control.type === "checkbox" ? control.checked : control.value; markRulesDirty(); renderFileManagementRules(); }));
    card.querySelector("[data-rule-remove]").addEventListener("click", () => { fileManagement.draft.splice(index, 1); markRulesDirty(); renderFileManagementRules(); });
  });
}

function selectRuleset(id, {dirty = false} = {}) {
  const ruleset = fileManagement.rulesets.find(item => item.id === id);
  if (!ruleset) return;
  fileManagement.current = ruleset.id;
  fileManagement.draft = (ruleset.rules || []).map(ruleToUi);
  fileManagement.dirty = dirty;
  $("file-management-ruleset-select").value = ruleset.id;
  $("file-management-ruleset-kind").textContent = ruleset.is_builtin && !dirty ? "Built-in" : "Custom Ruleset";
  $("file-management-rename").classList.toggle("hidden", ruleset.is_builtin);
  $("file-management-delete").classList.toggle("hidden", ruleset.is_builtin);
  renderFileManagementRules();
}

function renderFileManagementProfiles() {
  $("file-management-profiles").innerHTML = fileManagement.profiles.map(profile => { const settings = profile.settings || {}; const pending = profile.codec === "av1" || profile.codec === "avif"; return `<article class="profile-card"><div><h3>${escapeHtml(profile.name)}</h3><p class="muted">${escapeHtml(profile.codec.toUpperCase())}${profile.codec === "jpeg-xl" ? ` · Distance ${escapeHtml(settings.distance ?? "—")}` : ""}${settings.effort ? ` · Effort ${escapeHtml(settings.effort)}` : ""}</p></div><div>${profile.is_builtin ? `<span class="muted">Built-in${pending ? " · pending" : ""}</span>` : `<button class="secondary" type="button" data-profile-edit="${escapeHtml(profile.id)}">Edit</button>`}</div></article>`; }).join("");
  $("file-management-profiles").querySelectorAll("[data-profile-edit]").forEach(button => button.addEventListener("click", () => editProfile(button.dataset.profileEdit)));
}

async function refreshFileManagementEditor() {
  const [profiles, rulesets, presets] = await Promise.all([api("/api/file-management/profiles"), api("/api/file-management/rulesets"), api("/api/file-management/presets")]);
  fileManagement.profiles = profiles.profiles || [];
  fileManagement.rulesets = rulesets.rulesets || [];
  fileManagement.presets = presets.presets || [];
  $("file-management-ruleset-select").innerHTML = fileManagement.rulesets.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("");
  const preset = fileManagement.presets.find(item => item.name === "Archive cleanup") || fileManagement.presets[0];
  selectRuleset(preset?.ruleset_id || fileManagement.rulesets[0]?.id);
  renderFileManagementProfiles();
}

async function saveDraftRuleset(name = "Custom Ruleset") {
  const existing = currentRuleset();
  const ruleset = await api("/api/file-management/rulesets", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: existing && !existing.is_builtin ? existing.id : undefined, name, rules: fileManagement.draft.map(uiToRule)})});
  const index = fileManagement.rulesets.findIndex(item => item.id === ruleset.id);
  if (index >= 0) fileManagement.rulesets[index] = ruleset; else fileManagement.rulesets.push(ruleset);
  const preset = fileManagement.presets.find(item => item.ruleset_id === ruleset.id);
  const savedPreset = await api("/api/file-management/presets", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: preset?.id, name, ruleset_id: ruleset.id})});
  if (preset) fileManagement.presets[fileManagement.presets.indexOf(preset)] = savedPreset; else fileManagement.presets.push(savedPreset);
  fileManagement.current = ruleset.id;
  fileManagement.dirty = false;
  selectRuleset(ruleset.id);
  return ruleset;
}

async function loadFileManagementPlan() {
  try { if (fileManagement.dirty) await saveDraftRuleset("Custom Ruleset"); const data = await api(`/api/file-management/plan?ruleset_id=${encodeURIComponent(fileManagement.current || "")}`); renderFileManagementPlan(data); } catch (error) { showToast(`Plan analysis failed: ${error.message}`); }
}

function renderFileManagementPlan(data) {
  const summary = data.summary || {};
  const cards = [["DELETE", summary.delete, "bytes"], ["COMPRESS", summary.compress, "source_bytes"], ["COPY", summary.copy, "bytes_added"], ["MOVE", summary.move, "bytes_moved"]];
  $("file-management-plan-summary").innerHTML = `<div class="plan-summary-grid">${cards.map(([label, value, bytes]) => `<article class="plan-card"><strong>${label}</strong><span>${Number(value?.file_count || 0).toLocaleString()} ${Number(value?.file_count || 0) === 1 ? "file" : "files"}</span><small>${label === "COMPRESS" ? `${formatBytes(value?.[bytes] || 0)} → ${formatBytes(value?.estimated_output_bytes || 0)}` : formatBytes(value?.[bytes] || 0)}</small></article>`).join("")}</div><div class="plan-summary-callouts"><span>Estimated net space freed <strong>${formatBytes(summary.estimated_net_bytes_freed || 0)}</strong></span><span>Peak temporary space required <strong>${formatBytes(summary.peak_temporary_bytes || 0)}</strong></span><span>Available space <strong>${formatBytes(summary.available_space_bytes)}</strong></span><span>Logical assets with no remaining archive representation <strong>${Number(summary.assets_with_no_surviving_representation || 0).toLocaleString()}</strong></span></div>`;
  $("file-management-plan-conflicts").innerHTML = data.conflicts?.length ? `<section class="plan-conflicts"><h3>${data.conflicts.length} conflicts must be resolved</h3>${data.conflicts.map(conflict => `<article class="plan-conflict"><strong>${escapeHtml(conflict.reason)}</strong><span>${escapeHtml(conflict.physical_file_id || "")}</span></article>`).join("")}</section>` : `<p class="plan-ok">No conflicts detected.</p>`;
  const operations = data.operations || [];
  $("file-management-plan-details").innerHTML = ["delete", "compress", "copy", "move"].map(operation => { const rows = operations.filter(item => item.operation === operation); if (!rows.length) return ""; return `<details class="plan-operation" open><summary>${operation[0].toUpperCase() + operation.slice(1)} · ${rows.length}</summary>${rows.map(item => `<div class="plan-operation-row"><span><strong>${escapeHtml(item.filename || "File")}</strong><small>${escapeHtml(item.source_relative_path)}</small></span><span>${escapeHtml(item.target_relative_path || item.profile_name || "Delete")}</span><span>${formatBytes(item.bytes)}${item.destination_status === "already_satisfied" ? " · Already satisfied" : ""}${item.conflicts?.length ? ` · <em>${escapeHtml(item.conflicts.join("; "))}</em>` : ""}</span></div>`).join("")}</details>`; }).join("");
  $("file-management-plan-note").textContent = data.executor?.message || "Execution is not available.";
}

function editProfile(id) { const profile = fileManagement.profiles.find(item => item.id === id); if (!profile) return; $("file-management-profile-form").dataset.profileId = id; $("file-management-profile-name").value = profile.name; $("file-management-profile-codec").value = profile.codec; $("file-management-profile-distance").value = profile.settings?.distance ?? 1.5; $("file-management-profile-effort").value = profile.settings?.effort ?? 7; $("file-management-profile-form").classList.remove("hidden"); }

$("file-management-button").addEventListener("click", async () => { try { await refreshFileManagementEditor(); $("file-management-dialog").showModal(); } catch (error) { showToast(`File Management failed: ${error.message}`); } });
$("file-management-close").addEventListener("click", () => $("file-management-dialog").close());
document.querySelectorAll("[data-file-management-tab]").forEach(button => button.addEventListener("click", () => setFileManagementTab(button.dataset.fileManagementTab)));
$("file-management-ruleset-select").addEventListener("change", event => selectRuleset(event.target.value));
$("file-management-add-rule").addEventListener("click", () => { fileManagement.draft.push(ruleToUi({})); markRulesDirty(); renderFileManagementRules(); });
$("file-management-analyze").addEventListener("click", async () => { if (fileManagement.dirty) await saveDraftRuleset("Custom Ruleset"); setFileManagementTab("plan"); });
$("file-management-save-as").addEventListener("click", async () => { const name = window.prompt("Save ruleset as", "Custom Ruleset"); if (!name) return; try { await saveDraftRuleset(name); showToast("Ruleset saved."); } catch (error) { showToast(`Ruleset save failed: ${error.message}`); } });
$("file-management-rename").addEventListener("click", async () => { const name = window.prompt("Rename ruleset", currentRuleset()?.name || "Custom Ruleset"); if (!name) return; try { await saveDraftRuleset(name); } catch (error) { showToast(`Rename failed: ${error.message}`); } });
$("file-management-delete").addEventListener("click", async () => { const ruleset = currentRuleset(); if (!ruleset || ruleset.is_builtin || !window.confirm(`Delete ${ruleset.name}?`)) return; try { await api("/api/file-management/rulesets/delete", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: ruleset.id})}); await refreshFileManagementEditor(); showToast("Ruleset deleted."); } catch (error) { showToast(`Delete failed: ${error.message}`); } });
$("file-management-profile-cancel").addEventListener("click", () => $("file-management-profile-form").classList.add("hidden"));
$("file-management-new-profile").addEventListener("click", () => { $("file-management-profile-form").dataset.profileId = ""; $("file-management-profile-name").value = ""; $("file-management-profile-codec").value = "jpeg-xl"; $("file-management-profile-distance").value = "1.5"; $("file-management-profile-effort").value = "7"; $("file-management-profile-form").classList.remove("hidden"); });
$("file-management-profile-form").addEventListener("submit", async event => { event.preventDefault(); try { await api("/api/file-management/profiles", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: event.currentTarget.dataset.profileId || undefined, name: $("file-management-profile-name").value, codec: $("file-management-profile-codec").value, container: $("file-management-profile-codec").value === "jpeg-xl" ? "jxl" : $("file-management-profile-codec").value === "av1" ? "mp4" : "avif", settings: {distance: Number($("file-management-profile-distance").value), effort: Number($("file-management-profile-effort").value)}})}); $("file-management-profile-form").classList.add("hidden"); await refreshFileManagementEditor(); showToast("Profile saved."); } catch (error) { showToast(`Profile save failed: ${error.message}`); } });
