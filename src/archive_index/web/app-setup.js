async function loadHome() {
  document.body.classList.add("home-active");
  $("home-view").classList.remove("hidden");
  $("workspace-view").classList.add("hidden");
  $("setup-view").classList.add("hidden");
  $("setup-header-summary").classList.add("hidden");
  $("configure-workspace").classList.add("hidden");
  $("file-management-button").classList.add("hidden");
  $("index").classList.add("hidden");
  $("problems-button").classList.add("hidden");
  $("workspace-crumb").classList.add("hidden");
  const data = await api("/api/workspaces");
  $("recent-list").innerHTML = data.workspaces.map((workspace) => {
    const available = workspace.available !== false;
    const thumbnail = available && workspace.thumbnail_url ? `<img class="recent-thumb" loading="lazy" src="${escapeHtml(workspace.thumbnail_url)}" alt="${escapeHtml(workspace.name || "Workspace")} thumbnail" onerror="this.remove()">` : "";
    const indexed = available ? `<div class="workspace-indexed"><div>${workspaceAssetCount(workspace.assets)}</div><div class="muted workspace-indexed-time">Indexed on ${escapeHtml(formatHomeTimestamp(workspace.last_indexed) || "—")}</div></div>` : "<div class=\"muted\">Unavailable</div>";
    return `<article class="panel recent-card${thumbnail ? " has-thumbnail" : ""}"><div class="recent-card-content">${thumbnail}<div class="recent-card-copy"><h3>${escapeHtml(workspace.name || "Workspace")}</h3><div class="path">${escapeHtml(workspace.path || "")}</div>${indexed}</div></div><div class="recent-actions">${available ? `<button type="button" data-open="${escapeHtml(workspace.id)}">Open</button>` : ""}<button class="danger-button" type="button" data-remove="${escapeHtml(workspace.id)}">Remove</button></div></article>`;
  }).join("") || `<div class="empty">No recent workspaces yet.</div>`;
  $("recent-list").querySelectorAll("[data-open]").forEach((button) => button.addEventListener("click", () => { location.href = `/?workspace=${encodeURIComponent(button.dataset.open)}`; }));
  $("recent-list").querySelectorAll("[data-remove]").forEach((button) => button.addEventListener("click", () => confirmWorkspaceRemoval(button.dataset.remove)));
}

async function openWorkspace(path, create) {
  try {
    const response = await fetch("/api/workspaces/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not open workspace");
    if (payload.indexed && payload.workspace) location.href = `/?workspace=${encodeURIComponent(payload.workspace)}`;
    else showSetup(payload);
  } catch (error) {
    showToast(`Workspace open failed: ${error.message}`);
  }
}

async function pickWorkspace() {
  if (workspacePickerInFlight) return;
  workspacePickerInFlight = true;
  const button = $("browse-workspace");
  if (button) button.disabled = true;
  try {
    const response = await fetch("/api/workspaces/pick", { method: "POST" });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not choose a folder");
    if (payload.path) $("workspace-path").value = payload.path;
  } catch (error) {
    showToast(`Folder selection failed: ${error.message}`);
  } finally {
    workspacePickerInFlight = false;
    if (button) button.disabled = false;
  }
}

function filenameMarkup(filename) {
  const value = String(filename || "");
  const match = value.match(/^(.*?)(\.[^.]+)$/);
  if (!match) return escapeHtml(value);
  const extension = match[2].toLowerCase();
  const kind = supportedVideoExtensions.includes(extension) ? "video-extension" :
    [".arw", ".cr2", ".cr3", ".dng", ".nef", ".raf", ".rw2"].includes(extension) ? "raw-extension" :
    [".jxl", ".avif", ".webp"].includes(extension) ? "compressed-extension" :
    supportedImageExtensions.includes(extension) ? "image-extension" : "";
  return `${escapeHtml(match[1])}<span class="${kind}">${escapeHtml(match[2])}</span>`;
}

function folderRule(path, configuration) {
  const rules = Object.fromEntries((configuration.folder_rules || []).map((rule) => [rule.path.toLowerCase(), Boolean(rule.included)]));
  return rules[path.toLowerCase()] ?? true;
}

function folderRuleSetting(path, configuration) {
  const rule = (configuration.folder_rules || []).find((candidate) => candidate.path.toLowerCase() === path.toLowerCase());
  return rule ? (rule.included ? "include" : "exclude") : "inherit";
}

function renderSetupFolder(node, configuration) {
  const checked = folderRule(node.path, configuration);
  const planRow = state.setup?.plan?.folder_rows?.find((row) => row.path.toLowerCase() === node.path.toLowerCase());
  const label = node.path ? node.name : "Workspace root";
  const categories = planRow?.categories || {};
  const composition = [
    [categories.jpeg || 0, "image"], [categories.other_image || 0, "image"], [categories.raw || 0, "image"], [categories.video || 0, "video"],
  ].reduce((parts, [count, kind]) => { if (count) parts[kind] = (parts[kind] || 0) + count; return parts; }, {});
  const compositionText = Object.entries(composition).map(([kind, count]) => `${Number(count).toLocaleString()} ${kind}${count === 1 ? "" : "s"}`).join(" · ");
  const supported = Number(planRow?.supported_files ?? node.recognized_files ?? 0);
  const pending = Number(planRow?.pending_files ?? supported);
  const reusable = Number(planRow?.reusable_files ?? 0);
  const pendingEta = Number(planRow?.pending_eta_seconds ?? planRow?.eta_seconds ?? 0);
  const counts = `${supported.toLocaleString()}/${Number(node.direct_files || 0).toLocaleString()} supported${compositionText ? ` · ${compositionText}` : ""} · ${formatBytes(planRow?.supported_bytes ?? node.direct_bytes ?? 0)} · ${pending.toLocaleString()} pending · ${reusable.toLocaleString()} cached · ${pendingEta > 0 ? `+${formatEta(pendingEta)}` : "ready"}`;
  const children = (node.children || []).map((child) => renderSetupFolder(child, configuration)).join("");
  return `<div class="folder-node"><label class="folder-choice"><input type="checkbox" data-folder-path="${escapeHtml(node.path)}"${checked ? " checked" : ""}> <span>${escapeHtml(label)}</span></label><span class="muted folder-node-counts">${counts}</span>${node.error ? `<p class="error">${escapeHtml(node.error)}</p>` : ""}${children ? `<div class="folder-children">${children}</div>` : ""}</div>`;
}

function formatEta(seconds) {
  const numeric = Number(seconds);
  const value = Number.isFinite(numeric) ? Math.max(0, Math.ceil(numeric)) : 0;
  if (!value) return "ready";
  const minutes = Math.floor(value / 60);
  return `${String(minutes).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

function renderSetupPlanData(plan) {
  const files = plan.selected_files ?? plan.files ?? 0;
  const configuration = state.setup.draftConfiguration;
  state.setup.plan = plan;
  const remaining = Math.max(0, Number(plan.estimated_seconds || 0));
  const reusable = Number(plan.indexed_reusable_files || 0);
  $("setup-index-summary").innerHTML = `Indexed: ${reusable.toLocaleString()} / ${files.toLocaleString()} · Estimated indexing time: <span class="setup-eta-value">${escapeHtml(formatEta(remaining))}</span>`;
  const eta = plan.eta_seconds_by_feature || {};
  const qualityEta = Number(eta.rendered_quality || 0) + Number(eta.raw_quality || 0);
  $("setup-quality-eta").textContent = configuration.quality_enabled ? qualityEta > 0 ? `+${formatEta(qualityEta)}` : "ready" : "off";
  const videoEta = Number(configuration.video_quality_enabled ? eta.video_quality || 0 : 0) + Number(configuration.include_videos_in_semantic_search ? plan.embedding_video_estimated_seconds || 0 : 0);
  $("setup-video-eta").textContent = configuration.video_quality_enabled || configuration.include_videos_in_semantic_search ? videoEta > 0 ? `+${formatEta(videoEta)}` : "ready" : "off";
  const semanticEta = Number(eta.semantic_search ?? plan.embedding_estimated_seconds ?? 0);
  $("setup-semantic-eta").textContent = configuration.semantic_search_enabled ? semanticEta > 0 ? `+${formatEta(semanticEta)}` : "ready" : "off";
  $("setup-embedding-status").classList.toggle("hidden", !configuration.semantic_search_enabled);
  const quality = plan.lar_iqa_readiness || plan.rendered_quality_readiness || {};
  const qualityModel = quality.model || {};
  const qualityDetails = $("setup-quality-details");
  const qualityVisible = configuration.rendered_quality_provider === "lar-iqa";
  qualityDetails.classList.toggle("hidden", !qualityVisible);
  if (qualityVisible) {
    qualityDetails.innerHTML = `<strong>LAR-IQA</strong><span>${qualityModel.installed ? "Installed" : "Not installed"} · ${formatBytes(qualityModel.size_bytes)}</span>${qualityModel.installed ? "" : `<button id="setup-quality-install" class="secondary" type="button">Install model</button>`}`;
    $("setup-quality-install")?.addEventListener("click", installQualityModel);
  }
  $("setup-folder-tree").innerHTML = renderSetupFolder(state.setup.analysis.root, configuration);
  bindSetupControls();
  const videoFeaturesAvailable = configuration.quality_enabled || configuration.semantic_search_enabled;
  const videoParticipation = configuration.video_quality_enabled || configuration.include_videos_in_semantic_search;
  $("setup-video-section").classList.toggle("hidden", !videoFeaturesAvailable);
  $("setup-video-participation-label").innerHTML = configuration.quality_enabled && configuration.semantic_search_enabled
    ? '<span>Assess</span> <span class="setup-accent">video</span> <span>quality and</span> <span class="setup-accent">include videos</span> <span>in semantic search</span> <span class="setup-accent">too</span>'
    : configuration.quality_enabled ? '<span>Assess</span> <span class="setup-accent">video</span> <span>quality</span> <span class="setup-accent">too</span>' : '<span class="setup-accent">Include videos</span> <span>in semantic search</span> <span class="setup-accent">too</span>';
  $("setup-video-sampling").classList.toggle("hidden", !videoParticipation);
}

function renderSetupPlan() {
  const request = ++state.setup.planRequest;
  return fetch("/api/workspaces/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: state.setup.path, analysis: state.setup.analysis, configuration: state.setup.draftConfiguration }) })
    .then(async (response) => {
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not plan workspace");
      if (state.setup && request === state.setup.planRequest) renderSetupPlanData(payload.plan);
    })
    .catch(() => {});
}

function updateSetupConfiguration() {
  const configuration = state.setup.draftConfiguration;
  configuration.include_rendered_images = true;
  configuration.include_raw = true;
  configuration.include_images = true;
  configuration.include_videos = true;
  configuration.quality_enabled = $("setup-quality").checked;
  configuration.rendered_quality_provider = configuration.quality_enabled ? "lar-iqa" : "off";
  configuration.raw_quality_provider = configuration.quality_enabled ? "lar-iqa" : "off";
  const videoParticipation = $("setup-video-participation").checked;
  configuration.video_quality_enabled = configuration.quality_enabled && videoParticipation;
  configuration.include_videos_in_semantic_search = $("setup-semantic-search").checked && videoParticipation;
  configuration.video_processing_enabled = configuration.video_quality_enabled || configuration.include_videos_in_semantic_search;
  configuration.video_sampling_fps = Number($("setup-video-fps").value);
  configuration.video_sampling_min_frames = Number($("setup-video-min-frames").value);
  configuration.video_sampling_max_frames = Number($("setup-video-max-frames").value);
  configuration.semantic_search_enabled = $("setup-semantic-search").checked;
  loadEmbeddingModelStatus();
  renderSetupPlan();
}

function bindSetupControls() {
  $("setup-folder-tree").querySelectorAll("[data-folder-path]").forEach((input) => input.addEventListener("change", () => {
    const path = input.dataset.folderPath;
    const configuration = state.setup.draftConfiguration;
    state.setup.draftConfiguration.folder_rules = state.setup.draftConfiguration.folder_rules.filter((rule) => rule.path.toLowerCase() !== path.toLowerCase());
    if (folderRule(path, configuration) !== input.checked) state.setup.draftConfiguration.folder_rules.push({ path, included: input.checked });
    renderSetupPlan();
  }));
  ["setup-quality", "setup-video-participation", "setup-video-fps", "setup-video-min-frames", "setup-video-max-frames", "setup-semantic-search"].forEach((id) => $(id).onchange = updateSetupConfiguration);
}

function showSetup(payload) {
  document.body.classList.remove("home-active");
  state.setup = {
    path: payload.path,
    indexed: Boolean(payload.indexed),
    workspace: payload.workspace,
    analysis: payload.analysis,
    planRequest: 0,
    draftConfiguration: JSON.parse(JSON.stringify(payload.configuration || {})),
  };
  $("home-view").classList.add("hidden");
  $("workspace-view").classList.add("hidden");
  $("setup-view").classList.remove("hidden");
  $("workspace-tabs").classList.add("hidden");
  $("setup-header-summary").classList.remove("hidden");
  $("index").classList.remove("hidden");
  $("configure-workspace").classList.add("hidden");
  $("file-management-button").classList.add("hidden");
  $("workspace-crumb").classList.add("hidden");
  $("setup-title").textContent = "Workspace Setup";
  $("index").textContent = payload.indexed ? "Re-index" : "Index";
  $("setup-apply").textContent = payload.indexed ? "Re-index" : "Index";
  $("setup-path").textContent = payload.path;
  state.setup.draftConfiguration.include_rendered_images = true;
  state.setup.draftConfiguration.include_raw = true;
  state.setup.draftConfiguration.include_images = true;
  state.setup.draftConfiguration.include_videos = true;
  state.setup.draftConfiguration.embedding_provider = "openclip-b16-datacomp-xl";
  const qualityEnabled = state.setup.draftConfiguration.rendered_quality_provider === "lar-iqa" || state.setup.draftConfiguration.raw_quality_provider === "lar-iqa" || state.setup.draftConfiguration.quality_enabled === true;
  $("setup-quality").checked = qualityEnabled;
  $("setup-semantic-search").checked = state.setup.draftConfiguration.semantic_search_enabled !== false;
  $("setup-video-participation").checked = state.setup.draftConfiguration.video_quality_enabled === true || state.setup.draftConfiguration.include_videos_in_semantic_search === true;
  $("setup-video-fps").value = state.setup.draftConfiguration.video_sampling_fps ?? 2;
  $("setup-video-min-frames").value = state.setup.draftConfiguration.video_sampling_min_frames ?? 2;
  $("setup-video-max-frames").value = state.setup.draftConfiguration.video_sampling_max_frames ?? 32;
  state.setup.draftConfiguration.quality_enabled = qualityEnabled;
  state.setup.draftConfiguration.rendered_quality_provider = qualityEnabled ? "lar-iqa" : "off";
  state.setup.draftConfiguration.raw_quality_provider = qualityEnabled ? "lar-iqa" : "off";
  state.setup.draftConfiguration.video_quality_enabled = qualityEnabled && $("setup-video-participation").checked;
  state.setup.draftConfiguration.include_videos_in_semantic_search = $("setup-semantic-search").checked && $("setup-video-participation").checked;
  state.setup.draftConfiguration.video_processing_enabled = state.setup.draftConfiguration.video_quality_enabled || state.setup.draftConfiguration.include_videos_in_semantic_search;
  $("setup-folder-tree").innerHTML = renderSetupFolder(payload.analysis.root, state.setup.draftConfiguration);
  $("setup-quality-details").classList.add("hidden");
  bindSetupControls();
  loadEmbeddingModelStatus();
  renderSetupPlan();
}

async function loadEmbeddingModelStatus() {
  try {
    const response = await fetch("/api/embedding-models");
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not inspect embedding models");
    if (state.setup) {
      const model = payload.models.find(candidate => candidate.provider === "openclip-b16-datacomp-xl");
      if (!model) throw new Error("OpenCLIP model status unavailable");
      $("setup-embedding-status").innerHTML = `<div class="model-card active"><strong>OpenCLIP ViT-B/16 DataComp XL</strong><span>${model.installed ? "Installed" : "Not installed"} · ${formatBytes(model.installed ? model.cache_bytes : model.expected_download_bytes)}</span>${model.installed ? "" : `<button type="button" class="secondary model-install" data-install-model="${model.provider}">Install model</button>`}</div>`;
      $("setup-embedding-status").querySelectorAll("[data-install-model]").forEach(button => button.onclick = async (event) => {
        event.stopPropagation();
        button.disabled = true; button.textContent = "Installing…";
        try {
          const installed = await api("/api/embedding-models/install", {method:"POST", headers:{"Content-Type":"application/json"},body:JSON.stringify({provider:button.dataset.installModel})});
          await loadEmbeddingModelStatus();
          showToast(installed.compatible_embeddings ? "Model installed · Semantic search ready" : "Model installed · Re-index required");
        } catch(error) { button.disabled=false; button.textContent="Retry install"; showToast(error.message); }
      });
    }
  } catch (error) {
    if (state.setup) $("setup-embedding-status").textContent = "Embedding model status unavailable";
    showToast(`Embedding model request failed: ${error.message}`);
  }
}

async function configureWorkspace() {
  state.setupScroll = window.scrollY;
  state.browserAbort?.abort(); clearTimeout(state.searchPoll);
  try {
    const workspace = await api("/api/workspace");
    const response = await fetch("/api/workspaces/analyze", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: workspace.path }) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not analyze workspace");
    showSetup(payload);
  } catch (error) {
    showToast(`Workspace analysis failed: ${error.message}`);
  }
}

async function applySetup() {
  try {
    $("setup-apply").disabled = true;
    const response = await fetch("/api/workspaces/apply", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: state.setup.path, workspace: state.setup.workspace, configuration: state.setup.draftConfiguration }) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not apply workspace setup");
    location.href = `/?workspace=${encodeURIComponent(payload.workspace.id)}`;
  } catch (error) {
    $("setup-status").textContent = "Workspace setup failed";
    showToast(`Workspace setup failed: ${error.message}`);
    $("setup-apply").disabled = false;
  }
}

function cancelSetup() {
  if (state.setup?.workspace) { $("setup-view").classList.add("hidden"); $("setup-header-summary").classList.add("hidden"); $("workspace-view").classList.remove("hidden"); ["index","file-management-button","configure-workspace","workspace-crumb","workspace-tabs"].forEach(id=>$(id).classList.remove("hidden")); window.scrollTo(0,state.setupScroll || 0); loadCurrentView(); }
  else { state.setup = null; loadHome().catch((error) => showToast(`Workspace list failed: ${error.message}`)); }
}

function formatBytes(value) {
  if (value == null) return "Unavailable";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "Unavailable";
  if (Math.abs(numeric) < 1000) return `${Math.round(numeric)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = Math.abs(numeric);
  let unit = -1;
  while (amount >= 1000 && unit < units.length - 1) { amount /= 1000; unit += 1; }
  const precision = amount >= 100 ? 1 : 2;
  return `${numeric < 0 ? "-" : ""}${amount.toFixed(precision).replace(/\.0+$|(?<=\.[0-9])0+$/, "")} ${units[unit]}`;
}

async function confirmWorkspaceRemoval(id) {
  try {
    const info = await fetch("/api/workspaces/remove-info", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace: id }) }).then(async (response) => {
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not inspect workspace");
      return payload;
    });
    const activeMessage = info.active_job ? `<p class="error">Index deletion is unavailable while ${escapeHtml(info.active_job.kind)} is running.</p>` : "";
    const unavailableMessage = info.available === false ? `<p class="error">This workspace folder is unavailable. Only its registry entry can be removed.</p>` : "";
    const deleteIndex = info.available === false ? "" : `<button id="delete-index" class="danger-button" type="button"${info.active_job ? " disabled" : ""}>Delete the index as well</button>`;
    $("remove-workspace-dialog").innerHTML = `<div class="dialog-inner removal-dialog"><div class="dialog-header"><h2 id="remove-workspace-title">Are you sure?</h2><button id="remove-close" class="icon" type="button" aria-label="Close">×</button></div><p>This will only remove the link to this workspace from the app. The actual index (${escapeHtml(formatBytes(info.index_size_bytes))}) in that folder will remain.</p>${unavailableMessage}${activeMessage}<div class="removal-actions">${deleteIndex}<button id="remove-link" class="warning-button" type="button">Yes, only remove the link</button></div></div>`;
    const dialog = $("remove-workspace-dialog");
    dialog.showModal();
    document.body.classList.add("modal-open");
    const close = () => closeDialog(dialog);
    $("remove-close").addEventListener("click", close);
    $("delete-index")?.addEventListener("click", () => finishWorkspaceRemoval(id, true));
    $("remove-link").addEventListener("click", () => finishWorkspaceRemoval(id, false));
  } catch (error) {
    showToast(`Workspace removal check failed: ${error.message}`);
  }
}

async function finishWorkspaceRemoval(id, deleteIndex) {
  try {
    const response = await fetch("/api/workspaces/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace: id, delete_index: deleteIndex }) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Could not remove workspace");
    closeDialog($("remove-workspace-dialog"));
    await loadHome();
  } catch (error) {
    showToast(`Workspace removal failed: ${error.message}`);
  }
}
