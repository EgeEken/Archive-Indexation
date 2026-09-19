async function loadWorkspace() {
  document.body.classList.remove("home-active");
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
