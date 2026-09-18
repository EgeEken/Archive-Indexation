const query = new URLSearchParams(location.search);
const MAX_VIEWER_ZOOM = 40;
let workspacePickerInFlight = false;
const state = {
  workspace: query.get("workspace"),
  total: 0,
  items: [],
  viewerItems: [],
  viewMode: ["groups", "cloud"].includes(query.get("view")) ? query.get("view") : "gallery",
  searchState: "available", renderKeys: {}, browserAbort: null, searchPoll: null, semanticPending: false, semanticEnabled: false, auto: "all", manual: "all", layout: "", folders: null, folderPaths: [], folderCounts: {},
  scrollPositions: {}, windowStart: 0, windowRows: [], windowColumns: 1, windowHeight: 360, windowHasNext: false,
  collapsed: localStorage.getItem("archive-sidebar-collapsed") === "true",
  selectionFilter: query.get("selection") || "all",
  groupPage: Number(query.get("group_page") || 1),
  groupPageSize: 10,
  focusGroup: query.get("focus_group"),
  viewerIndex: -1,
  viewerZoom: 1,
  viewerPanX: 0,
  viewerPanY: 0,
  viewerSmooth: true,
  viewerInfoOpen: false,
  viewerContext: "gallery",
  viewerGroupId: null,
  viewerDetail: null,
  activeJobId: null,
  assetRequest: 0,
  jobsRequest: 0,
  groupRequest: 0,
  galleryLoading: false,
  galleryRequestInFlight: false,
  similar: null,
  dragging: false,
  viewerClickSuppressed: false,
  setup: null,
};
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[char]));
function showToast(message) {
  const region = $("viewer")?.open ? $("viewer-toast-region") : $("toast-region");
  if (!region) return;
  const normalized = String(message || "Unexpected error").replace(/\s+/g, " ").slice(0, 300);
  if ([...region.children].some((toast) => toast.dataset.message === normalized)) return;
  const toast = document.createElement("div");
  toast.className = "toast";
  toast.dataset.message = normalized;
  const text = document.createElement("span");
  text.textContent = normalized;
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "Dismiss";
  close.addEventListener("click", () => toast.remove());
  toast.append(text, close);
  region.appendChild(toast);
}
const labels = { offline: "Offline", unsupported: "Unsupported", failed: "Processing failed", processing: "Processing" };
const reviewFilters = ["all", "representatives", "recommended", "selected", "rejected", "undecided"];
const supportedImageExtensions = [".arw", ".avif", ".cr2", ".cr3", ".dng", ".heic", ".heif", ".jpeg", ".jpg", ".jxl", ".nef", ".png", ".raf", ".rw2", ".webp"];
const supportedVideoExtensions = [".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"];

function apiPath(path) {
  if (!state.workspace || !path.startsWith("/api/") || path === "/api/workspaces") return path;
  const url = new URL(path, location.origin);
  url.searchParams.set("workspace", state.workspace);
  return url.pathname + url.search;
}

async function api(path, options) {
  const response = await fetch(apiPath(path), options);
  let payload = {};
  try { payload = await response.json(); } catch {}
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

function syncUrl() {
  const params = filterParams();
  if (state.workspace) params.set("workspace", state.workspace);
  params.set("view", state.viewMode);
  history.replaceState(null, "", `${location.pathname}?${params}`);
}

function filterParams() {
  const params = new URLSearchParams({q: $("search").value.trim(), sort_by: $("sort-by").value,
    direction: $("direction").value, threshold: $("similarity-threshold").value,
    media_type: $("media-type").value, layout: state.layout, auto: state.auto, manual: state.manual,
    semantic: state.semanticEnabled && !state.semanticPending ? "1" : "0", async: "1"});
  if (state.folders !== null) params.set("folders", JSON.stringify([...state.folders].sort()));
  return params;
}

function formatCapture(value) {
  if (!value) return "";
  const text = String(value).replace("T", " ");
  const match = text.match(/^(.*:\d{2})(\.\d+)(.*)$/);
  const fraction = match ? match[2].slice(1, 3).replace(/0+$/, "") : "";
  const display = (match ? `${match[1]}${fraction ? `.${fraction}` : ""}${match[3]}` : text).replace(/(?:Z|[+-]\d{2}:?\d{2})$/, "");
  return display;
}

function renderReviewFilters() {
  document.querySelectorAll("[data-selection-filter]").forEach((button) => {
    const active = button.dataset.selectionFilter === state.selectionFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function issueMarkup(issues) {
  return (issues || []).map((issue) => `<span class="badge">${escapeHtml(labels[issue] || issue)}</span>`).join("");
}

function qualityColor(score) {
  const value = Math.max(0, Math.min(1, Number(score)));
  const stops = [[0.4, [128, 48, 64]], [0.7, [205, 185, 222]], [1, [169, 224, 239]]];
  if (value <= stops[0][0]) return `rgb(${stops[0][1].join(", ")})`;
  const upper = value <= stops[1][0] ? stops[1] : stops[2];
  const lower = upper === stops[1] ? stops[0] : stops[1];
  const progress = (value - lower[0]) / (upper[0] - lower[0]);
  return `rgb(${lower[1].map((channel, index) => Math.round(channel + (upper[1][index] - channel) * progress)).join(", ")})`;
}

function countLabel(count, singular, plural = `${singular}s`) {
  const value = Number(count);
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}

function scoreMarkup(score) {
  return score == null ? "" : `<span class="quality-chip" style="--quality-color: ${qualityColor(score)}">Quality: ${Number(score).toFixed(2)}</span>`;
}

function similarityMarkup(item) {
  if (item.similarity == null) return "";
  const timestamp = item.best_match_timestamp == null ? "" : ` · ${Number(item.best_match_timestamp).toFixed(1)} s`;
  const value = Math.max(0, Math.min(1, Number(item.similarity)));
  const kind = item.similarity_kind || (item.best_match_timestamp == null ? "image" : "text");
  const progress = kind === "text" ? Math.max(0, Math.min(1, (value - 0.18) / 0.17)) : value;
  const from = kind === "text" ? [217, 105, 105] : [217, 105, 105];
  const middle = [231, 198, 85];
  const to = [91, 190, 118];
  const color = value <= (kind === "text" ? 0.18 : 0.5)
    ? `rgb(${from.map((channel, index) => Math.round(channel + (middle[index] - channel) * (value / (kind === "text" ? 0.18 : 0.5)))).join(", ")})`
    : `rgb(${middle.map((channel, index) => Math.round(channel + (to[index] - channel) * progress)).join(", ")})`;
  return `<span class="similarity-chip" style="--similarity-color: ${color}">Similarity: ${Number(item.similarity).toFixed(2)}${timestamp}</span>`;
}

function selectionState(item) {
  const decision = item.user_decision || "undecided";
  if (decision === "selected" || decision === "rejected") return decision;
  if (item.auto_recommended) return "recommended";
  return item.is_representative ? "representative" : "";
}

function selectionStateMarkup(item) {
  const automatic = item.auto_recommended
    ? '<span class="state-tag recommended">Recommended</span>'
    : item.is_representative
      ? '<span class="state-tag representative">Representative</span>'
      : '';
  return `${automatic}${item.filename_match ? '<span class="state-tag">Filename match</span>' : ''}`;
}

function selectionActionsMarkup(item) {
  const decision = item.user_decision || "undecided";
  return `<div class="selection-actions" data-review="${decision}"><button type="button" class="selection-button select${decision === "selected" ? " active" : ""}" data-decision="selected" data-asset-id="${escapeHtml(item.asset_id)}">${decision === "selected" ? "Selected" : "Select"}</button><button type="button" class="selection-button reject${decision === "rejected" ? " active" : ""}" data-decision="rejected" data-asset-id="${escapeHtml(item.asset_id)}">${decision === "rejected" ? "Rejected" : "Reject"}</button></div>`;
}

function bindSelectionButtons(root) {
  root.querySelectorAll("[data-decision]").forEach((button) => button.addEventListener("click", (event) => {
    event.stopPropagation();
    setDecision(button.dataset.assetId, button.dataset.decision);
  }));
}

async function setDecision(assetId, decision) {
  try {
    const current = [...state.items, ...state.viewerItems, ...(state.groupItems || [])].find((item) => item?.asset_id === assetId)?.user_decision || "undecided";
    const nextDecision = decision === current && decision !== "undecided" ? "undecided" : decision;
    const payload = await api(`/api/assets/${encodeURIComponent(assetId)}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision: nextDecision }),
    });
    const update = (item) => {
      if (item?.asset_id === assetId) {
        item.user_decision = payload.user_decision;
        item.auto_recommended = payload.auto_recommended;
        item.recommendation_run_id = payload.recommendation_run_id;
      }
    };
    state.renderKeys = {};
    state.items.forEach(update);
    state.viewerItems.forEach(update);
    (state.groupItems || []).forEach(update);
    if ($("viewer").open) renderViewer();
    if (state.viewMode === "groups") await loadGroups(); else await loadAssets();
  } catch (error) {
    showToast(`Review update failed: ${error.message}`);
  }
}

function renderCard(item, index) {
  const media = item.thumbnail_url
    ? `<img class="thumb" loading="lazy" src="${item.thumbnail_url}" alt="${escapeHtml(item.filename)}">`
    : `<div class="placeholder">Preview unavailable</div>`;
  const date = item.capture_time?.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2})/);
  const capture = date ? `<div class="capture">${date[3]}/${date[2]}/${date[1]} · ${date[4]}</div>` : "";
  const automaticClass = item.auto_recommended ? " recommended-card" : item.is_representative ? " representative-card" : "";
  return `<article class="photo-card${automaticClass}" tabindex="0" data-index="${index}" aria-label="View ${escapeHtml(item.filename)}"><div class="photo-frame">${media}</div><button class="info-button" type="button" data-info="${index}" aria-label="Details for ${escapeHtml(item.filename)}">ⓘ</button><div class="photo-card-body"><div class="filename" title="${escapeHtml(item.filename)}">${filenameMarkup(item.filename)}</div><div class="card-metrics">${scoreMarkup(item.quality_score)}${similarityMarkup(item)}</div>${capture}<div class="card-state">${selectionStateMarkup(item)}</div><div class="issues">${issueMarkup(item.issues)}</div><div class="card-actions">${selectionActionsMarkup(item)}</div></div></article>`;
}

async function loadHome() {
  $("home-view").classList.remove("hidden");
  $("workspace-view").classList.add("hidden");
  $("setup-view").classList.add("hidden");
  $("setup-header-summary").classList.add("hidden");
  $("configure-workspace").classList.add("hidden");
  $("index").classList.add("hidden");
  $("problems-button").classList.add("hidden");
  $("workspace-crumb").classList.add("hidden");
  const data = await api("/api/workspaces");
  $("recent-list").innerHTML = data.workspaces.map((workspace) => `<article class="panel recent-card"><div><h3>${escapeHtml(workspace.name || "Workspace")}</h3><div class="path">${escapeHtml(workspace.path || "")}</div><div class="muted">${workspace.available === false ? "Unavailable" : `${workspace.assets ?? 0} indexed assets${workspace.last_indexed ? ` · indexed ${escapeHtml(workspace.last_indexed)}` : ""}`}</div></div><div class="recent-actions">${workspace.available === false ? "" : `<button type="button" data-open="${escapeHtml(workspace.id)}">Open</button>`}<button class="danger-button" type="button" data-remove="${escapeHtml(workspace.id)}">Remove</button></div></article>`).join("") || `<div class="empty">No recent workspaces yet.</div>`;
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
  const counts = `${supported.toLocaleString()}/${Number(node.direct_files || 0).toLocaleString()} supported${compositionText ? ` · ${compositionText}` : ""} · ${formatBytes(planRow?.supported_bytes ?? node.direct_bytes ?? 0)} · ${pending.toLocaleString()} pending · ${reusable.toLocaleString()} cached · +${formatEta(planRow?.pending_eta_seconds ?? planRow?.eta_seconds ?? 0)}`;
  const children = (node.children || []).map((child) => renderSetupFolder(child, configuration)).join("");
  return `<div class="folder-node"><label class="folder-choice"><input type="checkbox" data-folder-path="${escapeHtml(node.path)}"${checked ? " checked" : ""}> <span>${escapeHtml(label)}</span></label><span class="muted folder-node-counts">${counts}</span>${node.error ? `<p class="error">${escapeHtml(node.error)}</p>` : ""}${children ? `<div class="folder-children">${children}</div>` : ""}</div>`;
}

function formatEta(seconds) {
  const value = Math.max(0, Math.ceil(Number(seconds || 0)));
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
  $("setup-index-summary").textContent = `Indexed: ${reusable.toLocaleString()} / ${files.toLocaleString()} · Estimated indexing time: ${formatEta(remaining)}`;
  const eta = plan.eta_seconds_by_feature || {};
  const qualityEta = Number(eta.rendered_quality || 0) + Number(eta.raw_quality || 0);
  $("setup-quality-eta").textContent = configuration.quality_enabled ? `+${formatEta(qualityEta)}` : "off";
  const videoEta = Number(configuration.video_quality_enabled ? eta.video_quality || 0 : 0) + Number(configuration.include_videos_in_semantic_search ? plan.embedding_video_estimated_seconds || 0 : 0);
  $("setup-video-eta").textContent = configuration.video_quality_enabled || configuration.include_videos_in_semantic_search ? `+${formatEta(videoEta)}` : "off";
  $("setup-semantic-eta").textContent = configuration.semantic_search_enabled ? `+${formatEta(eta.semantic_search ?? plan.embedding_estimated_seconds)}` : "off";
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
    ? '<span class="setup-accent">Assess</span> video <span class="setup-accent">quality and</span> include videos <span class="setup-accent">in semantic search</span> too'
    : configuration.quality_enabled ? '<span class="setup-accent">Assess</span> video <span class="setup-accent">quality</span> too' : 'Include videos <span class="setup-accent">in semantic search</span> too';
  $("setup-video-sampling").classList.toggle("hidden", !videoParticipation);
}

function renderSetupPlan() {
  const configuration = state.setup.draftConfiguration;
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
  if (state.setup?.workspace) { $("setup-view").classList.add("hidden"); $("setup-header-summary").classList.add("hidden"); $("workspace-view").classList.remove("hidden"); ["index","configure-workspace","workspace-crumb","workspace-tabs"].forEach(id=>$(id).classList.remove("hidden")); window.scrollTo(0,state.setupScroll || 0); if(state.viewMode === "groups") loadGroups(); else if(state.viewMode === "gallery") loadAssets(); }
  else { state.setup = null; loadHome().catch((error) => showToast(`Workspace list failed: ${error.message}`)); }
}

function formatBytes(value) {
  if (value == null) return "Unavailable";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
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

async function loadWorkspace() {
  $("home-view").classList.add("hidden");
  $("setup-view").classList.add("hidden");
  $("setup-header-summary").classList.add("hidden");
  $("workspace-view").classList.remove("hidden");
  ["index", "configure-workspace", "workspace-crumb", "workspace-explorer", "workspace-tabs"].forEach(id => $(id).classList.remove("hidden"));
  state.browserAbort?.abort(); clearTimeout(state.searchPoll); state.assetRequest++; state.jobsRequest++; state.groupRequest++;
  state.items = []; state.total = 0; state.windowStart = 0; state.windowHasNext = false; state.groupItems = [];
  state.activeJobId = null; state.browserRevision = null; state.viewerItems = []; state.viewerDetail = null;
  state.renderKeys = {}; state.galleryRequestInFlight = false; state.galleryLoading = true; state.semanticPending = false; state.folders = null;
  $("gallery").innerHTML = ""; $("groups-list").innerHTML = "";
  const data = await api("/api/workspace");
  $("workspace-crumb").textContent = data.name;
  const folderData = await api("/api/folders");
  state.folderPaths = folderData.folders;
  state.folderCounts = folderData.counts || {};
  $("search").value = query.get("q") || query.get("text") || "";
  state.auto=query.get("auto") || "all"; state.manual=query.get("manual") || "all"; state.layout=query.get("layout") || "";
  $("media-type").value=query.get("media_type") || "";
  if(query.has("folders")) state.folders=new Set(JSON.parse(query.get("folders")));
  normalizeFolders(); renderFolderTree();
  $("sort-by").value = query.get("sort_by") || "capture_time";
  $("direction").value = query.get("direction") || "desc";
  $("similarity-threshold").value = localStorage.getItem(`archive-threshold-${state.workspace}`) || "0.20";
  $("similarity-value").textContent = Number($("similarity-threshold").value).toFixed(2);
  const config = await api("/api/workspace/configuration");
  state.semanticEnabled = Boolean(config.configuration?.semantic_search_enabled);
  renderSemanticControls();
  $("recommendation-threshold").value = config.configuration?.recommendation_threshold ?? 0.70;
  $("recommendation-value").textContent = Number($("recommendation-threshold").value).toFixed(2);
  document.body.classList.toggle("sidebar-collapsed", state.collapsed);
  $("sidebar-reopen").classList.toggle("hidden", !state.collapsed);
  renderFilterButtons();
  setViewMode(state.viewMode, false);
  await loadJobs();
  if (state.viewMode === "groups") await loadGroups(); else await loadAssets();
  setTimeout(prepareSearch, 250);
}

async function installQualityModel() {
  const button = $("setup-quality-install");
  if (!button) return;
  button.disabled = true;
  button.textContent = "Installing…";
  try {
    await api("/api/quality-model/install", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    renderSetupPlan();
  } catch (error) {
    button.disabled = false;
    button.textContent = "Retry install";
    showToast(`Model installation failed: ${error.message}`);
  }
}

function showSearchStatus(status, data = null) {
  state.searchState = status.state;
  if(status.provider) state.searchProvider=status.provider;
  $("search-status").dataset.state = status.state;
  $("search-status").textContent = status.state === "complete" ? (status.message || "Semantic search complete") : (status.message || "");
  if (data) {
    const shown = data.media_shown ?? data.total ?? 0;
    const total = data.workspace_total ?? data.media_total ?? shown;
    const queryActive = data.query_active ?? Boolean($("search")?.value?.trim?.());
    const filename = queryActive && data.filename_matches != null
      ? ` · ${data.filename_matches} filename ${data.filename_matches === 1 ? "match" : "matches"}`
      : "";
    $("search-count").textContent = `${shown} / ${total} media shown${filename}`;
  }
}

async function prepareSearch() {
  try {
    if (!$("search").value.trim() && state.searchState === "available") showSearchStatus({state:"loading", message:`Preparing semantic search · ${state.searchProvider || "OpenCLIP"}…`});
    const status = await api("/api/search/prepare", {method:"POST"});
    if (!$("search").value.trim()) showSearchStatus(status);
    if (status.state === "loading") setTimeout(prepareSearch, 150);
  } catch (error) { if (!$("search").value.trim()) showSearchStatus({state:"failed",message:"Search unavailable"}); showToast(`Search preparation failed: ${error.message}`); }
}

async function browserData(params) {
  state.browserAbort?.abort(); clearTimeout(state.searchPoll);
  const controller = new AbortController(); state.browserAbort = controller;
  if (params.get("q") && params.get("semantic") !== "0") {
    const loading = ["available", "loading"].includes(state.searchState);
    showSearchStatus({state:loading ? "loading" : "searching",message:loading ? `Loading ${state.searchProvider || "OpenCLIP"}…` : "Searching…"});
  }
  const data = await api(`/api/browser?${params}`, {signal:controller.signal});
  if (controller.signal.aborted) throw new DOMException("Search replaced", "AbortError");
  state.searchProvider = data.search.provider;
  showSearchStatus(data.search, data);
  if (["loading","searching"].includes(data.search.state) && params.get("semantic") !== "0") {
    state.searchPoll = setTimeout(() => {
      if (!$("workspace-view").classList.contains("hidden")) (state.viewMode === "groups" ? loadGroups() : loadAssets());
    }, 100);
  }
  return data;
}

function renderGalleryWindow(data, offset, columns, height) {
  const loading = state.galleryLoading || state.semanticPending || ["loading", "searching"].includes(data.search.state);
  const before = Math.floor(offset / columns) * height;
  const after = Math.max(0, Math.ceil(Math.max(0, (data.total || 0) - offset - data.items.length) / columns) * height);
  const spinner = loading || data.has_next ? `<div class="loading-state" role="status"><span class="spinner" aria-hidden="true"></span>${loading ? "Loading media…" : "Loading more media…"}</div>` : "";
  $("gallery").style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
  $("gallery").innerHTML = data.items.length
    ? `<div class="window-spacer" style="height:${before}px"></div>${data.items.map(renderCard).join("")}<div class="window-spacer" style="height:${after}px"></div>${loading || data.has_next ? spinner : ""}`
    : loading
      ? spinner
      : `<div class="empty">No media match these filters.</div>`;
  $("gallery").style.setProperty("--card-height", `${height - 12}px`);
  $("gallery").setAttribute("aria-busy", String(loading));
  bindGalleryCards();
}

async function loadAssets(requestedOffset = null) {
  if (state.galleryRequestInFlight) return;
  state.galleryRequestInFlight = true;
  const requestId = ++state.assetRequest;
  try {
    const width = $("gallery").clientWidth || 900;
    const columns = Math.max(1, Math.floor((width + 12) / 210));
    const height = Math.floor((width - (columns - 1) * 12) / columns) + 184;
    const top = $("gallery").getBoundingClientRect().top + window.scrollY;
    const row = Math.max(0, Math.floor((window.scrollY - top) / height) - 2);
    const loadedEnd = state.windowStart + state.items.length;
    const nextWindow = requestedOffset != null && requestedOffset >= loadedEnd && state.windowHasNext;
    const overlapRows = Math.ceil(window.innerHeight / height) + 2;
    const offset = requestedOffset == null ? row * columns : nextWindow ? Math.max(0, requestedOffset - overlapRows * columns) : Math.max(0, requestedOffset);
    const limit = Math.min(180, (Math.ceil(window.innerHeight / height) + 5) * columns);
    const params = filterParams(); params.set("offset", offset); params.set("limit", limit);
    const renderKey = `${params}|${columns}`;
    if (state.renderKeys.gallery === renderKey) return;
    const data = await browserData(params);
    if (requestId !== state.assetRequest) return;
    const pending = ["loading", "searching"].includes(data.search.state);
    const displayedItems = pending && !data.items.length ? state.items : data.items;
    const displayedOffset = pending && !data.items.length ? state.windowStart : offset;
    const displayedTotal = pending && !data.items.length ? Math.max(state.total, data.total) : data.total;
    const displayedHasNext = pending && !data.items.length ? state.windowHasNext : data.has_next;
    state.items = displayedItems; state.total = displayedTotal;
    state.galleryLoading = ["loading", "searching"].includes(data.search.state);
    state.windowStart = displayedOffset; state.windowColumns = columns; state.windowHeight = height; state.windowHasNext = displayedHasNext;
    if (!["loading","searching","failed"].includes(data.search.state)) state.renderKeys.gallery = renderKey;
    const renderScrollY = window.scrollY;
    renderGalleryWindow({...data, items: displayedItems, total: displayedTotal, has_next: displayedHasNext}, displayedOffset, columns, height);
    if (window.scrollY !== renderScrollY) window.scrollTo(0, renderScrollY);
    syncUrl();
  } catch (error) { if(error.name !== "AbortError") { showSearchStatus({state:"failed",message:"Search unavailable"}); showToast(`Gallery request failed: ${error.message}`); } }
  finally { if (requestId === state.assetRequest) state.galleryRequestInFlight = false; }
}

function reflowGallery() {
  if (state.viewMode !== "gallery" || !state.items.length || $("gallery").classList.contains("hidden")) return;
  const width = $("gallery").clientWidth || 900;
  const columns = Math.max(1, Math.floor((width + 12) / 210));
  const height = Math.floor((width - (columns - 1) * 12) / columns) + 184;
  state.windowColumns = columns;
  state.windowHeight = height;
  renderGalleryWindow({
    items: state.items,
    total: state.total,
    has_next: state.windowHasNext,
    search: {state: state.galleryLoading ? "loading" : "complete"},
  }, state.windowStart, columns, height);
}

function bindGalleryCards() {
  $("gallery").querySelectorAll(".photo-card").forEach((card) => {
    card.addEventListener("click", (event) => { if (!event.target.closest("button")) showViewer(Number(card.dataset.index), state.items, { mode: "gallery" }); });
    card.addEventListener("keydown", (event) => { if ((event.key === "Enter" || event.key === " ") && event.target === card) { event.preventDefault(); showViewer(Number(card.dataset.index), state.items, { mode: "gallery" }); } });
  });
  $("gallery").querySelectorAll("[data-info]").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); showDetails(state.items[Number(button.dataset.info)].asset_id, { mode: "gallery", items: state.items, index: Number(button.dataset.info) }); }));
  bindSelectionButtons($("gallery"));
}

function resetViewerZoom() {
  state.viewerZoom = 1;
  state.viewerPanX = 0;
  state.viewerPanY = 0;
}

function showViewer(index, items = state.items, context = { mode: "gallery" }) {
  state.viewerItems = items;
  if (!state.viewerItems[index]) return;
  state.viewerIndex = index;
  state.viewerContext = context.mode || "gallery";
  state.viewerStart = context.start ?? (state.viewerContext === "gallery" ? state.windowStart : 0);
  state.viewerGroupId = context.groupId || state.viewerItems[index].current_group_id || null;
  state.viewerInfoOpen = false;
  state.viewerDetail = null;
  if (!context.keepSimilar) {
    $("similar-gallery").classList.add("hidden");
    state.similar = null;
    state.similarSource = null;
    $("viewer").onscroll = null;
  }
  resetViewerZoom();
  renderViewer();
  if (!$("viewer").open) { $("viewer").showModal(); document.body.classList.add("modal-open"); }
}

function renderViewer() {
  const item = state.viewerItems[state.viewerIndex];
  if (!item) return;
  $("viewer-title").innerHTML = filenameMarkup(item.filename);
  $("viewer-count").textContent = `${state.viewerStart + state.viewerIndex + 1} of ${state.viewerContext === "gallery" ? state.total : state.viewerItems.length}`;
  $("viewer-previous").disabled = state.viewerIndex <= 0 && (state.viewerContext !== "gallery" || state.viewerStart === 0);
  $("viewer-next").disabled = state.viewerContext === "gallery" ? state.viewerStart + state.viewerIndex >= state.total - 1 : state.viewerIndex >= state.viewerItems.length - 1;
  $("viewer-selection").innerHTML = selectionActionsMarkup(item);
  $("viewer-similar").classList.toggle("hidden", item.media_type === "video");
  $("viewer-similar").textContent = state.similar ? "Close similar images" : "Show similar images";
  bindSelectionButtons($("viewer-selection"));
  $("viewer-grouping").classList.toggle("hidden", item.media_type === "video" || !state.viewerGroupId);
  $("smooth-control").classList.toggle("hidden", item.media_type === "video");
  $("viewer-smooth").checked = state.viewerSmooth;
  stopViewerMedia();
  $("viewer-media").innerHTML = "";
  if (!item.display_url && !item.original_url) {
    $("viewer-media").innerHTML = `<div class="viewer-error">${item.issues?.includes("offline") ? "This media is offline." : "This media cannot currently be rendered."}</div>`;
  } else {
    const media = item.media_type === "video" ? document.createElement("video") : document.createElement("img");
    media.className = "viewer-media";
    media.alt = item.filename;
    media.controls = item.media_type === "video";
    media.draggable = false;
    media.src = item.display_url || item.original_url;
    media.addEventListener("error", () => {
      if (!media.isConnected || !media.getAttribute("src")) return;
      const explanation = item.media_type === "video" && item.codec ? `This source video codec (${item.codec}) is not supported by the browser yet.` : item.media_type === "video" ? "This source video codec is not supported by the browser yet." : "This media could not be rendered.";
      $("viewer-media").innerHTML = `<div class="viewer-error">${escapeHtml(explanation)}</div>`;
    });
    $("viewer-media").appendChild(media);
    if (item.media_type === "image") media.addEventListener("load", () => applyViewerTransform(media));
    applyViewerTransform(media);
  }
  $("viewer-details").classList.toggle("hidden", !state.viewerInfoOpen);
  $("viewer-stage").classList.toggle("info-open", state.viewerInfoOpen);
  if (state.viewerInfoOpen) loadViewerDetails();
  requestAnimationFrame(() => applyViewerTransform($("viewer-media").querySelector("img.viewer-media")));
}

function applyViewerTransform(media) {
  if (!media || media.tagName !== "IMG") return;
  clampViewerPan(media);
  media.style.transform = `translate3d(${state.viewerPanX}px, ${state.viewerPanY}px, 0) scale(${state.viewerZoom})`;
  media.style.imageRendering = state.viewerSmooth ? "auto" : "pixelated";
  $("viewer-media-pane").classList.toggle("zoomed", state.viewerZoom > 1);
  $("viewer-media-pane").classList.toggle("dragging", state.dragging);
}

function clampViewerPan(media) {
  if (!media) return;
  const viewport = $("viewer-media-pane").getBoundingClientRect();
  const baseWidth = media.offsetWidth || media.getBoundingClientRect().width / Math.max(state.viewerZoom, 1);
  const baseHeight = media.offsetHeight || media.getBoundingClientRect().height / Math.max(state.viewerZoom, 1);
  const maxX = Math.max(0, (baseWidth * state.viewerZoom - viewport.width) / 2);
  const maxY = Math.max(0, (baseHeight * state.viewerZoom - viewport.height) / 2);
  state.viewerPanX = Math.max(-maxX, Math.min(maxX, state.viewerPanX));
  state.viewerPanY = Math.max(-maxY, Math.min(maxY, state.viewerPanY));
  if (maxX === 0) state.viewerPanX = 0;
  if (maxY === 0) state.viewerPanY = 0;
}

async function moveViewer(delta) {
  let next = state.viewerIndex + delta;
  if (state.viewerContext === "gallery" && (next < 0 || next >= state.viewerItems.length)) {
    const offset = next < 0 ? Math.max(0, state.viewerStart - 60) : state.viewerStart + state.viewerItems.length;
    if (offset < 0 || offset >= state.total) return;
    const params = filterParams(); params.set("offset",offset); params.set("limit",60);
    try {
      const data = await api(`/api/browser?${params}`);
      if (!data.items.length) return;
      if (next < 0) { next = data.items.length - 1; state.viewerItems = [...data.items, ...state.viewerItems]; state.viewerStart = offset; }
      else state.viewerItems = [...state.viewerItems, ...data.items];
    } catch(error) { $("viewer-title").textContent=error.message;return; }
  }
  if (next < 0 || next >= state.viewerItems.length) return;
  state.viewerIndex = next;
  resetViewerZoom();
  state.viewerDetail = null;
  renderViewer();
}

function stopViewerMedia() {
  $("viewer-media").querySelectorAll("video").forEach(video => {
    video.pause(); video.removeAttribute("src"); video.load();
  });
}

function closeDialog(dialog) {
  if (dialog.id === "viewer") {
    stopViewerMedia();
    state.similar = null;
    state.similarSource = null;
    dialog.onscroll = null;
    $("similar-gallery").classList.add("hidden");
    $("viewer-toast-region").replaceChildren();
  }
  if (dialog.open) dialog.close();
  if (!["viewer", "details", "problems-dialog", "offline-dialog", "remove-workspace-dialog"].some((id) => $(id).open)) document.body.classList.remove("modal-open");
}

function bindBackdropClose(dialog) {
  let pressedOutside = false;
  const isBackdrop = (event) => {
    if (event.target !== dialog) return false;
    const rect = dialog.getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
  };
  dialog.addEventListener("pointerdown", (event) => { pressedOutside = isBackdrop(event); });
  dialog.addEventListener("pointerup", (event) => {
    if (pressedOutside && isBackdrop(event)) closeDialog(dialog);
    pressedOutside = false;
  });
  dialog.addEventListener("pointercancel", () => { pressedOutside = false; });
}

async function showDetails(assetId, context = { mode: "gallery", items: state.items, index: 0 }) {
  try {
    const asset = await api(`/api/assets/${encodeURIComponent(assetId)}`);
    asset._context = context;
    $("details").innerHTML = renderDetails(asset, { standalone: true, showThumbnail: true });
    $("details").showModal();
    document.body.classList.add("modal-open");
    $("details-close").addEventListener("click", () => closeDialog($("details")));
    $("details").querySelector("[data-find-similar]")?.addEventListener("click", () => showSimilar(asset.asset_id));
    $("details").querySelector("[data-detail-thumbnail]")?.addEventListener("click", () => {
      closeDialog($("details"));
      const item = context.items?.[context.index] || assetToViewerItem(asset);
      showViewer(context.index || 0, context.items || [item], context);
    });
  } catch (error) {
    showToast(`Details request failed: ${error.message}`);
  }
}

function renderSimilarResults() {
  const displayFilename = typeof globalThis.filenameMarkup === "function" ? globalThis.filenameMarkup : escapeHtml;
  const filenameMarkup = displayFilename;
  const similar = state.similar;
  const section = $("similar-gallery");
  if (!similar) return;
  const strong = similar.strongCount;
  const heading = similar.loading && !similar.items.length ? "Similar images" : strong ? countLabel(strong, "similar image found", "similar images found") : "No strongly similar images found";
  const loadMore = similar.loading
    ? '<div class="loading-state" role="status"><span class="spinner" aria-hidden="true"></span>Loading similar images…</div>'
    : similar.hasNext && similar.autoLoad
      ? '<div class="loading-state" role="status"><span class="spinner" aria-hidden="true"></span>Scroll for more</div>'
      : similar.hasNext
        ? '<button id="similar-more" class="secondary" type="button">Load more</button>'
        : '';
  section.innerHTML = `<div class="dialog-header"><h3>${heading}</h3><button id="similar-close" type="button">Close similar images</button></div><div class="similar-grid">${similar.items.map((item, index) => `<button data-similar-index="${index}" class="similar-result"><img src="${item.thumbnail_url || ''}" alt=""><span>${filenameMarkup(item.filename)}</span>${similarityMarkup(item)}</button>`).join("")}</div>${loadMore}`;
  $("similar-close").onclick = closeSimilar;
  $("similar-more")?.addEventListener("click", () => { similar.autoLoad = true; loadSimilarPage(); });
  section.querySelectorAll("[data-similar-index]").forEach(button => button.onclick = () => {
    showViewer(Number(button.dataset.similarIndex) + 1, similarViewerItems(), {mode: "similar", keepSimilar:true});
  });
}

function similarViewerItems() {
  if (!state.similar) return [];
  const sourceId = state.similar.source.asset_id;
  return [state.similar.source, ...state.similar.items.filter(item => item.asset_id !== sourceId)];
}

function syncSimilarViewer() {
  if (state.viewerContext !== "similar" || !state.similar) return;
  const currentId = state.viewerItems[state.viewerIndex]?.asset_id || state.similar.source.asset_id;
  state.viewerItems = similarViewerItems();
  state.viewerIndex = Math.max(0, state.viewerItems.findIndex(item => item.asset_id === currentId));
  renderViewer();
}

function closeSimilar() {
  const source = state.similarSource;
  state.similar = null;
  state.similarSource = null;
  $("viewer").onscroll = null;
  $("similar-gallery").classList.add("hidden");
  if (source) showViewer(source.index, source.items, {...source.context, keepSimilar:false});
  else { renderViewer(); $("viewer").scrollTop = 0; }
}

async function loadSimilarPage() {
  const similar = state.similar;
  if (!similar || similar.loading || !similar.hasNext) return;
  similar.loading = true;
  renderSimilarResults();
  try {
    const limit = similar.initial ? 12 : 6;
    const data = await api(`/api/assets/${encodeURIComponent(similar.assetId)}/similar?offset=${similar.offset}&limit=${limit}&initial=${similar.initial ? 1 : 0}`);
    if (state.similar !== similar) return;
    const existing = new Set(similar.items.map(item => item.asset_id));
    similar.items = [...similar.items, ...data.items.filter(item => !existing.has(item.asset_id) && item.asset_id !== similar.source.asset_id)];
    similar.offset += data.items.length;
    similar.total = data.total;
    similar.strongCount = data.strong_count;
    similar.hasNext = Boolean(data.has_next);
    similar.initial = false;
    syncSimilarViewer();
  } catch (error) {
    if (state.similar === similar) similar.error = error.message;
  } finally {
    if (state.similar === similar) {
      similar.loading = false;
      renderSimilarResults();
      if (similar.error) { showToast(`Similar images failed: ${similar.error}`); similar.error = null; }
    }
  }
}

async function showSimilar(assetId) {
  const section = $("similar-gallery");
  const source = state.viewerItems[state.viewerIndex];
  state.similarSource = {items: state.viewerItems, index: state.viewerIndex, context: {mode:state.viewerContext, groupId:state.viewerGroupId, start:state.viewerStart}};
  state.similar = {assetId, source, items: [], offset: 0, total: 0, strongCount: 0, hasNext: true, initial: true, loading: false, autoLoad: false, error: null};
  section.classList.remove("hidden");
  section.textContent = "";
  $("viewer-similar").textContent = "Close similar images";
  showViewer(0, [source], {mode: "similar", keepSimilar:true});
  $("viewer").onscroll = () => {
    if (state.similar?.autoLoad && $("viewer").scrollTop + $("viewer").clientHeight >= $("viewer").scrollHeight - 240) loadSimilarPage();
  };
  await loadSimilarPage();
}

function assetToViewerItem(asset) {
  const first = asset.physical_files?.[0] || {};
  return {
    asset_id: asset.asset_id,
    media_type: asset.media_type,
    filename: first.filename || "Asset",
    thumbnail_url: first.thumbnail_url,
    original_url: first.original_url,
    display_url: asset.display_url || first.display_url || first.original_url,
    current_group_id: asset.current_group_id,
    user_decision: asset.user_decision,
    auto_recommended: asset.auto_recommended,
  };
}

function renderDetails(asset, options = {}) {
  const displayFilename = typeof globalThis.filenameMarkup === "function" ? globalThis.filenameMarkup : escapeHtml;
  const filenameMarkup = displayFilename;
  const first = asset.physical_files?.[0] || {};
  const dimensions = first.width && first.height ? `${first.width} × ${first.height} (${(first.width * first.height / 1000000).toFixed(2)} MP)` : "Unavailable";
  const thumbnail = options.showThumbnail ? `<button class="detail-thumbnail${first.thumbnail_url ? "" : " placeholder"}" type="button" data-detail-thumbnail aria-label="Open ${escapeHtml(first.filename)} in viewer">${first.thumbnail_url ? `<img src="${first.thumbnail_url}" alt="">` : "<span>Preview unavailable</span>"}</button>` : "";
  const quality = renderQuality(first, false);
  const absolutePath = first.absolute_path || first.relative_path || "Path unavailable";
  const pathRow = options.viewerPanel ? `<dt>Path</dt><dd>${escapeHtml(first.relative_path || "Unavailable")}</dd>` : "";
  const captureTime = formatCapture(asset.capture_time);
  const captureRow = captureTime ? `<dt>Capture time</dt><dd>${escapeHtml(captureTime)}</dd>` : "";
  const latitude = Number(asset.location?.latitude);
  const longitude = Number(asset.location?.longitude);
  const locationText = Number.isFinite(latitude) && Number.isFinite(longitude) ? `${latitude.toFixed(6)}, ${longitude.toFixed(6)}` : "";
  const locationUrl = locationText ? `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(`${latitude.toFixed(6)},${longitude.toFixed(6)}`)}` : "";
  const locationIcon = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 21s7-6.1 7-12A7 7 0 0 0 5 9c0 5.9 7 12 7 12Z" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="9" r="2.2" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>';
  const locationRow = locationText ? `<dt>Location</dt><dd class="location-value"><span>${escapeHtml(locationText)}</span><a class="explorer-button location-link" href="${escapeHtml(locationUrl)}" target="_blank" rel="noopener noreferrer" aria-label="Open location in Google Maps">${locationIcon}</a></dd>` : "";
  const header = options.viewerPanel
    ? `<div class="panel-header"><h2>Details</h2><button id="viewer-details-close" class="icon" type="button" aria-label="Close details">×</button></div>`
    : `<div class="dialog-header"><div><h2 id="details-title">${filenameMarkup(first.filename || "Asset details")}</h2><div class="muted detail-path" title="${escapeHtml(absolutePath)}">${escapeHtml(absolutePath)}</div></div><button id="details-close" class="icon" type="button" aria-label="Close details">×</button></div>`;
  const overview = `<section class="detail-overview"><h3>Overview</h3><dl class="kv"><dt>Dimensions</dt><dd>${escapeHtml(dimensions)}</dd><dt>File size</dt><dd>${escapeHtml(formatBytes(first.size_bytes))}</dd><dt>File created</dt><dd>${escapeHtml(formatCapture(first.file_created_time) || "Unavailable")}</dd>${captureRow}${locationRow}${pathRow}</dl></section>`;
  const technical = renderTechnicalDetails(first);
  const qualityPanel = quality ? `<div class="viewer-quality">${quality}</div>` : "";
  const technicalAndQuality = options.viewerPanel ? `<div class="viewer-technical-quality">${technical}${qualityPanel}</div>` : technical;
  const content = `${header}${overview}${technicalAndQuality}${renderRepresentations(asset)}`;
  return `<div class="details-content">${options.showThumbnail ? `<div class="detail-layout"><div class="detail-main">${content}</div><aside class="detail-aside">${thumbnail}${quality}</aside></div>` : content}</div>`;
}

function renderRepresentations(asset) {
  const displayFilename = typeof globalThis.filenameMarkup === "function" ? globalThis.filenameMarkup : escapeHtml;
  const filenameMarkup = displayFilename;
  const rows = asset.physical_files.map(file => {
    const problems = renderComponentProblems(file);
    return `<div class="representation-row"><div><div class="representation-title"><strong>${filenameMarkup(file.filename)}</strong> · ${escapeHtml(formatBytes(file.size_bytes))}${file.is_preferred ? " · Preferred" : ""}${file.is_online ? "" : " · Offline"}</div><div class="muted representation-path">${escapeHtml(file.relative_path)}</div>${problems ? `<details class="representation-diagnostics"><summary>⚠ Representation status</summary>${problems}</details>` : ""}</div><button class="explorer-button" data-reveal="${escapeHtml(file.id)}" aria-label="Reveal representation in Explorer">📁</button></div>`;
  }).join("");
  return `<section class="section"><h3>Representations</h3><div class="representations">${rows}</div></section>`;
}

function renderQuality(file, showFilename) {
  const status = file.components?.quality?.status;
  if (status === "not_requested" || !status && file.quality_score == null) return "";
  if (["failed", "unsupported"].includes(status) && file.quality_score == null) {
    return `<section class="file-card">${showFilename ? `<div class="muted">${escapeHtml(file.filename)}</div>` : ""}<div class="quality-summary"><strong>Overall technical quality</strong><span class="quality-score-box na"><span class="quality-score">N/A</span></span></div></section>`;
  }
  if (file.media_type === "video" && file.quality_score == null) {
    const message = status === "failed" ? "Technical quality scoring failed for this video." : status === "unsupported" ? "Video quality frame decoding is unsupported on this system." : status === "pending" || status === "running" ? "Technical quality scoring is processing video samples." : "Technical quality is unavailable.";
    return `<div class="quality-unsupported">${message}</div>`;
  }
  if (file.quality_score == null) {
    const error = file.components?.quality?.error;
    const message = status === "unsupported" && file.extension && [".arw", ".cr2", ".cr3", ".dng", ".nef", ".raf", ".rw2"].includes(file.extension) ? "RAW preview is unavailable for technical quality scoring." : status === "failed" ? "Technical quality scoring failed for this image." : status === "pending" || status === "running" ? "Technical quality scoring is processing." : "Technical quality is unavailable.";
    return `<div class="quality-unsupported">${message}${error ? `<div class="muted quality-error">${escapeHtml(error)}</div>` : ""}</div>`;
  }
  const score = Number(file.quality_score).toFixed(2);
  const color = file.quality_score == null ? "#26333f" : qualityColor(file.quality_score);
  return `<section class="file-card">${showFilename ? `<div class="muted">${escapeHtml(file.filename)}</div>` : ""}<div class="quality-summary"><strong>Overall technical quality</strong><span class="quality-score-box" style="--quality-color: ${color}"><span class="quality-score">${score}</span></span></div></section>`;
}

function meterMarkup(label, value, position) {
  if (!value || position == null) return `<dt>${label}</dt><dd>${escapeHtml(value || "Unavailable")}</dd>`;
  const scales = {
    "Focal length": {values:[10,40,150,600], labels:["10","40","150","600 mm"]},
    "Focal length (35mm eq.)": {values:[10,40,150,600], labels:["10","40","150","600 mm"]},
    "Aperture": {values:[1,2.8,8,22], labels:["f/1","f/2.8","f/8","f/22"]},
    "Shutter speed": {values:[30,2,1 / 125,1 / 8000], labels:["30 s","1/2 s","1/125 s","1/8000 s"]},
    "ISO": {values:[40,400,4000,40000], labels:["40","400","4000","40000"]}
  };
  const scale = scales[label];
  const positionOf = v => label === "Shutter speed" ? shutterPosition(v) : logPosition(v, scale.values[0], scale.values.at(-1));
  const majorPositions = [0, 33.333, 66.667, 100];
  const ticks = scale.values.map((_, index) => `<i class="scale-tick major" style="left:${majorPositions[index]}%"></i>`).join("");
  const labels = scale.values.map((_,index) => `<span class="scale-label${index===0 ? " first" : index===scale.values.length-1 ? " last" : ""}" style="left:${majorPositions[index]}%">${scale.labels[index]}</span>`).join("");
  const markerPosition = Math.max(0, Math.min(100, position));
  return `<dt>${label}</dt><dd class="technical-reading"><span class="technical-value">${escapeHtml(value)}</span><span class="measurement-scale" aria-hidden="true"><span class="scale-line">${ticks}<i class="scale-marker${position<0 || position>100 ? " overflow" : ""}" style="left:${markerPosition}%"></i></span><span class="scale-labels">${labels}</span></span></dd>`;
}

function logPosition(value, low, high) { return Number.isFinite(value) && value > 0 ? Math.log(value / low) / Math.log(high / low) * 100 : null; }
function shutterPosition(value) { return Number.isFinite(value) && value > 0 ? logPosition(30 / value, 1, 30 * 8000) : null; }

function renderTechnicalDetails(file) {
  const aperture = metadataValue(file, ["FNumber", "ApertureValue"]);
  const shutter = metadataValue(file, ["ExposureTime", "ShutterSpeedValue"]);
  const iso = metadataValue(file, ["ISOSpeedRatings", "PhotographicSensitivity"]);
  const equivalentFocalNumber = rationalNumber(rawMetadataValue(file, ["FocalLengthIn35mmFilm"]));
  const hasEquivalentFocal = Number.isFinite(equivalentFocalNumber) && equivalentFocalNumber > 0;
  const focalLabel = hasEquivalentFocal ? "Focal length (35mm eq.)" : "Focal length";
  const focal = hasEquivalentFocal ? `${numberText(equivalentFocalNumber)} mm` : metadataValue(file, ["FocalLength"]);
  const apertureNumber = rationalNumber(rawMetadataValue(file, ["FNumber"]));
  const shutterNumber = rationalNumber(rawMetadataValue(file, ["ExposureTime"]));
  const isoNumber = rationalNumber(rawMetadataValue(file, ["ISOSpeedRatings", "PhotographicSensitivity"]));
  const focalNumber = hasEquivalentFocal ? equivalentFocalNumber : rationalNumber(rawMetadataValue(file, ["FocalLength"]));
  const camera = cameraValue(file);
  const lens = metadataValue(file, ["LensModel", "LensMake"]);
  const rows = [["Camera", camera], ["Lens", lens]];
  const hasMeter = aperture || shutter || iso || focal;
  if (!rows.some(([, value]) => value) && !hasMeter) return `<section class="section technical-details"><h3>Technical details</h3><p class="muted">No camera info available</p></section>`;
  return `<section class="section technical-details"><h3>Technical details</h3><dl class="kv">${rows.filter(([, value]) => value).map(([label, value]) => `<dt>${label}</dt><dd>${escapeHtml(value)}</dd>`).join("")}${focal ? meterMarkup(focalLabel, focal, logPosition(focalNumber, 10, 600)) : ""}${aperture ? meterMarkup("Aperture", aperture, logPosition(apertureNumber, 1, 22)) : ""}${shutter ? meterMarkup("Shutter speed", shutter, shutterPosition(shutterNumber)) : ""}${iso ? meterMarkup("ISO", iso, logPosition(isoNumber, 40, 40000)) : ""}</dl></section>`;
}

function componentProblemMessage(name, component) {
  const error = component.error || component.status;
  if (name === "quality" && error.includes("checkpoint is not installed")) return "LAR-IQA model is not installed. Run `uv run archive-index model install lar-iqa`, then re-index.";
  if (name === "quality" && error.includes("requires one learned-quality extra")) return "LAR-IQA dependencies are not installed. Install the CPU or CUDA quality extra, then re-index.";
  return error;
}

function renderComponentProblems(file) {
  const raw = [".arw", ".cr2", ".cr3", ".dng", ".nef", ".raf", ".rw2"].includes(file.extension);
  return ["metadata", "thumbnail", "quality"].flatMap((name) => {
    const component = file.components?.[name];
    const expectedRawGap = raw && ["metadata", "thumbnail"].includes(name) && component?.status === "unsupported" && /no image decoder is configured for \.\w+/i.test(component.error || "");
    return component && !expectedRawGap && ["failed", "unsupported", "pending", "running"].includes(component.status) ? [`<p class="error">${escapeHtml(name[0].toUpperCase() + name.slice(1))}: ${escapeHtml(componentProblemMessage(name, component))}</p>`] : [];
  }).join("");
}

function cameraValue(file) { const exif = file.metadata?.exif || {}; return [exif.Make, exif.Model].filter((value) => value != null && readable(value)).map(formatExif).filter((value, index, values) => values.indexOf(value) === index).join(" "); }
function rawMetadataValue(file, keys) { const exif = file.metadata?.exif || {}; const key = keys.find((candidate) => exif[candidate] != null && readable(exif[candidate])); return key ? exif[key] : null; }
function metadataValue(file, keys) { const key = keys.find((candidate) => file.metadata?.exif?.[candidate] != null && readable(file.metadata.exif[candidate])); return key ? formatMetadataValue(key, file.metadata.exif[key]) : ""; }
function readable(value) { return (Array.isArray(value) || typeof value !== "object") && String(value).length < 160; }
function formatExif(value) { return Array.isArray(value) ? value.join(" / ") : String(value); }
function formatMetadataValue(key, value) { const text = formatExif(value); if (["FNumber", "ApertureValue"].includes(key)) { const number = rationalNumber(value); return number == null ? text : `f/${numberText(number)}`; } if (key === "ExposureTime") return `${formatExposure(value)} s`; if (key === "ShutterSpeedValue") return `${text} s`; if (["ISOSpeedRatings", "PhotographicSensitivity"].includes(key)) return `ISO ${text}`; if (["FocalLength", "FocalLengthIn35mmFilm"].includes(key)) { const number = rationalNumber(value); return number == null ? `${text} mm` : `${numberText(number)} mm`; } return text; }
function formatExposure(value) { const text = formatExif(value); const number = rationalNumber(value); if (number > 0 && number < 1) { const denominator = Math.round(1 / number); if (Math.abs(number - (1 / denominator)) < 0.000001) return `1/${denominator}`; } return text.replace(/\s*\/\s*/g, "/"); }
function rationalNumber(value) { const text = formatExif(value); const match = text.match(/^(-?\d+(?:\.\d+)?)\s*\/\s*(-?\d+(?:\.\d+)?)$/); if (match && Number(match[2]) !== 0) return Number(match[1]) / Number(match[2]); const number = Number(text); return Number.isFinite(number) ? number : null; }
function numberText(value) { return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, "").replace(/\.$/, ""); }

async function loadViewerDetails() {
  const item = state.viewerItems[state.viewerIndex];
  if (!item) return;
  try {
    const detail = await api(`/api/assets/${encodeURIComponent(item.asset_id)}`);
    if (state.viewerItems[state.viewerIndex]?.asset_id !== item.asset_id || !state.viewerInfoOpen) return;
    state.viewerDetail = detail;
    state.viewerDetail._context = { mode: state.viewerContext, items: state.viewerItems, index: state.viewerIndex, groupId: state.viewerGroupId };
    if (state.viewerInfoOpen) {
      $("viewer-details").innerHTML = renderDetails(state.viewerDetail, { viewerPanel: true });
      $("viewer-details").querySelector("#viewer-details-close")?.addEventListener("click", () => toggleViewerInfo(false));
      $("viewer-details").querySelector("[data-find-similar]")?.addEventListener("click", () => showSimilar(item.asset_id));
    }
  } catch (error) {
    $("viewer-details").innerHTML = '<div class="viewer-error">Details unavailable.</div>';
    showToast(`Details request failed: ${error.message}`);
  }
}

async function toggleViewerInfo(open = !state.viewerInfoOpen) {
  state.viewerInfoOpen = open;
  $("viewer-details").classList.toggle("hidden", !open);
  $("viewer-stage").classList.toggle("info-open", open);
  if (open) await loadViewerDetails();
  requestAnimationFrame(() => applyViewerTransform($("viewer-media").querySelector("img.viewer-media")));
}

async function loadJobs() {
  const requestId = ++state.jobsRequest;
  try {
    const data = await api("/api/jobs?limit=10");
    if (requestId !== state.jobsRequest) return;
    const revision = JSON.stringify(data.revision);
    if (state.browserRevision && state.browserRevision !== revision) state.renderKeys = {};
    state.browserRevision = revision;
    if (!$("search").value.trim() && !$("workspace-view").classList.contains("hidden")) {
      const readiness = await api("/api/search-status");
      if (requestId !== state.jobsRequest) return;
      if (!$("search").value.trim()) showSearchStatus(readiness);
    }
    const active = data.jobs.find((job) => ["pending", "running"].includes(job.status));
    if (!active) {
      $("job-banner").classList.add("hidden");
      if (state.activeJobId) {
        state.activeJobId = null;
        await api("/api/workspace");
        if (requestId !== state.jobsRequest) return;
        state.renderKeys = {};
        if (state.viewMode === "groups") await loadGroups(); else await loadAssets();
        await loadProblemsBadge();
      }
      return;
    }
    state.activeJobId = active.id;
    const percent = active.total_items ? (100 * active.completed_items / active.total_items).toFixed(1) : "0.0";
    const elapsed = active.started_at ? Math.max((Date.now() - Date.parse(active.started_at)) / 1000, .001) : .001;
    const rate = (active.completed_items / elapsed).toFixed(1);
    $("job-banner").classList.remove("hidden");
    $("job-copy").textContent = `${active.stage || active.kind} · ${active.completed_items}/${active.total_items} (${percent}%) · ${rate}/s · ${active.failed_items || 0} failed · ${active.skipped_items || 0} skipped`;
    if (active.substage) {
      const sub = active.substage;
      const outerCurrent = sub.outer_total ? sub.outer_completed : active.completed_items;
      const outerTotal = sub.outer_total || active.total_items;
      const frameRate = sub.rate == null ? "" : ` · ${sub.rate.toFixed(1)} frames/s`;
      const stageEta = sub.eta == null ? "" : ` · ETA ${formatEta(sub.eta)}`;
      $("job-copy").textContent = `${active.kind.replaceAll("_", " ")} · ${outerCurrent}/${outerTotal} videos · ${sub.item || ""} — ${sub.stage} · ${sub.current}/${sub.total}${frameRate}${stageEta}`;
    }
    $("job-progress").max = Math.max(active.total_items || 1, 1);
    $("job-progress").value = active.completed_items;
    $("cancel-job").onclick = async () => { await api(`/api/jobs/${active.id}/cancel`, { method: "POST" }); };
  } catch (error) {
    showToast(`Job status request failed: ${error.message}`);
  }
}

async function loadProblemsBadge() {
  try {
    const data = await api("/api/problems?limit=500");
    $("problems-button").classList.remove("hidden");
    $("problem-count").textContent = data.problems.length ? `(${data.problems.length})` : "";
  } catch (error) {
    showToast(`Diagnostics request failed: ${error.message}`);
  }
}

async function showProblems() {
  try {
    const data = await api("/api/problems?limit=100");
    $("problems-dialog").innerHTML = `<div class="dialog-inner"><div class="dialog-header"><h2 id="problems-title">Problems${data.problems.length ? ` (${data.problems.length})` : ""}</h2><button id="problems-close" class="secondary" type="button">Close</button></div>${data.problems.map((problem) => `<div class="file-card"><strong>${escapeHtml(problem.relative_path || "Workspace")}</strong><div class="muted">${escapeHtml(problem.job_kind || "Processing")} · ${escapeHtml(problem.job_created_at || problem.created_at || "")}</div><p class="error">${escapeHtml(problem.message)}</p></div>`).join("") || `<div class="empty">No problems recorded.</div>`}<div class="recent-actions"><button id="problems-index" type="button">Run index again</button></div></div>`;
    $("problems-dialog").showModal();
    document.body.classList.add("modal-open");
    $("problems-close").addEventListener("click", () => closeDialog($("problems-dialog")));
    $("problems-index").addEventListener("click", async () => { closeDialog($("problems-dialog")); await startIndex(); });
  } catch (error) {
    showToast(`Diagnostics request failed: ${error.message}`);
  }
}

async function showOfflineCleanup() {
  try {
    const data = await api("/api/offline-media");
    if (!data.count) {
      showToast("There is no offline media to forget.");
      return;
    }
    const noun = data.count === 1 ? "entry" : "entries";
    $("offline-dialog").innerHTML = `<div class="dialog-inner removal-dialog"><div class="dialog-header"><h2 id="offline-title">Forget ${data.count} offline media ${noun}?</h2><button id="offline-close" class="icon" type="button" aria-label="Close">×</button></div><p>${data.images} image${data.images === 1 ? "" : "s"} · ${data.videos} video${data.videos === 1 ? "" : "s"}</p><p class="muted">This does not delete files from disk.</p><div class="removal-actions"><button id="offline-cancel" class="secondary" type="button">Cancel</button><button id="offline-forget" class="danger-button" type="button">Forget</button></div></div>`;
    const dialog = $("offline-dialog");
    dialog.showModal();
    document.body.classList.add("modal-open");
    $("offline-close").addEventListener("click", () => closeDialog(dialog));
    $("offline-cancel").addEventListener("click", () => closeDialog(dialog));
    $("offline-forget").addEventListener("click", forgetOfflineMedia);
  } catch (error) {
    showToast(`Offline media request failed: ${error.message}`);
  }
}

async function forgetOfflineMedia() {
  try {
    const result = await api("/api/offline-media/forget", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    closeDialog($("offline-dialog"));
    state.renderKeys = {};
    await renderSetupPlan();
    await loadProblemsBadge();
    showToast(`${result.removed} offline media ${result.removed === 1 ? "entry" : "entries"} forgotten.`);
  } catch (error) {
    showToast(`Offline media cleanup failed: ${error.message}`);
  }
}

async function startIndex() {
  try { await api("/api/index", { method: "POST" }); $("status").textContent = ""; await loadJobs(); }
  catch (error) { showToast(`Index request failed: ${error.message}`); }
}

function renderFilterButtons() {
  for (const [attr, value] of [["auto", state.auto], ["manual", state.manual], ["type", $("media-type").value], ["layout", state.layout], ["sort", $("sort-by").value]]) {
    document.querySelectorAll(`[data-${attr}]`).forEach(b => { b.classList.toggle("active", b.dataset[attr] === value); b.setAttribute("aria-pressed", String(b.dataset[attr] === value)); });
  }
  $("direction-button").textContent = $("direction").value === "desc" ? "↓" : "↑";
}

function renderSemanticControls() {
  $("similarity-control").classList.toggle("hidden", !state.semanticEnabled);
  $("search-sort-button").classList.toggle("hidden", !state.semanticEnabled);
  if (!state.semanticEnabled && $("sort-by").value === "search") {
    $("sort-by").value = "capture_time";
    $("direction").value = "desc";
  }
}

function refreshBrowser() {
  state.renderKeys = {}; state.browserAbort?.abort(); clearTimeout(state.searchPoll);
  state.galleryRequestInFlight = false;
  state.assetRequest++; state.groupRequest++;
  state.groupPage = 1; state.scrollPositions = {}; window.scrollTo(0, 0);
  if (state.viewMode === "gallery") {
    state.galleryLoading = true;
    renderGalleryWindow({items: state.items, total: state.total, has_next: false, search: {state: "loading"}}, state.windowStart, state.windowColumns, state.windowHeight);
  }
  renderFilterButtons();
  if (state.viewMode === "groups") loadGroups(); else if (state.viewMode === "gallery") loadAssets();
}

function setupFilters() {
  for (const attr of ["auto", "manual", "type", "layout", "sort"]) document.querySelectorAll(`[data-${attr}]`).forEach(b => b.onclick = () => {
    if (attr === "type") $("media-type").value = b.dataset[attr];
    else if (attr === "sort") $("sort-by").value = b.dataset[attr];
    else state[attr] = b.dataset[attr];
    refreshBrowser();
  });
  $("direction-button").onclick = () => { $("direction").value = $("direction").value === "desc" ? "asc" : "desc"; refreshBrowser(); };
  let debounce;
  $("search").addEventListener("input", () => {
    clearTimeout(debounce); state.semanticPending = state.semanticEnabled && Boolean($("search").value.trim());
    if (state.semanticPending) {
      const cold = ["available", "loading"].includes(state.searchState);
      showSearchStatus({state:cold ? "loading" : "searching", message:cold ? `Loading ${state.searchProvider || "OpenCLIP"}…` : "Searching…"});
    }
    $("sort-by").value = state.semanticPending ? "search" : "capture_time"; $("direction").value = "desc";
    refreshBrowser();
    if (state.semanticPending) debounce = setTimeout(() => { state.semanticPending = false; refreshBrowser(); }, 500);
  });
  $("similarity-threshold").oninput = () => { $("similarity-value").textContent = Number($("similarity-threshold").value).toFixed(2); localStorage.setItem(`archive-threshold-${state.workspace}`, $("similarity-threshold").value); refreshBrowser(); };
  let thresholdSave = Promise.resolve();
  $("recommendation-threshold").oninput = () => {
    state.renderKeys = {};
    const threshold = Number($("recommendation-threshold").value); $("recommendation-value").textContent = threshold.toFixed(2);
    thresholdSave = thresholdSave.catch(() => {}).then(() => api("/api/recommendation-threshold", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({threshold})})).then(() => {
      if (Number($("recommendation-threshold").value) === threshold) refreshBrowser();
    }).catch(e => {$("status").textContent=e.message;});
  };
  const collapse = value => {state.collapsed = value; localStorage.setItem("archive-sidebar-collapsed", value); document.body.classList.toggle("sidebar-collapsed", value); $("sidebar-reopen").classList.toggle("hidden", !value); requestAnimationFrame(reflowGallery); setTimeout(reflowGallery, 240);};
  $("sidebar-collapse").onclick = () => collapse(true); $("sidebar-reopen").onclick = () => collapse(false);
  $("folder-open").onclick = () => { renderFolderTree(); $("folder-dialog").showModal(); document.body.classList.add("modal-open"); };
  $("folder-close").onclick = () => closeDialog($("folder-dialog"));
  $("folders-all").onclick = () => { state.folders = null; renderFolderTree(); refreshBrowser(); };
  $("folders-none").onclick = () => {state.folders = new Set(); renderFolderTree(); refreshBrowser();};
  $("folder-dialog").addEventListener("close", () => document.body.classList.remove("modal-open"));
  bindBackdropClose($("folder-dialog"));
  $("clear-filters").onclick = () => {state.auto="all";state.manual="all";state.layout="";state.folders=null;state.semanticPending=false;clearTimeout(debounce);$("search").value="";$("media-type").value="";$("sort-by").value="capture_time";$("direction").value="desc";$("folder-summary").textContent="All folders selected";$("similarity-threshold").value="0.20";$("similarity-value").textContent="0.20";localStorage.setItem(`archive-threshold-${state.workspace}`,"0.20");$("recommendation-threshold").value="0.70";$("recommendation-value").textContent="0.70";$("recommendation-threshold").oninput();refreshBrowser();};
}

function normalizeFolders() {
  if (state.folders !== null) {
    state.folders = new Set(state.folderPaths.filter(p => state.folders.has(p)));
    if (state.folderPaths.length && state.folders.size === state.folderPaths.length) state.folders = null;
  }
}

function folderSummary() {
  if (state.folders === null) return "All folders selected";
  const count = state.folders.size;
  return `${count} ${count === 1 ? "folder" : "folders"} selected`;
}

function toggleFolder(path, checked) {
  const selected = new Set(state.folders === null ? state.folderPaths : state.folders);
  if (checked) selected.add(path); else selected.delete(path);
  state.folders = selected; normalizeFolders();
}

function renderFolderTree() {
  normalizeFolders();
  const nodes = state.folderPaths;
  const countLabel = (path) => {
    const count = state.folderCounts[path] || {};
    if (typeof count === "number") return `${count.toLocaleString()} files`;
    const files = Number(count.files || 0);
    const images = Number(count.images || 0);
    const videos = Number(count.videos || 0);
    return `${files.toLocaleString()} file${files === 1 ? "" : "s"} · ${images.toLocaleString()} image${images === 1 ? "" : "s"} · ${videos.toLocaleString()} video${videos === 1 ? "" : "s"}`;
  };
  $("folder-tree").innerHTML = nodes.map((path,index) => `<label class="folder-check" style="padding-left:${(path ? path.split("/").length : 0) * 18}px"><input type="checkbox" data-folder-index="${index}"><span>${escapeHtml(path ? path.split("/").pop() : "Workspace root")}</span><span class="muted folder-count">${countLabel(path)}</span></label>`).join("");
  $("folder-tree").querySelectorAll("[data-folder-index]").forEach(input => {
    const path=nodes[Number(input.dataset.folderIndex)];
    input.checked = state.folders === null || state.folders.has(path);
    input.indeterminate = false;
    input.onchange=()=>{toggleFolder(path,input.checked); renderFolderTree(); refreshBrowser();};
  });
  $("folder-summary").textContent=folderSummary();
}

function setViewMode(mode, load = true) {
  const started = performance.now();
  if(state.workspace) {$("setup-view").classList.add("hidden");$("setup-header-summary").classList.add("hidden");$("workspace-view").classList.remove("hidden");["index","configure-workspace","workspace-crumb"].forEach(id=>$(id).classList.remove("hidden"));}
  if (load) {state.scrollPositions[state.viewMode] = window.scrollY; state.browserAbort?.abort(); clearTimeout(state.searchPoll);}
  state.viewMode = ["groups", "cloud"].includes(mode) ? mode : "gallery";
  $("gallery").classList.toggle("hidden", mode !== "gallery");
  $("groups-view").classList.toggle("hidden", mode !== "groups");
  $("cloud-view").classList.toggle("hidden", mode !== "cloud");
  for(const [id, value] of [["gallery-view-toggle","gallery"],["groups-view-toggle","groups"],["cloud-view-toggle","cloud"]]) $(id).setAttribute("aria-selected",String(mode===value));
  syncUrl(); window.scrollTo(0, state.scrollPositions[mode] || 0);
  const focusing = Boolean(state.focusGroup);
  if (load) (mode === "groups" ? loadGroups() : mode === "gallery" ? loadAssets() : Promise.resolve()).then(()=>{if(!focusing && state.viewMode===mode) window.scrollTo(0,state.scrollPositions[mode] || 0); requestAnimationFrame(()=>{performance.measure(`navigation:${mode}`,{start:started});console.debug(`navigation:${mode} ${(performance.now()-started).toFixed(1)} ms`);});});
}

function renderGroup(group) {
  const displayFilename = typeof globalThis.filenameMarkup === "function" ? globalThis.filenameMarkup : escapeHtml;
  const filenameMarkup = displayFilename;
  const members = group.members.map((item, index) => {
    const preview = item.thumbnail_url ? `<img class="group-thumb" loading="lazy" src="${item.thumbnail_url}" alt="${escapeHtml(item.filename)}" onerror="this.replaceWith(Object.assign(document.createElement('div'), {className:'group-thumb placeholder', textContent:'Preview unavailable'}))">` : `<div class="group-thumb placeholder">Preview unavailable</div>`;
    const automaticClass = item.auto_recommended ? " recommended" : item.is_representative ? " representative" : "";
    return `<div class="group-member${automaticClass}"><button class="group-photo" type="button" data-group-index="${index}" aria-label="View ${escapeHtml(item.filename)}">${preview}</button><button class="group-info info-button" type="button" data-info="${index}" aria-label="Details for ${escapeHtml(item.filename)}">ⓘ</button><div class="group-caption"><span title="${escapeHtml(item.filename)}">${filenameMarkup(item.filename)}</span>${scoreMarkup(item.quality_score)}${similarityMarkup(item)}<div class="group-state">${selectionStateMarkup(item)}</div><div class="group-actions">${selectionActionsMarkup(item)}</div></div></div>`;
  }).join("");
  const memberLabel = `${group.member_count} ${group.member_count === 1 ? "member" : "members"}`;
  return `<section class="group-row" data-group-id="${escapeHtml(group.group_id)}"><div class="group-heading"><strong>${escapeHtml(group.label)}</strong><span class="muted">${memberLabel}</span><span class="muted">${escapeHtml(formatCapture(group.first_capture_time, ""))}</span></div>${members || `<div class="empty">No members</div>`}</section>`;
}

async function loadGroups() {
  const requestId = ++state.groupRequest;
  try {
    const params=filterParams();params.set("view","groups");params.set("offset",(state.groupPage-1)*10);params.set("limit",10);
    const renderKey = String(params);
    if (state.renderKeys.groups === renderKey && !state.focusGroup) return;
    const data = await browserData(params);
    if (requestId !== state.groupRequest) return;
    if (!["loading","searching","failed"].includes(data.search.state)) state.renderKeys.groups = renderKey;
    data.page=state.groupPage;data.page_size=10;data.run_id="browser";
    if (requestId !== state.groupRequest) return;
    state.groupItems=data.groups.flatMap(g=>g.members);
    $("groups-list").innerHTML = data.groups.map(renderGroup).join("") || `<div class="empty">${escapeHtml(data.empty_reason || (data.run_id ? "No groups match these filters." : "Groups have not been built yet."))}</div>`;
    $("groups-status").textContent = data.run_id ? "" : "Run Re-index to build groups.";
    $("groups-list").querySelectorAll(".group-row").forEach((row) => {
      const group = data.groups.find((candidate) => candidate.group_id === row.dataset.groupId);
      row.querySelectorAll("[data-group-index]").forEach((button) => button.addEventListener("click", () => showViewer(Number(button.dataset.groupIndex), group.members, { mode: "group", groupId: group.group_id })));
      row.querySelectorAll("[data-info]").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); showDetails(group.members[Number(button.dataset.info)].asset_id, { mode: "group", items: group.members, index: Number(button.dataset.info), groupId: group.group_id }); }));
    });
    bindSelectionButtons($("groups-list"));
    renderGroupPager(data);
    syncUrl();
    if (state.focusGroup) {
      const target = $("groups-list").querySelector(`[data-group-id="${CSS.escape(state.focusGroup)}"]`);
      if (target) { target.classList.add("focused-group"); target.scrollIntoView({ block: "center", behavior: "instant" }); setTimeout(() => target.classList.remove("focused-group"), 1800); }
      state.focusGroup = null;
      syncUrl();
    }
  } catch (error) {
    if (error.name !== "AbortError") showToast(`Groups request failed: ${error.message}`);
  }
}

function renderGroupPager(data) {
  const pageCount = Math.max(1, Math.ceil(data.total / data.page_size));
  const first = data.total ? ((data.page - 1) * data.page_size) + 1 : 0;
  const last = data.total ? Math.min(data.total, data.page * data.page_size) : 0;
  const range = data.total ? `${first}–${last} / ${data.total} groups` : "0 / 0 groups";
  ["top", "bottom"].forEach((place) => {
    $(`groups-page-label-${place}`).textContent = `Page ${data.page} / ${pageCount}`;
    $(`groups-range-label-${place}`).textContent = range;
    $(`groups-previous-${place}`).disabled = data.page <= 1;
    $(`groups-next-${place}`).disabled = !data.has_next;
  });
}

async function locateCurrentGroup() {
  const started = performance.now();
  const item = state.viewerItems[state.viewerIndex];
  if (!item?.current_group_id) return;
  const params = filterParams("groups");
  params.set("group_id", item.current_group_id);
  const target = await api(`/api/browser/locate?${params}`);
  closeDialog($("viewer"));
  state.groupPage = target.found ? target.page : 1;
  state.focusGroup = item.current_group_id;
  setViewMode("groups");
  performance.measure("navigation:locate",{start:started});
  console.debug(`navigation:locate ${(performance.now()-started).toFixed(1)} ms`);
}

$("workspace-form").addEventListener("submit", (event) => { event.preventDefault(); openWorkspace($("workspace-path").value.trim(), false); });
$("browse-workspace").addEventListener("click", pickWorkspace);
$("configure-workspace").addEventListener("click", configureWorkspace);
$("setup-cancel").addEventListener("click", cancelSetup);
$("setup-apply").addEventListener("click", applySetup);
$("index").addEventListener("click", () => state.setup ? applySetup() : startIndex());
$("problems-button").addEventListener("click", showProblems);
$("forget-offline-button").addEventListener("click", showOfflineCleanup);
$("gallery-view-toggle").addEventListener("click", () => setViewMode("gallery"));
$("cloud-view-toggle").onclick = () => setViewMode("cloud");
$("viewer-similar").onclick = () => state.similar ? closeSimilar() : showSimilar(state.viewerItems[state.viewerIndex].asset_id);
$("workspace-explorer").onclick = () => revealFile();
$("viewer-explorer").onclick = () => revealFile(state.viewerItems[state.viewerIndex].preferred_physical_id);
$("groups-view-toggle").addEventListener("click", () => setViewMode("groups"));

["top", "bottom"].forEach((place) => { $(`groups-previous-${place}`).addEventListener("click", () => { state.groupPage = Math.max(1, state.groupPage - 1); syncUrl(); loadGroups().then(() => window.scrollTo({ top: 0, behavior: "smooth" })); }); $(`groups-next-${place}`).addEventListener("click", () => { state.groupPage += 1; syncUrl(); loadGroups().then(() => window.scrollTo({ top: 0, behavior: "smooth" })); }); });
$("viewer-close").addEventListener("click", () => { if(state.viewerContext === "similar" && state.similarSource) { const source=state.similarSource; state.similarSource=null; showViewer(source.index,source.items,source.context); } else closeDialog($("viewer")); });
$("viewer-previous").addEventListener("click", () => moveViewer(-1));
$("viewer-next").addEventListener("click", () => moveViewer(1));
$("viewer-info").addEventListener("click", () => toggleViewerInfo());
$("viewer-grouping").addEventListener("click", locateCurrentGroup);
$("viewer-smooth").addEventListener("change", (event) => { state.viewerSmooth = event.target.checked; applyViewerTransform($("viewer-media").querySelector("img.viewer-media")); });
$("viewer-stage").addEventListener("click", (event) => { if (state.viewerClickSuppressed) { state.viewerClickSuppressed = false; return; } if (["viewer-stage", "viewer-media-pane", "viewer-media"].includes(event.target.id)) closeDialog($("viewer")); });
$("viewer-media-pane").addEventListener("wheel", (event) => {
  const media = $("viewer-media").querySelector("img.viewer-media");
  if (!media) return;
  if (event.deltaY < 0 && !event.target.closest("img.viewer-media")) return;
  event.preventDefault();
  const rect = $("viewer-media-pane").getBoundingClientRect();
  const pointX = event.clientX - (rect.left + rect.width / 2);
  const pointY = event.clientY - (rect.top + rect.height / 2);
  const next = Math.max(1, Math.min(MAX_VIEWER_ZOOM, state.viewerZoom * (event.deltaY < 0 ? 1.2 : 1 / 1.2)));
  if (next === state.viewerZoom) return;
  if (event.deltaY < 0) {
    const contentX = (pointX - state.viewerPanX) / state.viewerZoom;
    const contentY = (pointY - state.viewerPanY) / state.viewerZoom;
    state.viewerZoom = next;
    state.viewerPanX = pointX - contentX * next;
    state.viewerPanY = pointY - contentY * next;
  } else {
    const progress = state.viewerZoom > 1 ? (next - 1) / (state.viewerZoom - 1) : 0;
    state.viewerZoom = next;
    state.viewerPanX *= progress;
    state.viewerPanY *= progress;
    if (next === 1) { state.viewerPanX = 0; state.viewerPanY = 0; }
  }
  applyViewerTransform(media);
}, { passive: false });
$("viewer-media-pane").addEventListener("pointerdown", (event) => {
  const media = $("viewer-media").querySelector("img.viewer-media");
  if (!media || state.viewerZoom <= 1 || event.button !== 0 || !event.target.closest("img.viewer-media")) return;
  state.dragging = true;
  state.dragStartX = event.clientX;
  state.dragStartY = event.clientY;
  state.dragPanX = state.viewerPanX;
  state.dragPanY = state.viewerPanY;
  event.currentTarget.setPointerCapture(event.pointerId);
  applyViewerTransform(media);
});
$("viewer-media-pane").addEventListener("pointermove", (event) => { if (!state.dragging) return; state.viewerPanX = state.dragPanX + event.clientX - state.dragStartX; state.viewerPanY = state.dragPanY + event.clientY - state.dragStartY; applyViewerTransform($("viewer-media").querySelector("img.viewer-media")); });
["pointerup", "pointercancel"].forEach((eventName) => $("viewer-media-pane").addEventListener(eventName, (event) => { if (!state.dragging) return; state.dragging = false; state.viewerClickSuppressed = true; setTimeout(() => { state.viewerClickSuppressed = false; }, 0); if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId); applyViewerTransform($("viewer-media").querySelector("img.viewer-media")); }));
["viewer", "details", "problems-dialog", "offline-dialog", "remove-workspace-dialog"].forEach((id) => $(id).addEventListener("close", () => { if (!["viewer", "details", "problems-dialog", "offline-dialog", "remove-workspace-dialog"].some((name) => $(name).open)) document.body.classList.remove("modal-open"); }));
["viewer", "details", "problems-dialog", "offline-dialog", "remove-workspace-dialog"].forEach((id) => bindBackdropClose($(id)));
window.addEventListener("resize", () => applyViewerTransform($("viewer-media").querySelector("img.viewer-media")));
document.addEventListener("keydown", (event) => {
  if ($("details").open || $("problems-dialog").open || $("offline-dialog").open || $("remove-workspace-dialog").open || ["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
  const key = event.key.toLowerCase();
  if ($("viewer").open) {
    if ({ s: "selected", r: "rejected", u: "undecided" }[key]) { event.preventDefault(); const item = state.viewerItems[state.viewerIndex]; if (item) setDecision(item.asset_id, { s: "selected", r: "rejected", u: "undecided" }[key]); return; }
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); moveViewer(event.key === "ArrowLeft" ? -1 : 1); }
    return;
  }
  if (state.viewMode === "gallery" && { s: "selected", r: "rejected", u: "undecided" }[key]) {
    const card = document.activeElement.closest?.(".photo-card");
    const item = card && state.items[Number(card.dataset.index)];
    if (item) { event.preventDefault(); setDecision(item.asset_id, { s: "selected", r: "rejected", u: "undecided" }[key]); }
  }
});
setupFilters();
if (state.workspace) {
  loadWorkspace().then(() => setInterval(() => { loadJobs(); loadProblemsBadge(); }, 1500)).catch((error) => showToast(`Workspace request failed: ${error.message}`));
} else { loadHome().catch((error) => showToast(`Workspace list failed: ${error.message}`)); }

async function revealFile(file_id) {
  try { await api("/api/reveal", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({file_id})}); }
  catch(error) { showToast(`Explorer request failed: ${error.message}`); }
}
document.addEventListener("click", event => {const button=event.target.closest("[data-reveal]");if(button) revealFile(button.dataset.reveal);});
let scrollTimer;
window.addEventListener("scroll", () => {
  if(state.viewMode!=="gallery" || $("workspace-view").classList.contains("hidden") || $("viewer").open) return;
  clearTimeout(scrollTimer); scrollTimer=setTimeout(()=>{
    const top=$("gallery").getBoundingClientRect().top+window.scrollY;
    const start=Math.max(0,Math.floor((window.scrollY-top)/state.windowHeight)-2)*state.windowColumns;
    const loadedEnd = state.windowStart + state.items.length;
    const visibleEnd = start + Math.ceil(window.innerHeight / state.windowHeight) * state.windowColumns;
    if (state.galleryRequestInFlight || !state.total) return;
    if (start < state.windowStart) loadAssets(start);
    else if (visibleEnd >= loadedEnd - state.windowColumns && loadedEnd < state.total && state.windowHasNext) loadAssets(loadedEnd);
  },70);
}, {passive:true});

$("viewer").addEventListener("cancel", event => {
  if(state.viewerContext === "similar" && state.similarSource) {event.preventDefault();const source=state.similarSource;state.similarSource=null;showViewer(source.index,source.items,source.context);}
});

$("viewer").addEventListener("close", stopViewerMedia);
$("viewer").addEventListener("cancel", () => {if(state.viewerContext !== "similar") stopViewerMedia();});
window.addEventListener("pagehide", stopViewerMedia);
