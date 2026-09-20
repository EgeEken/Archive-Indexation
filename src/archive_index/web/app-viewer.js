function resetViewerZoom() {
  state.viewerZoom = 1;
  state.viewerPanX = 0;
  state.viewerPanY = 0;
}

function showViewer(index, items = state.items, context = { mode: "gallery" }) {
  state.viewerItems = [...items];
  if (!state.viewerItems[index]) return;
  state.viewerIndex = index;
  state.viewerContext = context.mode || "gallery";
  state.viewerStart = context.start ?? (state.viewerContext === "gallery" ? state.windowStart : 0);
  state.viewerTotal = context.total ?? (state.viewerContext === "gallery" ? state.total : state.viewerItems.length);
  state.viewerFilterKey = state.viewerContext === "gallery" ? String(filterParams()) : null;
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
  const total = state.viewerContext === "gallery" ? state.viewerTotal : state.viewerItems.length;
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
  if (state.viewerContext === "gallery") {
    const target = state.viewerStart + state.viewerIndex + delta;
    if (target < 0 || target >= state.viewerTotal) return;
    if (target < state.viewerStart || target >= state.viewerStart + state.viewerItems.length) {
      try { await loadViewerPageAt(target); } catch (error) { $("viewer-title").textContent = error.message; return; }
    } else {
      state.viewerIndex = target - state.viewerStart;
    }
  } else {
    const next = state.viewerIndex + delta;
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
  if (!["viewer", "details", "problems-dialog", "offline-dialog", "remove-workspace-dialog"].some((id) => $(id).open)) document.body.classList.remove("modal-open");
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
