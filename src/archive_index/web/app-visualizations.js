const VISUALIZATION_MODES = ["geo", "timeline", "vector"];
const TIMELINE_INTERVALS = [0.001, 0.01, 0.1, 1, 5, 10, 30, 60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 604800, 2592000, 7776000, 31536000];
const TIMELINE_KERNEL = [1, 4, 6, 4, 1];
let worldFeaturesPromise;
const visualizationThumbnailCache = new Map();

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
  if (mode === "timeline") params.set("time_mode", visualizationView(mode).timeMode || "capture");
  const key = String(params);
  const view = visualizationView(mode);
  if (view.key === key && view.data) { renderVisualization(mode); return; }
  try {
    const data = await api(`/api/visualizations/${mode}?${params}`, {signal: controller.signal});
    if (controller.signal.aborted || requestId !== state.visualizationRequest) return;
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
      view.timelineCache = null;
      view.densityCache = null;
    }
    state.searchProvider = data.search?.provider || state.searchProvider;
    showSearchStatus(data.search || {state: "complete"}, {...data, media_shown: data.filtered_asset_count, workspace_total: data.workspace_total});
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

function timelineBounds(view) {
  const points = view.data?.points || [];
  if (!points.length) return {min: 0, max: 0};
  const min = Number(points[0].time);
  const max = Number(points[points.length - 1].time);
  const padding = min === max ? 43200 : Math.max(1, (max - min) * .06);
  return {min: min - padding, max: max + padding};
}

function clampTimelineCamera(view) {
  const bounds = timelineBounds(view);
  if (bounds.max <= bounds.min) return;
  const span = Math.min(Math.max(.001, view.visibleSpan), Math.max(.001, bounds.max - bounds.min));
  view.visibleSpan = span;
  const half = span / 2;
  view.centerTime = Math.max(bounds.min + half, Math.min(bounds.max - half, view.centerTime));
}

function zoomVisualization(mode, factor, point) {
  const canvas = visualizationCanvas(mode);
  const view = visualizationView(mode);
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, rect.width || 800);
  const height = Math.max(1, rect.height || 380);
  const cursor = point || {x: width / 2, y: height / 2};
  if (mode === "timeline") {
    const before = timelineTimeAt(view, cursor.x, width);
    view.visibleSpan = Math.max(.001, Math.min(Math.max(.001, timelineBounds(view).max - timelineBounds(view).min), view.visibleSpan / factor));
    view.centerTime = before - (cursor.x - width / 2) / width * view.visibleSpan;
    clampTimelineCamera(view);
  } else {
    const before = worldPoint(view, cursor.x, cursor.y, width, height);
    view.scale = Math.max(.001, Math.min(visualizationMaximum(mode, view), view.scale * factor));
    view.centerX = before.x - (cursor.x - width / 2) / view.scale;
    view.centerY = before.y - (cursor.y - height / 2) / view.scale;
  }
  renderVisualization(mode);
}

function fitVisualization(mode) { visualizationView(mode).needsFit = true; renderVisualization(mode); }

function panVisualization(mode, dx, dy) {
  const view = visualizationView(mode);
  const canvas = visualizationCanvas(mode);
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(1, rect.width || 800);
  if (mode === "timeline") {
    view.centerTime -= dx / width * view.visibleSpan;
    clampTimelineCamera(view);
  } else {
    view.centerX -= dx / Math.max(.001, view.scale);
    view.centerY -= dy / Math.max(.001, view.scale);
  }
  renderVisualization(mode);
}

function saveTimelineTransform(view) { return {centerTime: view.centerTime, visibleSpan: view.visibleSpan, needsFit: view.needsFit}; }

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

function drawGeo(canvas, view, data) {
  const surface = canvasSurface(canvas); const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  const points = (data.points || []).filter(point => Number.isFinite(Number(point.latitude)) && Number.isFinite(Number(point.longitude)));
  if (!data.available || !points.length) { view.hitTargets = []; drawCanvasMessage(ctx, width, height, data.empty_reason || "No geotagged assets."); return; }
  if (view.needsFit) fitGeo(view, points, width, height);
  if (!worldFeaturesPromise) worldFeaturesPromise = fetch("/world.json").then(response => response.ok ? response.json() : {features: []}).catch(() => ({features: []}));
  if (!view.world) worldFeaturesPromise.then(world => { if (state.viewMode === "geo" && view.data === data && !view.world) { view.world = world; renderVisualization("geo"); } });
  for (const feature of view.world?.features || []) drawGeoFeature(ctx, feature, view, width, height);
  const cellWorld = view.baseCellWorld / 2 ** geoLod(view); const clusters = new Map();
  for (const point of points) {
    const world = geoWorld(point.longitude, point.latitude); const screen = screenPoint(view, world.x, world.y, width, height);
    const key = `${Math.floor(world.x / cellWorld)}:${Math.floor(world.y / cellWorld)}`;
    const cluster = clusters.get(key) || {points: [], screenX: 0, screenY: 0};
    cluster.points.push({...point, worldX: world.x, worldY: world.y}); cluster.screenX += screen.x; cluster.screenY += screen.y; clusters.set(key, cluster);
  }
  view.hitTargets = [];
  for (const cluster of clusters.values()) {
    const x = cluster.screenX / cluster.points.length; const y = cluster.screenY / cluster.points.length;
    if (x < -220 || x > width + 220 || y < -220 || y > height + 220) continue;
    const size = thumbnailTier(cellWorld * view.scale, cluster.points.length); const thumbHeight = Math.max(40, Math.round(size * .72));
    const representative = representativeVisualizationPoint(cluster.points);
    drawVisualizationThumbnail(ctx, representative, x - size / 2, y - thumbHeight / 2, size, thumbHeight);
    const badgeX = x + size / 2 - 3; const badgeY = y - thumbHeight / 2 + 3;
    if (cluster.points.length > 1) {
      ctx.beginPath(); ctx.arc(badgeX, badgeY, 11, 0, Math.PI * 2); ctx.fillStyle = "#17222b"; ctx.fill();
      ctx.fillStyle = "#edf0f3"; ctx.font = "600 10px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(String(cluster.points.length), badgeX, badgeY);
    }
    view.hitTargets.push({x, y, badgeX, badgeY, hitRadius: Math.max(18, size / 2 + 8), points: cluster.points, sameLocation: sameGeoLocation(cluster.points)});
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
  if (!values.length) { view.centerX = .5; view.centerY = .5; view.scale = Math.min(width, height) * .86; view.baseScale = view.scale; view.baseCellWorld = 1 / 8; view.needsFit = false; return; }
  const bounds = boundsOf(values, .04);
  view.centerX = (bounds.minX + bounds.maxX) / 2; view.centerY = (bounds.minY + bounds.maxY) / 2;
  view.scale = Math.max(1, Math.min(width / (bounds.maxX - bounds.minX), height / (bounds.maxY - bounds.minY))); view.baseScale = view.scale;
  view.baseCellWorld = Math.max((bounds.maxX - bounds.minX), (bounds.maxY - bounds.minY)) / 8; view.needsFit = false;
}

function timelineBucketInterval(span, width) {
  const target = Math.max(.001, Number(span)) * 150 / Math.max(1, Number(width));
  return TIMELINE_INTERVALS.find(interval => interval >= target) || TIMELINE_INTERVALS.at(-1);
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

function buildTimelineCache(view, points, interval) {
  const key = `${view.key}|${view.timeMode}|${interval}`;
  if (view.timelineCache?.key === key) return view.timelineCache;
  const buckets = new Map();
  for (const point of points) {
    const start = timelineBucketStart(point.time, interval); const bucket = buckets.get(start) || {start, end: timelineBucketEnd(start, interval), count: 0, point: null};
    bucket.count += 1;
    if (!bucket.point || representativeVisualizationPoint([bucket.point, point]) === point) bucket.point = point;
    buckets.set(start, bucket);
  }
  const ordered = [...buckets.values()].sort((left, right) => left.start - right.start);
  const amplitudes = ordered.map((bucket, index) => TIMELINE_KERNEL.reduce((sum, weight, offset) => sum + (ordered[index + offset - 2]?.count || 0) * weight, 0) / 16);
  const maximum = Math.max(1, ...amplitudes);
  view.timelineCache = {key, buckets: ordered, amplitudes, maximum};
  return view.timelineCache;
}

function drawTimelineDensity(ctx, view, cache, start, end, axisY, width, height) {
  const buckets = cache.buckets;
  if (!buckets.length) return;
  ctx.beginPath(); ctx.moveTo(0, axisY);
  let drew = false;
  for (let index = 0; index < buckets.length; index += 1) {
    const bucket = buckets[index]; if (bucket.end < start || bucket.start > end) continue;
    const x1 = timelineX(view, Math.max(start, bucket.start), width); const x2 = timelineX(view, Math.min(end, bucket.end), width);
    const y = axisY - 16 - cache.amplitudes[index] / cache.maximum * Math.min(145, height * .42);
    if (!drew) { ctx.lineTo(x1, y); drew = true; } else ctx.lineTo(x1, y);
    ctx.lineTo(x2, y);
  }
  if (!drew) return;
  ctx.lineTo(width, axisY); ctx.closePath(); ctx.fillStyle = "rgba(55, 111, 139, .28)"; ctx.fill();
}

function drawTimeline(canvas, view, data) {
  const surface = canvasSurface(canvas); const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  const points = (data.points || []).filter(point => Number.isFinite(Number(point.time))).sort((left, right) => left.time - right.time || String(left.asset_id).localeCompare(String(right.asset_id)));
  if (!data.available || !points.length) { view.hitTargets = []; drawCanvasMessage(ctx, width, height, data.empty_reason || "No timed assets."); return; }
  if (view.needsFit) fitTimeline(view, points);
  clampTimelineCamera(view);
  const start = view.centerTime - view.visibleSpan / 2; const end = view.centerTime + view.visibleSpan / 2;
  const interval = timelineBucketInterval(view.visibleSpan, width); const cache = buildTimelineCache(view, points, interval); const axisY = height * .78;
  drawTimelineDensity(ctx, view, cache, start, end, axisY, width, height);
  view.hitTargets = [];
  for (const bucket of cache.buckets) {
    if (bucket.end < start || bucket.start > end) continue;
    const center = timelineX(view, (bucket.start + bucket.end) / 2, width); const pixels = Math.abs(timelineX(view, bucket.end, width) - timelineX(view, bucket.start, width));
    const size = thumbnailTier(pixels, bucket.count); const thumbHeight = Math.max(48, Math.round(size * .72)); const y = height * .36;
    drawVisualizationThumbnail(ctx, bucket.point, center - size / 2, y - thumbHeight / 2, size, thumbHeight);
    view.hitTargets.push({x: center, y, hitRadius: Math.max(24, size), thumbnailWidth: size, thumbnailHeight: thumbHeight, point: bucket.point, bucket: {start: bucket.start, end: bucket.end}});
  }
  drawTimelineAxis(ctx, view, start, end, axisY, width, height);
}

function fitTimeline(view, points) {
  const min = Number(points[0].time); const max = Number(points[points.length - 1].time); const range = Math.max(.001, max - min); const padding = min === max ? 43200 : Math.max(1, range * .06);
  view.centerTime = (min + max) / 2; view.visibleSpan = min === max ? 86400 : range + padding * 2; view.needsFit = false; view.modes[view.timeMode] = saveTimelineTransform(view);
}

function timelineTickStep(span, width) {
  const target = Math.max(.001, span) * 90 / Math.max(1, width);
  return TIMELINE_INTERVALS.find(interval => interval >= target) || TIMELINE_INTERVALS.at(-1);
}

function formatTimelineTick(value, step) {
  const date = new Date(Number(value) * 1000); const pad = number => String(number).padStart(2, "0");
  if (step < 1) return `${pad(date.getUTCSeconds())}.${String(date.getUTCMilliseconds()).padStart(3, "0")}`;
  if (step < 60) return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
  if (step < 86400) return `${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  if (step < 2592000) return `${pad(date.getUTCDate())}/${pad(date.getUTCMonth() + 1)}`;
  if (step < 31536000) return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}`;
  return String(date.getUTCFullYear());
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
  for (const time of timelineTicks(start, end, step)) { const x = timelineX(view, time, width); if (x < -30 || x > width + 30) continue; ctx.strokeStyle = "#61717e"; ctx.beginPath(); ctx.moveTo(x, axisY); ctx.lineTo(x, axisY + 7); ctx.stroke(); ctx.fillStyle = "#9faab5"; ctx.font = "11px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillText(formatTimelineTick(time, step), x, axisY + 10); }
}

function representativeVisualizationPoint(points) {
  return [...points].sort((left, right) => { const leftQuality = Number.isFinite(Number(left.quality_score)) ? Number(left.quality_score) : -Infinity; const rightQuality = Number.isFinite(Number(right.quality_score)) ? Number(right.quality_score) : -Infinity; return rightQuality - leftQuality || String(left.asset_id).localeCompare(String(right.asset_id)); })[0];
}

function thumbnailTier(pixelSpacing, count) {
  const available = Number(pixelSpacing) || 0;
  if (available >= 220 && count <= 1) return 190;
  if (available >= 160) return 170;
  if (available >= 110) return 150;
  if (available >= 72) return 112;
  if (available >= 44) return 88;
  return 72;
}

function visualizationThumbnail(assetId) {
  if (visualizationThumbnailCache.has(assetId)) return visualizationThumbnailCache.get(assetId);
  const entry = {image: null, loading: true}; const image = new Image();
  image.onload = () => { entry.image = image; entry.loading = false; renderVisualization(state.viewMode); }; image.onerror = () => { entry.loading = false; entry.failed = true; };
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

function vectorGridStep(view, width) {
  const targetWorld = 90 / Math.max(.001, view.scale); const power = 10 ** Math.floor(Math.log10(targetWorld));
  return [1, 2, 5, 10].find(value => value * power >= targetWorld) * power;
}

function buildVectorDensity(view, points) {
  const key = `${view.key}|${view.data?.browser_revision || ""}`;
  if (view.densityCache?.key === key) return view.densityCache;
  const size = 512; const raster = document.createElement("canvas"); raster.width = size; raster.height = size; const counts = new Float32Array(size * size); const bounds = view.worldBounds; let maximum = 0;
  for (const point of points) {
    const x = Math.max(0, Math.min(size - 1, Math.floor((point.x - bounds.minX) / (bounds.maxX - bounds.minX) * size)));
    const y = Math.max(0, Math.min(size - 1, Math.floor((bounds.maxY - point.y) / (bounds.maxY - bounds.minY) * size)));
    for (let row = Math.max(0, y - 3); row <= Math.min(size - 1, y + 3); row += 1) for (let column = Math.max(0, x - 3); column <= Math.min(size - 1, x + 3); column += 1) { const distance = Math.hypot(column - x, row - y); const weight = distance === 0 ? 1 : Math.exp(-distance * distance / 5); counts[row * size + column] += weight; maximum = Math.max(maximum, counts[row * size + column]); }
  }
  const image = raster.getContext("2d").createImageData(size, size);
  for (let index = 0; index < counts.length; index += 1) { const intensity = Math.sqrt(counts[index] / Math.max(1, maximum)); const offset = index * 4; image.data[offset] = Math.round(8 + intensity * 62); image.data[offset + 1] = Math.round(20 + intensity * 155); image.data[offset + 2] = Math.round(46 + intensity * 150); image.data[offset + 3] = Math.round(intensity * 190); }
  raster.getContext("2d").putImageData(image, 0, 0); view.densityCache = {key, raster}; return view.densityCache;
}

function drawVectorDensity(ctx, points, view, width, height) {
  const density = buildVectorDensity(view, points); const topLeft = screenPoint(view, view.worldBounds.minX, view.worldBounds.maxY, width, height); const bottomRight = screenPoint(view, view.worldBounds.maxX, view.worldBounds.minY, width, height);
  ctx.save(); ctx.globalAlpha = .78; ctx.imageSmoothingEnabled = true; ctx.drawImage(density.raster, topLeft.x, topLeft.y, bottomRight.x - topLeft.x, bottomRight.y - topLeft.y); ctx.restore();
}

function fitVector(view, points, width, height) {
  const values = points.map(point => ({x: Number(point.x), y: Number(point.y)})).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y));
  if (!values.length) return;
  const bounds = boundsOf(values, Math.max(.1, Math.max(...values.map(point => Math.max(Math.abs(point.x), Math.abs(point.y)))) * .12));
  view.worldBounds = bounds; view.centerX = (bounds.minX + bounds.maxX) / 2; view.centerY = (bounds.minY + bounds.maxY) / 2;
  view.scale = Math.max(1, Math.min(width / (bounds.maxX - bounds.minX), height / (bounds.maxY - bounds.minY))); view.baseScale = view.scale;
  view.baseCellWorld = Math.max(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY) / 8; view.gridOriginX = Math.floor(bounds.minX / view.baseCellWorld) * view.baseCellWorld; view.gridOriginY = Math.floor(bounds.minY / view.baseCellWorld) * view.baseCellWorld; view.needsFit = false;
}

function drawVectorGrid(ctx, view, width, height) {
  const step = vectorGridStep(view, width); const left = worldPoint(view, 0, 0, width, height).x; const right = worldPoint(view, width, 0, width, height).x; const top = worldPoint(view, 0, 0, width, height).y; const bottom = worldPoint(view, 0, height, width, height).y;
  ctx.font = "10px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "top";
  for (let value = Math.ceil(left / step) * step; value <= right; value += step) { const x = screenPoint(view, value, 0, width, height).x; ctx.strokeStyle = Math.abs(value) < step / 100 ? "rgba(190, 213, 225, .55)" : "rgba(116, 150, 166, .2)"; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke(); if (Math.abs(value) < step / 100) ctx.fillStyle = "#aebdc7", ctx.fillText("0,0", x + 4, screenPoint(view, 0, 0, width, height).y + 4); }
  for (let value = Math.ceil(top / step) * step; value <= bottom; value += step) { const y = screenPoint(view, 0, value, width, height).y; ctx.strokeStyle = Math.abs(value) < step / 100 ? "rgba(190, 213, 225, .55)" : "rgba(116, 150, 166, .2)"; ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke(); }
}

function drawVector(canvas, view, data) {
  const surface = canvasSurface(canvas); const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  const points = (data.points || []).filter(point => Number.isFinite(Number(point.x)) && Number.isFinite(Number(point.y))).map(point => ({...point, x: Number(point.x), y: Number(point.y)}));
  if (!data.available || !points.length) { view.hitTargets = []; view.grid = new Map(); drawCanvasMessage(ctx, width, height, data.empty_reason || "No projected semantic vectors."); return; }
  if (view.needsFit || !view.worldBounds) { view.densityCache = null; fitVector(view, points, width, height); }
  drawVectorDensity(ctx, points, view, width, height); drawVectorGrid(ctx, view, width, height);
  const cellWorld = view.baseCellWorld / 2 ** vectorLod(view); const cells = new Map(); view.grid = cells;
  for (const point of points) { const key = vectorCellKey(point.x, point.y, cellWorld, view.gridOriginX, view.gridOriginY); const cell = cells.get(key) || {points: [], x: 0, y: 0}; cell.points.push(point); cell.x += point.x; cell.y += point.y; cells.set(key, cell); }
  view.hitTargets = []; const cellPixels = Math.max(1, cellWorld * view.scale);
  for (const cell of cells.values()) {
    const center = screenPoint(view, cell.x / cell.points.length, cell.y / cell.points.length, width, height); if (center.x < -220 || center.x > width + 220 || center.y < -220 || center.y > height + 220) continue;
    const representative = representativeVisualizationPoint(cell.points); const size = thumbnailTier(cellPixels, cell.points.length); const thumbHeight = Math.max(48, Math.round(size * .72));
    drawVisualizationThumbnail(ctx, representative, center.x - size / 2, center.y - thumbHeight / 2, size, thumbHeight);
    view.hitTargets.push({x: center.x, y: center.y, hitRadius: Math.max(20, size / 2 + 8), point: representative});
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
  if (mode === "timeline" && nearest.bucket && (Math.abs(x - nearest.x) > nearest.thumbnailWidth / 2 || Math.abs(y - nearest.y) > nearest.thumbnailHeight / 2)) { focusTimelineRange(nearest.bucket.start, nearest.bucket.end); return; }
  if (mode === "geo" && nearest.points?.length > 1) { const onBadge = Math.hypot(nearest.badgeX - x, nearest.badgeY - y) <= 15; if (onBadge && nearest.sameLocation) { showGeoLocalStrip(nearest.points); return; } if (onBadge) { zoomVisualization("geo", 2.5, {x: nearest.x, y: nearest.y}); return; } openVisualizationAsset(representativeVisualizationPoint(nearest.points), view); return; }
  openVisualizationAsset(nearest.point || nearest.points?.[0], view);
}

function focusTimelineRange(start, end) {
  const view = visualizationView("timeline"); const range = Math.max(.001, end - start); view.centerTime = (start + end) / 2; view.visibleSpan = Math.max(.001, range * 1.25); clampTimelineCamera(view); renderVisualization("timeline");
}

function showGeoLocalStrip(points) {
  const strip = $("geo-local-strip"); if (!strip) return;
  strip.classList.remove("hidden"); strip.innerHTML = `<div class="visualization-local-strip-header"><strong>${points.length.toLocaleString()} assets at this position</strong><button type="button" class="icon" data-geo-strip-close aria-label="Close asset list">×</button></div><div class="visualization-local-strip-items">${points.slice(0, 24).map(point => `<button type="button" class="visualization-local-strip-item" data-geo-strip-asset="${escapeHtml(point.asset_id)}"><span>${escapeHtml(point.asset_id.slice(0, 12))}</span></button>`).join("")}</div>${points.length > 24 ? `<div class="muted">Showing 24 of ${points.length.toLocaleString()}</div>` : ""}`;
  strip.querySelector("[data-geo-strip-close]").onclick = () => strip.classList.add("hidden"); strip.querySelectorAll("[data-geo-strip-asset]").forEach(button => button.onclick = () => openVisualizationAsset(points.find(point => point.asset_id === button.dataset.geoStripAsset), visualizationView("geo")));
}

async function openVisualizationAsset(point, view) {
  if (!point?.asset_id) return;
  view.selectedId = point.asset_id; renderVisualization(state.viewMode);
  try { const asset = await api(`/api/assets/${encodeURIComponent(point.asset_id)}`); const item = assetToViewerItem(asset); showViewer(0, [item], {mode: "visualization", total: 1}); }
  catch (error) { showToast(`Asset preview unavailable: ${error.message}`); }
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
