const VISUALIZATION_MODES = ["geo", "timeline", "vector"];
const TIMELINE_INTERVALS = [0.001, 0.01, 0.1, 1, 5, 10, 30, 60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 604800, 2592000, 7776000, 31536000];
const TIMELINE_KERNEL = [1, 4, 6, 4, 1];
let worldFeaturesPromise;
const visualizationThumbnailCache = new Map();
let visualizationSelectionRequest = 0;

function visualizationCanvas(mode) { return $(`${mode}-canvas`); }
function visualizationView(mode) { return state.visualizations[mode]; }

function scheduleVisualizationRender(mode) {
  const view = visualizationView(mode);
  if (!view || view.renderFrame || state.viewMode !== mode || !view.data) return;
  view.renderFrame = requestAnimationFrame(() => {
    view.renderFrame = 0;
    if (state.viewMode === mode && view.data) renderVisualization(mode);
  });
}

function cancelVisualizationAnimation(mode) {
  const view = visualizationView(mode);
  if (view?.cameraFrame) cancelAnimationFrame(view.cameraFrame);
  if (view) view.cameraFrame = 0;
}

function syncVisualizationTargets(mode, view) {
  if (mode === "timeline") {
    if (!Number.isFinite(view.targetCenterTime)) view.targetCenterTime = view.centerTime;
    if (!Number.isFinite(view.targetVisibleSpan)) view.targetVisibleSpan = view.visibleSpan;
  } else {
    if (!Number.isFinite(view.targetCenterX)) view.targetCenterX = view.centerX;
    if (!Number.isFinite(view.targetCenterY)) view.targetCenterY = view.centerY;
    if (!Number.isFinite(view.targetScale)) view.targetScale = view.scale;
  }
}

function commitVisualizationCamera(mode) {
  const view = visualizationView(mode);
  cancelVisualizationAnimation(mode);
  syncVisualizationTargets(mode, view);
  if (mode === "timeline") {
    view.targetCenterTime = view.centerTime;
    view.targetVisibleSpan = view.visibleSpan;
  } else {
    view.targetCenterX = view.centerX;
    view.targetCenterY = view.centerY;
    view.targetScale = view.scale;
  }
}

function animateVisualizationCamera(mode) {
  const view = visualizationView(mode);
  if (!view || view.cameraFrame) return;
  syncVisualizationTargets(mode, view);
  let previous = 0;
  const step = timestamp => {
    if (state.viewMode !== mode || !view.data) { view.cameraFrame = 0; return; }
    const elapsed = previous ? Math.min(64, timestamp - previous) : 16;
    previous = timestamp;
    const amount = 1 - Math.exp(-elapsed / 105);
    if (mode === "timeline") {
      view.centerTime += (view.targetCenterTime - view.centerTime) * amount;
      view.visibleSpan *= Math.exp(Math.log(Math.max(.001, view.targetVisibleSpan) / Math.max(.001, view.visibleSpan)) * amount);
      if (Math.abs(view.targetCenterTime - view.centerTime) < .01 && Math.abs(Math.log(view.targetVisibleSpan / view.visibleSpan)) < .0005) {
        view.centerTime = view.targetCenterTime; view.visibleSpan = view.targetVisibleSpan; view.cameraFrame = 0; renderVisualization(mode); return;
      }
    } else {
      view.centerX += (view.targetCenterX - view.centerX) * amount;
      view.centerY += (view.targetCenterY - view.centerY) * amount;
      view.scale *= Math.exp(Math.log(Math.max(.001, view.targetScale) / Math.max(.001, view.scale)) * amount);
      if (Math.abs(view.targetCenterX - view.centerX) < .00001 && Math.abs(view.targetCenterY - view.centerY) < .00001 && Math.abs(Math.log(view.targetScale / view.scale)) < .0005) {
        view.centerX = view.targetCenterX; view.centerY = view.targetCenterY; view.scale = view.targetScale; view.cameraFrame = 0; renderVisualization(mode); return;
      }
    }
    renderVisualization(mode);
    view.cameraFrame = requestAnimationFrame(step);
  };
  view.cameraFrame = requestAnimationFrame(step);
}

async function loadVisualization(mode) {
  if (!VISUALIZATION_MODES.includes(mode)) return;
  const generation = state.searchGeneration;
  const requestId = ++state.visualizationRequest;
  state.visualizationAbort?.abort();
  const controller = new AbortController();
  state.visualizationAbort = controller;
  const params = filterParams();
  params.set("view", mode);
  if (mode === "timeline") params.set("time_mode", visualizationView(mode).timeMode || "capture");
  const key = String(params);
  const view = visualizationView(mode);
  if (view.key === key && view.data) { if (["loading", "searching"].includes(view.data.search?.state) && params.get("semantic") !== "0") scheduleSearchPoll(generation); renderVisualization(mode); return; }
  try {
    const data = await api(`/api/visualizations/${mode}?${params}`, {signal: controller.signal});
    if (controller.signal.aborted || requestId !== state.visualizationRequest || generation !== state.searchGeneration) return;
    const changed = view.key !== key || view.data?.browser_revision !== data.browser_revision;
    view.key = key;
    view.data = data;
    if (changed) {
      if (mode === "timeline" && view.preserveTransform) {
        const transform = view.modes?.[view.timeMode];
        if (transform) Object.assign(view, transform); else view.needsFit = true;
        view.preserveTransform = false;
      } else view.needsFit = true;
      view.hitTargets = [];
      view.selectedId = null;
      $("visualization-selection-panel")?.classList.add("hidden");
      view.timelineCache = null;
      view.timelineCaches = new Map();
      view.lodCaches = new Map();
      view.densityCache = null;
    }
    state.searchProvider = data.search?.provider || state.searchProvider;
    showSearchStatus(data.search || {state: "complete"}, {...data, media_shown: data.filtered_asset_count, workspace_total: data.workspace_total});
    if (["loading", "searching"].includes(data.search?.state) && params.get("semantic") !== "0") scheduleSearchPoll(generation);
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
  if (mode === "timeline") renderTimelineModeButtons(view.timeMode || "capture");
  if (mode === "geo") drawGeo(canvas, view, data);
  else if (mode === "timeline") drawTimeline(canvas, view, data);
  else drawVector(canvas, view, data);
  markVisualizationCanvas(mode, canvas, view, data);
}

function renderTimelineModeButtons(mode) {
  for (const [id, value] of [["timeline-mode-capture", "capture"], ["timeline-mode-file-created", "file_created"]]) {
    const button = $(id);
    if (!button) continue;
    button.classList.toggle("active", mode === value);
    button.setAttribute("aria-pressed", String(mode === value));
  }
}

function markVisualizationCanvas(mode, canvas, view, data) {
  canvas.dataset.pointCount = String(data.available ? data.represented_point_count || 0 : 0);
  canvas.dataset.viewScale = String(mode === "timeline" ? view.visibleSpan : view.scale);
  if (mode === "timeline") canvas.dataset.timeMode = view.timeMode || "capture";
  const target = view.hitTargets?.[0];
  const badgeTarget = view.hitTargets?.find(candidate => Number(candidate.count) > 1);
  canvas.dataset.badgeCount = String(badgeTarget?.count || 0);
  if (target) {
    canvas.dataset.firstTargetX = String(target.x);
    canvas.dataset.firstTargetY = String(target.y);
    canvas.dataset.firstTargetWidth = String(target.width || target.thumbnailWidth || 0);
  } else {
    delete canvas.dataset.firstTargetX;
    delete canvas.dataset.firstTargetY;
    delete canvas.dataset.firstTargetWidth;
  }
}

function canvasSurface(canvas) {
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, Math.round(rect.width || canvas.parentElement?.clientWidth || 800));
  const height = Math.max(1, Math.round(rect.height || 380));
  const ratio = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  if (canvas.width !== width * ratio || canvas.height !== height * ratio) { canvas.width = width * ratio; canvas.height = height * ratio; }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  return {context, width, height};
}

function screenPoint(view, x, y, width, height) {
  return {x: (x - view.centerX) * view.scale + width / 2, y: (y - view.centerY) * view.scale + height / 2};
}

function worldPoint(view, x, y, width, height) {
  return {x: (x - width / 2) / view.scale + view.centerX, y: (y - height / 2) / view.scale + view.centerY};
}

function visualizationMaximum(mode, view) {
  if (mode === "geo") return Math.max(1, Number(view.baseScale) || 1) * 2 ** 24;
  if (mode === "vector") return Math.max(1, Number(view.baseScale) || 1) * 2 ** 20;
  return 1;
}

function timelineTimeAt(view, x, width) { return view.centerTime + (x - width / 2) / width * view.visibleSpan; }

function timelineDataBounds(view) {
  const points = view.data?.points || [];
  if (!points.length) return {min: 0, max: 0};
  const min = Number(points[0].time);
  const max = Number(points[points.length - 1].time);
  return {min, max};
}

function timelineBounds(view, width = 800, requestedSpan = view.visibleSpan) {
  const data = timelineDataBounds(view);
  const span = Math.max(.001, Number(requestedSpan) || 1);
  const footprint = (visualizationThumbnailSize(view, "timeline") / 2 + 18) / Math.max(1, width) * span;
  const padding = data.min === data.max ? Math.max(43200, footprint) : Math.max(1, (data.max - data.min) * .06, footprint);
  return {min: data.min - padding, max: data.max + padding};
}

function clampTimelineCamera(view, width = 800) {
  const clamped = clampTimelineValues(view, view.centerTime, view.visibleSpan, width);
  view.centerTime = clamped.centerTime; view.visibleSpan = clamped.visibleSpan;
}

function clampTimelineValues(view, centerTime, visibleSpan, width = 800) {
  const bounds = timelineBounds(view, width, visibleSpan);
  if (bounds.max <= bounds.min) return {centerTime, visibleSpan: Math.max(.001, visibleSpan)};
  const span = Math.min(Math.max(.001, visibleSpan), Math.max(.001, bounds.max - bounds.min));
  const half = span / 2;
  return {visibleSpan: span, centerTime: Math.max(bounds.min + half, Math.min(bounds.max - half, centerTime))};
}

function zoomVisualization(mode, factor, point) {
  const canvas = visualizationCanvas(mode);
  const view = visualizationView(mode);
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, rect.width || 800);
  const height = Math.max(1, rect.height || 380);
  const cursor = point || {x: width / 2, y: height / 2};
  syncVisualizationTargets(mode, view);
  if (mode === "timeline") {
    const before = view.targetCenterTime + (cursor.x - width / 2) / width * view.targetVisibleSpan;
    const nextSpan = Math.max(.001, Math.min(Math.max(.001, timelineBounds(view, width, view.targetVisibleSpan).max - timelineBounds(view, width, view.targetVisibleSpan).min), view.targetVisibleSpan / factor));
    const clamped = clampTimelineValues(view, before - (cursor.x - width / 2) / width * nextSpan, nextSpan, width);
    view.targetCenterTime = clamped.centerTime; view.targetVisibleSpan = clamped.visibleSpan;
  } else {
    const before = {x: (cursor.x - width / 2) / view.targetScale + view.targetCenterX, y: (cursor.y - height / 2) / view.targetScale + view.targetCenterY};
    view.targetScale = Math.max(.001, Math.min(visualizationMaximum(mode, view), view.targetScale * factor));
    view.targetCenterX = before.x - (cursor.x - width / 2) / view.targetScale;
    view.targetCenterY = before.y - (cursor.y - height / 2) / view.targetScale;
  }
  animateVisualizationCamera(mode);
}

function fitVisualization(mode) { commitVisualizationCamera(mode); visualizationView(mode).needsFit = true; renderVisualization(mode); }

function panVisualization(mode, dx, dy) {
  const view = visualizationView(mode);
  const canvas = visualizationCanvas(mode);
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, rect.width || 800);
  commitVisualizationCamera(mode);
  if (mode === "timeline") {
    view.centerTime -= dx / width * view.visibleSpan;
    clampTimelineCamera(view, width);
    view.targetCenterTime = view.centerTime; view.targetVisibleSpan = view.visibleSpan;
  } else {
    view.centerX -= dx / Math.max(.001, view.scale);
    view.centerY -= dy / Math.max(.001, view.scale);
    view.targetCenterX = view.centerX; view.targetCenterY = view.centerY; view.targetScale = view.scale;
  }
  renderVisualization(mode);
}

function saveTimelineTransform(view) { return {centerTime: view.centerTime, visibleSpan: view.visibleSpan, fitVisibleSpan: view.fitVisibleSpan, targetCenterTime: view.targetCenterTime, targetVisibleSpan: view.targetVisibleSpan, needsFit: view.needsFit}; }

function switchTimelineMode(mode) {
  if (!["capture", "file_created"].includes(mode)) return;
  const view = visualizationView("timeline");
  view.modes[view.timeMode] = saveTimelineTransform(view);
  view.timeMode = mode;
  Object.assign(view, view.modes[mode]);
  view.preserveTransform = true;
  view.key = null; view.data = null;
  loadVisualization("timeline");
}

function geoWorld(longitude, latitude) {
  const safeLatitude = Math.max(-85, Math.min(85, Number(latitude)));
  const radians = safeLatitude * Math.PI / 180;
  const mercator = Math.log(Math.tan(Math.PI / 4 + radians / 2));
  return {x: (Number(longitude) + 180) / 360, y: .5 - mercator / (2 * Math.PI)};
}

function geoLod(view) { return Math.max(0, Math.min(24, Math.floor(Math.log2(Math.max(1, view.scale / Math.max(1, view.baseScale || 1)))))); }
function sameGeoLocation(points) {
  if (points.length < 2) return false;
  const latitude = Number(points[0].latitude); const longitude = Number(points[0].longitude);
  return points.every(point => Math.abs(Number(point.latitude) - latitude) < 1e-7 && Math.abs(Number(point.longitude) - longitude) < 1e-7);
}

function buildGeoLodCache(view, points, level) {
  if (!view.lodCaches) view.lodCaches = new Map();
  const key = `${view.key}|${view.data?.browser_revision || ""}|${level}`;
  if (view.lodCaches.has(key)) return view.lodCaches.get(key);
  const cellWorld = view.baseCellWorld / 2 ** level; const cells = new Map();
  for (const point of points) {
    const world = geoWorld(point.longitude, point.latitude); const key = `${Math.floor(world.x / cellWorld)}:${Math.floor(world.y / cellWorld)}`; const cell = cells.get(key) || {key, points: [], worldX: 0, worldY: 0};
    cell.points.push({...point, worldX: world.x, worldY: world.y}); cell.worldX += world.x; cell.worldY += world.y; cells.set(key, cell);
  }
  for (const cell of cells.values()) { cell.worldX /= cell.points.length; cell.worldY /= cell.points.length; }
  const cache = {key, level, cellWorld, cells}; view.lodCaches.set(key, cache);
  while (view.lodCaches.size > 5) view.lodCaches.delete(view.lodCaches.keys().next().value);
  return cache;
}

function visibleGeoCells(cache, view, width, height) {
  const overscan = 220 / Math.max(.001, view.scale); const left = worldPoint(view, -overscan, 0, width, height).x; const right = worldPoint(view, width + overscan, 0, width, height).x; const top = worldPoint(view, 0, -overscan, width, height).y; const bottom = worldPoint(view, 0, height + overscan, width, height).y; const startX = Math.floor(Math.min(left, right) / cache.cellWorld); const endX = Math.floor(Math.max(left, right) / cache.cellWorld); const startY = Math.floor(Math.min(top, bottom) / cache.cellWorld); const endY = Math.floor(Math.max(top, bottom) / cache.cellWorld); const cells = [];
  for (let column = startX; column <= endX; column += 1) for (let row = startY; row <= endY; row += 1) { const cell = cache.cells.get(`${column}:${row}`); if (cell) cells.push(cell); }
  return cells;
}

function geoLatitude(worldY) { return Math.atan(Math.sinh(Math.PI * (1 - 2 * worldY))) * 180 / Math.PI; }

function niceScaleValue(value) {
  const power = 10 ** Math.floor(Math.log10(Math.max(1, value))); const normalized = value / power; const factor = normalized >= 5 ? 5 : normalized >= 2 ? 2 : 1; return factor * power;
}

function drawGeoLocalField(ctx, view, width, height, alpha) {
  if (alpha <= 0) return;
  ctx.save(); ctx.globalAlpha = alpha; ctx.fillStyle = "#0d151b"; ctx.fillRect(0, 0, width, height); ctx.strokeStyle = "rgba(125, 164, 182, .18)"; ctx.lineWidth = 1;
  for (let x = 0; x <= width; x += 72) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); }
  for (let y = 0; y <= height; y += 72) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
  ctx.fillStyle = "#b6c9d4"; ctx.font = "12px system-ui"; ctx.textAlign = "right"; ctx.textBaseline = "top"; ctx.fillText("N", width - 14, 12); ctx.beginPath(); ctx.moveTo(width - 18, 36); ctx.lineTo(width - 10, 36); ctx.lineTo(width - 14, 22); ctx.closePath(); ctx.fill();
  const latitude = Math.max(-85, Math.min(85, geoLatitude(view.centerY))); const metersPerWorldX = 40075017 * Math.max(.1, Math.cos(latitude * Math.PI / 180)); const rawMeters = 120 / Math.max(.001, view.scale) * metersPerWorldX; const meters = niceScaleValue(rawMeters); const pixels = meters / metersPerWorldX * view.scale; const x = 18; const y = height - 22; ctx.strokeStyle = "#c5d8e3"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + pixels, y); ctx.stroke(); ctx.textAlign = "left"; ctx.textBaseline = "bottom"; ctx.fillText(`${meters >= 1000 ? `${(meters / 1000).toLocaleString()} km` : `${meters.toLocaleString()} m`}`, x, y - 4); ctx.restore();
}

function drawGeo(canvas, view, data) {
  const surface = canvasSurface(canvas); const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  const points = (data.points || []).filter(point => Number.isFinite(Number(point.latitude)) && Number.isFinite(Number(point.longitude)));
  if (!data.available || !points.length) { view.hitTargets = []; drawCanvasMessage(ctx, width, height, data.empty_reason || "No geotagged assets."); return; }
  if (view.needsFit) fitGeo(view, points, width, height);
  if (!worldFeaturesPromise) worldFeaturesPromise = fetch("/world.json").then(response => response.ok ? response.json() : {features: []}).catch(() => ({features: []}));
  if (!view.world) worldFeaturesPromise.then(world => { if (state.viewMode === "geo" && view.data === data && !view.world) { view.world = world; renderVisualization("geo"); } });
  const zoom = Math.max(0, Math.log2(Math.max(1, view.scale / Math.max(1, view.baseScale || 1)))); const localProgress = Math.max(0, Math.min(1, (zoom - 5) / 2)); const localAlpha = localProgress * localProgress * (3 - 2 * localProgress);
  ctx.save(); ctx.globalAlpha = 1 - localAlpha; for (const feature of view.world?.features || []) drawGeoFeature(ctx, feature, view, width, height); ctx.restore(); drawGeoLocalField(ctx, view, width, height, localAlpha);
  const cache = buildGeoLodCache(view, points, geoLod(view)); const clusters = visibleGeoCells(cache, view, width, height);
  view.hitTargets = [];
  for (const cluster of clusters) {
    const center = screenPoint(view, cluster.worldX, cluster.worldY, width, height); const x = center.x; const y = center.y;
    if (x < -220 || x > width + 220 || y < -220 || y > height + 220) continue;
    const size = visualizationThumbnailSize(view, "geo"); const thumbHeight = Math.max(40, Math.round(size * .72));
    const representative = representativeVisualizationPoint(cluster.points);
    drawVisualizationThumbnail(ctx, representative, x - size / 2, y - thumbHeight / 2, size, thumbHeight);
    drawVisualizationSelection(ctx, x - size / 2, y - thumbHeight / 2, size, thumbHeight, view.selectedId === representative.asset_id);
    const badgeX = x + size / 2 - 3; const badgeY = y - thumbHeight / 2 + 3;
    drawVisualizationBadge(ctx, badgeX, badgeY, cluster.points.length);
    view.hitTargets.push({x, y, badgeX, badgeY, width: size, height: thumbHeight, count: cluster.points.length, hitRadius: Math.max(18, size / 2 + 8), points: cluster.points, sameLocation: sameGeoLocation(cluster.points)});
  }
}

function drawGeoFeature(ctx, feature, view, width, height) {
  const polygons = feature.geometry?.type === "MultiPolygon" ? feature.geometry.coordinates.flat(1) : feature.geometry?.type === "Polygon" ? feature.geometry.coordinates : [];
  for (const polygon of polygons) {
    ctx.beginPath();
    polygon.forEach((coordinate, index) => { const world = geoWorld(coordinate[0], coordinate[1]); const screen = screenPoint(view, world.x, world.y, width, height); if (index === 0) ctx.moveTo(screen.x, screen.y); else ctx.lineTo(screen.x, screen.y); });
    ctx.closePath(); ctx.fillStyle = "#18232c"; ctx.fill(); ctx.strokeStyle = "#33424e"; ctx.lineWidth = 1; ctx.stroke();
  }
}

function fitGeo(view, points, width, height) {
  const values = points.map(point => geoWorld(point.longitude, point.latitude));
  if (!values.length) { view.centerX = .5; view.centerY = .5; view.scale = Math.min(width, height) * .86; view.baseScale = view.scale; view.baseCellWorld = 1 / 8; view.targetCenterX = view.centerX; view.targetCenterY = view.centerY; view.targetScale = view.scale; view.needsFit = false; return; }
  const bounds = boundsOf(values, .04);
  view.centerX = (bounds.minX + bounds.maxX) / 2; view.centerY = (bounds.minY + bounds.maxY) / 2;
  view.scale = Math.max(1, Math.min(width / (bounds.maxX - bounds.minX), height / (bounds.maxY - bounds.minY))); view.baseScale = view.scale;
  view.baseCellWorld = Math.max((bounds.maxX - bounds.minX), (bounds.maxY - bounds.minY)) / 8; view.targetCenterX = view.centerX; view.targetCenterY = view.centerY; view.targetScale = view.scale; view.needsFit = false;
}

function timelineBucketInterval(span, width) {
  const target = Math.max(.001, Number(span)) * 92 / Math.max(1, Number(width));
  return TIMELINE_INTERVALS.reduce((closest, interval) => Math.abs(Math.log(interval / target)) < Math.abs(Math.log(closest / target)) ? interval : closest, TIMELINE_INTERVALS[0]);
}

function timelineBucketStart(value, interval) {
  const date = new Date(Number(value) * 1000);
  if (interval >= 31536000) return Date.UTC(date.getUTCFullYear(), 0, 1) / 1000;
  if (interval >= 7776000) return Date.UTC(date.getUTCFullYear(), Math.floor(date.getUTCMonth() / 3) * 3, 1) / 1000;
  if (interval >= 2592000) return Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1) / 1000;
  return Math.floor(Number(value) / interval) * interval;
}

function timelineBucketEnd(start, interval) {
  const date = new Date(Number(start) * 1000);
  if (interval >= 31536000) return Date.UTC(date.getUTCFullYear() + 1, 0, 1) / 1000;
  if (interval >= 7776000) return Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 3, 1) / 1000;
  if (interval >= 2592000) return Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 1) / 1000;
  return start + interval;
}

function timelineX(view, time, width) { return width / 2 + (Number(time) - view.centerTime) / view.visibleSpan * width; }

function timelineBucketPrevious(start, interval) {
  const date = new Date(Number(start) * 1000);
  if (interval >= 31536000) return Date.UTC(date.getUTCFullYear() - 1, 0, 1) / 1000;
  if (interval >= 7776000) return Date.UTC(date.getUTCFullYear(), date.getUTCMonth() - 3, 1) / 1000;
  if (interval >= 2592000) return Date.UTC(date.getUTCFullYear(), date.getUTCMonth() - 1, 1) / 1000;
  return start - interval;
}

function timelineLod(view, width) {
  const target = Math.max(.001, Number(view.visibleSpan) || 1) * 92 / Math.max(1, width);
  if (target <= TIMELINE_INTERVALS[0]) return {coarse: TIMELINE_INTERVALS[0], fine: TIMELINE_INTERVALS[0], fineBlend: 1};
  for (let index = 1; index < TIMELINE_INTERVALS.length; index += 1) {
    const coarse = TIMELINE_INTERVALS[index];
    if (target <= coarse) {
      const fine = TIMELINE_INTERVALS[index - 1];
      const progress = Math.max(0, Math.min(1, Math.log(coarse / target) / Math.log(coarse / fine)));
      const smooth = progress * progress * (3 - 2 * progress);
      return {coarse, fine, fineBlend: smooth};
    }
  }
  const last = TIMELINE_INTERVALS.at(-1);
  return {coarse: last, fine: last, fineBlend: 1};
}

function timelineVisibleBins(cache, start, end) {
  const bins = [];
  let cursor = timelineBucketStart(start, cache.interval);
  while (cursor <= end) {
    const next = timelineBucketEnd(cursor, cache.interval);
    bins.push(cache.occupied.get(cursor) || {start: cursor, end: next, count: 0, point: null});
    if (!(next > cursor)) break;
    cursor = next;
  }
  return bins;
}

function timelineDensityAt(cache, start) {
  let cursor = timelineBucketPrevious(timelineBucketPrevious(start, cache.interval), cache.interval);
  let total = 0;
  for (const weight of TIMELINE_KERNEL) {
    total += (cache.occupied.get(cursor)?.count || 0) * weight;
    cursor = timelineBucketEnd(cursor, cache.interval);
  }
  return total / 16;
}

function buildTimelineCache(view, points, interval) {
  const key = `${view.key}|${view.timeMode}|${interval}`;
  if (!view.timelineCaches) view.timelineCaches = new Map();
  if (view.timelineCaches.has(key)) return view.timelineCaches.get(key);
  const occupied = new Map();
  for (const point of points) {
    const start = timelineBucketStart(point.time, interval); const bucket = occupied.get(start) || {start, end: timelineBucketEnd(start, interval), count: 0, point: null};
    bucket.count += 1;
    if (!bucket.point || representativeVisualizationPoint([bucket.point, point]) === point) bucket.point = point;
    occupied.set(start, bucket);
  }
  const cache = {key, interval, occupied, maximum: 1};
  for (const start of occupied.keys()) cache.maximum = Math.max(cache.maximum, timelineDensityAt(cache, start));
  view.timelineCaches.set(key, cache);
  while (view.timelineCaches.size > 5) view.timelineCaches.delete(view.timelineCaches.keys().next().value);
  return cache;
}

function drawTimelineDensity(ctx, view, cache, start, end, axisY, width, height) {
  const visible = timelineVisibleBins(cache, start, end);
  if (!visible.length) return;
  ctx.beginPath(); ctx.moveTo(Math.max(0, timelineX(view, start, width)), axisY);
  for (const bucket of visible) {
    const x1 = Math.max(0, timelineX(view, Math.max(start, bucket.start), width)); const x2 = Math.min(width, timelineX(view, Math.min(end, bucket.end), width));
    const y = axisY - 8 - timelineDensityAt(cache, bucket.start) / cache.maximum * Math.min(48, height * .14);
    ctx.lineTo(x1, y); ctx.lineTo(x2, y);
  }
  ctx.lineTo(Math.min(width, timelineX(view, end, width)), axisY); ctx.closePath(); ctx.fillStyle = "rgba(55, 111, 139, .22)"; ctx.fill();
}

function drawTimeline(canvas, view, data) {
  const surface = canvasSurface(canvas); const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  const points = (data.points || []).filter(point => Number.isFinite(Number(point.time))).sort((left, right) => left.time - right.time || String(left.asset_id).localeCompare(String(right.asset_id)));
  if (!data.available || !points.length) { view.hitTargets = []; drawCanvasMessage(ctx, width, height, data.empty_reason || "No timed assets."); return; }
  if (view.needsFit) fitTimeline(view, points, width);
  clampTimelineCamera(view, width);
  const start = view.centerTime - view.visibleSpan / 2; const end = view.centerTime + view.visibleSpan / 2;
  const lod = timelineLod(view, width); const coarse = buildTimelineCache(view, points, lod.coarse); const fine = buildTimelineCache(view, points, lod.fine); const axisY = height - 44;
  const fineAlpha = lod.coarse === lod.fine ? 1 : lod.fineBlend;
  ctx.save(); ctx.globalAlpha = 1 - fineAlpha; drawTimelineDensity(ctx, view, coarse, start, end, axisY, width, height); ctx.restore();
  if (fineAlpha > 0) { ctx.save(); ctx.globalAlpha = fineAlpha; drawTimelineDensity(ctx, view, fine, start, end, axisY, width, height); ctx.restore(); }
  const candidates = new Map();
  const collect = (cache, alpha, sizeMultiplier) => {
    for (const bucket of timelineVisibleBins(cache, start, end)) {
      if (!bucket.count || !bucket.point) continue;
      const existing = candidates.get(bucket.point.asset_id);
      const candidate = {bucket, alpha, sizeMultiplier, point: bucket.point};
      if (!existing || candidate.alpha >= existing.alpha) candidates.set(bucket.point.asset_id, candidate);
    }
  };
  collect(coarse, 1 - fineAlpha, 1 - .1 * fineAlpha);
  if (fineAlpha > 0) collect(fine, fineAlpha, .78 + .22 * fineAlpha);
  view.hitTargets = [];
  for (const candidate of candidates.values()) {
    const bucket = candidate.bucket; const x = timelineX(view, candidate.point.time, width); const size = Math.round(visualizationThumbnailSize(view, "timeline") * candidate.sizeMultiplier); const thumbHeight = Math.max(48, Math.round(size * .72)); const baseline = axisY - 38; const y = baseline - thumbHeight / 2;
    if (x < -size || x > width + size) continue;
    ctx.save(); ctx.globalAlpha = candidate.alpha; ctx.strokeStyle = "rgba(145, 169, 181, .45)"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, y + thumbHeight / 2); ctx.lineTo(x, baseline); ctx.stroke();
    drawVisualizationThumbnail(ctx, candidate.point, x - size / 2, y - thumbHeight / 2, size, thumbHeight);
    drawVisualizationSelection(ctx, x - size / 2, y - thumbHeight / 2, size, thumbHeight, view.selectedId === candidate.point.asset_id);
    const badgeX = x + size / 2 - 3; const badgeY = y - thumbHeight / 2 + 3; drawVisualizationBadge(ctx, badgeX, badgeY, bucket.count); ctx.restore();
    view.hitTargets.push({x, y, badgeX, badgeY, width: size, height: thumbHeight, count: bucket.count, hitRadius: Math.max(24, size), thumbnailWidth: size, thumbnailHeight: thumbHeight, point: candidate.point, bucket: {start: bucket.start, end: bucket.end}});
  }
  drawTimelineAxis(ctx, view, start, end, axisY, width, height);
}

function fitTimeline(view, points, width) {
  const min = Number(points[0].time); const max = Number(points[points.length - 1].time); const range = Math.max(.001, max - min); const padding = min === max ? 43200 : Math.max(1, range * .06);
  view.centerTime = (min + max) / 2; view.visibleSpan = min === max ? 86400 : range + padding * 2; view.fitVisibleSpan = view.visibleSpan; clampTimelineCamera(view, width); view.targetCenterTime = view.centerTime; view.targetVisibleSpan = view.visibleSpan; view.needsFit = false; view.modes[view.timeMode] = saveTimelineTransform(view);
}

function timelineTickStep(span, width) {
  const target = Math.max(.001, span) * 90 / Math.max(1, width);
  return TIMELINE_INTERVALS.find(interval => interval >= target) || TIMELINE_INTERVALS.at(-1);
}

function formatTimelineTick(value, step) {
  const labels = timelineTickLabels(value, step);
  return labels.detail || labels.context;
}

function timelineTickLabels(value, step, previousValue = null) {
  const date = new Date(Number(value) * 1000); const pad = number => String(number).padStart(2, "0"); const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const context = step >= 31536000 ? String(date.getUTCFullYear()) : step >= 2592000 ? `${months[date.getUTCMonth()]} ${date.getUTCFullYear()}` : step >= 86400 ? `${pad(date.getUTCDate())} ${months[date.getUTCMonth()]}` : previousValue === null || new Date(Number(previousValue) * 1000).getUTCDate() !== date.getUTCDate() ? `${pad(date.getUTCDate())} ${months[date.getUTCMonth()]}` : "";
  let detail = "";
  if (step < .01) detail = `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}.${String(date.getUTCMilliseconds()).padStart(3, "0")}`;
  else if (step < 1) detail = `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
  else if (step < 86400) detail = `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}${step < 60 ? `:${pad(date.getUTCSeconds())}` : ""}`;
  return {context, detail};
}

function timelineTicks(start, end, step) {
  const values = [];
  if (step >= 2592000) {
    let cursor = new Date(timelineBucketStart(start, step) * 1000);
    while (cursor.getTime() / 1000 <= end) { values.push(cursor.getTime() / 1000); cursor = new Date(timelineBucketEnd(cursor.getTime() / 1000, step) * 1000); }
    return values;
  }
  for (let value = Math.ceil(start / step) * step; value <= end; value += step) values.push(value);
  return values;
}

function drawTimelineAxis(ctx, view, start, end, axisY, width) {
  const step = timelineTickStep(end - start, width);
  ctx.strokeStyle = "#61717e"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0, axisY); ctx.lineTo(width, axisY); ctx.stroke();
  let previous = null;
  for (const time of timelineTicks(start, end, step)) { const x = timelineX(view, time, width); if (x < -30 || x > width + 30) continue; ctx.strokeStyle = "#61717e"; ctx.beginPath(); ctx.moveTo(x, axisY); ctx.lineTo(x, axisY + 7); ctx.stroke(); const labels = timelineTickLabels(time, step, previous); ctx.fillStyle = "#9faab5"; ctx.font = "11px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "top"; if (labels.context) ctx.fillText(labels.context, x, axisY + 9); if (labels.detail) ctx.fillText(labels.detail, x, axisY + 24); previous = time; }
}

function representativeVisualizationPoint(points) {
  return [...points].sort((left, right) => { const leftQuality = Number.isFinite(Number(left.quality_score)) ? Number(left.quality_score) : -Infinity; const rightQuality = Number.isFinite(Number(right.quality_score)) ? Number(right.quality_score) : -Infinity; return rightQuality - leftQuality || String(left.asset_id).localeCompare(String(right.asset_id)); })[0];
}

function visualizationThumbnailSize(view, mode) {
  const current = mode === "timeline" ? Number(view.visibleSpan) || 1 : Number(view.scale) || 1;
  const fit = mode === "timeline" ? Number(view.fitVisibleSpan) || current : Number(view.baseScale) || current;
  const zoom = mode === "timeline" ? Math.max(1, fit / Math.max(.001, current)) : Math.max(1, current / Math.max(.001, fit));
  const base = mode === "timeline" ? 124 : 104;
  const growth = mode === "timeline" ? 42 : 52;
  const maximum = mode === "timeline" ? 220 : 238;
  return Math.round(Math.min(maximum, base + growth * Math.log2(zoom)));
}

function drawVisualizationBadge(ctx, x, y, count) {
  if (Number(count) <= 1) return;
  ctx.beginPath(); ctx.arc(x, y, 11, 0, Math.PI * 2); ctx.fillStyle = "#17222b"; ctx.fill();
  ctx.fillStyle = "#edf0f3"; ctx.font = "600 10px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(String(count), x, y);
}

function drawVisualizationSelection(ctx, x, y, width, height, selected) {
  if (!selected) return;
  ctx.save(); ctx.strokeStyle = "#8dcaf1"; ctx.lineWidth = 3; ctx.strokeRect(x - 2, y - 2, width + 4, height + 4); ctx.restore();
}

function visualizationThumbnail(assetId) {
  if (visualizationThumbnailCache.has(assetId)) return visualizationThumbnailCache.get(assetId);
  const entry = {image: null, loading: true}; const image = new Image();
  image.onload = () => { entry.image = image; entry.loading = false; scheduleVisualizationRender(state.viewMode); }; image.onerror = () => { entry.loading = false; entry.failed = true; scheduleVisualizationRender(state.viewMode); };
  image.src = apiPath(`/api/assets/${encodeURIComponent(assetId)}/thumbnail`); visualizationThumbnailCache.set(assetId, entry);
  while (visualizationThumbnailCache.size > 240) visualizationThumbnailCache.delete(visualizationThumbnailCache.keys().next().value);
  return entry;
}

function drawVisualizationThumbnail(ctx, point, x, y, width, height) {
  const entry = visualizationThumbnail(point.asset_id);
  if (entry.image) { const imageRatio = entry.image.naturalWidth / Math.max(1, entry.image.naturalHeight); const boxRatio = width / height; let drawWidth = width; let drawHeight = height; if (imageRatio > boxRatio) drawHeight = width / imageRatio; else drawWidth = height * imageRatio; ctx.drawImage(entry.image, x + (width - drawWidth) / 2, y + (height - drawHeight) / 2, drawWidth, drawHeight); return; }
  ctx.fillStyle = point.media_type === "video" ? "#9c7240" : "#39708d"; ctx.fillRect(x, y, width, height); ctx.fillStyle = "#c9d5dc"; ctx.font = "600 10px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(point.media_type === "video" ? "VIDEO" : "IMAGE", x + width / 2, y + height / 2);
}

function vectorLod(view) { return Math.max(0, Math.min(20, Math.floor(Math.log2(Math.max(1, view.scale / Math.max(1, view.baseScale || 1)))))); }
function vectorCellKey(x, y, cellWorld, originX, originY) { return `${Math.floor((Number(x) - originX) / cellWorld)}:${Math.floor((Number(y) - originY) / cellWorld)}`; }
function vectorContinuousLod(view) {
  const zoom = Math.max(0, Math.min(20, Math.log2(Math.max(1, view.scale / Math.max(1, view.baseScale || 1)))));
  const coarse = Math.floor(zoom); const fine = Math.min(20, coarse + 1); const progress = Math.min(1, Math.max(0, zoom - coarse));
  return {coarse, fine, fineBlend: progress * progress * (3 - 2 * progress)};
}

function buildVectorLodCache(view, points, level) {
  if (!view.lodCaches) view.lodCaches = new Map();
  const key = `${view.key}|${view.data?.browser_revision || ""}|${level}`;
  if (view.lodCaches.has(key)) return view.lodCaches.get(key);
  const cellWorld = view.baseCellWorld / 2 ** level; const cells = new Map();
  for (const point of points) {
    const key = vectorCellKey(point.x, point.y, cellWorld, view.gridOriginX, view.gridOriginY); const cell = cells.get(key);
    if (!cell) cells.set(key, {key, count: 1, point});
    else { cell.count += 1; if (representativeVisualizationPoint([cell.point, point]) === point) cell.point = point; }
  }
  const cache = {key, level, cellWorld, cells}; view.lodCaches.set(key, cache);
  while (view.lodCaches.size > 5) view.lodCaches.delete(view.lodCaches.keys().next().value);
  return cache;
}

function visibleVectorCells(cache, view, width, height) {
  const overscan = 220 / Math.max(.001, view.scale); const left = worldPoint(view, -overscan, 0, width, height).x; const right = worldPoint(view, width + overscan, 0, width, height).x; const top = worldPoint(view, 0, -overscan, width, height).y; const bottom = worldPoint(view, 0, height + overscan, width, height).y;
  const startX = Math.floor((Math.min(left, right) - view.gridOriginX) / cache.cellWorld); const endX = Math.floor((Math.max(left, right) - view.gridOriginX) / cache.cellWorld); const startY = Math.floor((Math.min(top, bottom) - view.gridOriginY) / cache.cellWorld); const endY = Math.floor((Math.max(top, bottom) - view.gridOriginY) / cache.cellWorld); const cells = [];
  for (let column = startX; column <= endX; column += 1) for (let row = startY; row <= endY; row += 1) { const cell = cache.cells.get(`${column}:${row}`); if (cell) cells.push(cell); }
  return cells;
}
function vectorDensityRasterPoint(point, bounds, size) {
  const rangeX = Math.max(.000001, bounds.maxX - bounds.minX); const rangeY = Math.max(.000001, bounds.maxY - bounds.minY);
  return {x: Math.max(0, Math.min(size - 1, Math.floor((point.x - bounds.minX) / rangeX * size))), y: Math.max(0, Math.min(size - 1, Math.floor((point.y - bounds.minY) / rangeY * size)))};
}

function vectorGridStep(view, width) {
  const targetWorld = 90 / Math.max(.001, view.scale); const power = 10 ** Math.floor(Math.log10(targetWorld));
  return [1, 2, 5, 10].find(value => value * power >= targetWorld) * power;
}

function buildVectorDensity(view, points) {
  const key = `${view.key}|${view.data?.browser_revision || ""}`;
  if (view.densityCache?.key === key) return view.densityCache;
  const size = 512; const raster = document.createElement("canvas"); raster.width = size; raster.height = size; const counts = new Float32Array(size * size); const bounds = view.worldBounds; let maximum = 0;
  for (const point of points) {
    const {x, y} = vectorDensityRasterPoint(point, bounds, size);
    for (let row = Math.max(0, y - 3); row <= Math.min(size - 1, y + 3); row += 1) for (let column = Math.max(0, x - 3); column <= Math.min(size - 1, x + 3); column += 1) { const distance = Math.hypot(column - x, row - y); const weight = distance === 0 ? 1 : Math.exp(-distance * distance / 5); counts[row * size + column] += weight; maximum = Math.max(maximum, counts[row * size + column]); }
  }
  const image = raster.getContext("2d").createImageData(size, size);
  for (let index = 0; index < counts.length; index += 1) { const intensity = Math.sqrt(counts[index] / Math.max(1, maximum)); const offset = index * 4; image.data[offset] = Math.round(8 + intensity * 62); image.data[offset + 1] = Math.round(20 + intensity * 155); image.data[offset + 2] = Math.round(46 + intensity * 150); image.data[offset + 3] = Math.round(intensity * 190); }
  raster.getContext("2d").putImageData(image, 0, 0); view.densityCache = {key, raster}; return view.densityCache;
}

function drawVectorDensity(ctx, points, view, width, height) {
  const density = buildVectorDensity(view, points); const topLeft = screenPoint(view, view.worldBounds.minX, view.worldBounds.minY, width, height); const bottomRight = screenPoint(view, view.worldBounds.maxX, view.worldBounds.maxY, width, height);
  ctx.save(); ctx.globalAlpha = .78; ctx.imageSmoothingEnabled = true; ctx.drawImage(density.raster, topLeft.x, topLeft.y, bottomRight.x - topLeft.x, bottomRight.y - topLeft.y); ctx.restore();
}

function fitVector(view, points, width, height) {
  const values = points.map(point => ({x: Number(point.x), y: Number(point.y)})).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
  if (!values.length) return;
  const bounds = boundsOf(values, Math.max(.1, Math.max(...values.map(point => Math.max(Math.abs(point.x), Math.abs(point.y)))) * .12));
  view.worldBounds = bounds; view.centerX = (bounds.minX + bounds.maxX) / 2; view.centerY = (bounds.minY + bounds.maxY) / 2;
  view.scale = Math.max(1, Math.min(width / (bounds.maxX - bounds.minX), height / (bounds.maxY - bounds.minY))); view.baseScale = view.scale;
  view.baseCellWorld = Math.max(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY) / 8; view.gridOriginX = Math.floor(bounds.minX / view.baseCellWorld) * view.baseCellWorld; view.gridOriginY = Math.floor(bounds.minY / view.baseCellWorld) * view.baseCellWorld; view.targetCenterX = view.centerX; view.targetCenterY = view.centerY; view.targetScale = view.scale; view.needsFit = false;
}

function drawVectorGrid(ctx, view, width, height) {
  const step = vectorGridStep(view, width); const left = worldPoint(view, 0, 0, width, height).x; const right = worldPoint(view, width, 0, width, height).x; const top = worldPoint(view, 0, 0, width, height).y; const bottom = worldPoint(view, 0, height, width, height).y;
  for (let value = Math.ceil(left / step) * step; value <= right; value += step) { const x = screenPoint(view, value, 0, width, height).x; ctx.strokeStyle = Math.abs(value) < step / 100 ? "rgba(190, 213, 225, .55)" : "rgba(116, 150, 166, .2)"; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); }
  for (let value = Math.ceil(top / step) * step; value <= bottom; value += step) { const y = screenPoint(view, 0, value, width, height).y; ctx.strokeStyle = Math.abs(value) < step / 100 ? "rgba(190, 213, 225, .55)" : "rgba(116, 150, 166, .2)"; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
  const origin = screenPoint(view, 0, 0, width, height); if (origin.x >= 0 && origin.x <= width && origin.y >= 0 && origin.y <= height) { ctx.fillStyle = "#aebdc7"; ctx.font = "10px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "top"; ctx.fillText("0,0", origin.x + 5, origin.y + 5); }
}

function drawVector(canvas, view, data) {
  const surface = canvasSurface(canvas); const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  const points = (data.points || []).filter(point => Number.isFinite(Number(point.x)) && Number.isFinite(Number(point.y))).map(point => ({...point, x: Number(point.x), y: Number(point.y)}));
  if (!data.available || !points.length) { view.hitTargets = []; view.grid = new Map(); drawCanvasMessage(ctx, width, height, data.empty_reason || "No projected semantic vectors."); return; }
  if (view.needsFit || !view.worldBounds) { view.densityCache = null; fitVector(view, points, width, height); }
  drawVectorDensity(ctx, points, view, width, height); drawVectorGrid(ctx, view, width, height);
  const lod = vectorContinuousLod(view); const coarse = buildVectorLodCache(view, points, lod.coarse); const fine = buildVectorLodCache(view, points, lod.fine); const coarseCells = visibleVectorCells(coarse, view, width, height); const fineCells = visibleVectorCells(fine, view, width, height); view.grid = fine.cells;
  const candidates = new Map(); const collect = (cells, alpha, sizeMultiplier) => { for (const cell of cells) { const existing = candidates.get(cell.point.asset_id); const candidate = {cell, alpha, sizeMultiplier}; if (!existing || candidate.alpha >= existing.alpha) candidates.set(cell.point.asset_id, candidate); } };
  collect(coarseCells, 1 - lod.fineBlend, 1 - .1 * lod.fineBlend); if (lod.fineBlend > 0) collect(fineCells, lod.fineBlend, .78 + .22 * lod.fineBlend);
  view.hitTargets = [];
  for (const candidate of candidates.values()) {
    const cell = candidate.cell; const representative = cell.point; const center = screenPoint(view, representative.x, representative.y, width, height); if (center.x < -220 || center.x > width + 220 || center.y < -220 || center.y > height + 220) continue;
    const size = Math.round(visualizationThumbnailSize(view, "vector") * candidate.sizeMultiplier); const thumbHeight = Math.max(48, Math.round(size * .72));
    ctx.save(); ctx.globalAlpha = candidate.alpha; drawVisualizationThumbnail(ctx, representative, center.x - size / 2, center.y - thumbHeight / 2, size, thumbHeight);
    drawVisualizationSelection(ctx, center.x - size / 2, center.y - thumbHeight / 2, size, thumbHeight, view.selectedId === representative.asset_id);
    const badgeX = center.x + size / 2 - 3; const badgeY = center.y - thumbHeight / 2 + 3; drawVisualizationBadge(ctx, badgeX, badgeY, cell.count); ctx.restore();
    view.hitTargets.push({x: center.x, y: center.y, badgeX, badgeY, width: size, height: thumbHeight, count: cell.count, hitRadius: Math.max(20, size / 2 + 8), point: representative});
  }
}

function boundsOf(values, padding) { let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity; for (const value of values) { minX = Math.min(minX, value.x); maxX = Math.max(maxX, value.x); minY = Math.min(minY, value.y); maxY = Math.max(maxY, value.y); } const width = Math.max(padding, maxX - minX); const height = Math.max(padding, maxY - minY); return {minX: minX - width * .08 - padding, maxX: maxX + width * .08 + padding, minY: minY - height * .08 - padding, maxY: maxY + height * .08 + padding}; }
function lowerBound(points, value) { let low = 0; let high = points.length; while (low < high) { const middle = (low + high) >> 1; if (points[middle].time < value) low = middle + 1; else high = middle; } return low; }
function upperBound(points, value) { let low = 0; let high = points.length; while (low < high) { const middle = (low + high) >> 1; if (points[middle].time <= value) low = middle + 1; else high = middle; } return low; }
function drawCanvasMessage(ctx, width, height, message) { ctx.fillStyle = "#9faab5"; ctx.font = "14px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(message, width / 2, height / 2); }

function hitVisualization(mode, event) {
  const canvas = visualizationCanvas(mode); const view = visualizationView(mode); const rect = canvas.getBoundingClientRect(); const x = event.clientX - rect.left; const y = event.clientY - rect.top; let nearest = null; let distance = Infinity;
  for (const target of view.hitTargets) { const current = Math.hypot(target.x - x, target.y - y); if (current <= (target.hitRadius || 24) && current < distance) { distance = current; nearest = target; } }
  if (!nearest) return;
  if (mode === "timeline" && nearest.bucket && nearest.count > 1 && Math.hypot(nearest.badgeX - x, nearest.badgeY - y) <= 15) { focusTimelineRange(nearest.bucket.start, nearest.bucket.end); return; }
  if (mode === "timeline" && nearest.bucket && (Math.abs(x - nearest.x) > nearest.thumbnailWidth / 2 || Math.abs(y - nearest.y) > nearest.thumbnailHeight / 2)) { focusTimelineRange(nearest.bucket.start, nearest.bucket.end); return; }
  if (mode === "geo" && nearest.points?.length > 1) { const onBadge = Math.hypot(nearest.badgeX - x, nearest.badgeY - y) <= 15; if (onBadge && nearest.sameLocation) { showGeoLocalStrip(nearest.points); return; } if (onBadge) { zoomVisualization("geo", 2.5, {x: nearest.x, y: nearest.y}); return; } openVisualizationAsset(representativeVisualizationPoint(nearest.points), view); return; }
  if (mode === "vector" && nearest.count > 1 && Math.hypot(nearest.badgeX - x, nearest.badgeY - y) <= 15) { zoomVisualization("vector", 2.5, {x: nearest.x, y: nearest.y}); return; }
  openVisualizationAsset(nearest.point || nearest.points?.[0], view);
}

function focusTimelineRange(start, end) {
  const view = visualizationView("timeline"); const canvas = visualizationCanvas("timeline"); const width = Math.max(1, canvas.getBoundingClientRect().width || 800); const range = Math.max(.001, end - start); commitVisualizationCamera("timeline"); const clamped = clampTimelineValues(view, (start + end) / 2, Math.max(.001, range * 1.25), width); view.targetCenterTime = clamped.centerTime; view.targetVisibleSpan = clamped.visibleSpan; animateVisualizationCamera("timeline");
}

function showGeoLocalStrip(points) {
  const strip = $("geo-local-strip"); if (!strip) return;
  strip.classList.remove("hidden"); strip.innerHTML = `<div class="visualization-local-strip-header"><strong>${points.length.toLocaleString()} assets at this position</strong><button type="button" class="icon" data-geo-strip-close aria-label="Close asset list">×</button></div><div class="visualization-local-strip-items">${points.slice(0, 24).map(point => `<button type="button" class="visualization-local-strip-item" data-geo-strip-asset="${escapeHtml(point.asset_id)}"><span>${escapeHtml(point.asset_id.slice(0, 12))}</span></button>`).join("")}</div>${points.length > 24 ? `<div class="muted">Showing 24 of ${points.length.toLocaleString()}</div>` : ""}`;
  strip.querySelector("[data-geo-strip-close]").onclick = () => strip.classList.add("hidden"); strip.querySelectorAll("[data-geo-strip-asset]").forEach(button => button.onclick = () => openVisualizationAsset(points.find(point => point.asset_id === button.dataset.geoStripAsset), visualizationView("geo")));
}

async function openVisualizationAsset(point, view) {
  if (!point?.asset_id) return;
  view.selectedId = point.asset_id; renderVisualization(state.viewMode);
  const request = ++visualizationSelectionRequest; const panel = $("visualization-selection-panel"); if (!panel) return;
  panel.classList.remove("hidden"); panel.innerHTML = `<div class="muted">Loading asset preview…</div>`;
  try {
    const asset = await api(`/api/assets/${encodeURIComponent(point.asset_id)}`);
    if (request !== visualizationSelectionRequest || view.selectedId !== point.asset_id) return;
    const item = assetToViewerItem(asset); const first = asset.physical_files?.[0] || {}; const thumbnail = first.thumbnail_url || item.thumbnail_url; const capture = formatCapture(asset.capture_time); const quality = first.quality_score == null ? "" : `Quality ${Number(first.quality_score).toFixed(2)}`;
    panel.innerHTML = `<div class="visualization-selection-content">${thumbnail ? `<img class="visualization-selection-thumb" src="${escapeHtml(thumbnail)}" alt="" onerror="this.remove()">` : ""}<div class="visualization-selection-meta"><div class="visualization-selection-name" title="${escapeHtml(first.filename || point.asset_id)}">${escapeHtml(first.filename || point.asset_id)}</div>${capture ? `<div class="muted">${escapeHtml(capture)}</div>` : ""}${quality ? `<div class="muted">${quality} · ${escapeHtml(asset.media_type || point.media_type || "")}</div>` : `<div class="muted">${escapeHtml(asset.media_type || point.media_type || "")}</div>`}</div></div><div class="visualization-selection-actions"><button type="button" class="secondary" data-visualization-open>Open</button><button type="button" class="secondary" data-visualization-details>Details</button></div>`;
    panel.querySelector("[data-visualization-open]").onclick = () => showViewer(0, [item], {mode: "visualization", total: 1});
    panel.querySelector("[data-visualization-details]").onclick = () => showDetails(asset.asset_id, {mode: "visualization", items: [item], index: 0, total: 1});
  } catch (error) { if (request === visualizationSelectionRequest) panel.innerHTML = `<div class="muted">Asset preview unavailable.</div>`; }
}

function bindVisualizationEvents(mode) {
  const canvas = visualizationCanvas(mode); let dragging = false; let startX = 0; let startY = 0; let moved = false; let frame = 0;
  canvas.addEventListener("wheel", event => { event.preventDefault(); const rect = canvas.getBoundingClientRect(); zoomVisualization(mode, event.deltaY < 0 ? 1.12 : 1 / 1.12, {x: event.clientX - rect.left, y: event.clientY - rect.top}); }, {passive: false});
  canvas.addEventListener("pointerdown", event => { if (event.button !== 0) return; dragging = true; moved = false; startX = event.clientX; startY = event.clientY; canvas.classList.add("dragging"); canvas.setPointerCapture(event.pointerId); });
  canvas.addEventListener("pointermove", event => { if (!dragging) return; const dx = event.clientX - startX; const dy = event.clientY - startY; if (Math.abs(dx) + Math.abs(dy) > 4) moved = true; if (moved) { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => panVisualization(mode, dx, dy)); } startX = event.clientX; startY = event.clientY; });
  ["pointerup", "pointercancel"].forEach(name => canvas.addEventListener(name, event => { if (!dragging) return; dragging = false; canvas.classList.remove("dragging"); if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId); if (moved) canvas.dataset.suppressClick = "1"; }));
  canvas.addEventListener("click", event => { if (canvas.dataset.suppressClick) { delete canvas.dataset.suppressClick; return; } hitVisualization(mode, event); });
  $(`${mode}-fit`).onclick = () => fitVisualization(mode); $(`${mode}-zoom-in`).onclick = () => zoomVisualization(mode, 1.35); $(`${mode}-zoom-out`).onclick = () => zoomVisualization(mode, 1 / 1.35);
}

VISUALIZATION_MODES.forEach(bindVisualizationEvents);
window.addEventListener("resize", () => { if (VISUALIZATION_MODES.includes(state.viewMode)) renderVisualization(state.viewMode); });
