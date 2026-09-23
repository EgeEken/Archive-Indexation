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
  container.querySelectorAll("[data-representation-view]").forEach(button => button.addEventListener("click", () => openRepresentationInspection(asset, button.dataset.representationView)));
}

function compatibleRepresentations(asset, fileId) {
  return (asset.physical_files || []).filter(file => file.id !== fileId && file.media_type === "image" && file.is_online && ![".arw", ".cr2", ".cr3", ".dng", ".nef", ".raf", ".rw2"].includes(file.extension));
}

function defaultComparisonTarget(asset, fileId) {
  const compatible = compatibleRepresentations(asset, fileId);
  return compatible.find(file => file.is_preferred) || compatible[0] || null;
}

function representationPreviewUrl(fileId) { return apiPath(`/api/files/${encodeURIComponent(fileId)}/comparison-preview`); }
const displayPreviewUrl = file => file.display_preview_url || file.thumbnail_url || file.original_url;
const showViewerForRepresentation = (asset, file) => ({...assetToViewerItem(asset), filename: file.filename, preferred_physical_id: file.id, display_url: displayPreviewUrl(file)});

function openRepresentationInspection(asset, fileId) {
  const file = asset.physical_files?.find(candidate => candidate.id === fileId);
  if (!file) return;
  const target = defaultComparisonTarget(asset, fileId);
  const dialog = $("representation-comparison");
  const relationship = file.relationships?.includes("External JPEG XL representation") ? "External JPEG XL representation · Lineage unknown" : file.representation_label || file.role || "Physical file";
  dialog.innerHTML = `<div class="dialog-inner representation-inspection"><div class="dialog-header"><div><h2>${escapeHtml(file.filename)}</h2><p class="muted">${escapeHtml(relationship)}</p></div><button class="icon" type="button" data-comparison-close aria-label="Close representation">×</button></div><div class="representation-preview-wrap"><img src="${escapeHtml(representationPreviewUrl(file.id))}" alt="${escapeHtml(file.filename)}"></div><dl class="kv"><dt>Format</dt><dd>${escapeHtml(file.extension?.replace(".", "").toUpperCase() || "Unavailable")}</dd><dt>File size</dt><dd>${escapeHtml(formatBytes(file.size_bytes))}</dd><dt>Dimensions</dt><dd>${file.width && file.height ? `${file.width} × ${file.height}` : "Unavailable"}</dd><dt>Role</dt><dd>${escapeHtml(relationship)}</dd></dl>${target ? `<div class="comparison-toolbar"><label>Compare with <select data-comparison-target>${compatibleRepresentations(asset, fileId).map(candidate => `<option value="${escapeHtml(candidate.id)}"${candidate.id === target.id ? " selected" : ""}>${escapeHtml(candidate.filename)}</option>`).join("")}</select></label><button class="secondary" type="button" data-compare-mode="side">Side by side</button><button class="secondary" type="button" data-compare-mode="slider">Slider</button></div><div data-comparison-result></div>` : ""}</div>`;
  dialog.showModal();
  dialog.querySelector("[data-comparison-close]").onclick = () => dialog.close();
  dialog.querySelectorAll("[data-compare-mode]").forEach(button => button.addEventListener("click", () => renderRepresentationComparison(asset, fileId, dialog.querySelector("[data-comparison-target]").value, button.dataset.compareMode)));
}

async function renderRepresentationComparison(asset, leftId, rightId, mode = "side") {
  const dialog = $("representation-comparison");
  const result = dialog.querySelector("[data-comparison-result]");
  if (!result) return;
  result.innerHTML = `<p class="muted">Calculating comparison…</p>`;
  try {
    const data = await api(`/api/files/${encodeURIComponent(leftId)}/comparison?with_id=${encodeURIComponent(rightId)}`);
    const metrics = data.metrics || {};
    const metricText = `${formatBytes(metrics.source_bytes)} → ${formatBytes(metrics.comparison_bytes)}${metrics.compression_ratio ? ` · ${metrics.compression_ratio}× smaller` : ""}${metrics.mse == null ? "" : ` · MSE ${metrics.mse} · PSNR ${metrics.psnr} dB`}`;
    const stage = mode === "slider" ? `<div class="comparison-slider"><img src="${escapeHtml(data.right.preview_url)}" alt="${escapeHtml(data.right.filename)}"><img class="comparison-slider-top" data-slider-image src="${escapeHtml(data.left.preview_url)}" alt="${escapeHtml(data.left.filename)}"><input type="range" min="0" max="100" value="50" aria-label="Comparison wipe position"></div>` : `<div class="comparison-stage"><div class="comparison-pane"><img src="${escapeHtml(data.left.preview_url)}" alt="${escapeHtml(data.left.filename)}"></div><div class="comparison-pane"><img src="${escapeHtml(data.right.preview_url)}" alt="${escapeHtml(data.right.filename)}"></div></div>`;
    result.innerHTML = `${stage}<div class="muted comparison-labels">${escapeHtml(data.left.filename)} · ${escapeHtml(data.right.filename)} · ${escapeHtml(metricText)}</div>${data.metrics_note ? `<p class="muted">${escapeHtml(data.metrics_note)}</p>` : ""}`;
    const slider = result.querySelector("input[type=range]");
    slider?.addEventListener("input", event => result.querySelector("[data-slider-image]").style.clipPath = `inset(0 ${100 - Number(event.target.value)}% 0 0)`);
  } catch (error) { result.innerHTML = `<p class="error">Comparison unavailable: ${escapeHtml(error.message)}</p>`; }
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
    return `<div class="representation-row"><div><div class="representation-title"><strong>${filenameMarkup(file.filename)}</strong> · ${escapeHtml(formatBytes(file.size_bytes))}${file.is_preferred ? " · Preferred" : ""}${file.is_online ? "" : " · Offline"}</div><div class="muted representation-path">${escapeHtml(file.relative_path)}</div>${problems ? `<details class="representation-diagnostics"><summary>⚠ Representation status</summary>${problems}</details>` : ""}</div><div class="representation-actions"><button class="icon" type="button" data-representation-view="${escapeHtml(file.id)}" data-representation-open="${escapeHtml(file.id)}" data-representation-compare="${escapeHtml(file.id)}" aria-label="View representation" title="View representation">◉</button><button class="explorer-button" data-reveal="${escapeHtml(file.id)}" aria-label="Show in Explorer" title="Show in Explorer">📁</button></div></div>`;
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
