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
  state.renderKeys = {}; state.browserAbort?.abort(); clearTimeout(state.searchPoll); state.searchGeneration++; state.searchPollCount = 0;
  state.galleryRequestInFlight = false;
  state.assetRequest++; state.groupRequest++;
  state.groupPage = 1; state.scrollPositions = {}; window.scrollTo(0, 0);
  if (state.viewMode === "gallery") {
    state.galleryLoading = true;
    renderGalleryWindow({items: state.items, total: state.total, has_next: false, search: {state: "loading"}}, state.windowStart, state.windowColumns, state.windowHeight);
  }
  renderFilterButtons();
  loadCurrentView();
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
    clearTimeout(debounce); state.browserAbort?.abort(); clearTimeout(state.searchPoll); state.searchGeneration++; state.searchPollCount = 0;
    const value = $("search").value.trim(); state.semanticPending = state.semanticEnabled && Boolean(value);
    if (state.semanticPending) {
      const cold = ["available", "loading"].includes(state.searchState);
      showSearchStatus({state:cold ? "loading" : "searching", message:cold ? `Loading ${state.searchProvider || "OpenCLIP"}…` : "Searching…"});
    }
    $("sort-by").value = state.semanticPending ? "search" : "capture_time"; $("direction").value = "desc";
    if (!value) { state.semanticPending = false; refreshBrowser(); return; }
    const generation = state.searchGeneration; syncUrl();
    debounce = setTimeout(() => { if (generation === state.searchGeneration && $("search").value.trim()) { state.semanticPending = false; refreshBrowser(); } }, 250);
  });
  $("search").addEventListener("keydown", event => {
    if (event.key === "Enter" && $("search").value.trim()) { clearTimeout(debounce); state.semanticPending = false; refreshBrowser(); }
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
  mode = viewModeAvailable(mode) ? mode : "gallery";
  const previousMode = state.viewMode;
  if(state.workspace) {$('setup-view').classList.add("hidden");$('setup-header-summary').classList.add("hidden");$('workspace-view').classList.remove("hidden");["index","configure-workspace","workspace-crumb"].forEach(id=>$(id).classList.remove("hidden"));}
  if (load) {state.scrollPositions[state.viewMode] = window.scrollY; state.browserAbort?.abort(); clearTimeout(state.searchPoll);}
  state.viewMode = ["groups", "geo", "timeline", "vector"].includes(mode) ? mode : "gallery";
  document.body.classList.toggle("visualization-active", ["geo", "timeline", "vector"].includes(state.viewMode));
  $("gallery").classList.toggle("hidden", mode !== "gallery");
  $("groups-view").classList.toggle("hidden", mode !== "groups");
  $("geo-view").classList.toggle("hidden", mode !== "geo");
  $("timeline-view").classList.toggle("hidden", mode !== "timeline");
  $("vector-view").classList.toggle("hidden", mode !== "vector");
  renderVisualizationNavigation();
  syncUrl(); window.scrollTo(0, state.scrollPositions[mode] || 0);
  const focusing = Boolean(state.focusGroup);
  if (load) loadCurrentView().then(()=>{if(!focusing && state.viewMode===mode) window.scrollTo(0,state.scrollPositions[mode] || 0); requestAnimationFrame(()=>{performance.measure(`navigation:${mode}`,{start:started});console.debug(`navigation:${mode} ${(performance.now()-started).toFixed(1)} ms`);});});
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
    data.groups.forEach((group) => {
      const row = $("groups-list").querySelector('[data-group-id="' + CSS.escape(group.group_id) + '"]');
      row?.querySelectorAll(".group-member").forEach((member, index) => { if (group.members[index]) member.dataset.assetId = group.members[index].asset_id; });
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
  if (!item || !state.viewerGroupId) return;
  const params = filterParams("groups");
  params.set("group_id", state.viewerGroupId);
  const target = await api(`/api/browser/locate?${params}`);
  closeDialog($("viewer"));
  state.groupPage = target.found ? target.page : 1;
  state.focusGroup = state.viewerGroupId;
  setViewMode("groups");
  performance.measure("navigation:locate",{start:started});
  console.debug(`navigation:locate ${(performance.now()-started).toFixed(1)} ms`);
}
