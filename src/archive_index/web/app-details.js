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
const comparisonDataCache = new Map();
const MAX_COMPARISON_DATA_CACHE = 6;
const differenceDataCache = new Map();
const MAX_DIFFERENCE_DATA_CACHE = 4;
const MAX_DIFFERENCE_DATA_CACHE_BYTES = 32 * 1024 * 1024;
let differenceDataCacheBytes = 0;

function comparisonFingerprint(file) {
  return [file.id, file.relative_path, file.size_bytes, file.mtime_ns, file.sha256, file.width, file.height, file.display_preview_version, file.display_preview_fingerprint];
}

function comparisonPairKey(compressed, reference) {
  return JSON.stringify(["comparison-metrics-v4", state.workspace || "", comparisonFingerprint(reference), comparisonFingerprint(compressed)]);
}

function comparisonDisplayData(compressed, reference) {
  const pairKey = comparisonPairKey(compressed, reference);
  const representation = file => ({
    id: file.id,
    filename: file.filename,
    format: String(file.extension || "").replace(".", "").toUpperCase(),
    size_bytes: file.size_bytes,
    width: file.width,
    height: file.height,
    preview_url: file.display_preview_url || file.original_url || file.thumbnail_url || representationPreviewUrl(file.id),
  });
  return {
    pairKey,
    reference: representation(reference),
    compressed: representation(compressed),
    metrics: null,
    difference_data_url: null,
    same_dimensions: reference.width === compressed.width && reference.height === compressed.height,
  };
}

function readComparisonCache(key) {
  const data = comparisonDataCache.get(key);
  if (!data) return null;
  comparisonDataCache.delete(key);
  comparisonDataCache.set(key, data);
  return data;
}

function writeComparisonCache(key, data) {
  comparisonDataCache.delete(key);
  comparisonDataCache.set(key, data);
  while (comparisonDataCache.size > MAX_COMPARISON_DATA_CACHE) comparisonDataCache.delete(comparisonDataCache.keys().next().value);
}

function readDifferenceCache(key) {
  const data = differenceDataCache.get(key);
  if (!data) return null;
  differenceDataCache.delete(key);
  differenceDataCache.set(key, data);
  return data;
}

function writeDifferenceCache(key, data) {
  if (data.size > MAX_DIFFERENCE_DATA_CACHE_BYTES) return false;
  const previous = differenceDataCache.get(key);
  if (previous) differenceDataCacheBytes -= previous.size;
  differenceDataCache.delete(key);
  differenceDataCache.set(key, data);
  differenceDataCacheBytes += data.size;
  while (differenceDataCache.size > MAX_DIFFERENCE_DATA_CACHE || differenceDataCacheBytes > MAX_DIFFERENCE_DATA_CACHE_BYTES) {
    const oldestKey = differenceDataCache.keys().next().value;
    const oldest = differenceDataCache.get(oldestKey);
    differenceDataCache.delete(oldestKey);
    differenceDataCacheBytes -= oldest.size;
    URL.revokeObjectURL(oldest.url);
  }
  return true;
}

function comparisonMseColor(value) {
  const stops = [[0, [121, 216, 155]], [50, [226, 199, 85]], [100, [232, 110, 110]]];
  const mse = Math.max(0, Number(value));
  if (mse >= stops.at(-1)[0]) return `rgb(${stops.at(-1)[1].join(",")})`;
  const upper = stops.findIndex(stop => mse <= stop[0]);
  if (upper < 0) return `rgb(${stops.at(-1)[1].join(",")})`;
  if (upper === 0) return `rgb(${stops[0][1].join(",")})`;
  const lower = stops[upper - 1] || stops.at(-1);
  const high = stops[upper] || stops.at(-1);
  const ratio = Math.max(0, Math.min(1, (mse - lower[0]) / (high[0] - lower[0] || 1)));
  const rgb = lower[1].map((channel, index) => Math.round(channel + (high[1][index] - channel) * ratio));
  return `rgb(${rgb.join(",")})`;
}

function disposeRepresentationDialog(dialog) {
  dialog.classList.remove("raw-inspection-dialog");
  const rawImage = dialog.querySelector("[data-raw-image]");
  if (rawImage?.dataset.objectUrl) URL.revokeObjectURL(rawImage.dataset.objectUrl);
  for (const url of dialog._ephemeralDifferenceUrls || []) URL.revokeObjectURL(url);
  dialog._ephemeralDifferenceUrls = [];
  dialog._comparisonCamera?.destroy();
  dialog._rawCamera?.destroy();
  dialog._comparisonCamera = null;
  dialog._rawCamera = null;
  dialog._comparisonData = null;
  dialog._comparisonDisplayData = null;
  dialog._comparisonDisplayError = false;
  dialog._comparisonViews = null;
  dialog._comparisonDataKey = null;
  dialog._comparisonPairKey = null;
  dialog._comparisonMode = null;
  dialog._comparisonRequestToken = (dialog._comparisonRequestToken || 0) + 1;
  dialog._comparisonAbort?.abort();
  dialog._comparisonAbort = null;
  dialog._differenceRequestToken = (dialog._differenceRequestToken || 0) + 1;
  dialog._differenceAbort?.abort();
  dialog._differenceAbort = null;
  dialog._differencePromise = null;
  dialog._differencePairKey = null;
  dialog._comparisonDifferenceError = null;
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
  const controls = target ? `<div class="comparison-toolbar"><div class="comparison-mode" role="group" aria-label="Comparison mode"><button class="secondary active" type="button" data-compare-mode="slider">Slider</button><button class="secondary" type="button" data-compare-mode="side">Side by side</button><button class="secondary" type="button" data-compare-mode="difference">Difference</button></div></div><div data-comparison-result></div>` : `<p class="muted comparison-unavailable">Preferred representation is unavailable for comparison.</p>`;
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
  if (comparisonOnly) {
    const initialData = comparisonDisplayData(file, target);
    dialog._comparisonDisplayData = initialData;
    renderRepresentationComparison(asset, fileId, target.id, "slider", initialData);
    loadRepresentationComparisonMetrics(asset, fileId, target.id, initialData);
  }
}

function isCompressedRepresentation(file) {
  const extension = file?.extension?.toLowerCase() || (file?.format ? `.${file.format.toLowerCase()}` : "");
  return [".jxl", ".avif", ".webp"].includes(extension);
}
function comparisonFactor(value) {
  const number = Number(value);
  return number.toFixed(number < 1.01 ? 3 : 2).replace(/\.?0+$/, "");
}
const comparisonLegendNumberFormat = new Intl.NumberFormat(undefined, {maximumFractionDigits: 2});
function comparisonLegendNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? comparisonLegendNumberFormat.format(number) : "—";
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
  const mseValue = metrics.mse == null ? NaN : Number(metrics.mse);
  const mse = Number.isFinite(mseValue) ? `<span class="comparison-metric-mse" style="color:${escapeHtml(comparisonMseColor(mseValue))}">MSE ${Math.round(mseValue)}</span>` : `<span class="comparison-metric-mse">MSE unavailable</span>`;
  const identity = metrics.byte_identical ? "Exact duplicate of preferred representation" : metrics.pixel_identical ? "Pixel-identical to preferred representation" : "";
  const parts = [`Reference ${escapeHtml(formatBytes(referenceBytes))} → ${comparedLabel} ${escapeHtml(formatBytes(comparedBytes))}`, `<span class="comparison-metric-ratio">${escapeHtml(sizeLabel)}</span>`, `<span class="comparison-metric-percent">${escapeHtml(percent)}</span>`, mse];
  return `${identity ? `<div class="comparison-metric-identity">${identity}</div>` : ""}<div class="comparison-metric-row">${parts.join(" · ")}</div>`;
}

function comparisonLabel(file) { return `${escapeHtml(file.filename)} · ${escapeHtml(formatBytes(file.size_bytes))}`; }

function differenceLegendMarkup(data) {
  const maximum = Number(data?.max_pixel_mse);
  if (!Number.isFinite(maximum)) return "";
  const logMaximum = Math.log1p(maximum);
  const ticks = [0, .25, .5, .75, 1].map(fraction => {
    const value = Math.expm1(fraction * logMaximum);
    return `<div class="difference-legend-tick" style="bottom:${fraction * 100}%"><span>${escapeHtml(comparisonLegendNumber(value))}</span></div>`;
  }).join("");
  return `<aside class="difference-legend" aria-label="Absolute pixel MSE scale"><div class="difference-legend-bar">${ticks}</div></aside>`;
}

function comparisonViewMarkup(data, mode) {
  const reference = data.reference || data.left;
  const compressed = data.compressed || data.right;
  const referenceLabel = comparisonLabel(reference);
  const compressedLabel = comparisonLabel(compressed);
  if (mode === "slider") return `<div class="comparison-slider" data-comparison-viewport><div class="comparison-slider-frame" data-comparison-frame><img class="comparison-slider-sizer" src="${escapeHtml(reference.preview_url)}" alt="" aria-hidden="true"><div class="comparison-slider-base"><img class="comparison-compressed" data-comparison-image src="${escapeHtml(compressed.preview_url)}" alt="${escapeHtml(compressed.filename)}"></div><div class="comparison-wipe-top" data-wipe-top><img class="comparison-reference" data-comparison-image src="${escapeHtml(reference.preview_url)}" alt="${escapeHtml(reference.filename)}"></div><button class="comparison-divider" data-wipe-handle type="button" aria-label="Move comparison divider" aria-valuemin="0" aria-valuemax="100" aria-valuenow="50"><span></span></button></div><span class="comparison-wipe-label comparison-wipe-label-left">${referenceLabel}</span><span class="comparison-wipe-label comparison-wipe-label-right">${compressedLabel}</span></div>`;
  if (mode === "difference") {
    const image = data.difference_url ? `<img class="comparison-difference-image" data-comparison-image src="${escapeHtml(data.difference_url)}" alt="Log-scaled per-pixel squared-error map">` : data.difference_error ? `<p class="error comparison-unavailable">${escapeHtml(data.difference_error)}</p>` : data.same_dimensions ? `<p class="comparison-unavailable" data-comparison-pending>Calculating difference…</p>` : `<p class="comparison-unavailable">Difference is unavailable because representation dimensions differ.</p>`;
    return `<div class="comparison-difference-layout"><div class="comparison-difference" data-comparison-viewport><div class="comparison-difference-frame">${image}</div></div>${differenceLegendMarkup(data)}</div>`;
  }
  return `<div class="comparison-stage" data-comparison-viewport><div class="comparison-pane"><span class="comparison-wipe-label comparison-wipe-label-left">${referenceLabel}</span><img class="comparison-reference" data-comparison-image src="${escapeHtml(reference.preview_url)}" alt="${escapeHtml(reference.filename)}"></div><div class="comparison-pane"><span class="comparison-wipe-label comparison-wipe-label-right">${compressedLabel}</span><img class="comparison-compressed" data-comparison-image src="${escapeHtml(compressed.preview_url)}" alt="${escapeHtml(compressed.filename)}"></div></div>`;
}

function recordComparisonTiming(dialog, entry) {
  dialog._comparisonTimings = [...(dialog._comparisonTimings || []), {...entry, at: performance.now()}].slice(-32);
}

async function renderRepresentationComparison(asset, compressedId, referenceId, mode = "slider", suppliedData = null) {
  const renderStarted = performance.now();
  const dialog = $("representation-comparison");
  const result = dialog.querySelector("[data-comparison-result]");
  if (!result) return;
  const pairKey = suppliedData?.pairKey || (suppliedData?.profile_id || suppliedData?.profile_name ? `preview:${suppliedData.profile_id || suppliedData.profile_name}` : dialog._comparisonPairKey || `${referenceId}:${compressedId}`);
  const samePair = dialog._comparisonPairKey === pairKey;
  const previousCamera = samePair ? dialog._comparisonCamera : null;
  const cameraState = previousCamera ? {zoom: previousCamera.zoom, panX: previousCamera.panX, panY: previousCamera.panY} : null;
  dialog._comparisonPairKey = pairKey;
  dialog._comparisonMode = mode;
  try {
    const data = suppliedData || dialog._comparisonData || dialog._comparisonDisplayData;
    if (!data) throw new Error("Comparison data is unavailable.");
    if (data.metrics) dialog._comparisonData = data;
    if (data.pairKey) dialog._comparisonDataKey = data.pairKey;
    const metrics = data.metrics || {};
    const reference = data.reference || data.left;
    const compressed = data.compressed || data.right;
    const headerMetrics = dialog.querySelector("[data-comparison-metrics]");
    if (!samePair) {
      dialog._comparisonViews = new Map();
      dialog._comparisonDisplayError = false;
    }
    if (headerMetrics) headerMetrics.innerHTML = dialog._comparisonDisplayError ? "Comparison display unavailable" : data.metrics ? comparisonMetricsMarkup(metrics, compressed) : "Calculating comparison…";
    if (data.difference_url || data.difference_error) dialog._comparisonViews?.delete("difference");
    const views = dialog._comparisonViews || (dialog._comparisonViews = new Map());
    let view = views.get(mode);
    if (!view) {
      const template = document.createElement("template");
      template.innerHTML = comparisonViewMarkup(data, mode);
      view = template.content.firstElementChild;
      views.set(mode, view);
    }
    dialog._comparisonCamera?.destroy();
    dialog._comparisonCamera = null;
    result.replaceChildren(view);
    const viewport = result.querySelector("[data-comparison-viewport]");
    const camera = new SharedImageCamera({
      viewport,
      getFrames: () => mode === "slider"
        ? [...result.querySelectorAll("[data-comparison-image]")].map(image => ({image, frame: result.querySelector("[data-comparison-frame]") || viewport}))
        : mode === "difference"
          ? [...result.querySelectorAll("[data-comparison-image]")].map(image => ({image, frame: result.querySelector(".comparison-difference-frame") || viewport}))
        : [...result.querySelectorAll(".comparison-pane")].map(frame => ({image: frame.querySelector("img"), frame})),
      onChange: change => result.querySelectorAll("img").forEach(image => { image.style.imageRendering = change.zoom > 1 ? "pixelated" : "auto"; }),
    });
    dialog._comparisonCamera = camera;
    if (cameraState) Object.assign(camera, cameraState);
    camera.setImages([...result.querySelectorAll("[data-comparison-image]")]);
    result.querySelectorAll("img").forEach(image => {
      if (image._comparisonLoadHandler) image.removeEventListener("load", image._comparisonLoadHandler);
      if (image._comparisonErrorHandler) image.removeEventListener("error", image._comparisonErrorHandler);
      clearTimeout(image._comparisonTimeout);
      image._comparisonLoadHandler = () => { clearTimeout(image._comparisonTimeout); camera.apply(); };
      image._comparisonErrorHandler = () => {
        clearTimeout(image._comparisonTimeout);
        if (!image.isConnected || dialog._comparisonPairKey !== pairKey) return;
        dialog._comparisonDisplayError = true;
        const header = dialog.querySelector("[data-comparison-metrics]");
        if (header) header.textContent = "Comparison display unavailable";
      };
      image.addEventListener("load", image._comparisonLoadHandler);
      image.addEventListener("error", image._comparisonErrorHandler);
      image._comparisonTimeout = setTimeout(() => { if (image.isConnected && (!image.complete || !image.naturalWidth)) image._comparisonErrorHandler(); }, 15000);
      if (image.complete) image.naturalWidth ? camera.apply() : image._comparisonErrorHandler();
    });
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
      if (!view._wipeBound) {
        let wiping = false;
        handle.addEventListener("pointerdown", event => { wiping = true; handle.setPointerCapture(event.pointerId); event.preventDefault(); });
        handle.addEventListener("pointermove", event => { if (!wiping) return; const rect = result.querySelector("[data-comparison-frame]").getBoundingClientRect(); setWipe((event.clientX - rect.left) / rect.width * 100); });
        ["pointerup", "pointercancel"].forEach(name => handle.addEventListener(name, () => { wiping = false; }));
        handle.addEventListener("keydown", event => { if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return; event.preventDefault(); setWipe(Number(handle.getAttribute("aria-valuenow")) + (event.key === "ArrowRight" ? 5 : -5)); });
        view._wipeBound = true;
      }
    }
    recordComparisonTiming(dialog, {kind: samePair ? "mode-switch" : "render", mode, pairKey, cache: Boolean(data.metrics), render_ms: roundTiming(performance.now() - renderStarted)});
    if (mode === "difference" && data.same_dimensions && !data.difference_url && !data.difference_error) loadRepresentationDifference(asset, compressedId, referenceId, data);
  } catch (error) {
    const headerMetrics = dialog.querySelector("[data-comparison-metrics]");
    if (headerMetrics) headerMetrics.textContent = "Comparison unavailable";
    result.innerHTML = `<p class="error">Comparison unavailable: ${escapeHtml(error.message)}</p>`;
  }
}

async function loadRepresentationDifference(asset, compressedId, referenceId, data) {
  const dialog = $("representation-comparison");
  const pairKey = data.pairKey || dialog._comparisonPairKey;
  if (!pairKey || !data.same_dimensions) return;
  if (dialog._differencePromise && dialog._differencePairKey === pairKey) return dialog._differencePromise;
  const cached = readDifferenceCache(pairKey);
  if (cached) {
    data.difference_url = cached.url;
    data.max_pixel_mse = cached.max_pixel_mse;
    data.difference_error = null;
    if (dialog._comparisonPairKey === pairKey && dialog._comparisonMode === "difference") {
      dialog._comparisonViews?.delete("difference");
      await renderRepresentationComparison(asset, compressedId, referenceId, "difference", data);
    }
    recordComparisonTiming(dialog, {kind: "difference", pairKey, cache: "frontend", total: 0});
    return;
  }
  const requestToken = (dialog._differenceRequestToken || 0) + 1;
  dialog._differenceRequestToken = requestToken;
  dialog._differenceAbort?.abort();
  const controller = new AbortController();
  dialog._differenceAbort = controller;
  dialog._differencePairKey = pairKey;
  const started = performance.now();
  const promise = (async () => {
    try {
      const response = await fetch(apiPath(`/api/files/${encodeURIComponent(compressedId)}/comparison-difference?with_id=${encodeURIComponent(referenceId)}`), {signal: controller.signal});
      if (!response.ok) {
        let message = `Request failed (${response.status})`;
        try { message = (await response.json()).error || message; } catch {}
        throw new Error(message);
      }
      const blob = await response.blob();
      const backend = (() => { try { return JSON.parse(response.headers.get("X-Comparison-Timings") || "null"); } catch { return null; } })();
      const cachedData = {url: URL.createObjectURL(blob), size: blob.size, max_pixel_mse: backend?.max_pixel_mse, global_mse: backend?.global_mse};
      const retained = writeDifferenceCache(pairKey, cachedData);
      if (!retained && dialog._differenceRequestToken === requestToken && !controller.signal.aborted) {
        dialog._ephemeralDifferenceUrls ||= [];
        dialog._ephemeralDifferenceUrls.push(cachedData.url);
      } else if (!retained) {
        URL.revokeObjectURL(cachedData.url);
      }
      if (dialog._differenceRequestToken === requestToken && !controller.signal.aborted) {
        const currentData = dialog._comparisonData?.pairKey === pairKey ? dialog._comparisonData : data;
        currentData.difference_url = cachedData.url;
        currentData.max_pixel_mse = cachedData.max_pixel_mse;
        currentData.difference_error = null;
        if (dialog._comparisonPairKey === pairKey && dialog._comparisonMode === "difference") {
          dialog._comparisonViews?.delete("difference");
          await renderRepresentationComparison(asset, compressedId, referenceId, "difference", currentData);
        }
      }
      recordComparisonTiming(dialog, {kind: "difference", pairKey, cache: backend?.cache_hit ? "backend" : "miss", frontend_total: roundTiming(performance.now() - started), backend});
    } catch (error) {
      if (error.name === "AbortError" || dialog._differenceRequestToken !== requestToken) return;
      if (dialog._comparisonPairKey === pairKey) {
        const currentData = dialog._comparisonData?.pairKey === pairKey ? dialog._comparisonData : data;
        currentData.difference_error = `Difference unavailable: ${error.message}`;
        if (dialog._comparisonMode === "difference") {
          dialog._comparisonViews?.delete("difference");
          await renderRepresentationComparison(asset, compressedId, referenceId, "difference", currentData);
        }
      }
    } finally {
      if (dialog._differenceRequestToken === requestToken) {
        dialog._differenceAbort = null;
        dialog._differencePromise = null;
        dialog._differencePairKey = null;
      }
    }
  })();
  dialog._differencePromise = promise;
  return promise;
}

async function loadRepresentationComparisonMetrics(asset, compressedId, referenceId, initialData) {
  const dialog = $("representation-comparison");
  const pairKey = initialData.pairKey;
  const cached = readComparisonCache(pairKey);
  if (cached) {
    const cachedDifference = readDifferenceCache(pairKey);
    if (cachedDifference) {
      cached.difference_url = cachedDifference.url;
      cached.max_pixel_mse = cachedDifference.max_pixel_mse;
    }
    dialog._comparisonData = cached;
    await renderRepresentationComparison(asset, compressedId, referenceId, dialog._comparisonMode || "slider", cached);
    recordComparisonTiming(dialog, {kind: "metrics", pairKey, cache: "frontend", total: 0});
    return;
  }
  const requestToken = (dialog._comparisonRequestToken || 0) + 1;
  dialog._comparisonRequestToken = requestToken;
  dialog._comparisonAbort?.abort();
  const controller = new AbortController();
  dialog._comparisonAbort = controller;
  const started = performance.now();
  let timedOut = false;
  const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 30000);
  try {
    const data = await api(`/api/files/${encodeURIComponent(compressedId)}/comparison?with_id=${encodeURIComponent(referenceId)}`, {signal: controller.signal});
    if (dialog._comparisonRequestToken !== requestToken || controller.signal.aborted || dialog._comparisonPairKey !== pairKey) return;
    data.pairKey = pairKey;
    const cachedDifference = readDifferenceCache(pairKey);
    if (cachedDifference) {
      data.difference_url = cachedDifference.url;
      data.max_pixel_mse = cachedDifference.max_pixel_mse;
    }
    writeComparisonCache(pairKey, data);
    dialog._comparisonData = data;
    const mode = dialog._comparisonMode || "slider";
    if (mode === "difference") dialog._comparisonViews?.delete("difference");
    await renderRepresentationComparison(asset, compressedId, referenceId, mode, data);
    recordComparisonTiming(dialog, {kind: "metrics", pairKey, cache: data.cache_hit ? "backend" : "miss", frontend_total: roundTiming(performance.now() - started), backend: data.timings_ms || null});
  } catch (error) {
    if ((error.name === "AbortError" && !timedOut) || dialog._comparisonRequestToken !== requestToken) return;
    if (dialog._comparisonPairKey === pairKey) {
      const header = dialog.querySelector("[data-comparison-metrics]");
      if (header) header.textContent = "Comparison unavailable";
      const result = dialog.querySelector("[data-comparison-result]");
      if (result && dialog._comparisonMode === "difference") result.innerHTML = `<p class="error">Comparison unavailable: ${escapeHtml(error.message)}</p>`;
    }
  } finally {
    clearTimeout(timeout);
    if (dialog._comparisonRequestToken === requestToken) dialog._comparisonAbort = null;
  }
}

function roundTiming(value) { return Math.round(value * 1000) / 1000; }

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
  dialog.classList.add("raw-inspection-dialog");
  const resetIcon = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M4 11a8 8 0 1 0 2.3-5.6L4 7.7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 4.5v3.2h3.2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const resetButton = name => `<button type="button" class="raw-control-reset" data-raw-reset-control="${name}" aria-label="Reset ${name.replace("_ev", "").replace("_", " ")}" title="Reset ${name.replace("_ev", "").replace("_", " ")}">${resetIcon}</button>`;
  dialog.innerHTML = `<div class="dialog-inner raw-inspection"><div class="dialog-header"><div><h2>${escapeHtml(file.filename)}</h2><p class="muted">RAW source (${escapeHtml(formatBytes(file.size_bytes))})</p></div><button class="icon" type="button" data-comparison-close aria-label="Close RAW viewer">×</button></div><div class="raw-inspection-stage" data-raw-stage><img class="hidden" data-raw-image alt="${escapeHtml(file.filename)}"><p class="raw-error hidden" data-raw-error>RAW development is unavailable for this file.</p></div><div class="raw-development-controls"><div class="raw-development-status"><span class="sr-only" data-raw-wb-status aria-live="polite">Camera/as-shot white balance when available</span><span class="raw-loading-inline hidden" data-raw-loading>Loading preview…</span></div><label class="raw-exposure-control"><span>Exposure</span><output data-raw-exposure-value>+0.00 EV</output>${resetButton("exposure_ev")}<input data-raw-exposure type="range" min="-5" max="5" step="0.5" value="0" aria-label="RAW exposure"></label><label class="raw-exposure-control"><span>White balance</span><output data-raw-wb-value>Camera</output>${resetButton("white_balance")}<small>Cool ← Camera → Warm</small><input data-raw-white-balance type="range" min="-100" max="100" step="5" value="0" aria-label="White balance"></label><label class="raw-exposure-control"><span>Saturation</span><output data-raw-saturation-value>100%</output>${resetButton("saturation")}<input data-raw-saturation type="range" min="50" max="150" step="5" value="100" aria-label="Saturation"></label><label class="raw-exposure-control"><span>Highlights</span><output data-raw-highlights-value>0</output>${resetButton("highlights")}<input data-raw-highlights type="range" min="-100" max="100" step="5" value="0" aria-label="Highlights"></label><label class="raw-exposure-control"><span>Shadows</span><output data-raw-shadows-value>0</output>${resetButton("shadows")}<input data-raw-shadows type="range" min="-100" max="100" step="5" value="0" aria-label="Shadows"></label><button type="button" class="secondary raw-reset-all" data-raw-reset>Reset all</button></div></div>`;
  dialog.showModal();
  dialog.querySelector("[data-comparison-close]").onclick = () => dialog.close();
  const stage = dialog.querySelector("[data-raw-stage]");
  const image = dialog.querySelector("[data-raw-image]");
  const loading = dialog.querySelector("[data-raw-loading]");
  const errorNode = dialog.querySelector("[data-raw-error]");
  const wbStatus = dialog.querySelector("[data-raw-wb-status]");
  const inputs = {
    exposure_ev: dialog.querySelector("[data-raw-exposure]"),
    white_balance: dialog.querySelector("[data-raw-white-balance]"),
    saturation: dialog.querySelector("[data-raw-saturation]"),
    highlights: dialog.querySelector("[data-raw-highlights]"),
    shadows: dialog.querySelector("[data-raw-shadows]"),
  };
  const updateControls = () => {
    const exposure = Number(inputs.exposure_ev.value);
    const wb = Number(inputs.white_balance.value);
    const saturation = Number(inputs.saturation.value);
    const highlights = Number(inputs.highlights.value);
    const shadows = Number(inputs.shadows.value);
    dialog.querySelector("[data-raw-exposure-value]").textContent = rawExposureLabel(exposure);
    dialog.querySelector("[data-raw-wb-value]").textContent = wb === 0 ? "Camera" : `${wb < 0 ? "Cool" : "Warm"} ${Math.abs(wb)}`;
    dialog.querySelector("[data-raw-saturation-value]").textContent = `${saturation}%`;
    dialog.querySelector("[data-raw-highlights-value]").textContent = String(highlights);
    dialog.querySelector("[data-raw-shadows-value]").textContent = String(shadows);
    Object.values(inputs).forEach(input => {
      const min = Number(input.min), max = Number(input.max), value = Number(input.value);
      input.style.setProperty("--range-progress", `${((value - min) / (max - min)) * 100}%`);
    });
  };
  const camera = new SharedImageCamera({viewport: stage, getFrames: () => [{image, frame: stage}], onChange: change => { image.style.imageRendering = change.zoom > 1 ? "pixelated" : "auto"; }});
  dialog._rawCamera = camera;
  let timer = null;
  const load = async () => {
    const generation = (dialog._rawGeneration || 0) + 1;
    dialog._rawGeneration = generation;
    dialog._rawAbort?.abort();
    const controller = new AbortController();
    dialog._rawAbort = controller;
    loading.classList.remove("hidden");
    errorNode.classList.add("hidden");
    try {
      const params = new URLSearchParams(Object.entries(inputs).map(([key, input]) => [key, input.value]));
      const response = await fetch(apiPath(`/api/files/${encodeURIComponent(file.id)}/raw-development-preview?${params}`), {signal: controller.signal});
      if (!response.ok) throw new Error("RAW development is unavailable for this file.");
      const blob = await response.blob();
      if (generation !== dialog._rawGeneration || controller.signal.aborted) return;
      wbStatus.textContent = response.headers.get("X-RAW-White-Balance") || "White balance status unavailable";
      const url = URL.createObjectURL(blob);
      const previous = image.dataset.objectUrl;
      image.dataset.objectUrl = url;
      image.onload = () => {
        if (generation !== dialog._rawGeneration || controller.signal.aborted) return;
        requestAnimationFrame(() => requestAnimationFrame(() => {
          if (generation !== dialog._rawGeneration || controller.signal.aborted) return;
          camera.setImages([image]);
          if (camera.zoom === 1) camera.reset(); else camera.apply();
          if (previous) URL.revokeObjectURL(previous);
        }));
      };
      image.onerror = () => {
        if (generation !== dialog._rawGeneration || controller.signal.aborted) return;
        image.classList.add("hidden");
        errorNode.textContent = "RAW preview could not be decoded.";
        errorNode.classList.remove("hidden");
      };
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
  Object.values(inputs).forEach(input => input.addEventListener("input", () => {
    updateControls();
    clearTimeout(timer);
    timer = setTimeout(load, 180);
  }));
  dialog.querySelector("[data-raw-reset]").addEventListener("click", () => {
    inputs.exposure_ev.value = "0";
    inputs.white_balance.value = "0";
    inputs.saturation.value = "100";
    inputs.highlights.value = "0";
    inputs.shadows.value = "0";
    updateControls();
    clearTimeout(timer);
    timer = setTimeout(load, 0);
  });
  dialog.querySelectorAll("[data-raw-reset-control]").forEach(button => button.addEventListener("click", () => {
    const input = inputs[button.dataset.rawResetControl];
    if (!input) return;
    input.value = input.defaultValue;
    updateControls();
    clearTimeout(timer);
    timer = setTimeout(load, 180);
  }));
  updateControls();
  load();
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
    return `<div class="representation-row"><div><div class="representation-title"><strong>${filenameMarkup(file.filename)}</strong> · ${escapeHtml(formatBytes(file.size_bytes))}${file.is_preferred ? " · Preferred" : ""}${file.is_online ? "" : " · Offline"}</div>${file.representation_label ? `<div class="muted representation-label">${escapeHtml(file.representation_label)}</div>` : ""}<div class="muted representation-path">${escapeHtml(file.relative_path)}</div>${problems ? `<details class="representation-diagnostics"><summary>⚠ Representation status</summary>${problems}</details>` : ""}</div><div class="representation-actions"><button class="representation-eye" type="button" data-representation-view="${escapeHtml(file.id)}" aria-label="View representation" title="View representation">${eye}</button><button class="explorer-button" data-reveal="${escapeHtml(file.id)}" aria-label="Show in Explorer" title="Show in Explorer"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3 5.5h7l2 2H21v11H3Z" fill="none" stroke="currentColor" stroke-width="1.8"/><path d="M3 9h18" fill="none" stroke="currentColor" stroke-width="1.8"/></svg></button></div></div>`;
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
