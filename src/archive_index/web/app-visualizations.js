const VISUALIZATION_MODES = ["geo", "timeline", "vector"];
const TIMELINE_INTERVALS = [0.01, 0.1, 1, 5, 10, 30, 60, 300, 900, 1800, 3600, 10800, 21600, 43200, 86400, 604800, 2592000, 7776000, 31536000];
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
      state.visualizationSelectedId = null;
      clearVisualizationSelection();
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
  canvas.dataset.viewScale = String(view.scale);
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
  const height = Math.max(1, Math.round(rect.height || 560));
  const ratio = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
  if (canvas.width !== width * ratio || canvas.height !== height * ratio) { canvas.width = width * ratio; canvas.height = height * ratio; }
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

function visualizationMaximum(mode, view) {
  const base = Math.max(1, Number(view.baseScale) || 1);
  if (mode === "geo") return base * 2 ** 22;
  if (mode === "vector") return base * 2 ** 18;
  return 2 ** 30;
}

function zoomVisualization(mode, factor, point) {
  const canvas = visualizationCanvas(mode);
  const view = visualizationView(mode);
  const {width, height} = canvasSurface(canvas);
  const cursor = point || {x: width / 2, y: height / 2};
  const before = worldPoint(view, cursor.x, cursor.y, width, height);
  view.scale = Math.max(.05, Math.min(visualizationMaximum(mode, view), view.scale * factor));
  const after = screenPoint(view, before.x, before.y, width, height);
  view.panX += cursor.x - after.x;
  if (mode !== "timeline") view.panY += cursor.y - after.y;
  clampVisualizationPan(view, mode, width, height);
  renderVisualization(mode);
}

function fitVisualization(mode) { visualizationView(mode).needsFit = true; renderVisualization(mode); }

function panVisualization(mode, dx, dy) {
  const view = visualizationView(mode);
  view.panX += dx;
  if (mode !== "timeline") view.panY += dy;
  const canvas = visualizationCanvas(mode); const rect = canvas.getBoundingClientRect();
  clampVisualizationPan(view, mode, Math.max(1, rect.width), Math.max(1, rect.height));
  renderVisualization(mode);
}

function clampVisualizationPan(view, mode, width, height) {
  const limit = mode === "timeline" ? width * 2 : Math.max(width, height) * 4;
  view.panX = Math.max(-limit, Math.min(limit, view.panX));
  if (mode !== "timeline") view.panY = Math.max(-limit, Math.min(limit, view.panY));
}

function saveTimelineTransform(view) {
  return {origin: view.origin, span: view.span, centerX: view.centerX, centerY: view.centerY, scale: view.scale, baseScale: view.baseScale, panX: view.panX, panY: view.panY, needsFit: view.needsFit};
}

function switchTimelineMode(mode) {
  if (!["capture", "file_created"].includes(mode)) return;
  const view = visualizationView("timeline");
  view.modes[view.timeMode] = saveTimelineTransform(view);
  view.timeMode = mode;
  Object.assign(view, view.modes[mode]);
  view.preserveTransform = true;
  view.key = null; view.data = null;
  state.visualizationSelectedId = null;
  clearVisualizationSelection();
  loadVisualization("timeline");
}

function geoWorld(longitude, latitude) {
  const safeLatitude = Math.max(-85, Math.min(85, Number(latitude)));
  const radians = safeLatitude * Math.PI / 180;
  const mercator = Math.log(Math.tan(Math.PI / 4 + radians / 2));
  return {x: (Number(longitude) + 180) / 360, y: .5 - mercator / (2 * Math.PI)};
}

function drawGeo(canvas, view, data) {
  const surface = canvasSurface(canvas); const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  if (!data.available || !data.points?.length) { view.hitTargets = []; drawCanvasMessage(ctx, width, height, data.empty_reason || "No geotagged assets."); return; }
  const points = data.points.filter(point => Number.isFinite(Number(point.latitude)) && Number.isFinite(Number(point.longitude)));
  if (view.needsFit) fitGeo(view, points, width, height);
  if (!view.world) {
    if (!worldFeaturesPromise) worldFeaturesPromise = fetch("/world.json").then(response => response.ok ? response.json() : {features: []}).catch(() => ({features: []}));
    worldFeaturesPromise.then(world => { if (state.viewMode === "geo" && view.data === data && !view.world) { view.world = world; renderVisualization("geo"); } });
  }
  for (const feature of view.world?.features || []) drawGeoFeature(ctx, feature, view, width, height);
  const lod = geoLod(view); const cellWorld = 1 / (64 * 2 ** lod); const clusters = new Map();
  for (const point of points) {
    const world = geoWorld(point.longitude, point.latitude); const screen = screenPoint(view, world.x, world.y, width, height);
    const key = `${Math.floor(world.x / cellWorld)}:${Math.floor(world.y / cellWorld)}`;
    const cluster = clusters.get(key) || {points: [], screenX: 0, screenY: 0};
    cluster.points.push({...point, worldX: world.x, worldY: world.y, screenX: screen.x, screenY: screen.y});
    cluster.screenX += screen.x; cluster.screenY += screen.y; clusters.set(key, cluster);
  }
  view.hitTargets = []; const cellPixels = Math.max(1, cellWorld * view.scale);
  for (const cluster of clusters.values()) {
    const x = cluster.screenX / cluster.points.length; const y = cluster.screenY / cluster.points.length;
    if (x < -140 || x > width + 140 || y < -140 || y > height + 140) continue;
    const size = thumbnailTier(cellPixels, cluster.points.length); const thumbHeight = Math.max(24, Math.round(size * .72));
    const representative = representativeVisualizationPoint(cluster.points);
    drawVisualizationThumbnail(ctx, representative, x - size / 2, y - thumbHeight / 2, size, thumbHeight);
    if (cluster.points.some(point => point.asset_id === state.visualizationSelectedId)) { ctx.strokeStyle = "#d9e0e6"; ctx.lineWidth = 2; ctx.strokeRect(x - size / 2 - 2, y - thumbHeight / 2 - 2, size + 4, thumbHeight + 4); }
    const badgeX = x + size / 2 - 2; const badgeY = y - thumbHeight / 2 + 2;
    if (cluster.points.length > 1) {
      ctx.beginPath(); ctx.arc(badgeX, badgeY, 10, 0, Math.PI * 2); ctx.fillStyle = "#17222b"; ctx.fill();
      ctx.fillStyle = "#edf0f3"; ctx.font = "600 10px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(String(cluster.points.length), badgeX, badgeY);
    }
    view.hitTargets.push({x, y, badgeX, badgeY, hitRadius: Math.max(14, size / 2 + 8), points: cluster.points, sameLocation: sameGeoLocation(cluster.points)});
  }
}

function geoLod(view) { return Math.max(0, Math.min(22, Math.floor(Math.log2(Math.max(1, view.scale / Math.max(1, view.baseScale || 1))) * 1.5))); }
function sameGeoLocation(points) {
  if (points.length < 2) return false;
  const latitude = Number(points[0].latitude); const longitude = Number(points[0].longitude);
  return points.every(point => Math.abs(Number(point.latitude) - latitude) < 1e-7 && Math.abs(Number(point.longitude) - longitude) < 1e-7);
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
  if (!values.length) { view.centerX = .5; view.centerY = .5; view.scale = Math.min(width, height) * .86; view.baseScale = view.scale; view.panX = view.panY = 0; view.needsFit = false; return; }
  const bounds = boundsOf(values, .08);
  view.centerX = (bounds.minX + bounds.maxX) / 2; view.centerY = (bounds.minY + bounds.maxY) / 2;
  view.scale = Math.max(1, Math.min(width / (bounds.maxX - bounds.minX), height / (bounds.maxY - bounds.minY))); view.baseScale = view.scale;
  view.panX = view.panY = 0; view.needsFit = false;
}

function drawTimeline(canvas, view, data) {
  const surface = canvasSurface(canvas); const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  if (!data.available || !data.points?.length) { view.hitTargets = []; drawCanvasMessage(ctx, width, height, data.empty_reason || "No timed assets."); return; }
  const points = data.points;
  if (view.needsFit) fitTimeline(view, points, width, height);
  const axisY = height * .78; const left = worldPoint(view, 0, .5, width, height).x; const right = worldPoint(view, width, .5, width, height).x;
  const visibleStart = view.origin + left * view.span; const visibleEnd = view.origin + right * view.span;
  const visible = points.slice(lowerBound(points, visibleStart), upperBound(points, visibleEnd));
  const interval = timelineBucketInterval(Math.max(.01, visibleEnd - visibleStart), width);
  const detailed = visible.length <= Math.max(80, Math.floor(width / 8)) && timelinePointSpacing(visible, view, width) >= 28;
  view.hitTargets = [];
  drawTimelineDensity(ctx, visible, visibleStart, visibleEnd, axisY, width, height);
  if (detailed) drawTimelinePoints(ctx, view, visible, axisY, width, height); else drawTimelineBuckets(ctx, view, visible, interval, axisY, width, height);
  drawTimelineAxis(ctx, view, visibleStart, visibleEnd, axisY, width, height);
}

function timelinePointSpacing(points, view, width) {
  if (points.length < 2) return width;
  let minimum = Infinity;
  for (let index = 1; index < points.length; index += 1) minimum = Math.min(minimum, (points[index].time - points[index - 1].time) * view.scale / view.span);
  return Number.isFinite(minimum) ? minimum : width;
}

function timelineBucketInterval(span, width) {
  const targetBuckets = Math.max(8, Math.floor(width / 72));
  return TIMELINE_INTERVALS.find(interval => span / interval <= targetBuckets) || TIMELINE_INTERVALS.at(-1);
}

function timelineX(view, time, width, height) { return screenPoint(view, (time - view.origin) / view.span, .5, width, height).x; }

function drawTimelineDensity(ctx, points, start, end, axisY, width, height) {
  if (!points.length || end <= start) return;
  const bins = Math.max(32, Math.min(180, Math.floor(width / 5))); const counts = new Float32Array(bins);
  for (const point of points) { const index = Math.max(0, Math.min(bins - 1, Math.floor((point.time - start) / (end - start) * bins))); counts[index] += 1; }
  const maximum = Math.max(1, ...counts); const curve = [];
  for (let index = 0; index < bins; index += 1) curve.push({x: index / (bins - 1) * width, y: axisY - 18 - counts[index] / maximum * Math.min(210, height * .46)});
  ctx.beginPath(); ctx.moveTo(0, axisY);
  for (let index = 0; index < curve.length; index += 1) { const current = curve[index]; if (index === 0) ctx.lineTo(current.x, current.y); else { const previous = curve[index - 1]; const midpoint = (previous.x + current.x) / 2; ctx.quadraticCurveTo(previous.x, previous.y, midpoint, (previous.y + current.y) / 2); ctx.quadraticCurveTo(current.x, current.y, current.x, current.y); } }
  ctx.lineTo(width, axisY); ctx.closePath(); ctx.fillStyle = "rgba(79, 133, 161, .28)"; ctx.fill();
}

function drawTimelineBuckets(ctx, view, points, interval, axisY, width, height) {
  const buckets = new Map();
  for (const point of points) { const start = Math.floor(point.time / interval) * interval; const bucket = buckets.get(start) || []; bucket.push(point); buckets.set(start, bucket); }
  const maximum = Math.max(1, ...[...buckets.values()].map(bucket => bucket.length));
  for (const [start, bucketPoints] of buckets) {
    const end = start + interval; const x1 = timelineX(view, start, width, height); const x2 = timelineX(view, end, width, height); const centerX = (x1 + x2) / 2;
    const heightPx = 18 + bucketPoints.length / maximum * Math.min(120, height * .25); const representative = representativeVisualizationPoint(bucketPoints); const size = thumbnailTier(Math.abs(x2 - x1), bucketPoints.length);
    if (x2 - x1 >= size + 12) drawVisualizationThumbnail(ctx, representative, centerX - size / 2, axisY - heightPx - Math.max(24, size * .72), size, Math.max(24, size * .72));
    ctx.strokeStyle = "rgba(105, 159, 184, .7)"; ctx.lineWidth = Math.max(1, Math.min(5, x2 - x1 - 4)); ctx.beginPath(); ctx.moveTo(centerX, axisY - 8); ctx.lineTo(centerX, axisY - heightPx); ctx.stroke();
    view.hitTargets.push({x: centerX, y: axisY - heightPx / 2, hitRadius: Math.max(14, Math.abs(x2 - x1) / 2), bucket: {start, end}});
  }
}

function drawTimelinePoints(ctx, view, points, axisY, width, height) {
  const size = thumbnailTier(timelinePointSpacing(points, view, width), 1); const thumbnailHeight = Math.max(24, Math.round(size * .72)); const y = axisY - 50;
  for (const point of points) {
    const x = timelineX(view, point.time, width, height); drawVisualizationThumbnail(ctx, point, x - size / 2, y - thumbnailHeight / 2, size, thumbnailHeight);
    if (point.asset_id === state.visualizationSelectedId) { ctx.strokeStyle = "#d9e0e6"; ctx.lineWidth = 2; ctx.strokeRect(x - size / 2 - 2, y - thumbnailHeight / 2 - 2, size + 4, thumbnailHeight + 4); }
    view.hitTargets.push({x, y, hitRadius: Math.max(14, size / 2 + 4), point});
  }
}

function fitTimeline(view, points, width, height) {
  const min = points[0].time; const max = points[points.length - 1].time; const range = Math.max(1, max - min); const padding = min === max ? 43200 : Math.max(1, range * .06);
  view.origin = min - padding; view.span = min === max ? 86400 : range + padding * 2; view.centerX = .5; view.centerY = .5; view.scale = Math.max(1, width / 1.08); view.baseScale = view.scale; view.panX = view.panY = 0; view.needsFit = false; view.modes[view.timeMode] = saveTimelineTransform(view);
}

function drawTimelineAxis(ctx, view, start, end, axisY, width, height) {
  const span = Math.max(.01, end - start); const step = timelineTickStep(span);
  ctx.strokeStyle = "#465663"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0, axisY); ctx.lineTo(width, axisY); ctx.stroke();
  for (let time = Math.ceil(start / step) * step; time <= end; time += step) { const x = timelineX(view, time, width, height); if (x < -20 || x > width + 20) continue; ctx.strokeStyle = "#61717e"; ctx.beginPath(); ctx.moveTo(x, axisY); ctx.lineTo(x, axisY + 7); ctx.stroke(); ctx.fillStyle = "#9faab5"; ctx.font = "11px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillText(formatTimelineTick(time, step), x, axisY + 10); }
}

function timelineTickStep(span) { return TIMELINE_INTERVALS.slice(2).find(step => span / step <= 9) || TIMELINE_INTERVALS.at(-1); }
function formatTimelineTick(value, step) { const date = new Date(value * 1000); const pad = number => String(number).padStart(2, "0"); if (step < 60) return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`; if (step < 86400) return `${pad(date.getUTCDate())} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`; if (step < 31536000) return `${pad(date.getUTCDate())}/${pad(date.getUTCMonth() + 1)}/${date.getUTCFullYear()}`; return String(date.getUTCFullYear()); }

function representativeVisualizationPoint(points) {
  return [...points].sort((left, right) => { const leftQuality = Number.isFinite(Number(left.quality_score)) ? Number(left.quality_score) : -Infinity; const rightQuality = Number.isFinite(Number(right.quality_score)) ? Number(right.quality_score) : -Infinity; return rightQuality - leftQuality || String(left.asset_id).localeCompare(String(right.asset_id)); })[0];
}

function thumbnailTier(pixelSpacing, count) { const available = Number(pixelSpacing) || 0; if (available >= 220 && count <= 1) return 112; if (available >= 130 && count <= 4) return 88; if (available >= 72) return 64; return available >= 42 ? 48 : 36; }

function visualizationThumbnail(assetId) {
  if (visualizationThumbnailCache.has(assetId)) return visualizationThumbnailCache.get(assetId);
  const entry = {image: null, loading: true}; const image = new Image();
  image.onload = () => { entry.image = image; entry.loading = false; renderVisualization(state.viewMode); }; image.onerror = () => { entry.loading = false; entry.failed = true; }; image.src = apiPath(`/api/assets/${encodeURIComponent(assetId)}/thumbnail`);
  visualizationThumbnailCache.set(assetId, entry); while (visualizationThumbnailCache.size > 240) visualizationThumbnailCache.delete(visualizationThumbnailCache.keys().next().value); return entry;
}

function drawVisualizationThumbnail(ctx, point, x, y, width, height) {
  const entry = visualizationThumbnail(point.asset_id);
  if (entry.image) { const imageRatio = entry.image.naturalWidth / Math.max(1, entry.image.naturalHeight); const boxRatio = width / height; let drawWidth = width; let drawHeight = height; if (imageRatio > boxRatio) drawHeight = width / imageRatio; else drawWidth = height * imageRatio; ctx.drawImage(entry.image, x + (width - drawWidth) / 2, y + (height - drawHeight) / 2, drawWidth, drawHeight); return; }
  ctx.fillStyle = point.media_type === "video" ? "#9c7240" : "#39708d"; ctx.fillRect(x, y, width, height); ctx.fillStyle = "#c9d5dc"; ctx.font = "600 10px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(point.media_type === "video" ? "VIDEO" : "IMAGE", x + width / 2, y + height / 2);
}

function drawVector(canvas, view, data) {
  const surface = canvasSurface(canvas); const {context: ctx, width, height} = surface;
  ctx.fillStyle = "#10161c"; ctx.fillRect(0, 0, width, height);
  if (!data.available || !data.points?.length) { view.hitTargets = []; view.grid = new Map(); drawCanvasMessage(ctx, width, height, data.empty_reason || "No projected semantic vectors."); return; }
  const points = data.points.filter(point => Number.isFinite(Number(point.x)) && Number.isFinite(Number(point.y)));
  if (view.needsFit) fitVector(view, points, width, height);
  drawVectorDensity(ctx, points, view, width, height);
  const axis = screenPoint(view, view.centerX, view.centerY, width, height); ctx.strokeStyle = "rgba(121, 145, 160, .35)"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0, axis.y); ctx.lineTo(width, axis.y); ctx.moveTo(axis.x, 0); ctx.lineTo(axis.x, height); ctx.stroke();
  ctx.fillStyle = "#8393a0"; ctx.font = "11px system-ui"; ctx.fillText("PCA 1", width - 42, Math.max(14, axis.y - 8)); ctx.save(); ctx.translate(Math.max(12, axis.x - 8), 42); ctx.rotate(-Math.PI / 2); ctx.fillText("PCA 2", 0, 0); ctx.restore();
  for (const point of points) { const screen = screenPoint(view, Number(point.x), Number(point.y), width, height); if (screen.x < -4 || screen.x > width + 4 || screen.y < -4 || screen.y > height + 4) continue; ctx.beginPath(); ctx.arc(screen.x, screen.y, point.asset_id === state.visualizationSelectedId ? 4 : 2, 0, Math.PI * 2); ctx.fillStyle = point.asset_id === state.visualizationSelectedId ? "#d9e0e6" : point.media_type === "video" ? "#c68b52" : "#5f9eb8"; ctx.fill(); }
  const cellWorld = view.baseCellWorld / 2 ** vectorLod(view); const cells = new Map(); view.grid = cells;
  for (const point of points) { const key = vectorCellKey(point.x, point.y, cellWorld, view.gridOriginX, view.gridOriginY); const cell = cells.get(key) || {points: [], x: 0, y: 0}; cell.points.push(point); cell.x += Number(point.x); cell.y += Number(point.y); cells.set(key, cell); }
  view.hitTargets = []; const cellPixels = Math.max(1, cellWorld * view.scale);
  for (const cell of cells.values()) { const center = screenPoint(view, cell.x / cell.points.length, cell.y / cell.points.length, width, height); if (center.x < -140 || center.x > width + 140 || center.y < -140 || center.y > height + 140) continue; const representative = representativeVisualizationPoint(cell.points); const size = thumbnailTier(cellPixels, cell.points.length); const thumbnailHeight = Math.max(24, size * .72); if (cellPixels >= 42) drawVisualizationThumbnail(ctx, representative, center.x - size / 2, center.y - thumbnailHeight / 2, size, thumbnailHeight); if (cell.points.some(point => point.asset_id === state.visualizationSelectedId)) { ctx.strokeStyle = "#d9e0e6"; ctx.lineWidth = 2; ctx.strokeRect(center.x - size / 2 - 2, center.y - thumbnailHeight / 2 - 2, size + 4, thumbnailHeight + 4); } view.hitTargets.push({x: center.x, y: center.y, hitRadius: Math.max(12, size / 2 + 6), point: representative}); }
}

function drawVectorDensity(ctx, points, view, width, height) {
  const columns = Math.min(180, Math.max(96, Math.floor(width / 6))); const rows = Math.min(110, Math.max(64, Math.floor(height / 6))); const counts = new Float32Array(columns * rows); let maximum = 0;
  for (const point of points) { const screen = screenPoint(view, Number(point.x), Number(point.y), width, height); if (screen.x < -8 || screen.x > width + 8 || screen.y < -8 || screen.y > height + 8) continue; const column = Math.max(0, Math.min(columns - 1, Math.floor(screen.x / width * columns))); const row = Math.max(0, Math.min(rows - 1, Math.floor(screen.y / height * rows))); for (let y = Math.max(0, row - 2); y <= Math.min(rows - 1, row + 2); y += 1) for (let x = Math.max(0, column - 2); x <= Math.min(columns - 1, column + 2); x += 1) { const distance = Math.abs(x - column) + Math.abs(y - row); counts[y * columns + x] += distance === 0 ? 1 : distance === 1 ? .45 : .16; maximum = Math.max(maximum, counts[y * columns + x]); } }
  const raster = document.createElement("canvas"); raster.width = columns; raster.height = rows; const rasterContext = raster.getContext("2d"); const image = rasterContext.createImageData(columns, rows);
  for (let index = 0; index < counts.length; index += 1) { const intensity = Math.sqrt(counts[index] / Math.max(1, maximum)); const offset = index * 4; image.data[offset] = Math.round(20 + intensity * 42); image.data[offset + 1] = Math.round(50 + intensity * 105); image.data[offset + 2] = Math.round(88 + intensity * 120); image.data[offset + 3] = Math.round(intensity * 105); }
  rasterContext.putImageData(image, 0, 0); ctx.save(); ctx.globalAlpha = .72; ctx.imageSmoothingEnabled = true; ctx.drawImage(raster, 0, 0, width, height); ctx.restore();
}

function vectorLod(view) { return Math.max(0, Math.min(18, Math.floor(Math.log2(Math.max(1, view.scale / Math.max(1, view.baseScale || 1)))))); }
function vectorCellKey(x, y, cellWorld, originX, originY) { return `${Math.floor((Number(x) - originX) / cellWorld)}:${Math.floor((Number(y) - originY) / cellWorld)}`; }

function fitVector(view, points, width, height) {
  const values = points.map(point => ({x: Number(point.x), y: Number(point.y)})).filter(point => Number.isFinite(point.x) && Number.isFinite(point.y)); if (!values.length) return;
  let magnitude = 0; for (const point of values) magnitude = Math.max(magnitude, Math.abs(point.x), Math.abs(point.y)); const bounds = boundsOf(values, Math.max(.1, magnitude * .12));
  view.centerX = (bounds.minX + bounds.maxX) / 2; view.centerY = (bounds.minY + bounds.maxY) / 2; view.scale = Math.max(1, Math.min(width / (bounds.maxX - bounds.minX), height / (bounds.maxY - bounds.minY))); view.baseScale = view.scale; view.baseCellWorld = Math.max(bounds.maxX - bounds.minX, bounds.maxY - bounds.minY) / 8; view.gridOriginX = Math.floor(bounds.minX / view.baseCellWorld) * view.baseCellWorld; view.gridOriginY = Math.floor(bounds.minY / view.baseCellWorld) * view.baseCellWorld; view.panX = view.panY = 0; view.needsFit = false;
}

function boundsOf(values, padding) { let minX = Infinity; let maxX = -Infinity; let minY = Infinity; let maxY = -Infinity; for (const value of values) { minX = Math.min(minX, value.x); maxX = Math.max(maxX, value.x); minY = Math.min(minY, value.y); maxY = Math.max(maxY, value.y); } const width = Math.max(padding, maxX - minX); const height = Math.max(padding, maxY - minY); return {minX: minX - width * .08 - padding, maxX: maxX + width * .08 + padding, minY: minY - height * .08 - padding, maxY: maxY + height * .08 + padding}; }
function lowerBound(points, value) { let low = 0; let high = points.length; while (low < high) { const middle = (low + high) >> 1; if (points[middle].time < value) low = middle + 1; else high = middle; } return low; }
function upperBound(points, value) { let low = 0; let high = points.length; while (low < high) { const middle = (low + high) >> 1; if (points[middle].time <= value) low = middle + 1; else high = middle; } return low; }
function drawCanvasMessage(ctx, width, height, message) { ctx.fillStyle = "#9faab5"; ctx.font = "14px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(message, width / 2, height / 2); }

function hitVisualization(mode, event) {
  const canvas = visualizationCanvas(mode); const view = visualizationView(mode); const rect = canvas.getBoundingClientRect(); const x = event.clientX - rect.left; const y = event.clientY - rect.top; let nearest = null; let distance = Infinity;
  for (const target of view.hitTargets) { const current = Math.hypot(target.x - x, target.y - y); if (current <= (target.hitRadius || 24) && current < distance) { distance = current; nearest = target; } }
  if (!nearest) return;
  if (nearest.bucket) { focusTimelineRange(nearest.bucket.start, nearest.bucket.end); return; }
  if (mode === "geo" && nearest.points?.length > 1) { const onBadge = Math.hypot(nearest.badgeX - x, nearest.badgeY - y) <= 14; if (onBadge && (nearest.sameLocation || geoLod(view) >= 21)) { showGeoLocalStrip(nearest.points); return; } if (onBadge) { zoomVisualization("geo", 2.5, {x: nearest.x, y: nearest.y}); return; } selectVisualizationAsset(representativeVisualizationPoint(nearest.points)); return; }
  selectVisualizationAsset(nearest.point || nearest.points?.[0]);
}

function focusTimelineRange(start, end) { const view = visualizationView("timeline"); const canvas = visualizationCanvas("timeline"); const {width} = canvasSurface(canvas); const range = Math.max(.01, end - start); view.centerX = ((start + end) / 2 - view.origin) / view.span; view.panX = 0; view.scale = Math.min(2 ** 30, Math.max(view.scale * 1.4, width / (range / view.span))); renderVisualization("timeline"); }

function showGeoLocalStrip(points) {
  const strip = $("geo-local-strip"); if (!strip) return;
  strip.classList.remove("hidden"); strip.innerHTML = `<div class="visualization-local-strip-header"><strong>${points.length.toLocaleString()} assets at this position</strong><button type="button" class="icon" data-geo-strip-close aria-label="Close asset list">×</button></div><div class="visualization-local-strip-items">${points.slice(0, 24).map(point => `<button type="button" class="visualization-local-strip-item" data-geo-strip-asset="${escapeHtml(point.asset_id)}"><img src="${escapeHtml(apiPath(`/api/assets/${encodeURIComponent(point.asset_id)}/thumbnail`))}" alt="" onerror="this.remove()"><span>${escapeHtml(point.filename || point.asset_id.slice(0, 8))}</span></button>`).join("")}</div>${points.length > 24 ? `<div class="muted">Showing 24 of ${points.length.toLocaleString()}</div>` : ""}`;
  strip.querySelector("[data-geo-strip-close]").onclick = () => strip.classList.add("hidden"); strip.querySelectorAll("[data-geo-strip-asset]").forEach(button => button.onclick = () => selectVisualizationAsset(points.find(point => point.asset_id === button.dataset.geoStripAsset)));
}

async function selectVisualizationAsset(point) {
  if (!point?.asset_id) return;
  state.visualizationSelectedId = point.asset_id; VISUALIZATION_MODES.forEach(mode => { if (visualizationView(mode).data) renderVisualization(mode); });
  const token = (state.visualizationSelectionRequest || 0) + 1; state.visualizationSelectionRequest = token;
  try { const asset = await api(`/api/assets/${encodeURIComponent(point.asset_id)}`); if (token !== state.visualizationSelectionRequest) return; renderVisualizationSelection(asset); }
  catch (error) { showToast(`Asset preview unavailable: ${error.message}`); }
}

function renderVisualizationSelection(asset) {
  const panel = $("visualization-selection"); if (!panel) return;
  const item = assetToViewerItem(asset); const first = asset.physical_files?.[0] || {}; const capture = asset.capture_time ? formatCapture(asset.capture_time) : ""; const quality = Number.isFinite(Number(asset.quality_score)) ? ` · Quality ${Number(asset.quality_score).toFixed(2)}` : ""; const thumbnail = first.thumbnail_url ? `<img class="visualization-selection-thumb" src="${escapeHtml(first.thumbnail_url)}" alt="">` : "";
  panel.classList.remove("hidden"); panel.innerHTML = `${thumbnail}<div class="visualization-selection-copy"><strong>${escapeHtml(item.filename || "Asset")}</strong><span>${escapeHtml(capture || item.media_type || "")}${escapeHtml(quality)}</span></div><div class="visualization-selection-actions"><button type="button" class="primary-action" data-visualization-open>Open</button><button type="button" class="secondary" data-visualization-details>Details</button><button type="button" class="icon" data-visualization-close aria-label="Close preview">×</button></div>`;
  panel.querySelector("[data-visualization-open]").onclick = () => showViewer(0, [item], {mode: "visualization", total: 1}); panel.querySelector("[data-visualization-details]").onclick = () => showDetails(asset.asset_id, {mode: "visualization", items: [item], index: 0, total: 1}); panel.querySelector("[data-visualization-close]").onclick = clearVisualizationSelection;
}

function clearVisualizationSelection() { state.visualizationSelectedId = null; state.visualizationSelectionRequest = (state.visualizationSelectionRequest || 0) + 1; $("geo-local-strip")?.classList.add("hidden"); $("visualization-selection")?.classList.add("hidden"); }

function bindVisualizationEvents(mode) {
  const canvas = visualizationCanvas(mode); let dragging = false; let startX = 0; let startY = 0; let moved = false;
  canvas.addEventListener("wheel", event => { event.preventDefault(); const rect = canvas.getBoundingClientRect(); zoomVisualization(mode, event.deltaY < 0 ? 1.22 : 1 / 1.22, {x: event.clientX - rect.left, y: event.clientY - rect.top}); }, {passive: false});
  canvas.addEventListener("pointerdown", event => { if (event.button !== 0) return; dragging = true; moved = false; startX = event.clientX; startY = event.clientY; canvas.classList.add("dragging"); canvas.setPointerCapture(event.pointerId); });
  canvas.addEventListener("pointermove", event => { if (!dragging) return; const dx = event.clientX - startX; const dy = event.clientY - startY; if (Math.abs(dx) + Math.abs(dy) > 4) moved = true; if (moved) panVisualization(mode, dx, dy); startX = event.clientX; startY = event.clientY; });
  ["pointerup", "pointercancel"].forEach(name => canvas.addEventListener(name, event => { if (!dragging) return; dragging = false; canvas.classList.remove("dragging"); if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId); if (moved) canvas.dataset.suppressClick = "1"; }));
  canvas.addEventListener("click", event => { if (canvas.dataset.suppressClick) { delete canvas.dataset.suppressClick; return; } hitVisualization(mode, event); });
  $(`${mode}-fit`).onclick = () => fitVisualization(mode); $(`${mode}-zoom-in`).onclick = () => zoomVisualization(mode, 1.35); $(`${mode}-zoom-out`).onclick = () => zoomVisualization(mode, 1 / 1.35);
}

VISUALIZATION_MODES.forEach(bindVisualizationEvents);
window.addEventListener("resize", () => { if (VISUALIZATION_MODES.includes(state.viewMode)) renderVisualization(state.viewMode); });
