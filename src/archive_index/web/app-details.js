function assetToViewerItem(asset) {
  const files = asset.physical_files || [];
  const first = files.find(file => file.is_preferred)
    || files.find(file => file.is_online && file.in_scope !== false)
    || files[0]
    || {};
  return {
    asset_id: asset.asset_id,
    media_type: asset.media_type,
    filename: first.filename || "Asset",
    thumbnail_url: asset.thumbnail_url || first.thumbnail_url,
    original_url: asset.original_url || first.original_url,
    display_url: asset.display_url || first.display_url || first.original_url,
    preferred_physical_id: first.id || asset.preferred_physical_id || null,
    codec: first.codec || asset.codec,
    quality_score: first.quality_score ?? asset.quality_score,
    issues: asset.issues || [],
    current_group_id: asset.current_group_id,
    strict_group_member_count: asset.strict_group_member_count,
    user_decision: asset.user_decision,
    auto_recommended: asset.auto_recommended,
  };
}

function bindRepresentationActions(container, asset) {
  container.querySelectorAll("[data-representation-view]").forEach(button => button.addEventListener("click", () => {
    const file = asset.physical_files?.find(candidate => candidate.id === button.dataset.representationView);
    if (!file) return;
    if (!file.is_online || file.in_scope === false) {
      openOfflineRepresentation(file);
    } else if (isRawRepresentation(file)) {
      openRawInspection(asset, file.id);
    } else if (file.is_preferred && asset._context) {
      const context = asset._context;
      const items = context.items?.length ? context.items.map(item => item.display_url || item.original_url ? item : assetToViewerItem(item)) : [assetToViewerItem(asset)];
      const index = Math.max(0, Math.min(Number(context.index) || 0, items.length - 1));
      $("details").close();
      showViewer(index, items, context);
    } else {
      openRepresentationInspection(asset, file.id);
    }
  }));
}

const RAW_EXTENSIONS = new Set([".arw", ".cr2", ".cr3", ".dng", ".nef", ".raf", ".rw2"]);
function isRawRepresentation(file) { return RAW_EXTENSIONS.has(file.extension?.toLowerCase()); }
function compatibleRepresentations(asset, fileId) {
  return (asset.physical_files || []).filter(file => file.id !== fileId && file.media_type === "image" && file.is_online && !isRawRepresentation(file));
}
function defaultComparisonTarget(asset, fileId) {
  return compatibleRepresentations(asset, fileId).find(file => file.is_preferred) || null;
}
function openOfflineRepresentation(file) {
  const dialog = $("representation-comparison");
  ensureRepresentationDialogLifecycle(dialog);
  disposeRepresentationDialog(dialog);
  const format = isRawRepresentation(file) ? "RAW source" : (String(file.extension || "").replace(".", "").toUpperCase() || "File") + " representation";
  dialog.innerHTML = `<div class="dialog-inner representation-offline"><div class="dialog-header"><div><h2>${escapeHtml(file.filename)}</h2><p class="muted">${escapeHtml(format)} (${escapeHtml(formatBytes(file.size_bytes))})</p></div><button class="icon" type="button" data-comparison-close aria-label="Close representation">×</button></div><div class="representation-offline-message"><strong>This representation is offline.</strong><p>Reconnect the storage containing this file to view it.</p></div></div>`;
  dialog.showModal();
  dialog.querySelector("[data-comparison-close]").onclick = () => dialog.close();
}
function representationPreviewUrl(fileId) { return apiPath(`/api/files/${encodeURIComponent(fileId)}/comparison-preview`); }
const displayPreviewUrl = file => file.display_preview_url || file.thumbnail_url || file.original_url;
const showViewerForRepresentation = (asset, file) => ({...assetToViewerItem(asset), filename: file.filename, preferred_physical_id: file.id, display_url: displayPreviewUrl(file)});

function comparisonMseColor(value) {
  const stops = [[0, [121, 216, 155]], [50, [226, 199, 85]], [100, [232, 110, 110]]];
  const mse = Math.max(0, Number(value));
  const upper = stops.findIndex(stop => mse <= stop[0]);
  if (upper <= 0) return `rgb(${stops[0][1].join(",")})`;
  const lower = stops[upper - 1] || stops.at(-1);
  const high = stops[upper] || stops.at(-1);
  const ratio = Math.max(0, Math.min(1, (mse - lower[0]) / (high[0] - lower[0] || 1)));
  const rgb = lower[1].map((channel, index) => Math.round(channel + (high[1][index] - channel) * ratio));
  return `rgb(${rgb.join(",")})`;
}

function disposeRepresentationDialog(dialog) {
  const rawImage = dialog.querySelector("[data-raw-image]");
  if (rawImage?.dataset.objectUrl) URL.revokeObjectURL(rawImage.dataset.objectUrl);
  dialog._comparisonCamera?.destroy();
  dialog._rawCamera?.destroy();
  dialog._comparisonCamera = null;
  dialog._rawCamera = null;
  dialog._comparisonData = null;
  dialog._comparisonDataKey = null;
  dialog._comparisonPairKey = null;
  dialog._previewData = null;
  dialog._rawGeneration = (dialog._rawGeneration || 0) + 1;
  dialog._rawAbort?.abort();
  dialog._rawAbort = null;
}

function ensureRepresentationDialogLifecycle(dialog) {
  if (dialog._lifecycleBound) return;
  dialog._lifecycleBound = true;
  dialog.addEventListener("close", () => disposeRepresentationDialog(dialog));
}

function openRepresentationInspection(asset, fileId) {
  const file = asset.physical_files?.find(candidate => candidate.id === fileId);
  if (!file) return;
  if (!file.is_online || file.in_scope === false) return openOfflineRepresentation(file);
  if (isRawRepresentation(file)) return openRawInspection(asset, fileId);
  const target = defaultComparisonTarget(asset, fileId);
  const dialog = $("representation-comparison");
  ensureRepresentationDialogLifecycle(dialog);
  disposeRepresentationDialog(dialog);
  const relationship = file.relationships?.includes("External JPEG XL representation") ? "External JPEG XL representation · Lineage unknown" : file.representation_label || file.role || "Physical file";
  const comparisonOnly = !file.is_preferred && Boolean(target);
  const controls = target ? `<div class="comparison-toolbar"><div class="comparison-mode" role="group" aria-label="Comparison mode"><button class="secondary active" type="button" data-compare-mode="slider">Slider</button><button class="secondary" type="button" data-compare-mode="side">Side by side</button></div></div><div data-comparison-result></div>` : `<p class="muted comparison-unavailable">Preferred representation is unavailable for comparison.</p>`;
  const standalone = `<div class="representation-preview-wrap"><img src="${escapeHtml(representationPreviewUrl(file.id))}" alt="${escapeHtml(file.filename)}"></div><p class="muted representation-file-size">${escapeHtml(formatBytes(file.size_bytes))}</p><div class="representation-inspection-actions"><button class="secondary" type="button" data-representation-open-viewer>Open</button><button class="secondary" type="button" data-representation-details>Details</button></div>`;
  dialog.innerHTML = `<div class="dialog-inner representation-inspection${comparisonOnly ? " comparison-only" : ""}"><div class="dialog-header"><div><h2>${escapeHtml(file.filename)}</h2><p class="muted">${escapeHtml(relationship)}</p>${comparisonOnly ? `<div class="comparison-header-metrics" data-comparison-metrics>Calculating comparison…</div>` : ""}</div><button class="icon" type="button" data-comparison-close aria-label="Close representation">×</button></div>${comparisonOnly ? controls : standalone + (target ? controls : "")}</div>`;
  dialog.showModal();
  dialog.querySelector("[data-comparison-close]").onclick = () => dialog.close();
  const openButton = dialog.querySelector("[data-representation-open-viewer]");
  if (openButton) openButton.onclick = () => {
    dialog.close();
    const context = asset._context || {mode: "visualization", assetIds: [asset.asset_id], total: 1};
    showViewer(0, [showViewerForRepresentation(asset, file)], {...context, assetIds: [asset.asset_id], total: 1});
  };
  const detailsButton = dialog.querySelector("[data-representation-details]");
  if (detailsButton) detailsButton.onclick = () => {
    dialog.close();
    showDetails(asset.asset_id, {mode: "visualization", items: [asset], index: 0, total: 1});
  };
  dialog.querySelectorAll("[data-compare-mode]").forEach(button => button.addEventListener("click", () => {
    dialog.querySelectorAll("[data-compare-mode]").forEach(candidate => candidate.classList.toggle("active", candidate === button));
    renderRepresentationComparison(asset, fileId, target.id, button.dataset.compareMode);
  }));
  if (comparisonOnly) renderRepresentationComparison(asset, fileId, target.id, "slider");
}

function isCompressedRepresentation(file) {
  const extension = file?.extension?.toLowerCase() || (file?.format ? `.${file.format.toLowerCase()}` : "");
  return [".jxl", ".avif", ".webp"].includes(extension);
}
function comparisonFactor(value) {
  const number = Number(value);
  return number.toFixed(number < 1.01 ? 3 : 2).replace(/\.?0+$/, "");
}
function comparisonMetricsMarkup(metrics, comparedFile) {
  const referenceBytes = Number(metrics.reference_bytes || 0);
  const comparedBytes = Number(metrics.compressed_bytes || 0);
  const sizeLabel = comparedBytes === referenceBytes
    ? "Same size"
    : comparedBytes < referenceBytes
      ? `${comparisonFactor(referenceBytes / comparedBytes)}× smaller`
      : `${comparisonFactor(comparedBytes / referenceBytes)}× larger`;
  const comparedLabel = isCompressedRepresentation(comparedFile) ? "Compressed" : "Compared";
  const percent = metrics.compressed_percent == null ? "Unavailable" : `${metrics.compressed_percent}% of reference`;
  const mse = metrics.mse == null ? `<span class="comparison-metric-mse">MSE Unavailable</span>` : `<span class="comparison-metric-mse" style="color:${escapeHtml(comparisonMseColor(metrics.mse))}">MSE ${escapeHtml(metrics.mse)}</span>`;
  const identity = metrics.byte_identical ? "Exact duplicate of preferred representation" : metrics.pixel_identical ? "Pixel-identical to preferred representation" : "";
  const parts = [`Reference ${escapeHtml(formatBytes(referenceBytes))} → ${comparedLabel} ${escapeHtml(formatBytes(comparedBytes))}`, `<span class="comparison-metric-ratio">${escapeHtml(sizeLabel)}</span>`, `<span class="comparison-metric-percent">${escapeHtml(percent)}</span>`, mse];
  return `${identity ? `<div class="comparison-metric-identity">${identity}</div>` : ""}<div class="comparison-metric-row">${parts.join(" · ")}</div>`;
}

function comparisonLabel(file) { return `${escapeHtml(file.filename)} · ${escapeHtml(formatBytes(file.size_bytes))}`; }

async function renderRepresentationComparison(asset, compressedId, referenceId, mode = "slider", suppliedData = null) {
  const dialog = $("representation-comparison");
  const result = dialog.querySelector("[data-comparison-result]");
  if (!result) return;
  const pairKey = suppliedData ? `preview:${suppliedData.profile_id || suppliedData.profile_name}` : `${referenceId}:${compressedId}`;
  const samePair = dialog._comparisonPairKey === pairKey;
  const previousCamera = samePair ? dialog._comparisonCamera : null;
  const cameraState = previousCamera ? {zoom: previousCamera.zoom, panX: previousCamera.panX, panY: previousCamera.panY} : null;
  const requestToken = (dialog._comparisonRequestToken || 0) + 1;
  dialog._comparisonRequestToken = requestToken;
  try {
    const dataKey = suppliedData ? pairKey : `${compressedId}:${referenceId}`;
    const data = suppliedData || (dialog._comparisonDataKey === dataKey ? dialog._comparisonData : await api(`/api/files/${encodeURIComponent(compressedId)}/comparison?with_id=${encodeURIComponent(referenceId)}`));
    if (dialog._comparisonRequestToken !== requestToken) return;
    dialog._comparisonData = data;
    dialog._comparisonDataKey = dataKey;
    dialog._comparisonPairKey = pairKey;
    const metrics = data.metrics || {};
    const reference = data.reference || data.left;
    const compressed = data.compressed || data.right;
    const headerMetrics = dialog.querySelector("[data-comparison-metrics]");
    if (headerMetrics) headerMetrics.innerHTML = comparisonMetricsMarkup(metrics, compressed);
    const referenceLabel = comparisonLabel(reference);
    const compressedLabel = comparisonLabel(compressed);
    const stage = mode === "slider" ? `<div class="comparison-slider" data-comparison-viewport><div class="comparison-slider-frame" data-comparison-frame><img class="comparison-slider-sizer" src="${escapeHtml(reference.preview_url)}" alt="" aria-hidden="true"><div class="comparison-slider-base"><img class="comparison-compressed" data-comparison-image src="${escapeHtml(compressed.preview_url)}" alt="${escapeHtml(compressed.filename)}"></div><div class="comparison-wipe-top" data-wipe-top><img class="comparison-reference" data-comparison-image src="${escapeHtml(reference.preview_url)}" alt="${escapeHtml(reference.filename)}"></div><button class="comparison-divider" data-wipe-handle type="button" aria-label="Move comparison divider" aria-valuemin="0" aria-valuemax="100" aria-valuenow="50"><span></span></button></div><span class="comparison-wipe-label comparison-wipe-label-left">${referenceLabel}</span><span class="comparison-wipe-label comparison-wipe-label-right">${compressedLabel}</span></div>` : `<div class="comparison-stage" data-comparison-viewport><div class="comparison-pane"><span class="comparison-wipe-label comparison-wipe-label-left">${referenceLabel}</span><img class="comparison-reference" data-comparison-image src="${escapeHtml(reference.preview_url)}" alt="${escapeHtml(reference.filename)}"></div><div class="comparison-pane"><span class="comparison-wipe-label comparison-wipe-label-right">${compressedLabel}</span><img class="comparison-compressed" data-comparison-image src="${escapeHtml(compressed.preview_url)}" alt="${escapeHtml(compressed.filename)}"></div></div>`;
    dialog._comparisonCamera?.destroy();
    dialog._comparisonCamera = null;
    result.innerHTML = stage;
    const viewport = result.querySelector("[data-comparison-viewport]");
    const camera = new SharedImageCamera({
      viewport,
      getFrames: () => mode === "slider"
        ? [...result.querySelectorAll("[data-comparison-image]")].map(image => ({image, frame: result.querySelector("[data-comparison-frame]") || viewport}))
        : [...result.querySelectorAll(".comparison-pane")].map(frame => ({image: frame.querySelector("img"), frame})),
      onChange: change => result.querySelectorAll("img").forEach(image => { image.style.imageRendering = change.zoom > 1 ? "pixelated" : "auto"; }),
    });
    dialog._comparisonCamera = camera;
    if (cameraState) Object.assign(camera, cameraState);
    camera.setImages([...result.querySelectorAll("[data-comparison-image]")]);
    result.querySelectorAll("img").forEach(image => image.addEventListener("load", () => camera.apply(), {once: true}));
    const wipe = result.querySelector("[data-wipe-top]");
    const handle = result.querySelector("[data-wipe-handle]");
    if (wipe && handle) {
      const setWipe = value => {
        const position = Math.max(0, Math.min(100, Number(value)));
        dialog._comparisonWipePosition = position;
        wipe.style.clipPath = `inset(0 ${100 - position}% 0 0)`;
        handle.style.left = `${position}%`;
        handle.setAttribute("aria-valuenow", String(Math.round(position)));
      };
      setWipe(samePair && dialog._comparisonWipePosition != null ? dialog._comparisonWipePosition : 50);
      let wiping = false;
      handle.addEventListener("pointerdown", event => { wiping = true; handle.setPointerCapture(event.pointerId); event.preventDefault(); });
      handle.addEventListener("pointermove", event => { if (!wiping) return; const rect = viewport.getBoundingClientRect(); setWipe((event.clientX - rect.left) / rect.width * 100); });
      ["pointerup", "pointercancel"].forEach(name => handle.addEventListener(name, () => { wiping = false; }));
      handle.addEventListener("keydown", event => { if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return; event.preventDefault(); setWipe(Number(handle.getAttribute("aria-valuenow")) + (event.key === "ArrowRight" ? 5 : -5)); });
    }
  } catch (error) {
    if (dialog._comparisonRequestToken !== requestToken) return;
    const headerMetrics = dialog.querySelector("[data-comparison-metrics]");
    if (headerMetrics) headerMetrics.textContent = "Comparison unavailable";
    result.innerHTML = `<p class="error">Comparison unavailable: ${escapeHtml(error.message)}</p>`;
  }
}

async function openCompressionProfilePreview(profile) {
  if (!profile || !["jpeg-xl", "avif"].includes(profile.codec)) return;
  const dialog = $("representation-comparison");
  ensureRepresentationDialogLifecycle(dialog);
  disposeRepresentationDialog(dialog);
  dialog._profilePreviewToken = (dialog._profilePreviewToken || 0) + 1;
  const token = dialog._profilePreviewToken;
  dialog.innerHTML = `<div class="dialog-inner compression-profile-preview"><div class="dialog-header"><div><h2>Compression preset preview</h2><p class="muted" data-preview-status>Preparing preview…</p></div><button class="icon" type="button" data-comparison-close aria-label="Close compression preset preview">×</button></div><div class="profile-preview-tabs" role="tablist"></div><div class="compression-preview-info" data-preview-info></div><div class="comparison-header-metrics" data-comparison-metrics>Calculating comparison…</div><div class="comparison-toolbar"><div class="comparison-mode" role="group" aria-label="Comparison mode"><button class="secondary active" type="button" data-preview-mode="slider">Slider</button><button class="secondary" type="button" data-preview-mode="side">Side by side</button></div></div><div data-comparison-result></div></div>`;
  dialog.showModal();
  dialog.querySelector("[data-comparison-close]").onclick = () => dialog.close();
  const status = dialog.querySelector("[data-preview-status]");
  const info = dialog.querySelector("[data-preview-info]");
  const mode = {value: "slider"};
  let manifest;
  try {
    manifest = profile.is_builtin
      ? await fetch("/assets/compression-preview/manifest.json").then(response => response.json())
      : null;
    if (token !== dialog._profilePreviewToken) return;
    const profiles = manifest?.profiles || [profile];
    const tabs = dialog.querySelector("[role=tablist]");
    tabs.innerHTML = profiles.map(item => `<button class="secondary${item.id === profile.id ? " active" : ""}" type="button" data-preview-profile="${escapeHtml(item.id)}">${escapeHtml(item.name)}</button>`).join("");
    const loadProfile = async selected => {
      const selectedProfile = fileManagement.profiles?.find(item => item.id === selected) || profile;
      const entry = manifest?.profiles?.find(item => item.id === selected);
      const data = entry || await api(`/api/file-management/profile-preview?profile_id=${encodeURIComponent(selected)}`);
      if (token !== dialog._profilePreviewToken) return;
      data.profile_id = selected;
      data.reference.extension = ".jpg";
      data.compressed.extension = data.codec === "jpeg-xl" ? ".jxl" : ".avif";
      data.reference.preview_url = data.reference.url;
      data.compressed.preview_url = data.compressed.url.startsWith("/api/") ? apiPath(data.compressed.url) : data.compressed.url;
      dialog._previewData = data;
      info.innerHTML = `<strong>${escapeHtml(data.profile_name || selectedProfile.name)}</strong><span>${data.settings?.quality != null ? `Quality ${escapeHtml(data.settings.quality)} · Effort ${escapeHtml(data.settings.effort ?? "—")}` : escapeHtml(selectedProfile.codec.toUpperCase())}</span>`;
      status.textContent = "Reference and actual encoded output";
      await renderRepresentationComparison(null, "", "", mode.value, data);
    };
    dialog.querySelectorAll("[data-preview-profile]").forEach(button => button.addEventListener("click", async () => {
      dialog.querySelectorAll("[data-preview-profile]").forEach(item => item.classList.toggle("active", item === button));
      await loadProfile(button.dataset.previewProfile);
    }));
    dialog.querySelectorAll("[data-preview-mode]").forEach(button => button.addEventListener("click", async () => {
      mode.value = button.dataset.previewMode;
      dialog.querySelectorAll("[data-preview-mode]").forEach(item => item.classList.toggle("active", item === button));
      const current = dialog._comparisonData;
      if (current) await renderRepresentationComparison(null, "", "", mode.value, current);
    }));
    await loadProfile(profile.id);
  } catch (error) {
    if (token !== dialog._profilePreviewToken) return;
    status.textContent = "Preview unavailable";
    dialog.querySelector("[data-comparison-result]").innerHTML = `<p class="error">Preview unavailable — ${escapeHtml(error.message)}</p>`;
  }
}

function rawExposureLabel(value) { return `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)} EV`; }

function openRawInspection(asset, fileId) {
  const file = asset.physical_files?.find(candidate => candidate.id === fileId);
  if (!file) return;
  if (!file.is_online || file.in_scope === false) return openOfflineRepresentation(file);
  const dialog = $("representation-comparison");
  ensureRepresentationDialogLifecycle(dialog);
  disposeRepresentationDialog(dialog);
  dialog.innerHTML = `<div class="dialog-inner raw-inspection"><div class="dialog-header"><div><h2>${escapeHtml(file.filename)}</h2><p class="muted">RAW source (${escapeHtml(formatBytes(file.size_bytes))})</p></div><button class="icon" type="button" data-comparison-close aria-label="Close RAW viewer">×</button></div><div class="raw-inspection-stage" data-raw-stage><img class="hidden" data-raw-image alt="${escapeHtml(file.filename)}"><p class="raw-error hidden" data-raw-error>RAW development is unavailable for this file.</p></div><label class="raw-exposure-control"><span>Exposure</span><span class="raw-exposure-readout"><output data-raw-exposure-label><span data-raw-exposure-value>+0.00 EV</span></output><span class="raw-loading-inline hidden" data-raw-loading>Loading preview…</span></span><input data-raw-exposure type="range" min="-5" max="5" step="0.25" value="0" aria-label="RAW exposure"></label></div>`;
  dialog.showModal();
  dialog.querySelector("[data-comparison-close]").onclick = () => dialog.close();
  const stage = dialog.querySelector("[data-raw-stage]");
  const image = dialog.querySelector("[data-raw-image]");
  const loading = dialog.querySelector("[data-raw-loading]");
  const errorNode = dialog.querySelector("[data-raw-error]");
  const valueNode = dialog.querySelector("[data-raw-exposure-value]");
  const input = dialog.querySelector("[data-raw-exposure]");
  const updateExposureControl = () => {
    const value = Number(input.value);
    valueNode.textContent = rawExposureLabel(value);
    input.style.setProperty("--range-progress", `${((value + 5) / 10) * 100}%`);
  };
  const camera = new SharedImageCamera({viewport: stage, getFrames: () => [{image, frame: stage}], onChange: change => { image.style.imageRendering = change.zoom > 1 ? "pixelated" : "auto"; }});
  dialog._rawCamera = camera;
  camera.setImages([image]);
  let timer = null;
  const load = async exposure => {
    const generation = (dialog._rawGeneration || 0) + 1;
    dialog._rawGeneration = generation;
    dialog._rawAbort?.abort();
    const controller = new AbortController();
    dialog._rawAbort = controller;
    loading.classList.remove("hidden");
    errorNode.classList.add("hidden");
    try {
      const response = await fetch(apiPath(`/api/files/${encodeURIComponent(file.id)}/raw-development-preview?exposure_ev=${encodeURIComponent(exposure)}`), {signal: controller.signal});
      if (!response.ok) throw new Error("RAW development is unavailable for this file.");
      const blob = await response.blob();
      if (generation !== dialog._rawGeneration || controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const previous = image.dataset.objectUrl;
      image.dataset.objectUrl = url;
      image.onload = () => { camera.apply(); if (previous) URL.revokeObjectURL(previous); };
      image.src = url;
      image.classList.remove("hidden");
    } catch (error) {
      if (error.name === "AbortError" || generation !== dialog._rawGeneration) return;
      errorNode.textContent = error.message || "RAW development is unavailable for this file.";
      errorNode.classList.remove("hidden");
    } finally {
      if (generation === dialog._rawGeneration) loading.classList.add("hidden");
    }
  };
  input.addEventListener("input", () => {
    updateExposureControl();
    clearTimeout(timer);
    timer = setTimeout(() => load(Number(input.value)), 150);
  });
  updateExposureControl();
  load(0);
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
    const eye = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M2.5 12s3.4-5.5 9.5-5.5 9.5 5.5 9.5 5.5-3.4 5.5-9.5 5.5S2.5 12 2.5 12Z" fill="none" stroke="currentColor" stroke-width="1.8"/><circle cx="12" cy="12" r="2.5" fill="none" stroke="currentColor" stroke-width="1.8"/></svg>';
    return `<div class="representation-row"><div><div class="representation-title"><strong>${filenameMarkup(file.filename)}</strong> · ${escapeHtml(formatBytes(file.size_bytes))}${file.is_preferred ? " · Preferred" : ""}${file.is_online ? "" : " · Offline"}</div><div class="muted representation-path">${escapeHtml(file.relative_path)}</div>${problems ? `<details class="representation-diagnostics"><summary>⚠ Representation status</summary>${problems}</details>` : ""}</div><div class="representation-actions"><button class="representation-eye" type="button" data-representation-view="${escapeHtml(file.id)}" aria-label="View representation" title="View representation">${eye}</button><button class="explorer-button" data-reveal="${escapeHtml(file.id)}" aria-label="Show in Explorer" title="Show in Explorer"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3 5.5h7l2 2H21v11H3Z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M3 9h18" fill="none" stroke="currentColor" stroke-width="1.8"/></svg></button></div></div>`;
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
      bindRepresentationActions($("viewer-details"), state.viewerDetail);
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
