$("workspace-form").addEventListener("submit", (event) => { event.preventDefault(); openWorkspace($("workspace-path").value.trim(), false); });
$("browse-workspace").addEventListener("click", pickWorkspace);
$("configure-workspace").addEventListener("click", configureWorkspace);
$("setup-cancel").addEventListener("click", cancelSetup);
$("setup-apply").addEventListener("click", applySetup);
$("index").addEventListener("click", () => state.setup ? applySetup() : startIndex());
$("problems-button").addEventListener("click", showProblems);
$("forget-offline-button").addEventListener("click", showOfflineCleanup);
$("gallery-view-toggle").addEventListener("click", () => setViewMode("gallery"));
$("geo-view-toggle").addEventListener("click", () => setViewMode("geo"));
$("timeline-view-toggle").addEventListener("click", () => setViewMode("timeline"));
$("timeline-mode-capture").addEventListener("click", () => switchTimelineMode("capture"));
$("timeline-mode-file-created").addEventListener("click", () => switchTimelineMode("file_created"));
$("vector-view-toggle").addEventListener("click", () => setViewMode("vector"));
$("viewer-similar").onclick = () => state.similar ? closeSimilar() : showSimilar(state.viewerItems[state.viewerIndex].asset_id);
$("viewer-open-normal").onclick = openSimilarNormally;
$("workspace-explorer").onclick = () => revealFile();
$("viewer-explorer").onclick = () => revealFile(state.viewerItems[state.viewerIndex]?.preferred_physical_id, {asset: true});
$("groups-view-toggle").addEventListener("click", () => setViewMode("groups"));
$("export-selected").addEventListener("click", () => { window.location.href = apiPath("/api/exports/selected.zip"); });

["top", "bottom"].forEach((place) => { $(`groups-previous-${place}`).addEventListener("click", () => { state.groupPage = Math.max(1, state.groupPage - 1); syncUrl(); loadGroups().then(() => window.scrollTo({ top: 0, behavior: "smooth" })); }); $(`groups-next-${place}`).addEventListener("click", () => { state.groupPage += 1; syncUrl(); loadGroups().then(() => window.scrollTo({ top: 0, behavior: "smooth" })); }); });
$("viewer-close").addEventListener("click", () => closeDialog($("viewer")));
$("viewer-previous").addEventListener("click", () => moveViewer(-1));
$("viewer-next").addEventListener("click", () => moveViewer(1));
$("viewer-info").addEventListener("click", () => toggleViewerInfo());
$("viewer-grouping").addEventListener("click", locateCurrentGroup);
$("viewer-smooth").addEventListener("change", (event) => { state.viewerSmooth = event.target.checked; applyViewerTransform($("viewer-media").querySelector("img.viewer-media")); });
$("viewer-stage").addEventListener("click", (event) => { if (state.viewerClickSuppressed) { state.viewerClickSuppressed = false; return; } if (event.target.closest("video") || $("viewer-media").querySelector("video")) return; if (["viewer-stage", "viewer-media-pane", "viewer-media"].includes(event.target.id)) closeDialog($("viewer")); });
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
["viewer", "details", "representation-comparison", "file-management-dialog", "problems-dialog", "offline-dialog", "remove-workspace-dialog"].forEach((id) => $(id).addEventListener("close", () => { if (!["viewer", "details", "representation-comparison", "file-management-dialog", "problems-dialog", "offline-dialog", "remove-workspace-dialog"].some((name) => $(name).open)) document.body.classList.remove("modal-open"); }));
["viewer", "details", "representation-comparison", "file-management-dialog", "problems-dialog", "offline-dialog", "remove-workspace-dialog"].forEach((id) => bindBackdropClose($(id)));
window.addEventListener("resize", () => applyViewerTransform($("viewer-media").querySelector("img.viewer-media")));
document.addEventListener("keydown", (event) => {
  if ($("details").open || $("representation-comparison").open || $("file-management-dialog").open || $("problems-dialog").open || $("offline-dialog").open || $("remove-workspace-dialog").open || ["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
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

async function revealFile(file_id, options = {}) {
  if (options.asset && !file_id) { showToast("This asset has no revealable physical file."); return; }
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

$("viewer").addEventListener("cancel", event => { event.preventDefault(); closeDialog($("viewer")); });
$("viewer").addEventListener("close", stopViewerMedia);
window.addEventListener("pagehide", stopViewerMedia);
