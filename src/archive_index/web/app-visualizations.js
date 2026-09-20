const VISUALIZATION_MODES = ["geo", "timeline", "vector"];
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
      if (mode === "timeline" && view.preserveTransform) {
        const transform = view.modes?.[view.timeMode];
        if (transform) Object.assign(view, transform);
        else view.needsFit = true;
        view.preserveTransform = false;
      } else view.needsFit = true;
      view.hitTargets = [];
      state.visualizationSelectedId = null;
      clearVisualizationSelection();
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
  if (mode === "timeline") $("timeline-time-mode").value = view.timeMode || "capture";
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

function saveTimelineTransform(view) {
  return {
    origin: view.origin, span: view.span, centerX: view.centerX, centerY: view.centerY,
    scale: view.scale, panX: view.panX, panY: view.panY, needsFit: view.needsFit,
  };
}

function switchTimelineMode(mode) {
  if (!['capture', 'file_created'].includes(mode)) return;
  const view = visualizationView('timeline');
  view.modes[view.timeMode] = saveTimelineTransform(view);
  view.timeMode = mode;
  Object.assign(view, view.modes[mode]);
  view.preserveTransform = true;
  view.key = null;
  view.data = null;
  state.visualizationSelectedId = null;
  loadVisualization('timeline');
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
  const clusterCellSize = Math.max(30, Math.min(64, 56 / Math.sqrt(Math.max(1, view.scale / 700))));
  for (const point of points) {
    const world = geoWorld(point.longitude, point.latitude);
    const screen = screenPoint(view, world.x, world.y, width, height);
    const key = `${Math.floor(screen.x / clusterCellSize)}:${Math.floor(screen.y / clusterCellSize)}`;
    const cluster = clusters.get(key) || [];
    cluster.push({...point, screenX: screen.x, screenY: screen.y});
    clusters.set(key, cluster);
  }
  view.hitTargets = [];
  for (const cluster of clusters.values()) {
    const x = cluster.reduce((sum, point) => sum + point.screenX, 0) / cluster.length;
    const y = cluster.reduce((sum, point) => sum + point.screenY, 0) / cluster.length;
    const representative = representativeVisualizationPoint(cluster);
    const thumbWidth = cluster.length === 1 ? 36 : 44;
    const thumbHeight = cluster.length === 1 ? 28 : 32;
    drawVisualizationThumbnail(ctx, representative, x - thumbWidth / 2, y - thumbHeight / 2, thumbWidth, thumbHeight);
    if (cluster.some(point => point.asset_id === state.visualizationSelectedId)) {
      ctx.strokeStyle = "#d9e0e6"; ctx.lineWidth = 2; ctx.strokeRect(x - thumbWidth / 2 - 2, y - thumbHeight / 2 - 2, thumbWidth + 4, thumbHeight + 4);
    }
    const badgeX = x + thumbWidth / 2 - 2;
    const badgeY = y - thumbHeight / 2 + 2;
    if (cluster.length > 1) {
      ctx.beginPath(); ctx.arc(badgeX, badgeY, 10, 0, Math.PI * 2); ctx.fillStyle = "#17222b"; ctx.fill();
      ctx.fillStyle = "#edf0f3"; ctx.font = "600 10px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle"; ctx.fillText(String(cluster.length), badgeX, badgeY);
    }
    view.hitTargets.push({x, y, badgeX, badgeY, points: cluster});
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
  if (!data.available || !data.points?.length) {
    view.hitTargets = [];
    drawCanvasMessage(ctx, width, height, data.empty_reason || "No timed assets.");
    return;
  }
  const points = data.points;
  if (view.needsFit) fitTimeline(view, points, width, height);
  const axisY = height * .82;
  ctx.strokeStyle = "#465663"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(0, axisY); ctx.lineTo(width, axisY); ctx.stroke();
  const left = Math.max(0, worldPoint(view, 0, .5, width, height).x);
  const right = Math.min(1, worldPoint(view, width, .5, width, height).x);
  const visibleStart = view.origin + left * view.span;
  const visibleEnd = view.origin + right * view.span;
  const first = lowerBound(points, visibleStart);
  const last = upperBound(points, visibleEnd);
  const visible = points.slice(first, last);
  const pixelsPerPoint = width / Math.max(1, visible.length);
  const broad = visible.length > Math.max(220, width * 0.85) || pixelsPerPoint < 8;
  view.hitTargets = [];
  if (broad) {
    drawTimelineBuckets(ctx, view, visible, visibleStart, visibleEnd, axisY, width, height);
  } else {
    drawTimelinePoints(ctx, view, visible, axisY, width, height);
  }
  drawTimelineTicks(ctx, view, visibleStart, visibleEnd, axisY, width, height);
}

function drawTimelineBuckets(ctx, view, points, visibleStart, visibleEnd, axisY, width, height) {
  const bucketCount = Math.max(8, Math.ceil(width / 72));
  const bucketWidth = Math.max(1, (visibleEnd - visibleStart) / bucketCount);
  const buckets = new Map();
  for (const point of points) {
    const index = Math.min(bucketCount - 1, Math.max(0, Math.floor((point.time - visibleStart) / bucketWidth)));
    const bucket = buckets.get(index) || {points: []};
    bucket.points.push(point);
    buckets.set(index, bucket);
  }
  const maxCount = Math.max(...[...buckets.values()].map(bucket => bucket.points.length), 1);
  for (const [index, bucket] of buckets) {
    const start = visibleStart + index * bucketWidth;
    const end = start + bucketWidth;
    const leftPoint = screenPoint(view, (start - view.origin) / view.span, 0, width, height);
    const rightPoint = screenPoint(view, (end - view.origin) / view.span, 0, width, height);
    const barWidth = Math.max(2, rightPoint.x - leftPoint.x - 2);
    const barHeight = 20 + 165 * bucket.points.length / maxCount;
    const centerX = (leftPoint.x + rightPoint.x) / 2;
    ctx.fillStyle = "#35566d";
    ctx.fillRect(leftPoint.x, axisY - barHeight, barWidth, barHeight);
    const representative = representativeVisualizationPoint(bucket.points);
    if (barWidth >= 52) drawVisualizationThumbnail(ctx, representative, centerX - 24, axisY - barHeight - 44, 48, 36);
    view.hitTargets.push({x: centerX, y: axisY - barHeight / 2, bucket: {start, end}});
  }
}

function drawTimelinePoints(ctx, view, points, axisY, width, height) {
  const lanes = 6;
  for (const point of points) {
    const x = screenPoint(view, (point.time - view.origin) / view.span, 0, width, height).x;
    const lane = stableLane(point.asset_id, lanes);
    const y = height * (.24 + lane * .085);
    const size = point.asset_id === state.visualizationSelectedId ? 66 : 56;
    drawVisualizationThumbnail(ctx, point, x - size / 2, y - 20, size, 40);
    if (point.asset_id === state.visualizationSelectedId) {
      ctx.strokeStyle = "#d9e0e6"; ctx.lineWidth = 2; ctx.strokeRect(x - size / 2 - 2, y - 22, size + 4, 44);
    }
    view.hitTargets.push({x, y, point});
  }
}

function fitTimeline(view, points, width, height) {
  const min = points[0].time; const max = points[points.length - 1].time;
  view.origin = min === max ? min - 43200 : min;
  view.span = min === max ? 86400 : Math.max(1, max - min);
  view.centerX = .5; view.centerY = .5; view.scale = Math.max(1, width / 1.08); view.panX = view.panY = 0; view.needsFit = false;
  view.modes[view.timeMode] = saveTimelineTransform(view);
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

function representativeVisualizationPoint(points) {
  return [...points].sort((left, right) => {
    const leftQuality = Number.isFinite(Number(left.quality_score)) ? Number(left.quality_score) : -Infinity;
    const rightQuality = Number.isFinite(Number(right.quality_score)) ? Number(right.quality_score) : -Infinity;
    return rightQuality - leftQuality || String(left.asset_id).localeCompare(String(right.asset_id));
  })[0];
}

function stableLane(assetId, laneCount) {
  let hash = 2166136261;
  for (const char of String(assetId)) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
  return Math.abs(hash) % laneCount;
}

function visualizationThumbnail(assetId) {
  if (visualizationThumbnailCache.has(assetId)) return visualizationThumbnailCache.get(assetId);
  const entry = {image: null, loading: true};
  const image = new Image();
  image.onload = () => { entry.image = image; entry.loading = false; renderVisualization(state.viewMode); };
  image.onerror = () => { entry.loading = false; entry.failed = true; };
  image.src = apiPath(`/api/assets/${encodeURIComponent(assetId)}/thumbnail`);
  visualizationThumbnailCache.set(assetId, entry);
  while (visualizationThumbnailCache.size > 240) visualizationThumbnailCache.delete(visualizationThumbnailCache.keys().next().value);
  return entry;
}

function drawVisualizationThumbnail(ctx, point, x, y, width, height) {
  const entry = visualizationThumbnail(point.asset_id);
  if (entry.image) {
    const imageRatio = entry.image.naturalWidth / Math.max(1, entry.image.naturalHeight);
    const boxRatio = width / height;
    let drawWidth = width; let drawHeight = height;
    if (imageRatio > boxRatio) drawHeight = width / imageRatio;
    else drawWidth = height * imageRatio;
    ctx.drawImage(entry.image, x + (width - drawWidth) / 2, y + (height - drawHeight) / 2, drawWidth, drawHeight);
    return;
  }
  ctx.fillStyle = point.media_type === "video" ? "#9c7240" : "#39708d";
  ctx.fillRect(x, y, width, height);
  ctx.fillStyle = "#c9d5dc"; ctx.font = "600 10px system-ui"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
  ctx.fillText(point.media_type === "video" ? "VIDEO" : "IMAGE", x + width / 2, y + height / 2);
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
  const cellSize = 72;
  view.grid = new Map(); view.hitTargets = [];
  for (const point of data.points) {
    const screen = screenPoint(view, Number(point.x), Number(point.y), width, height);
    if (screen.x < -8 || screen.x > width + 8 || screen.y < -8 || screen.y > height + 8) continue;
    const key = `${Math.floor(screen.x / cellSize)}:${Math.floor(screen.y / cellSize)}`;
    const cell = view.grid.get(key) || {points: [], x: 0, y: 0};
    cell.points.push({point, x: screen.x, y: screen.y}); cell.x += screen.x; cell.y += screen.y; view.grid.set(key, cell);
  }
  for (const cell of view.grid.values()) {
    const count = cell.points.length;
    const centerX = cell.x / count; const centerY = cell.y / count;
    const column = Math.floor(centerX / cellSize) * cellSize;
    const row = Math.floor(centerY / cellSize) * cellSize;
    ctx.fillStyle = count > 1 ? `rgba(95, 142, 174, ${Math.min(.34, .08 + count / 80)})` : "rgba(95, 142, 174, .04)";
    ctx.fillRect(column + 1, row + 1, cellSize - 2, cellSize - 2);
    const representative = representativeVisualizationPoint(cell.points.map(item => item.point));
    const selected = representative.asset_id === state.visualizationSelectedId;
    drawVisualizationThumbnail(ctx, representative, centerX - 27, centerY - 19, 54, 38);
    if (count > 1) {
      ctx.fillStyle = "#edf0f3"; ctx.font = "600 11px system-ui"; ctx.textAlign = "right"; ctx.textBaseline = "top"; ctx.fillText(String(count), column + cellSize - 5, row + 5);
    }
    if (selected) { ctx.strokeStyle = "#d9e0e6"; ctx.lineWidth = 2; ctx.strokeRect(centerX - 29, centerY - 21, 58, 42); }
    view.hitTargets.push({x: centerX, y: centerY, point: representative});
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
    let nearest = null; let distance = 38;
    for (const target of view.hitTargets) { const current = Math.hypot(target.x - x, target.y - y); if (current < distance) { distance = current; nearest = target.point; } }
    if (nearest) selectVisualizationAsset(nearest);
    return;
  }
  let nearest = null; let distance = 24;
  for (const target of view.hitTargets) { const current = Math.hypot(target.x - x, target.y - y); if (current < distance) {distance = current; nearest = target;} }
  if (!nearest) return;
  if (nearest.bucket) { focusTimelineRange(nearest.bucket.start, nearest.bucket.end); return; }
  if (nearest.points?.length > 1) {
    if (mode === "geo") {
      const onBadge = Math.hypot(nearest.badgeX - x, nearest.badgeY - y) <= 14;
      if (onBadge && view.scale >= 900) showGeoLocalStrip(nearest.points);
      else if (onBadge) zoomVisualization("geo", 2.5, {x: nearest.x, y: nearest.y});
      else selectVisualizationAsset(representativeVisualizationPoint(nearest.points));
      return;
    }
  }
  selectVisualizationAsset(nearest.point || nearest.points[0]);
}

function focusTimelineRange(start, end) {
  const view = visualizationView("timeline"); const canvas = visualizationCanvas("timeline"); const {width} = canvasSurface(canvas); const range = Math.max(1, end - start); view.centerX = ((start + end) / 2 - view.origin) / view.span; view.panX = 0; view.scale = Math.min(100000, Math.max(view.scale * 1.4, width / (range / view.span))); renderVisualization("timeline");
}

function showGeoLocalStrip(points) {
  const strip = $("geo-local-strip");
  if (!strip) return;
  strip.classList.remove("hidden");
  strip.innerHTML = `<div class="visualization-local-strip-header"><strong>${points.length.toLocaleString()} assets at this position</strong><button type="button" class="icon" data-geo-strip-close aria-label="Close asset list">×</button></div><div class="visualization-local-strip-items">${points.slice(0, 24).map(point => `<button type="button" class="visualization-local-strip-item" data-geo-strip-asset="${escapeHtml(point.asset_id)}"><img src="${escapeHtml(apiPath(`/api/assets/${encodeURIComponent(point.asset_id)}/thumbnail`))}" alt="" onerror="this.remove()"><span>${escapeHtml(point.filename || point.asset_id.slice(0, 8))}</span></button>`).join("")}</div>${points.length > 24 ? `<div class="muted">Showing 24 of ${points.length.toLocaleString()}</div>` : ""}`;
  strip.querySelector("[data-geo-strip-close]").onclick = () => strip.classList.add("hidden");
  strip.querySelectorAll("[data-geo-strip-asset]").forEach(button => button.onclick = () => selectVisualizationAsset(points.find(point => point.asset_id === button.dataset.geoStripAsset)));
}

async function selectVisualizationAsset(point) {
  if (!point?.asset_id) return;
  state.visualizationSelectedId = point.asset_id;
  VISUALIZATION_MODES.forEach(mode => { if (visualizationView(mode).data) renderVisualization(mode); });
  const token = (state.visualizationSelectionRequest || 0) + 1; state.visualizationSelectionRequest = token;
  try {
    const asset = await api(`/api/assets/${encodeURIComponent(point.asset_id)}`);
    if (token !== state.visualizationSelectionRequest) return;
    const viewerItem = assetToViewerItem(asset);
    showViewer(0, [viewerItem], {mode: "visualization", total: 1});
  } catch (error) { showToast(`Asset preview unavailable: ${error.message}`); }
}

function clearVisualizationSelection() { state.visualizationSelectedId = null; state.visualizationSelectionRequest = (state.visualizationSelectionRequest || 0) + 1; $("geo-local-strip")?.classList.add("hidden"); }

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
