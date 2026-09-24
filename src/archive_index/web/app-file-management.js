let fileManagement = {profiles: [], rulesets: [], presets: [], current: null, draft: [], dirty: false};

const assetSelectors = [["all", "All"], ["selected", "Selected"], ["undecided", "Undecided"], ["rejected", "Rejected"]];
const representationSelectors = [["all", "All"], ["raw", "RAW"], ["conventional-image", "JPEG/PNG"], ["jpeg", "JPEG"], ["png", "PNG"], ["jxl", "JXL"], ["avif", "AVIF"], ["webp", "WebP"], ["video", "Video"]];
const operationSelectors = [["copy", "Copy"], ["move", "Move"], ["compress", "Compress"], ["delete", "Delete"]];

function fileManagementOptionList(values, selected) { return values.map(([value, label]) => `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(label)}</option>`).join(""); }
function fileManagementStatus(message = "", error = false) { const node = $("file-management-status"); if (!node) return; node.textContent = message; node.classList.toggle("error", error); node.classList.toggle("hidden", !message); }
function currentRuleset() { return fileManagement.rulesets.find(item => item.id === fileManagement.current) || null; }
function newId() { return globalThis.crypto?.randomUUID?.() || `rule-${Date.now()}-${Math.random().toString(16).slice(2)}`; }

function ruleToUi(rule = {}) {
  const match = rule.match || {};
  const action = rule.action || {};
  let representation = match.representation_class || match.format || "all";
  if (match.formats?.length === 2 && match.formats.includes("jpeg") && match.formats.includes("png")) representation = "conventional-image";
  return {id: rule.id || newId(), enabled: true, asset: match.selection_state || "all", representation, operation: action.operation || "delete", profileId: action.profile_id || fileManagement.profiles.find(profile => profile.codec === "jpeg-xl")?.id || "", disposition: action.source_disposition !== "replace", inPlace: action.compress_in_place !== false, destination: action.destination_dir || action.target_template || "", preserve: action.preserve_relative_structure !== false, renameOnConflict: action.rename_on_conflict !== false};
}

function uiToRule(rule) {
  const match = {};
  if (rule.asset !== "all") match.selection_state = rule.asset;
  if (["raw", "conventional-image", "compressed-image", "video"].includes(rule.representation)) match.representation_class = rule.representation;
  if (["jpeg", "png", "jxl", "avif", "webp"].includes(rule.representation)) match.format = rule.representation;
  if (rule.representation === "conventional-image") match.formats = ["jpeg", "png"];
  const action = {operation: rule.operation};
  if (rule.operation === "compress") { action.profile_id = rule.profileId || null; action.source_disposition = rule.disposition ? "keep" : "replace"; action.compress_in_place = rule.inPlace; action.rename_on_conflict = rule.renameOnConflict; if (!rule.inPlace) action.destination_dir = rule.destination; }
  if (["copy", "move"].includes(rule.operation)) { action.destination_dir = rule.destination; action.preserve_relative_structure = rule.preserve; action.rename_on_conflict = rule.renameOnConflict; }
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
}

function markRulesDirty() {
  fileManagement.dirty = true;
  $("file-management-ruleset-kind").textContent = "Custom Ruleset";
  $("file-management-rename").classList.toggle("hidden", !currentRuleset() || currentRuleset().is_builtin);
  $("file-management-delete").classList.toggle("hidden", !currentRuleset() || currentRuleset().is_builtin);
  renderRulesetSelector();
}

function renderFileManagementRules() {
  const profiles = fileManagement.profiles.filter(profile => profile.codec === "jpeg-xl" || profile.codec === "av1" || profile.codec === "avif");
  $("file-management-rules").innerHTML = fileManagement.draft.map((rule, index) => {
    const profileOptions = profiles.map(profile => `<option value="${escapeHtml(profile.id)}"${profile.id === rule.profileId ? " selected" : ""}>${escapeHtml(profile.name)}${profile.codec === "av1" ? " · pending" : ""}</option>`).join("");
    const actionFields = rule.operation === "compress"
      ? `<div class="rule-options rule-options-compress"><label class="rule-field">Profile <select data-rule-field="profileId">${profileOptions}</select></label><label class="rule-option-toggle rule-checkbox"><input type="checkbox" data-rule-field="disposition"${rule.disposition ? " checked" : ""}> <span>Keep source</span></label><label class="rule-option-toggle rule-checkbox"><input type="checkbox" data-rule-field="inPlace"${rule.inPlace ? " checked" : ""}> <span>Compress in place</span></label><label class="rule-option-toggle rule-checkbox"><input type="checkbox" data-rule-field="renameOnConflict"${rule.renameOnConflict ? " checked" : ""}> <span>Rename in case of conflict</span></label><label class="rule-field">Destination <input data-rule-field="destination" value="${escapeHtml(rule.destination)}" placeholder="compressed/"${rule.inPlace ? " disabled" : ""}></label></div>`
      : ["copy", "move"].includes(rule.operation)
        ? `<div class="rule-options rule-options-copy"><label class="rule-field">Destination <input data-rule-field="destination" value="${escapeHtml(rule.destination)}" placeholder="raws/"></label><label class="rule-option-toggle rule-checkbox"><input type="checkbox" data-rule-field="preserve"${rule.preserve ? " checked" : ""}> <span>Recreate source folders inside destination</span></label><label class="rule-option-toggle rule-checkbox"><input type="checkbox" data-rule-field="renameOnConflict"${rule.renameOnConflict ? " checked" : ""}> <span>Rename in case of conflict</span></label><p class="muted rule-help" data-rule-help>${ruleHelperMarkup(rule)}</p></div>`
        : "";
    return `<article class="file-rule-card" data-rule-index="${index}"><div class="rule-card-header"><span class="rule-number">Rule ${index + 1}</span><button class="icon" type="button" data-rule-remove aria-label="Remove rule">×</button></div><div class="rule-line">For <select data-rule-field="representation">${fileManagementOptionList(representationSelectors, rule.representation)}</select> representations of <select data-rule-field="asset">${fileManagementOptionList(assetSelectors, rule.asset)}</select> assets → <select data-rule-field="operation">${fileManagementOptionList(operationSelectors, rule.operation)}</select></div>${actionFields}</article>`;
  }).join("") || `<p class="muted">No rules yet. Add a rule to define the plan.</p>`;
  $("file-management-rules").querySelectorAll("[data-rule-index]").forEach(card => {
    const index = Number(card.dataset.ruleIndex);
    card.querySelectorAll("[data-rule-field]").forEach(control => {
      const update = () => {
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
  try { if (fileManagement.dirty) await saveDraftRuleset("Custom Ruleset"); const data = await api(`/api/file-management/plan?ruleset_id=${encodeURIComponent(fileManagement.current || "")}`); renderFileManagementPlan(data); }
  catch (error) { fileManagementStatus(`Plan analysis failed: ${error.message}`, true); }
}

function renderFileManagementPlan(data) {
  const summary = data.summary || {};
  const cards = [["DELETE", summary.delete, "bytes"], ["COMPRESS", summary.compress, "source_bytes"], ["COPY", summary.copy, "bytes_added"], ["MOVE", summary.move, "bytes_moved"]];
  const delta = Number(summary.estimated_storage_delta_bytes || 0);
  const deltaLabel = delta < 0 ? `Estimated net space freed <strong>${formatBytes(-delta)}</strong>` : delta > 0 ? `Estimated net space added <strong>${formatBytes(delta)}</strong>` : `Estimated net space change <strong>0 B</strong>`;
  const currentFree = summary.available_space_bytes;
  const afterPlan = currentFree == null ? null : Number(currentFree) - delta;
  $("file-management-plan-summary").innerHTML = `<div class="plan-summary-grid">${cards.map(([label, value, bytes]) => { const storageDelta = Number(value?.estimated_storage_delta_bytes || 0); const amount = label === "COMPRESS" ? `${formatBytes(value?.source_bytes || 0)} → ${formatBytes(value?.estimated_output_bytes || 0)}` : formatBytes(value?.[bytes] || 0); const deltaLine = storageDelta ? `<em class="${storageDelta < 0 ? "storage-free" : "storage-add"}">${storageDelta < 0 ? "Frees " : "Adds "}${formatBytes(Math.abs(storageDelta))}</em>` : ""; return `<article class="plan-card"><strong>${label}</strong><span>${Number(value?.file_count || 0).toLocaleString()} ${Number(value?.file_count || 0) === 1 ? "file" : "files"}</span><small>${amount}</small>${deltaLine}</article>`; }).join("")}</div><div class="plan-summary-callouts"><span>${deltaLabel}</span><span>Temporary space upper bound <strong>${formatBytes(summary.temporary_space_upper_bound_bytes || 0)}</strong></span><span>Current free disk space <strong>${formatDiskSpace(currentFree)}</strong></span>${afterPlan == null ? "" : `<span>Estimated free disk space after plan <strong>${formatDiskSpace(afterPlan)}</strong></span>`}<span>Logical assets with no remaining archive representation <strong>${Number(summary.assets_with_no_surviving_representation || 0).toLocaleString()}</strong></span></div>`;
  const blockers = data.blockers || summary.capability_blockers || [];
  $("file-management-plan-blockers").innerHTML = blockers.length ? `<section class="plan-blockers"><h3>Plan blockers</h3>${blockers.map(blocker => `<article class="plan-blocker"><strong>${escapeHtml(blocker.profile_name || "Capability")}</strong><span>${escapeHtml(blocker.reason)}</span><small>${Number(blocker.affected_count || 0).toLocaleString()} planned ${Number(blocker.affected_count || 0) === 1 ? "operation" : "operations"} affected.</small></article>`).join("")}</section>` : "";
  $("file-management-plan-conflicts").innerHTML = data.conflicts?.length ? `<section class="plan-conflicts"><h3>${data.conflicts.length} conflicts must be resolved</h3>${data.conflicts.map(conflict => `<article class="plan-conflict"><strong>${escapeHtml(conflict.filename || "File")}</strong><span>${escapeHtml(conflict.source_relative_path || "")}${conflict.target_relative_path ? ` → ${escapeHtml(conflict.target_relative_path)}` : ""}</span><p>${escapeHtml(conflict.reason)}</p><small>Rule ${escapeHtml(conflict.rule_id || "")}</small></article>`).join("")}</section>` : `<p class="plan-ok">No conflicts detected.</p>`;
  const operations = data.operations || [];
  $("file-management-plan-details").innerHTML = ["delete", "compress", "copy", "move"].map(operation => { const rows = operations.filter(item => item.operation === operation); if (!rows.length) return ""; return `<details class="plan-operation" open><summary>${operation[0].toUpperCase() + operation.slice(1)} · ${rows.length}</summary>${rows.map(item => `<div class="plan-operation-row"><span><strong>${escapeHtml(item.filename || "File")}</strong><small>${escapeHtml(item.source_relative_path || "")}</small></span><span>${escapeHtml(item.target_relative_path || item.profile_name || "Delete")}${item.renamed_to_avoid_conflict ? " · Renamed to avoid conflict" : item.replaces_source_in_place ? " · Replaces source in place" : ""}</span><span>${formatBytes(item.bytes)}${item.destination_status === "already_satisfied" ? " · Already satisfied" : ""}${item.conflicts?.length ? ` · <em>${escapeHtml(item.conflicts.join("; "))}</em>` : ""}${item.blockers?.length ? ` · <em>Blocked: ${escapeHtml(item.blockers.join("; "))}</em>` : ""}</span></div>`).join("")}</details>`; }).join("");
  $("file-management-plan-note").textContent = data.executor?.message || "Execution is not available.";
}

function editProfile(id) { const profile = fileManagement.profiles.find(item => item.id === id); if (!profile) return; const form = $("file-management-profile-form"); form.dataset.profileId = id; $("file-management-profile-name").value = profile.name; $("file-management-profile-codec").value = profile.codec; $("file-management-profile-quality").value = profile.settings?.quality ?? 60; $("file-management-profile-effort").value = profile.settings?.effort ?? 7; form.classList.remove("hidden"); }

$("file-management-button").addEventListener("click", async () => { try { await refreshFileManagementEditor(); $("file-management-dialog").showModal(); } catch (error) { fileManagementStatus(`File Management failed: ${error.message}`, true); } });
$("file-management-close").addEventListener("click", () => $("file-management-dialog").close());
document.querySelectorAll("[data-file-management-tab]").forEach(button => button.addEventListener("click", () => setFileManagementTab(button.dataset.fileManagementTab)));
$("file-management-ruleset-select").addEventListener("change", event => { if (event.target.value === "custom-draft") return; selectRuleset(event.target.value); });
$("file-management-add-rule").addEventListener("click", () => { fileManagement.draft.push(ruleToUi({})); markRulesDirty(); renderFileManagementRules(); });
$("file-management-new-ruleset").addEventListener("click", () => { fileManagement.current = null; fileManagement.draft = []; fileManagement.dirty = true; $("file-management-ruleset-kind").textContent = "Custom Ruleset"; $("file-management-rename").classList.add("hidden"); $("file-management-delete").classList.add("hidden"); renderRulesetSelector(); renderFileManagementRules(); });
$("file-management-analyze").addEventListener("click", () => { setFileManagementTab("plan"); });
$("file-management-save-as").addEventListener("click", async () => { const name = window.prompt("Save ruleset as", "Custom Ruleset"); if (!name) return; try { await saveDraftRuleset(name, {forceNew: true}); } catch (error) { fileManagementStatus(`Ruleset save failed: ${error.message}`, true); } });
$("file-management-rename").addEventListener("click", async () => { const name = window.prompt("Rename ruleset", currentRuleset()?.name || "Custom Ruleset"); if (!name) return; try { await saveDraftRuleset(name); } catch (error) { fileManagementStatus(`Rename failed: ${error.message}`, true); } });
$("file-management-delete").addEventListener("click", async () => { const ruleset = currentRuleset(); if (!ruleset || ruleset.is_builtin || !window.confirm(`Delete ${ruleset.name}?`)) return; try { await api("/api/file-management/rulesets/delete", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: ruleset.id})}); await refreshFileManagementEditor(); } catch (error) { fileManagementStatus(`Delete failed: ${error.message}`, true); } });
$("file-management-profile-cancel").addEventListener("click", () => $("file-management-profile-form").classList.add("hidden"));
$("file-management-new-profile").addEventListener("click", () => { const form = $("file-management-profile-form"); form.dataset.profileId = ""; $("file-management-profile-name").value = ""; $("file-management-profile-codec").value = "jpeg-xl"; $("file-management-profile-quality").value = 60; $("file-management-profile-effort").value = 7; form.classList.remove("hidden"); });
$("file-management-profile-form").addEventListener("submit", async event => { event.preventDefault(); try { await api("/api/file-management/profiles", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: event.currentTarget.dataset.profileId || undefined, name: $("file-management-profile-name").value, codec: $("file-management-profile-codec").value, container: $("file-management-profile-codec").value === "jpeg-xl" ? "jxl" : $("file-management-profile-codec").value === "av1" ? "mp4" : "avif", settings: {quality: Number($("file-management-profile-quality").value), effort: Number($("file-management-profile-effort").value)}})}); $("file-management-profile-form").classList.add("hidden"); await refreshFileManagementEditor(); } catch (error) { fileManagementStatus(`Profile save failed: ${error.message}`, true); } });
