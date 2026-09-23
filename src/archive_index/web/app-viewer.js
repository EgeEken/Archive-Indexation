let viewerCamera;

function ensureViewerCamera() {
  if (viewerCamera) return viewerCamera;
  viewerCamera = new SharedImageCamera({
    viewport: $("viewer-media-pane"),
    getImages: () => [$("viewer-media")?.querySelector("img.viewer-media")],
    onChange: change => {
      if (change.zoom != null) state.viewerZoom = change.zoom;
      if (change.panX != null) state.viewerPanX = change.panX;
      if (change.panY != null) state.viewerPanY = change.panY;
      if (change.dragging != null) state.dragging = change.dragging;
      if (change.clickSuppressed) {
        state.viewerClickSuppressed = true;
        setTimeout(() => { state.viewerClickSuppressed = false; }, 0);
      }
      $("viewer-media-pane").classList.toggle("zoomed", state.viewerZoom > 1);
      $("viewer-media-pane").classList.toggle("dragging", state.dragging);
    },
  });
  return viewerCamera;
}

function resetViewerZoom() {
  state.viewerZoom = 1;
  state.viewerPanX = 0;
  state.viewerPanY = 0;
  ensureViewerCamera().reset();
}

function visibleStrictGroupId(item) {
  return Number(item?.strict_group_member_count) >= 2 ? item.current_group_id : null;
}

function showViewer(index, items = state.items, context = { mode: "gallery" }) {
  if (context.mode === "visualization") {
    state.viewerSequenceIds = [...new Set(context.assetIds || items.map(item => item.asset_id))];
    state.viewerItems = Array(state.viewerSequenceIds.length).fill(null);
    for (const item of items) {
      if (!item) continue;
      const itemIndex = state.viewerSequenceIds.indexOf(item.asset_id);
      if (itemIndex >= 0) state.viewerItems[itemIndex] = item;
    }
  } else {
    state.viewerSequenceIds = [];
    state.viewerItems = [...items];
  }
  if (!state.viewerItems[index]) return;
  state.viewerIndex = index;
  state.viewerContext = context.mode || "gallery";
  state.viewerStart = context.start ?? (state.viewerContext === "gallery" ? state.windowStart : 0);
  state.viewerTotal = context.total ?? (state.viewerContext === "gallery" ? state.total : state.viewerItems.length);
  state.viewerFilterKey = state.viewerContext === "gallery" ? String(filterParams()) : null;
  state.viewerGroupId = context.groupId || visibleStrictGroupId(state.viewerItems[index]);
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
  const total = state.viewerContext === "gallery" ? state.viewerTotal : state.viewerContext === "visualization" ? state.viewerSequenceIds.length : state.viewerItems.length;
  const absoluteIndex = state.viewerStart + state.viewerIndex;
  $("viewer-title").innerHTML = filenameMarkup(item.filename);
  $("viewer-count").textContent = (absoluteIndex + 1) + " of " + total;
  $("viewer-previous").disabled = absoluteIndex <= 0;
  $("viewer-next").disabled = absoluteIndex >= total - 1;
  updateViewerReviewState();
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
    if (item.media_type === "video") {
      media.preload = "metadata";
      media.playsInline = true;
      media.classList.add("viewer-video");
    }
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

async function loadVisualizationViewerItem(index) {
  const assetId = state.viewerSequenceIds[index];
  if (!assetId) throw new Error("The requested asset is no longer available.");
  if (state.viewerItems[index]) return state.viewerItems[index];
  const asset = await api(`/api/assets/${encodeURIComponent(assetId)}`);
  const item = assetToViewerItem(asset);
  if (state.viewerSequenceIds[index] !== assetId) throw new Error("The viewer sequence changed.");
  state.viewerItems[index] = item;
  return item;
}

function applyViewerTransform(media) {
  if (!media || media.tagName !== "IMG") return;
  const camera = ensureViewerCamera();
  camera.zoom = state.viewerZoom;
  camera.panX = state.viewerPanX;
  camera.panY = state.viewerPanY;
  camera.setImages([media]);
  media.style.imageRendering = state.viewerSmooth ? "auto" : "pixelated";
  $("viewer-media-pane").classList.toggle("zoomed", state.viewerZoom > 1);
  $("viewer-media-pane").classList.toggle("dragging", state.dragging);
}

async function moveViewer(delta) {
  if (state.viewerContext === "gallery") {
    const target = state.viewerStart + state.viewerIndex + delta;
    if (target < 0 || target >= state.viewerTotal) return;
    if (target < state.viewerStart || target >= state.viewerStart + state.viewerItems.length) {
      try { await loadViewerPageAt(target); } catch (error) { $("viewer-title").textContent = error.message; return; }
    } else {
      state.viewerIndex = target - state.viewerStart;
    }
  } else if (state.viewerContext === "visualization") {
    const next = state.viewerIndex + delta;
    if (next < 0 || next >= state.viewerSequenceIds.length) return;
    try {
      await loadVisualizationViewerItem(next);
    } catch (error) {
      showToast(`Viewer navigation failed: ${error.message}`);
      return;
    }
    state.viewerIndex = next;
  } else {
    let next = state.viewerIndex + delta;
    if (state.viewerContext === "similar" && delta > 0 && next >= state.viewerItems.length && state.similar?.autoLoad && state.similar.hasNext) {
      await loadSimilarPage();
      next = state.viewerIndex + delta;
    }
    if (next < 0 || next >= state.viewerItems.length) return;
    state.viewerIndex = next;
  }
  resetViewerZoom();
  state.viewerDetail = null;
  renderViewer();
}

async function loadViewerPageAt(absoluteIndex) {
  const params = filterParams();
  const offset = Math.floor(absoluteIndex / state.viewerPageSize) * state.viewerPageSize;
  params.set("offset", String(offset));
  params.set("limit", String(state.viewerPageSize));
  const data = await api("/api/browser?" + params);
  if (!data.items.length || absoluteIndex >= data.total) throw new Error("The requested asset is no longer in this filtered result.");
  state.viewerItems = data.items;
  state.viewerStart = offset;
  state.viewerTotal = data.total;
  state.viewerIndex = absoluteIndex - offset;
}

function updateViewerReviewState() {
  const item = state.viewerItems[state.viewerIndex];
  if (!item) return;
  $("viewer-selection").innerHTML = selectionActionsMarkup(item);
  bindSelectionButtons($("viewer-selection"));
  $("viewer-similar").textContent = state.similar ? "Close similar assets" : "Show similar assets";
  $("viewer-similar").classList.remove("hidden");
  const similarSourceId = state.similar?.source?.asset_id;
  const canOpenNormally = state.viewerContext === "similar" && item.asset_id !== similarSourceId;
  $("viewer-open-normal")?.classList.toggle("hidden", !canOpenNormally);
}

function stopViewerMedia() {
  $("viewer-media").querySelectorAll("video").forEach(video => {
    video.pause(); video.removeAttribute("src"); video.load();
  });
}

function closeDialog(dialog) {
  if (dialog.id === "viewer") {
    if (state.viewerCloseInFlight) return;
    state.viewerCloseInFlight = true;
    prepareViewerReturn().catch(error => showToast("Viewer return failed: " + error.message)).finally(() => {
      state.viewerCloseInFlight = false;
      finishCloseDialog(dialog);
    });
    return;
  }
  finishCloseDialog(dialog);
}

function finishCloseDialog(dialog) {
  if (dialog.id === "viewer") {
    stopViewerMedia();
    state.similar = null;
    state.similarSource = null;
    dialog.onscroll = null;
    $("similar-gallery").classList.add("hidden");
    $("viewer-toast-region").replaceChildren();
  }
  if (dialog.open) dialog.close();
  if (!["viewer", "details", "representation-comparison", "file-management-dialog", "problems-dialog", "offline-dialog", "remove-workspace-dialog"].some((id) => $(id).open)) document.body.classList.remove("modal-open");
  if (dialog.id === "viewer") {
    highlightViewerReturn(state.viewerReturnAssetId);
    state.viewerReturnAssetId = null;
    if (state.viewDirty) {
      state.viewDirty = false;
      loadCurrentView();
    }
  }
}

async function prepareViewerReturn() {
  const item = state.viewerItems[state.viewerIndex];
  if (!item) return;
  state.viewerReturnAssetId = item.asset_id;
  if (state.viewerContext === "gallery") {
    const absolute = state.viewerStart + state.viewerIndex;
    const params = filterParams();
    params.set("offset", String(Math.floor(absolute / state.viewerPageSize) * state.viewerPageSize));
    params.set("limit", String(state.viewerPageSize));
    const data = await api("/api/browser?" + params);
    if (data.items.length) {
      state.items = data.items;
      state.total = data.total;
      state.windowStart = Number(params.get("offset"));
      state.windowHasNext = Boolean(data.has_next);
      renderGalleryWindow({...data, items: state.items}, state.windowStart, state.windowColumns, state.windowHeight);
      syncUrl();
    }
  } else if (state.viewerContext === "group") {
    document.querySelector(".group-member[data-asset-id=\"" + CSS.escape(item.asset_id) + "\"]")?.scrollIntoView({block: "center", behavior: "instant"});
  }
}

function highlightViewerReturn(assetId) {
  if (!assetId) return;
  const target = document.querySelector("[data-asset-id=\"" + CSS.escape(assetId) + "\"]");
  if (!target) return;
  target.classList.remove("viewer-return-focus");
  void target.offsetWidth;
  target.classList.add("viewer-return-focus");
  target.scrollIntoView({block: "center", behavior: "instant"});
  setTimeout(() => target.classList.remove("viewer-return-focus"), 1200);
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
    bindRepresentationActions($("details"), asset);
    $("details").querySelector("[data-detail-thumbnail]")?.addEventListener("click", () => {
      closeDialog($("details"));
      const item = context.items?.[context.index] || assetToViewerItem(asset);
      showViewer(context.index || 0, context.items || [item], {...context, total: context.total ?? (context.mode === "gallery" ? state.total : (context.items || [item]).length)});
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
  const heading = similar.loading && !similar.items.length ? "Similar assets" : strong ? countLabel(strong, "similar asset found", "similar assets found") : "No strongly similar assets found";
  const loadMore = similar.loading
    ? '<div class="loading-state" role="status"><span class="spinner" aria-hidden="true"></span>Loading similar assets…</div>'
    : similar.hasNext && similar.autoLoad
      ? '<div class="loading-state" role="status"><span class="spinner" aria-hidden="true"></span>Scroll for more</div>'
      : similar.hasNext
        ? '<button id="similar-more" class="secondary" type="button">Load more</button>'
        : '';
  const cards = similar.items.map((item, index) => {
    const media = item.thumbnail_url
      ? '<img src="' + escapeHtml(item.thumbnail_url) + '" alt="' + escapeHtml(item.filename) + '">'
      : '<div class="similar-placeholder">Preview unavailable</div>';
    const score = typeof scoreMarkup === "function" ? scoreMarkup(item.quality_score) : "";
    const reviewState = typeof selectionStateMarkup === "function" ? selectionStateMarkup(item) : "";
    const reviewActions = typeof selectionActionsMarkup === "function" ? selectionActionsMarkup(item) : "";
    return '<article class="similar-result"><button type="button" class="similar-result-media" data-similar-index="' + index + '" aria-label="Open ' + escapeHtml(item.filename) + '">' + media + '</button><div class="similar-result-body"><div class="filename" title="' + escapeHtml(item.filename) + '">' + filenameMarkup(item.filename) + '</div><div class="card-metrics">' + score + similarityMarkup(item) + '</div><div class="card-state">' + reviewState + '</div><div class="card-actions">' + reviewActions + '</div></div></article>';
  }).join("");
  section.innerHTML = '<div class="dialog-header"><h3>' + heading + '</h3><button id="similar-close" type="button">Close similar assets</button></div><div class="similar-grid">' + cards + '</div>' + loadMore;
  $("similar-close").onclick = closeSimilar;
  $("similar-more")?.addEventListener("click", () => { similar.autoLoad = true; loadSimilarPage(); });
  section.querySelectorAll("[data-similar-index]").forEach(button => button.onclick = () => {
    showViewer(Number(button.dataset.similarIndex) + 1, similarViewerItems(), {mode: "similar", keepSimilar:true});
  });
  if (typeof bindSelectionButtons === "function") bindSelectionButtons(section);
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
  const current = state.viewerItems[state.viewerIndex];
  if (!current) return;
  $("viewer-count").textContent = (state.viewerIndex + 1) + " of " + state.viewerItems.length;
  $("viewer-previous").disabled = state.viewerIndex <= 0;
  $("viewer-next").disabled = state.viewerIndex >= state.viewerItems.length - 1;
  updateViewerReviewState();
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

async function openSimilarNormally() {
  if (!state.similar || state.viewerContext !== "similar") return;
  const item = state.viewerItems[state.viewerIndex];
  if (!item || item.asset_id === state.similar.source.asset_id) return;
  const assetId = item.asset_id;
  state.similar = null;
  state.similarSource = null;
  $("viewer").onscroll = null;
  $("similar-gallery").classList.add("hidden");
  try {
    const params = filterParams();
    params.set("asset_id", assetId);
    const located = await api("/api/browser/locate-asset?" + params);
    if (located.found) {
      const pageParams = new URLSearchParams(params);
      pageParams.set("offset", String(located.offset));
      pageParams.set("limit", String(state.viewerPageSize));
      const page = await api("/api/browser?" + pageParams);
      state.viewerItems = page.items;
      state.viewerStart = located.offset;
      state.viewerTotal = page.total;
      state.viewerIndex = located.index - located.offset;
      state.viewerContext = "gallery";
      state.viewerGroupId = visibleStrictGroupId(item);
      renderViewer();
      return;
    }
  } catch (error) {
    showToast("Normal viewer lookup failed: " + error.message);
  }
  state.viewerContext = "single";
  state.viewerStart = 0;
  state.viewerTotal = 1;
  state.viewerItems = [item];
  state.viewerIndex = 0;
  state.viewerGroupId = visibleStrictGroupId(item);
  renderViewer();
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
      if (similar.error) { showToast(`Similar assets failed: ${similar.error}`); similar.error = null; }
    }
  }
}

async function showSimilar(assetId) {
  const section = $("similar-gallery");
  const source = state.viewerItems[state.viewerIndex];
  state.similarSource = {items: state.viewerItems, index: state.viewerIndex, context: {mode:state.viewerContext, groupId:state.viewerGroupId, start:state.viewerStart, assetIds: state.viewerContext === "visualization" ? state.viewerSequenceIds : undefined, total: state.viewerContext === "visualization" ? state.viewerSequenceIds.length : undefined}};
  state.similar = {assetId, source, items: [], offset: 0, total: 0, strongCount: 0, hasNext: true, initial: true, loading: false, autoLoad: false, error: null};
  section.classList.remove("hidden");
  section.textContent = "";
  $("viewer-similar").textContent = "Close similar assets";
  showViewer(0, [source], {mode: "similar", keepSimilar:true});
  $("viewer").onscroll = () => {
    if (state.similar?.autoLoad && $("viewer").scrollTop + $("viewer").clientHeight >= $("viewer").scrollHeight - 240) loadSimilarPage();
  };
  await loadSimilarPage();
}
