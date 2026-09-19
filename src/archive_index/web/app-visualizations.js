const VISUALIZATION_MODES = ["geo", "timeline", "vector"];
let worldFeaturesPromise;

function visualizationCanvas(mode) { return $(`${mode}-canvas`); }
function visualizationView(mode) { return state.visualizations[mode]; }

async function loadVisualization(mode) {
  if (!VISUALIZATION_MODES.includes(mode)) return;
  const requestId = ++state.visualizationRequest;
  state.visualizationAbort?.abort();
  const controller = new AbortController();
  state.visualizationAbort = controller;
  const params = filterParams();
  params.set("view", mode);
  const key = String(params);
  const view = visualizationView(mode);
  if (view.key === key && view.data) {
    renderVisualization(mode);
    return;
  }
  try {
    const data = await api(`/api/visualizations/${mode}?${params}`, {signal: controller.signal});
    if (controller.signal.aborted || requestId !== state.visualizationRequest) return;
    const changed = view.key !== key || view.data?.browser_revision !== data.browser_revision;
    view.key = key;
    view.data = data;
    if (changed) {
      view.needsFit = true;
      view.hitTargets = [];
      state.visualizationSelectedId = null;
      hideVisualizationSelection();
    }
    state.searchProvider = data.search?.provider || state.searchProvider;
    showSearchStatus(data.search || {state: "complete"}, {
      ...data,
      media_shown: data.filtered_asset_count,
      workspace_total: data.workspace_total,
    });
    renderVisualization(mode);
  } catch (error) {
    if (error.name !== "AbortError") showToast(`${mode} request failed: ${error.message}`);
  }
}

function renderVisualization(mode) {
  const view = visualizationView(mode);
  const data = view.data;
  if (!data) return;
  const noun = mode === "geo" ? "geotagged assets" : mode === "timeline" ? "timed assets" : "projected assets";
  const status = $(`${mode}-status`);
  status.textContent = data.available
    ? `${Number(data.represented_point_count || 0).toLocaleString()} ${noun} · ${Number(data.filtered_asset_count || 0).toLocaleString()} filtered assets${data.empty_reason ? ` · ${data.empty_reason}` : ""}`
    : data.empty_reason || "Visualization unavailable.";
  const canvas = visualizationCanvas(mode);
  if (mode === "geo") drawGeo(canvas, view, data);
  else if (mode === "timeline") drawTimeline(canvas, view, data);
  else drawVector(canvas, view, data);
  markVisualizationCanvas(canvas, view, data);
}

function markVisualizationCanvas(canvas, view, data) {
  canvas.dataset.pointCount = String(data.available ? data.represented_point_count || 0 : 0);
  canvas.dataset.viewScale = String(view.scale);
  const target = view.hitTargets?.[0] || [...(view.grid?.values() || [])][0]?.[0];
  if (target) {
    canvas.dataset.firstTargetX = String(target.x);
    canvas.dataset.firstTargetY = String(target.y);
  } else {
    delete canvas.dataset.firstTargetX;
    delete canvas.dataset.firstTargetY;
  }
}

function canvasSurface(canvas) {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width || canvas.parentElement?.clientWidth || 800));
  const height = Math.max(1, Math.round(rect.height || 560));
  const ratio = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
    canvas.width = width * ratio;
    canvas.height = height * ratio;
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return {context, width, height};
}

function screenPoint(view, x, y, width, height) {
  return {x: (x - view.centerX) * view.scale + width / 2 + view.panX, y: (y - view.centerY) * view.scale + height / 2 + view.panY};
}

function worldPoint(view, x, y, width, height) {
  return {x: (x - width / 2 - view.panX) / view.scale + view.centerX, y: (y - height / 2 - view.panY) / view.scale + view.centerY};
}

function zoomVisualization(mode, factor, point) {
  const canvas = visualizationCanvas(mode);
  const view = visualizationView(mode);
  const {width, height} = canvasSurface(canvas);
  const cursor = point || {x: width / 2, y: height / 2};
  const before = worldPoint(view, cursor.x, cursor.y, width, height);
  const maximum = mode === "timeline" ? 100000 : 1000000;
  view.scale = Math.max(.05, Math.min(maximum, view.scale * factor));
  const after = screenPoint(view, before.x, before.y, width, height);
  view.panX += cursor.x - after.x;
  if (mode !== "timeline") view.panY += cursor.y - after.y;
  renderVisualization(mode);
}

function fitVisualization(mode) {
  const view = visualizationView(mode);
  view.needsFit = true;
  renderVisualization(mode);
}

function panVisualization(mode, dx, dy) {
  const view = visualizationView(mode);
  view.panX += dx;
  if (mode !== "timeline") view.panY += dy;
  renderVisualization(mode);
}

function geoWorld(longitude, latitude) {
  const safeLatitude = Math.max(-85, Math.min(85, Number(latitude)));
  const radians = safeLatitude * Math.PI / 180;
  const mercator = Math.log(Math.tan(Math.PI / 4 + radians / 2));
  return {x: (Number(longitude) + 180) / 360, y: .5 - mercator / (2 * Math.PI)};
}

function drawGeo(canvas, view, data) {
  const surface = canvasSurface(canvas);
  const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c";
  ctx.fillRect(0, 0, width, height);
  if (!data.available || !data.points?.length) {
    view.hitTargets = [];
    drawCanvasMessage(ctx, width, height, data.empty_reason || "No geotagged assets.");
    return;
  }
  const points = data.points.filter(point => Number.isFinite(Number(point.latitude)) && Number.isFinite(Number(point.longitude)));
  if (view.needsFit) fitGeo(view, points, width, height);
  if (!view.world) {
    if (!worldFeaturesPromise) worldFeaturesPromise = fetch("/world.json").then(response => response.ok ? response.json() : {features: []}).catch(() => ({features: []}));
    worldFeaturesPromise.then(world => { if (state.viewMode === "geo" && view.data === data && !view.world) { view.world = world; renderVisualization("geo"); } });
  }
  for (const feature of view.world?.features || []) drawGeoFeature(ctx, feature, view, width, height);
  const clusters = new Map();
  for (const point of points) {
    const world = geoWorld(point.longitude, point.latitude);
    const screen = screenPoint(view, world.x, world.y, width, height);
    const key = `${Math.floor(screen.x / 42)}:${Math.floor(screen.y / 42)}`;
    const cluster = clusters.get(key) || [];
    cluster.push({...point, screenX: screen.x, screenY: screen.y});
    clusters.set(key, cluster);
  }
  view.hitTargets = [];
  for (const cluster of clusters.values()) {
    const x = cluster.reduce((sum, point) => sum + point.screenX, 0) / cluster.length;
    const y = cluster.reduce((sum, point) => sum + point.screenY, 0) / cluster.length;
    const radius = cluster.length === 1 ? 4.5 : Math.min(18, 7 + Math.sqrt(cluster.length) * 1.8);
    ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fillStyle = cluster.some(point => point.asset_id === state.visualizationSelectedId) ? "#d9e0e6" : cluster.length === 1 ? "#7fc7e8" : "#5f8eae";
    ctx.fill();
    if (cluster.length > 1) {
      ctx.fillStyle = "#edf0f3"; ctx.font = "600 11px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(String(cluster.length), x, y);
    }
    view.hitTargets.push({x, y, points: cluster});
  }
}

function drawGeoFeature(ctx, feature, view, width, height) {
  const polygons = feature.geometry?.type === "MultiPolygon" ? feature.geometry.coordinates.flat(1) : feature.geometry?.type === "Polygon" ? feature.geometry.coordinates : [];
  for (const polygon of polygons) {
    ctx.beginPath();
    polygon.forEach((coordinate, index) => {
      const world = geoWorld(coordinate[0], coordinate[1]);
      const screen = screenPoint(view, world.x, world.y, width, height);
      if (index === 0) ctx.moveTo(screen.x, screen.y); else ctx.lineTo(screen.x, screen.y);
    });
    ctx.closePath(); ctx.fillStyle = "#18232c"; ctx.fill(); ctx.strokeStyle = "#33424e"; ctx.lineWidth = 1; ctx.stroke();
  }
}

function fitGeo(view, points, width, height) {
  const values = points.map(point => geoWorld(point.longitude, point.latitude));
  if (!values.length) { view.centerX = .5; view.centerY = .5; view.scale = Math.min(width, height) * .86; view.panX = view.panY = 0; view.needsFit = false; return; }
  const bounds = boundsOf(values, 0.08);
  view.centerX = (bounds.minX + bounds.maxX) / 2; view.centerY = (bounds.minY + bounds.maxY) / 2;
  view.scale = Math.max(1, Math.min(width / (bounds.maxX - bounds.minX), height / (bounds.maxY - bounds.minY)));
  view.panX = view.panY = 0; view.needsFit = false;
}

function drawTimeline(canvas, view, data) {
  const surface = canvasSurface(canvas);
  const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  if (!data.available || !data.points?.length) { view.hitTargets = []; drawCanvasMessage(ctx, width, height, data.empty_reason || "No timed assets."); return; }
  const points = data.points;
  if (view.needsFit) fitTimeline(view, points, width, height);
  const axisY = screenPoint(view, .5, .82, width, height).y;
  ctx.strokeStyle = "#465663"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0, axisY); ctx.lineTo(width, axisY); ctx.stroke();
  const left = Math.max(0, worldPoint(view, 0, .5, width, height).x);
  const right = Math.min(1, worldPoint(view, width, .5, width, height).x);
  const visibleStart = view.origin + left * view.span;
  const visibleEnd = view.origin + right * view.span;
  const first = lowerBound(points, visibleStart);
  const last = upperBound(points, visibleEnd);
  const visible = points.slice(first, last);
  const bucketWidth = Math.max(1, (visibleEnd - visibleStart) / Math.max(8, width / 50));
  const broad = visible.length > width * 1.35;
  view.hitTargets = [];
  if (broad) {
    const buckets = new Map();
    for (const point of visible) {
      const index = Math.floor((point.time - visibleStart) / bucketWidth);
      const bucket = buckets.get(index) || {count: 0, min: point.time, max: point.time};
      bucket.count += 1; bucket.min = Math.min(bucket.min, point.time); bucket.max = Math.max(bucket.max, point.time); buckets.set(index, bucket);
    }
    const maxCount = Math.max(...[...buckets.values()].map(bucket => bucket.count), 1);
    for (const [index, bucket] of buckets) {
      const start = visibleStart + index * bucketWidth;
      const end = start + bucketWidth;
      const leftPoint = screenPoint(view, (start - view.origin) / view.span, .78, width, height);
      const rightPoint = screenPoint(view, (end - view.origin) / view.span, .78, width, height);
      const barHeight = 22 + 150 * bucket.count / maxCount;
      ctx.fillStyle = "#5f8eae"; ctx.fillRect(leftPoint.x, axisY - barHeight, Math.max(2, rightPoint.x - leftPoint.x - 1), barHeight);
      view.hitTargets.push({x: (leftPoint.x + rightPoint.x) / 2, y: axisY - barHeight / 2, bucket: {start, end}});
    }
  } else {
    visible.forEach((point, index) => {
      const x = screenPoint(view, (point.time - view.origin) / view.span, .35 + (index % 6) * .07, width, height).x;
      const y = screenPoint(view, (point.time - view.origin) / view.span, .35 + (index % 6) * .07, width, height).y;
      ctx.beginPath(); ctx.arc(x, y, point.asset_id === state.visualizationSelectedId ? 6 : 4, 0, Math.PI * 2);
      ctx.fillStyle = point.asset_id === state.visualizationSelectedId ? "#d9e0e6" : "#7fc7e8"; ctx.fill();
      view.hitTargets.push({x, y, point});
    });
  }
  drawTimelineTicks(ctx, view, visibleStart, visibleEnd, axisY, width, height);
}

function fitTimeline(view, points, width, height) {
  const min = points[0].time; const max = points[points.length - 1].time;
  view.origin = min === max ? min - 43200 : min;
  view.span = min === max ? 86400 : Math.max(1, max - min);
  view.centerX = .5; view.centerY = .5; view.scale = Math.min(width / 1.08, height / .85); view.panX = view.panY = 0; view.needsFit = false;
}

function drawTimelineTicks(ctx, view, start, end, axisY, width, height) {
  const span = Math.max(1, end - start);
  const step = timelineTickStep(span);
  for (let time = Math.ceil(start / step) * step; time <= end; time += step) {
    const x = screenPoint(view, (time - view.origin) / view.span, .82, width, height).x;
    if (x < -20 || x > width + 20) continue;
    ctx.strokeStyle = "#61717e"; ctx.beginPath(); ctx.moveTo(x, axisY); ctx.lineTo(x, axisY + 7); ctx.stroke();
    ctx.fillStyle = "#9faab5"; ctx.font = "11px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillText(formatTimelineTick(time, step), x, axisY + 10);
  }
}

function timelineTickStep(span) {
  const steps = [1, 5, 10, 30, 60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 604800, 2592000, 7776000, 31536000];
  return steps.find(step => span / step <= 9) || steps.at(-1);
}

function formatTimelineTick(value, step) {
  const date = new Date(value * 1000);
  const pad = number => String(number).padStart(2, "0");
  if (step < 60) return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
  if (step < 86400) return `${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  if (step < 31536000) return `${pad(date.getUTCDate())}/${pad(date.getUTCMonth() + 1)}/${date.getUTCFullYear()}`;
  return String(date.getUTCFullYear());
}

function drawVector(canvas, view, data) {
  const surface = canvasSurface(canvas);
  const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  if (!data.available || !data.points?.length) { view.hitTargets = []; view.grid = new Map(); drawCanvasMessage(ctx, width, height, data.empty_reason || "No projected semantic vectors."); return; }
  if (view.needsFit) fitVector(view, data.points, width, height);
  ctx.strokeStyle = "#33424e"; ctx.lineWidth = 1; const horizontal = screenPoint(view, view.centerX, view.centerY, width, height);
  ctx.beginPath(); ctx.moveTo(0, horizontal.y); ctx.lineTo(width, horizontal.y); ctx.moveTo(horizontal.x, 0); ctx.lineTo(horizontal.x, height); ctx.stroke();
  ctx.fillStyle = "#8393a0"; ctx.font = "11px system-ui"; ctx.fillText("PCA 1", width - 42, Math.max(14, horizontal.y - 8)); ctx.save(); ctx.translate(Math.max(12, horizontal.x - 8), 42); ctx.rotate(-Math.PI / 2); ctx.fillText("PCA 2", 0, 0); ctx.restore();
  view.grid = new Map(); view.hitTargets = [];
  for (const point of data.points) {
    const screen = screenPoint(view, Number(point.x), Number(point.y), width, height);
    if (screen.x < -8 || screen.x > width + 8 || screen.y < -8 || screen.y > height + 8) continue;
    const selected = point.asset_id === state.visualizationSelectedId;
    ctx.beginPath(); ctx.arc(screen.x, screen.y, selected ? 5.5 : 3.2, 0, Math.PI * 2); ctx.fillStyle = selected ? "#d9e0e6" : point.media_type === "video" ? "#e5ae64" : "#72a7ff"; ctx.fill();
    const key = `${Math.floor(screen.x / 24)}:${Math.floor(screen.y / 24)}`;
    const cell = view.grid.get(key) || []; cell.push({point, x: screen.x, y: screen.y}); view.grid.set(key, cell);
  }
}

function fitVector(view, points, width, height) {
  const values = points.map(point => ({x: Number(point.x), y: Number(point.y)})).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
  let magnitude = 0;
  for (const point of values) magnitude = Math.max(magnitude, Math.abs(point.x), Math.abs(point.y));
  const bounds = boundsOf(values, Math.max(.1, magnitude * .12));
  view.centerX = (bounds.minX + bounds.maxX) / 2; view.centerY = (bounds.minY + bounds.maxY) / 2; view.scale = Math.max(1, Math.min(width / (bounds.maxX - bounds.minX), height / (bounds.maxY - bounds.minY))); view.panX = view.panY = 0; view.needsFit = false;
}

function boundsOf(values, padding) {
  let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity;
  for (const value of values) { minX = Math.min(minX, value.x); maxX = Math.max(maxX, value.x); minY = Math.min(minY, value.y); maxY = Math.max(maxY, value.y); }
  const width = Math.max(padding, maxX - minX); const height = Math.max(padding, maxY - minY);
  return {minX: minX - width * .08 - padding, maxX: maxX + width * .08 + padding, minY: minY - height * .08 - padding, maxY: maxY + height * .08 + padding};
}

function lowerBound(points, value) { let low = 0, high = points.length; while (low < high) { const middle = (low + high) >> 1; if (points[middle].time < value) low = middle + 1; else high = middle; } return low; }
function upperBound(points, value) { let low = 0, high = points.length; while (low < high) { const middle = (low + high) >> 1; if (points[middle].time <= value) low = middle + 1; else high = middle; } return low; }
function drawCanvasMessage(ctx, width, height, message) { ctx.fillStyle = "#9faab5"; ctx.font = "14px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(message, width / 2, height / 2); }

function hitVisualization(mode, event) {
  const canvas = visualizationCanvas(mode); const view = visualizationView(mode); const rect = canvas.getBoundingClientRect(); const x = event.clientX - rect.left; const y = event.clientY - rect.top;
  if (mode === "vector") {
    const cellX = Math.floor(x / 24); const cellY = Math.floor(y / 24); let nearest = null; let distance = 12;
    for (let ix = cellX - 1; ix <= cellX + 1; ix++) for (let iy = cellY - 1; iy <= cellY + 1; iy++) for (const target of view.grid.get(`${ix}:${iy}`) || []) { const current = Math.hypot(target.x - x, target.y - y); if (current < distance) {distance = current; nearest = target.point;} }
    if (nearest) selectVisualizationAsset(nearest);
    return;
  }
  let nearest = null; let distance = 24;
  for (const target of view.hitTargets) { const current = Math.hypot(target.x - x, target.y - y); if (current < distance) {distance = current; nearest = target;} }
  if (!nearest) return;
  if (nearest.bucket) { focusTimelineRange(nearest.bucket.start, nearest.bucket.end); return; }
  if (nearest.points?.length > 1) {
    if (mode === "geo" && view.scale < 900) { zoomVisualization("geo", 2.5, {x: nearest.x, y: nearest.y}); return; }
    showVisualizationCluster(nearest.points); return;
  }
  selectVisualizationAsset(nearest.point || nearest.points[0]);
}

function focusTimelineRange(start, end) {
  const view = visualizationView("timeline"); const canvas = visualizationCanvas("timeline"); const {width} = canvasSurface(canvas); const range = Math.max(1, end - start); view.centerX = ((start + end) / 2 - view.origin) / view.span; view.panX = 0; view.scale = Math.min(100000, Math.max(view.scale * 1.4, width / (range / view.span))); renderVisualization("timeline");
}

function showVisualizationCluster(points) {
  const panel = $("visualization-selection"); state.visualizationSelectedId = null; panel.classList.remove("hidden"); panel.innerHTML = `<div class="visualization-selection-header"><div><h3>${points.length.toLocaleString()} assets at this location</h3><div class="muted">Choose an asset to preview.</div></div><button class="icon" type="button" data-visualization-close aria-label="Close preview">×</button></div><div class="visualization-cluster-list">${points.slice(0, 100).map(point => `<button type="button" data-visualization-cluster="${escapeHtml(point.asset_id)}">${escapeHtml(point.asset_id.slice(0, 8))}</button>`).join("")}</div>`;
  panel.querySelector("[data-visualization-close]").onclick = hideVisualizationSelection;
  panel.querySelectorAll("[data-visualization-cluster]").forEach(button => button.onclick = () => selectVisualizationAsset(points.find(point => point.asset_id === button.dataset.visualizationCluster)));
}

async function selectVisualizationAsset(point) {
  if (!point?.asset_id) return;
  state.visualizationSelectedId = point.asset_id;
  const panel = $("visualization-selection"); panel.classList.remove("hidden"); panel.innerHTML = `<div class="muted">Loading asset preview…</div>`;
  VISUALIZATION_MODES.forEach(mode => { if (visualizationView(mode).data) renderVisualization(mode); });
  const token = (state.visualizationSelectionRequest || 0) + 1; state.visualizationSelectionRequest = token;
  try {
    const asset = await api(`/api/assets/${encodeURIComponent(point.asset_id)}`);
    if (token !== state.visualizationSelectionRequest) return;
    const first = asset.physical_files?.[0] || {}; const thumbnail = first.thumbnail_url ? `<img class="visualization-selection-thumb" src="${escapeHtml(first.thumbnail_url)}" alt="">` : "";
    const quality = first.quality_score == null ? "" : ` · Quality ${Number(first.quality_score).toFixed(2)}`;
    panel.innerHTML = `<div class="visualization-selection-header"><div><h3>${escapeHtml(first.filename || "Asset")}</h3><div class="muted">${escapeHtml(formatCapture(asset.capture_time) || "Capture time unavailable")} · ${escapeHtml(asset.media_type || "media")}${quality}</div></div><button class="icon" type="button" data-visualization-close aria-label="Close preview">×</button></div><div class="visualization-selection-body">${thumbnail}<div class="visualization-selection-copy"><span>${escapeHtml(first.relative_path || "")}</span><span class="muted">${first.width && first.height ? `${first.width} × ${first.height}` : ""}</span></div></div><div class="visualization-selection-actions"><button type="button" data-visualization-open>Open</button><button type="button" class="secondary" data-visualization-details>Details</button></div>`;
    const image = panel.querySelector("img"); if (image) image.onerror = () => image.remove();
    panel.querySelector("[data-visualization-close]").onclick = hideVisualizationSelection;
    const viewerItem = assetToViewerItem(asset);
    panel.querySelector("[data-visualization-open]").onclick = () => showViewer(0, [viewerItem], {mode: "visualization"});
    panel.querySelector("[data-visualization-details]").onclick = () => showDetails(asset.asset_id, {mode: "visualization", items: [viewerItem], index: 0});
  } catch (error) { panel.innerHTML = `<div class="error">Asset preview unavailable: ${escapeHtml(error.message)}</div>`; }
}

function hideVisualizationSelection() { state.visualizationSelectedId = null; state.visualizationSelectionRequest = (state.visualizationSelectionRequest || 0) + 1; $("visualization-selection").classList.add("hidden"); }

function bindVisualizationEvents(mode) {
  const canvas = visualizationCanvas(mode); let dragging = false; let startX = 0; let startY = 0; let moved = false;
  canvas.addEventListener("wheel", event => { event.preventDefault(); const rect = canvas.getBoundingClientRect(); zoomVisualization(mode, event.deltaY < 0 ? 1.22 : 1 / 1.22, {x: event.clientX - rect.left, y: event.clientY - rect.top}); }, {passive: false});
  canvas.addEventListener("pointerdown", event => { if (event.button !== 0) return; dragging = true; moved = false; startX = event.clientX; startY = event.clientY; canvas.classList.add("dragging"); canvas.setPointerCapture(event.pointerId); });
  canvas.addEventListener("pointermove", event => { if (!dragging) return; const dx = event.clientX - startX; const dy = event.clientY - startY; if (Math.abs(dx) + Math.abs(dy) > 4) moved = true; if (moved) panVisualization(mode, dx, dy); startX = event.clientX; startY = event.clientY; });
  ["pointerup", "pointercancel"].forEach(name => canvas.addEventListener(name, event => { if (!dragging) return; dragging = false; canvas.classList.remove("dragging"); if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId); if (moved) canvas.dataset.suppressClick = "1"; }));
  canvas.addEventListener("click", event => { if (canvas.dataset.suppressClick) { delete canvas.dataset.suppressClick; return; } hitVisualization(mode, event); });
  $(`${mode}-fit`).onclick = () => fitVisualization(mode);
  $(`${mode}-zoom-in`).onclick = () => zoomVisualization(mode, 1.35);
  $(`${mode}-zoom-out`).onclick = () => zoomVisualization(mode, 1 / 1.35);
}

VISUALIZATION_MODES.forEach(bindVisualizationEvents);
window.addEventListener("resize", () => { if (VISUALIZATION_MODES.includes(state.viewMode)) renderVisualization(state.viewMode); });
