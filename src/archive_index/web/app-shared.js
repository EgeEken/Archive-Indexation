const query = new URLSearchParams(location.search);
const MAX_VIEWER_ZOOM = 40;
let workspacePickerInFlight = false;
const state = {
  workspace: query.get("workspace"),
  total: 0,
  items: [],
  viewerItems: [],
  viewMode: ["groups", "geo", "timeline", "vector"].includes(query.get("view")) ? query.get("view") : "gallery",
  searchState: "available", renderKeys: {}, browserAbort: null, searchPoll: null, searchGeneration: 0, searchPollCount: 0, semanticPending: false, semanticEnabled: false, auto: "all", manual: "all", layout: "", folders: null, folderPaths: [], folderCounts: {},
  scrollPositions: {}, windowStart: 0, windowRows: [], windowColumns: 1, windowHeight: 360, windowHasNext: false,
  collapsed: localStorage.getItem("archive-sidebar-collapsed") === "true",
  selectionFilter: query.get("selection") || "all",
  groupPage: Number(query.get("group_page") || 1),
  groupPageSize: 10,
  focusGroup: query.get("focus_group"),
  viewerIndex: -1,
  viewerTotal: 0,
  viewerPageSize: 60,
  viewerFilterKey: null,
  viewerReturnAssetId: null,
  viewDirty: false,
  viewerZoom: 1,
  viewerPanX: 0,
  viewerPanY: 0,
  viewerSmooth: true,
  viewerInfoOpen: false,
  viewerContext: "gallery",
  viewerSequenceIds: [],
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
  visualizationRequest: 0,
  visualizationAbort: null,
  visualizations: {
    geo: {key: null, data: null, centerX: .5, centerY: .5, scale: 1, baseScale: 1, localMetricReferenceLatitude: null, targetCenterX: .5, targetCenterY: .5, targetScale: 1, needsFit: true, hitTargets: [], lodCaches: new Map(), cameraFrame: 0, interactionPhase: "settled", settledLod: 0, lodSettleTimer: 0, lodSettleFrame: 0},
    timeline: {key: null, data: null, timeMode: "capture", modes: {capture: {centerTime: 0, visibleSpan: 86400, fitVisibleSpan: 86400, needsFit: true}, file_created: {centerTime: 0, visibleSpan: 86400, fitVisibleSpan: 86400, needsFit: true}}, centerTime: 0, visibleSpan: 86400, fitVisibleSpan: 86400, targetCenterTime: 0, targetVisibleSpan: 86400, needsFit: true, hitTargets: [], timelineCaches: new Map(), cameraFrame: 0, interactionPhase: "settled", settledLod: null, lodSettleTimer: 0, lodSettleFrame: 0},
    vector: {key: null, data: null, centerX: 0, centerY: 0, scale: 1, targetCenterX: 0, targetCenterY: 0, targetScale: 1, baseScale: 1, baseCellWorld: 1, worldBounds: null, needsFit: true, hitTargets: [], lodCaches: new Map(), vectorDisplayCaches: new Map(), displayGroups: [], grid: new Map(), cameraFrame: 0, interactionPhase: "settled", settledLod: 0, lodSettleTimer: 0, lodSettleFrame: 0},
  },
  visualizationCapabilities: {geo: true, timeline: true, vector: true},
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

function formatHomeTimestamp(value) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const pad = number => String(number).padStart(2, "0");
  return `${pad(date.getDate())}/${pad(date.getMonth() + 1)}/${date.getFullYear()} at ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function workspaceAssetCount(count) {
  const value = Number(count || 0);
  return `<strong class="workspace-asset-count">${value.toLocaleString()}</strong> ${value === 1 ? "indexed asset" : "indexed assets"}`;
}

function scoreMarkup(score) {
  return score == null ? "" : `<span class="quality-chip" style="--quality-color: ${qualityColor(score)}">Quality: ${Number(score).toFixed(2)}</span>`;
}

function similarityMarkup(item) {
  if (item.similarity == null) return "";
  const candidateTimestamp = item.best_match_timestamp == null ? "" : " · Match " + Number(item.best_match_timestamp).toFixed(1) + " s";
  const sourceTimestamp = item.source_match_timestamp == null ? "" : " · Source " + Number(item.source_match_timestamp).toFixed(1) + " s";
  const timestamp = item.source_match_timestamp != null && item.best_match_timestamp != null
    ? " · " + Number(item.source_match_timestamp).toFixed(1) + " s ↔ " + Number(item.best_match_timestamp).toFixed(1) + " s"
    : candidateTimestamp || sourceTimestamp;
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
    if ($("viewer").open) {
      updateViewerReviewState();
      if (state.similar) renderSimilarResults();
      state.viewDirty = true;
    } else {
      await loadCurrentView();
    }
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

async function loadCurrentView() {
  if (state.viewMode === "groups") return loadGroups();
  if (["geo", "timeline", "vector"].includes(state.viewMode)) return loadVisualization(state.viewMode);
  return loadAssets();
}

function viewModeAvailable(mode) {
  return ["gallery", "groups", "timeline"].includes(mode) || Boolean(state.visualizationCapabilities[mode]);
}

function renderVisualizationNavigation() {
  for (const [id, mode] of [["gallery-view-toggle", "gallery"], ["groups-view-toggle", "groups"], ["geo-view-toggle", "geo"], ["timeline-view-toggle", "timeline"], ["vector-view-toggle", "vector"]]) {
    const button = $(id);
    const available = viewModeAvailable(mode);
    button.classList.toggle("hidden", !available);
    button.setAttribute("aria-selected", String(available && state.viewMode === mode));
  }
}

function applyVisualizationCapabilities(capabilities) {
  state.visualizationCapabilities = {
    geo: Boolean(capabilities?.geo),
    timeline: true,
    vector: Boolean(capabilities?.vector),
  };
  renderVisualizationNavigation();
  if (!viewModeAvailable(state.viewMode)) {
    if (typeof setViewMode === "function") setViewMode("gallery", false);
    else {
      state.viewMode = "gallery";
      syncUrl();
    }
  }
}

async function loadVisualizationCapabilities() {
  const capabilities = await api("/api/workspace/visualization-capabilities");
  applyVisualizationCapabilities(capabilities);
}
