    const query = new URLSearchParams(location.search);
    const MAX_VIEWER_ZOOM = 40;
     const state = { workspace: query.get("workspace"), page: Number(query.get("page") || 1), pageSize: Number(localStorage.getItem("archive-index-page-size") || query.get("page_size") || 60), total: 0, items: [], viewerItems: [], viewMode: query.get("view") === "groups" ? "groups" : "gallery", selectionFilter: query.get("selection") || "all", groupPage: 1, groupPageSize: 10, viewerIndex: -1, viewerZoom: 1, viewerPanX: 0, viewerPanY: 0, viewerSmooth: true, activeJobId: null, assetRequest: 0 };
    const $ = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;","\"":"&quot;"}[char]));
    const labels = { offline: "Offline", unsupported: "Unsupported", failed: "Processing failed", processing: "Processing" };
    function apiPath(path) {
      if (!state.workspace || !path.startsWith("/api/") || path === "/api/workspaces") return path;
      const url = new URL(path, location.origin);
      url.searchParams.set("workspace", state.workspace);
      return url.pathname + url.search;
    }
    async function api(path, options) {
      const response = await fetch(apiPath(path), options);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Request failed");
      return payload;
    }
    function syncUrl() {
      const params = new URLSearchParams();
      if (state.workspace) params.set("workspace", state.workspace);
      if (state.page > 1) params.set("page", state.page);
      if ($("search").value.trim()) params.set("q", $("search").value.trim());
      if ($("folder").value) params.set("folder", $("folder").value);
      if ($("media-type").value) params.set("media_type", $("media-type").value);
      if (state.selectionFilter !== "all") params.set("selection", state.selectionFilter);
      params.set("sort_by", $("sort-by").value);
      params.set("direction", $("direction").value);
      params.set("page_size", state.pageSize);
      if (state.viewMode === "groups") params.set("view", "groups");
      history.replaceState(null, "", `${location.pathname}?${params}`);
    }
    function filterParams() {
      const params = new URLSearchParams({ page: state.page, page_size: state.pageSize, sort_by: $("sort-by").value, direction: $("direction").value });
      if ($("search").value.trim()) params.set("q", $("search").value.trim());
      if ($("folder").value) params.set("folder", $("folder").value);
      if ($("media-type").value) params.set("media_type", $("media-type").value);
      if (state.selectionFilter !== "all") params.set("selection", state.selectionFilter);
      return `/api/assets?${params}`;
    }
    function formatCapture(value, kind) {
      if (!value) return "";
      const local = value.replace("T", " ");
      return `${local}${kind === "exif_local_unknown" ? " · local time; timezone unknown" : ""}`;
    }
    function issueMarkup(issues) { return (issues || []).map((issue) => `<span class="badge">${escapeHtml(labels[issue] || issue)}</span>`).join(""); }
    function qualityColor(score) { const value = Math.max(0, Math.min(1, Number(score))); const from = [217, 155, 155]; const to = [155, 184, 210]; return `rgb(${from.map((channel, index) => Math.round(channel + (to[index] - channel) * value)).join(", ")})`; }
    function scoreMarkup(score) { return score == null ? "" : `<span class="quality-chip" style="--quality-color: ${qualityColor(score)}">Quality: ${Number(score).toFixed(2)}</span>`; }
    function selectionStateMarkup(item) {
      const decision = item.user_decision || "undecided";
      const effective = decision !== "undecided" ? decision : item.auto_recommended ? "recommended" : item.is_representative ? "representative" : "";
      const label = effective ? effective[0].toUpperCase() + effective.slice(1) : "";
      const stateTag = label ? `<span class="state-tag ${effective === "selected" || effective === "rejected" ? `manual-${effective}` : effective}">${label}</span>` : "";
      return stateTag;
    }
    function selectionActionsMarkup(item) { const decision = item.user_decision || "undecided"; return `<div class="selection-actions"><button type="button" class="selection-button select${decision === "selected" ? " active" : ""}" data-decision="selected" data-asset-id="${escapeHtml(item.asset_id)}">Select</button><button type="button" class="selection-button reject${decision === "rejected" ? " active" : ""}" data-decision="rejected" data-asset-id="${escapeHtml(item.asset_id)}">Reject</button></div>`; }
     function bindSelectionButtons(root) { root.querySelectorAll("[data-decision]").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); setDecision(button.dataset.assetId, button.dataset.decision); })); }
     async function setDecision(assetId, decision) { try { const current = [...state.items, ...state.viewerItems].find((item) => item && item.asset_id === assetId)?.user_decision || "undecided"; const nextDecision = decision === current && decision !== "undecided" ? "undecided" : decision; const payload = await api(`/api/assets/${encodeURIComponent(assetId)}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decision: nextDecision }) }); const update = (item) => { if (item && item.asset_id === assetId) { item.user_decision = payload.user_decision; item.auto_recommended = payload.auto_recommended; item.recommendation_run_id = payload.recommendation_run_id; } }; state.items.forEach(update); state.viewerItems.forEach(update); if ($("viewer").open) renderViewer(); if (state.viewMode === "groups") await loadGroups(); else await loadAssets(); } catch (error) { $("status").textContent = error.message; } }
    function renderCard(item, index) {
      const media = item.thumbnail_url ? `<img class="thumb" loading="lazy" src="${item.thumbnail_url}" alt="${escapeHtml(item.filename)}">` : `<div class="placeholder">Preview unavailable</div>`;
      const capture = $("sort-by").value === "capture_time" && item.capture_time ? `<div class="capture">${escapeHtml(formatCapture(item.capture_time, item.capture_time_kind))}</div>` : "";
       return `<article class="photo-card${item.is_representative ? " representative-card" : ""}" tabindex="0" data-index="${index}" aria-label="View ${escapeHtml(item.filename)}"><div class="photo-frame">${media}</div><button class="info-button" type="button" data-info="${index}" aria-label="Details for ${escapeHtml(item.filename)}">ⓘ</button><div class="photo-card-body"><div class="filename" title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</div>${scoreMarkup(item.quality_score)}${capture}<div class="card-state">${selectionStateMarkup(item)}</div><div class="issues">${issueMarkup(item.issues)}</div><div class="card-actions">${selectionActionsMarkup(item)}</div></div></article>`;
    }
    async function loadHome() {
      $("home-view").classList.remove("hidden"); $("workspace-view").classList.add("hidden"); $("index").classList.add("hidden"); $("problems-button").classList.add("hidden"); $("workspace-crumb").classList.add("hidden");
      const data = await api("/api/workspaces");
      $("recent-list").innerHTML = data.workspaces.map((workspace) => `<article class="panel recent-card"><div><h3>${escapeHtml(workspace.name || "Workspace")}</h3><div class="path">${escapeHtml(workspace.path || "")}</div><div class="muted">${workspace.available === false ? "Unavailable" : `${workspace.assets ?? 0} indexed assets${workspace.last_indexed ? ` · indexed ${escapeHtml(workspace.last_indexed)}` : ""}`}</div></div><div class="recent-actions">${workspace.available === false ? "" : `<button type="button" data-open="${escapeHtml(workspace.id)}">Open</button>`}<button class="secondary" type="button" data-remove="${escapeHtml(workspace.id)}">Remove from list</button></div></article>`).join("") || `<div class="empty">No recent workspaces yet.</div>`;
      $("recent-list").querySelectorAll("[data-open]").forEach((button) => button.addEventListener("click", () => { location.href = `/?workspace=${encodeURIComponent(button.dataset.open)}`; }));
      $("recent-list").querySelectorAll("[data-remove]").forEach((button) => button.addEventListener("click", () => removeRecent(button.dataset.remove)));
    }
    async function openWorkspace(path, create) {
      try {
        const response = await fetch("/api/workspaces/open", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path, create }) });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "Could not open workspace");
        location.href = `/?workspace=${encodeURIComponent(payload.workspace.id)}`;
      } catch (error) { $("home-status").textContent = error.message; }
    }
    async function pickWorkspace() { try { const response = await fetch("/api/workspaces/pick", { method: "POST" }); const payload = await response.json(); if (!response.ok) throw new Error(payload.error || "Could not choose a folder"); if (payload.path) $("workspace-path").value = payload.path; } catch (error) { $("home-status").textContent = error.message; } }
    async function removeRecent(id) {
      try { await fetch("/api/workspaces/remove", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workspace: id }) }); loadHome(); } catch (error) { $("status").textContent = error.message; }
    }
    async function loadWorkspace() {
      $("home-view").classList.add("hidden"); $("workspace-view").classList.remove("hidden"); $("index").classList.remove("hidden"); $("workspace-crumb").classList.remove("hidden");
      const data = await api("/api/workspace");
      $("workspace-crumb").textContent = data.name; $("workspace-current-path").textContent = data.path; $("workspace-summary").textContent = `${data.assets} assets · ${data.online_files} online · ${data.offline_files} offline`;
      const folders = await api("/api/folders");
      const selected = query.get("folder") || "";
      $("folder").innerHTML = `<option value="">All folders</option>` + folders.folders.map((folder) => `<option value="${escapeHtml(folder)}">${escapeHtml(folder)}</option>`).join("");
      $("folder").value = selected;
       $("search").value = query.get("q") || ""; $("media-type").value = query.get("media_type") || ""; $("sort-by").value = query.get("sort_by") || "capture_time"; $("direction").value = query.get("direction") || "desc"; state.selectionFilter = ["all", "representatives", "recommended", "selected", "rejected", "undecided"].includes(query.get("selection")) ? query.get("selection") : "all"; $("selection-filter").value = state.selectionFilter; $("page-size").value = String(state.pageSize); if (!["60", "120", "180"].includes($("page-size").value)) { state.pageSize = 60; $("page-size").value = "60"; }
      await loadAssets(); await loadJobs(); await loadProblemsBadge(); setViewMode(state.viewMode, state.viewMode === "groups");
    }
    async function loadAssets() {
      const requestId = ++state.assetRequest;
      try {
        const data = await api(filterParams());
        if (requestId !== state.assetRequest) return;
        state.items = data.items; state.viewerItems = state.items; state.total = data.total;
        $("status").textContent = state.selectionFilter === "representatives" && !data.grouping_available ? "Strict groups have not been built for this workspace yet." : "";
        $("gallery").innerHTML = data.items.map(renderCard).join("") || `<div class="empty">No matching photos.</div>`;
        $("gallery").querySelectorAll(".photo-card").forEach((card) => { card.addEventListener("click", (event) => { if (!event.target.closest("button")) showViewer(Number(card.dataset.index)); }); card.addEventListener("keydown", (event) => { if ((event.key === "Enter" || event.key === " ") && event.target === card) { event.preventDefault(); showViewer(Number(card.dataset.index)); } }); });
         $("gallery").querySelectorAll("[data-info]").forEach((button) => button.addEventListener("click", (event) => { event.stopPropagation(); showDetails(state.items[Number(button.dataset.info)].asset_id); })); bindSelectionButtons($("gallery"));
        const pageCount = Math.max(1, Math.ceil(data.total / data.page_size));
        const first = data.total ? ((data.page - 1) * data.page_size) + 1 : 0;
        const last = data.total ? Math.min(data.total, data.page * data.page_size) : 0;
        const range = data.total ? `${first}–${last} / ${data.total} matches` : "0 / 0 matches";
        ["top", "bottom"].forEach((place) => { $(`page-label-${place}`).textContent = `Page ${data.page} / ${pageCount}`; $(`range-label-${place}`).textContent = range; $(`previous-${place}`).disabled = data.page <= 1; $(`next-${place}`).disabled = !data.has_next; });
        syncUrl();
      } catch (error) { $("status").textContent = error.message; }
    }
    function changePage(delta) { state.page = Math.max(1, state.page + delta); syncUrl(); loadAssets().then(() => window.scrollTo({ top: 0, behavior: "smooth" })); }
    function resetViewerZoom() { state.viewerZoom = 1; state.viewerPanX = 0; state.viewerPanY = 0; }
    function showViewer(index, items = state.items) { state.viewerItems = items; if (!state.viewerItems[index]) return; state.viewerIndex = index; resetViewerZoom(); renderViewer(); if (!$('viewer').open) { $("viewer").showModal(); document.body.classList.add("modal-open"); } }
    function renderViewer() {
      const item = state.viewerItems[state.viewerIndex]; if (!item) return;
       $("viewer-title").textContent = item.filename; $("viewer-count").textContent = `${state.viewerIndex + 1} of ${state.viewerItems.length}`; $("viewer-previous").disabled = state.viewerIndex <= 0; $("viewer-next").disabled = state.viewerIndex >= state.viewerItems.length - 1; $("viewer-selection").innerHTML = `${selectionStateMarkup(item)}${selectionActionsMarkup(item)}`; bindSelectionButtons($("viewer-selection"));
      $("viewer-media").innerHTML = "";
      if (!item.original_url) { $("viewer-media").innerHTML = `<div class="viewer-error">This media is offline or cannot currently be rendered.</div>`; return; }
      const media = item.media_type === "video" ? document.createElement("video") : document.createElement("img"); media.className = "viewer-media"; media.alt = item.filename; media.controls = item.media_type === "video"; media.src = item.original_url; media.addEventListener("error", () => { $("viewer-media").innerHTML = `<div class="viewer-error">${item.media_type === "video" ? "This video format is not supported by the browser or is unavailable." : "This media could not be rendered."}</div>`; }); $("viewer-media").appendChild(media); applyViewerZoom(media);
    }
    function applyViewerZoom(media) { media.style.transform = `translate3d(${state.viewerPanX}px, ${state.viewerPanY}px, 0) scale(${state.viewerZoom})`; media.style.imageRendering = state.viewerSmooth ? "auto" : "pixelated"; }
    function moveViewer(delta) { const next = state.viewerIndex + delta; if (next < 0 || next >= state.viewerItems.length) return; state.viewerIndex = next; resetViewerZoom(); renderViewer(); }
    function closeDialog(dialog) { if (dialog.open) dialog.close(); if (!["viewer", "details", "problems-dialog"].some((id) => $(id).open)) document.body.classList.remove("modal-open"); }
    async function showDetails(assetId) {
      try { const asset = await api(`/api/assets/${encodeURIComponent(assetId)}`); $("details").innerHTML = renderDetails(asset); $("details").showModal(); document.body.classList.add("modal-open"); $("details-close").addEventListener("click", () => closeDialog($("details"))); } catch (error) { $("status").textContent = error.message; }
    }
    function renderDetails(asset) {
      const first = asset.physical_files[0] || {};
      const dimensions = first.width && first.height ? `${first.width} × ${first.height} (${(first.width * first.height / 1000000).toFixed(2)} MP)` : "Unavailable";
      return `<div class="dialog-inner"><div class="dialog-header"><div><h2 id="details-title">${escapeHtml(first.filename || "Asset details")}</h2><div class="muted">${escapeHtml(formatCapture(asset.capture_time, asset.capture_time_kind) || "Capture time unavailable")}</div></div><button id="details-close" class="secondary" type="button">Close</button></div><section><h3>Overview</h3><dl class="kv"><dt>Dimensions</dt><dd>${escapeHtml(dimensions)}</dd><dt>File size</dt><dd>${escapeHtml(formatBytes(first.size_bytes))}</dd><dt>Capture time</dt><dd>${escapeHtml(formatCapture(asset.capture_time, asset.capture_time_kind) || "Unavailable")}</dd></dl></section>${renderTechnicalDetails(first)}${asset.physical_files.map((file, index) => renderQuality(file, index > 0)).join("")}${asset.physical_files.map(renderComponentProblems).join("")}</div>`;
    }
    function renderQuality(file, showFilename) { if (file.media_type === "video" || file.components?.quality?.status === "not_requested") return `<div class="quality-unsupported">Technical quality review is not supported for video yet.</div>`; const score = file.quality_score == null ? "Unavailable" : Number(file.quality_score).toFixed(2); const components = file.quality_components || {}; const metrics = [["Focus", "focus"], ["Exposure", "exposure"], ["Contrast", "contrast"], ["Noise", "noise"]]; const color = file.quality_score == null ? "#26333f" : qualityColor(file.quality_score); return `<div class="file-card">${showFilename ? `<div class="muted">${escapeHtml(file.filename)}</div>` : ""}<div class="quality-summary"><strong>Overall technical quality</strong><span class="quality-score-box" style="--quality-color: ${color}"><span class="quality-score">${score}</span></span></div>${metrics.map(([label, key]) => { const value = components[key]; const number = value == null ? null : Number(value); const negative = key === "noise"; const display = negative && number != null ? 1 - number : number; return `<div class="metric"><span>${label}</span><div class="bar${negative ? " negative" : ""}"><span style="--value:${display == null ? 0 : Math.max(0, Math.min(100, display * 100))}%"></span></div><span>${display == null ? "—" : display.toFixed(2)}</span></div>`; }).join("")}</div>`; }
    function meterMarkup(label, value, position) { if (!value || position == null) return `<dt>${label}</dt><dd>${escapeHtml(value || "Unavailable")}</dd>`; const clamped = Math.max(0, Math.min(100, position)); const overflow = position < 0 ? "<" : position > 100 ? ">" : ""; return `<dt>${label}</dt><dd class="technical-reading"><span>${escapeHtml(value)}</span><span class="meter" aria-hidden="true"><span class="meter-fill" style="--meter-position: ${clamped}%"></span><span class="meter-overflow">${overflow}</span></span></dd>`; }
    function logPosition(value, low, high) { if (!Number.isFinite(value) || value <= 0) return null; return Math.log(value / low) / Math.log(high / low) * 100; }
    function shutterPosition(value) { if (!Number.isFinite(value) || value <= 0) return null; return logPosition(30 / value, 1, 30 * 8000); }
    function renderTechnicalDetails(file) { const aperture = metadataValue(file, ["FNumber", "ApertureValue"]); const shutter = metadataValue(file, ["ExposureTime", "ShutterSpeedValue"]); const iso = metadataValue(file, ["ISOSpeedRatings", "PhotographicSensitivity"]); const focal = metadataValue(file, ["FocalLength", "FocalLengthIn35mmFilm"]); const apertureNumber = rationalNumber(rawMetadataValue(file, ["FNumber"])); const shutterNumber = rationalNumber(rawMetadataValue(file, ["ExposureTime"])); const isoNumber = rationalNumber(rawMetadataValue(file, ["ISOSpeedRatings", "PhotographicSensitivity"])); const focalNumber = rationalNumber(rawMetadataValue(file, ["FocalLength"])); const rows = [["Camera", cameraValue(file)], ["Lens", metadataValue(file, ["LensModel", "LensMake"])]]; const hasMeter = aperture || shutter || iso || focal; if (!rows.some(([, value]) => value) && !hasMeter) return ""; return `<section class="section"><h3>Technical details</h3><dl class="kv">${rows.filter(([, value]) => value).map(([label, value]) => `<dt>${label}</dt><dd>${escapeHtml(value)}</dd>`).join("")}${aperture ? meterMarkup("Aperture", aperture, logPosition(apertureNumber, 1, 22)) : ""}${shutter ? meterMarkup("Shutter speed", shutter, shutterPosition(shutterNumber)) : ""}${iso ? meterMarkup("ISO", iso, logPosition(isoNumber, 40, 40000)) : ""}${focal ? meterMarkup("Focal length", focal, logPosition(focalNumber, 10, 1000)) : ""}</dl></section>`; }
    function renderComponentProblems(file) { const problems = ["metadata", "thumbnail", "quality"].flatMap((name) => { const component = file.components[name]; return component && ["failed", "unsupported", "pending", "running"].includes(component.status) ? [`<p class="error">${escapeHtml(name[0].toUpperCase() + name.slice(1))}: ${escapeHtml(component.error || component.status)}</p>`] : []; }).join(""); return problems; }
    function cameraValue(file) { const exif = file.metadata?.exif || {}; return [exif.Make, exif.Model].filter((value) => value != null && readable(value)).map(formatExif).filter((value, index, values) => values.indexOf(value) === index).join(" "); }
    function rawMetadataValue(file, keys) { const exif = file.metadata?.exif || {}; const key = keys.find((candidate) => exif[candidate] != null && readable(exif[candidate])); return key ? exif[key] : null; }
    function metadataValue(file, keys) { const key = keys.find((candidate) => file.metadata?.exif?.[candidate] != null && readable(file.metadata.exif[candidate])); return key ? formatMetadataValue(key, file.metadata.exif[key]) : ""; }
    function readable(value) { return (Array.isArray(value) || typeof value !== "object") && String(value).length < 160; }
    function formatExif(value) { return Array.isArray(value) ? value.join(" / ") : String(value); }
    function formatMetadataValue(key, value) { const text = formatExif(value); if (["FNumber", "ApertureValue"].includes(key)) { const number = rationalNumber(value); return number == null ? text : `f/${numberText(number)}`; } if (key === "ExposureTime") return `${formatExposure(value)} s`; if (key === "ShutterSpeedValue") return `${text} s`; if (["ISOSpeedRatings", "PhotographicSensitivity"].includes(key)) return `ISO ${text}`; if (["FocalLength", "FocalLengthIn35mmFilm"].includes(key)) { const number = rationalNumber(value); return number == null ? `${text} mm` : `${numberText(number)} mm`; } return text; }
    function formatExposure(value) { const text = formatExif(value); const number = rationalNumber(value); if (number > 0 && number < 1) { const denominator = Math.round(1 / number); if (Math.abs(number - (1 / denominator)) < 0.000001) return `1/${denominator}`; } return text.replace(/\s*\/\s*/g, "/"); }
    function rationalNumber(value) { const text = formatExif(value); const match = text.match(/^(-?\d+(?:\.\d+)?)\s*\/\s*(-?\d+(?:\.\d+)?)$/); if (match && Number(match[2]) !== 0) return Number(match[1]) / Number(match[2]); const number = Number(text); return Number.isFinite(number) ? number : null; }
    function numberText(value) { return Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, "").replace(/\.$/, ""); }
    function formatBytes(value) { if (value == null) return "Unavailable"; if (value < 1024) return `${value} B`; if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`; return `${(value / 1024 / 1024).toFixed(1)} MB`; }
    async function loadJobs() { try { const data = await api("/api/jobs?limit=10"); const active = data.jobs.find((job) => ["pending", "running"].includes(job.status)); if (!active) { $("job-banner").classList.add("hidden"); if (state.activeJobId) { state.activeJobId = null; const workspace = await api("/api/workspace"); $("workspace-summary").textContent = `${workspace.assets} assets · ${workspace.online_files} online · ${workspace.offline_files} offline`; if (state.viewMode === "groups") await loadGroups(); else await loadAssets(); await loadProblemsBadge(); } return; } state.activeJobId = active.id; const percent = active.total_items ? (100 * active.completed_items / active.total_items).toFixed(1) : "0.0"; const elapsed = active.started_at ? Math.max((Date.now() - Date.parse(active.started_at)) / 1000, .001) : .001; const rate = (active.completed_items / elapsed).toFixed(1); $("job-banner").classList.remove("hidden"); $("job-copy").textContent = `${active.stage || active.kind} · ${active.completed_items}/${active.total_items} (${percent}%) · ${rate}/s · ${active.failed_items || 0} failed · ${active.skipped_items || 0} skipped`; $("job-progress").max = Math.max(active.total_items || 1, 1); $("job-progress").value = active.completed_items; $("cancel-job").onclick = async () => { await api(`/api/jobs/${active.id}/cancel`, { method: "POST" }); }; } catch (error) { $("status").textContent = error.message; } }
    async function loadProblemsBadge() { try { const data = await api("/api/problems?limit=500"); const count = data.problems.length; $("problems-button").classList.toggle("hidden", count === 0); $("problem-count").textContent = count ? `(${count})` : ""; } catch (error) { $("status").textContent = error.message; } }
    async function showProblems() { try { const data = await api("/api/problems?limit=100"); $("problems-dialog").innerHTML = `<div class="dialog-inner"><div class="dialog-header"><h2 id="problems-title">Problems${data.problems.length ? ` (${data.problems.length})` : ""}</h2><button id="problems-close" class="secondary" type="button">Close</button></div>${data.problems.map((problem) => `<div class="file-card"><strong>${escapeHtml(problem.relative_path || "Workspace")}</strong><div class="muted">${escapeHtml(problem.job_kind || "Processing")} · ${escapeHtml(problem.job_created_at || problem.created_at || "")}</div><p class="error">${escapeHtml(problem.message)}</p></div>`).join("") || `<div class="empty">No problems recorded.</div>`}<div class="recent-actions"><button id="problems-index" type="button">Run index again</button></div></div>`; $("problems-dialog").showModal(); document.body.classList.add("modal-open"); $("problems-close").addEventListener("click", () => closeDialog($("problems-dialog"))); $("problems-index").addEventListener("click", async () => { closeDialog($("problems-dialog")); await startIndex(); }); } catch (error) { $("status").textContent = error.message; } }
    async function startIndex() { try { await api("/api/index", { method: "POST" }); $("status").textContent = ""; await loadJobs(); } catch (error) { $("status").textContent = error.message; } }
     function setupFilters() { ["folder", "media-type", "sort-by", "direction", "selection-filter"].forEach((id) => $(id).addEventListener("change", () => { if (id === "selection-filter") state.selectionFilter = $(id).value; state.page = 1; syncUrl(); loadAssets(); })); $("page-size").addEventListener("change", () => { state.pageSize = Number($("page-size").value); localStorage.setItem("archive-index-page-size", state.pageSize); state.page = 1; syncUrl(); loadAssets(); }); let debounce; $("search").addEventListener("input", () => { clearTimeout(debounce); debounce = setTimeout(() => { state.page = 1; syncUrl(); loadAssets(); }, 250); }); }
     function setViewMode(mode, load = true) {
       state.viewMode = mode === "groups" ? "groups" : "gallery";
       const groups = state.viewMode === "groups";
       ["filters", "status", "pager-top", "gallery", "pager-bottom"].forEach((id) => $(id).classList.toggle("hidden", groups));
       $("groups-view").classList.toggle("hidden", !groups);
       $("gallery-view-toggle").setAttribute("aria-selected", String(!groups));
       $("groups-view-toggle").setAttribute("aria-selected", String(groups));
       syncUrl();
       if (load) (groups ? loadGroups() : loadAssets());
     }
     function renderGroup(group) {
       const members = group.members.map((item, index) => { const preview = item.thumbnail_url ? `<img class="group-thumb" loading="lazy" src="${item.thumbnail_url}" alt="${escapeHtml(item.filename)}" onerror="this.replaceWith(Object.assign(document.createElement('div'), {className:'group-thumb placeholder', textContent:'Preview unavailable'}))">` : `<div class="group-thumb placeholder">Preview unavailable</div>`; return `<div class="group-member${item.is_representative ? " representative" : ""}"><button class="group-photo" type="button" data-group-index="${index}" aria-label="View ${escapeHtml(item.filename)}">${preview}</button><div class="group-caption"><span title="${escapeHtml(item.filename)}">${escapeHtml(item.filename)}</span>${scoreMarkup(item.quality_score)}<div class="group-state">${selectionStateMarkup(item)}</div><div class="group-actions">${selectionActionsMarkup(item)}</div></div></div>`; }).join("");
      const memberLabel = `${group.member_count} ${group.member_count === 1 ? "member" : "members"}`;
      return `<section class="group-row" data-group-id="${escapeHtml(group.group_id)}"><div class="group-heading"><strong>${escapeHtml(group.label)}</strong><span class="muted">${memberLabel}</span><span class="muted">${escapeHtml(formatCapture(group.first_capture_time, ""))}</span></div>${members || `<div class="empty">No members</div>`}</section>`;
    }
    async function loadGroups() {
      try {
        const data = await api(`/api/groups?page=${state.groupPage}&page_size=${state.groupPageSize}`);
        $("groups-list").innerHTML = data.groups.map(renderGroup).join("") || `<div class="empty">${data.run_id ? "No groups match this view." : "Groups have not been built yet."}</div>`;
        $("groups-status").textContent = data.run_id ? "" : "Run Rebuild groups after indexing.";
        $("groups-list").querySelectorAll(".group-row").forEach((row) => {
          const group = data.groups.find((candidate) => candidate.group_id === row.dataset.groupId);
           row.querySelectorAll("[data-group-index]").forEach((button) => button.addEventListener("click", () => showViewer(Number(button.dataset.groupIndex), group.members)));
        });
        bindSelectionButtons($("groups-list"));
        const pageCount = Math.max(1, Math.ceil(data.total / data.page_size));
        const first = data.total ? ((data.page - 1) * data.page_size) + 1 : 0;
        const last = data.total ? Math.min(data.total, data.page * data.page_size) : 0;
        $("groups-page-label").textContent = `Page ${data.page} / ${pageCount}`;
        $("groups-range-label").textContent = data.total ? `${first}–${last} / ${data.total} groups` : "0 / 0 groups";
        $("groups-previous").disabled = data.page <= 1; $("groups-next").disabled = !data.has_next;
      } catch (error) { $("groups-status").textContent = error.message; }
    }
     async function rebuildGroups() { try { await api("/api/groups/rebuild", { method: "POST" }); await loadJobs(); } catch (error) { $("groups-status").textContent = error.message; } }
     async function rebuildRecommendations() { try { await api("/api/recommendations/rebuild", { method: "POST" }); await loadJobs(); } catch (error) { $("status").textContent = error.message; } }
    $("workspace-form").addEventListener("submit", (event) => { event.preventDefault(); openWorkspace($("workspace-path").value.trim(), false); });
    $("browse-workspace").addEventListener("click", pickWorkspace);
    $("index").addEventListener("click", startIndex);
    $("problems-button").addEventListener("click", showProblems);
     $("gallery-view-toggle").addEventListener("click", () => setViewMode("gallery")); $("groups-view-toggle").addEventListener("click", () => setViewMode("groups"));
     $("rebuild-groups").addEventListener("click", rebuildGroups);
     $("rebuild-recommendations").addEventListener("click", rebuildRecommendations);
    $("groups-previous").addEventListener("click", () => { state.groupPage = Math.max(1, state.groupPage - 1); loadGroups(); }); $("groups-next").addEventListener("click", () => { state.groupPage += 1; loadGroups(); });
    $("previous-top").addEventListener("click", () => changePage(-1)); $("previous-bottom").addEventListener("click", () => changePage(-1));
    $("next-top").addEventListener("click", () => changePage(1)); $("next-bottom").addEventListener("click", () => changePage(1));
    $("viewer-close").addEventListener("click", () => closeDialog($("viewer")));
    $("viewer-previous").addEventListener("click", () => moveViewer(-1)); $("viewer-next").addEventListener("click", () => moveViewer(1));
    $("viewer-smooth").addEventListener("change", (event) => { state.viewerSmooth = event.target.checked; const media = $("viewer-media").querySelector("img.viewer-media"); if (media) applyViewerZoom(media); });
    $("viewer-info").addEventListener("click", () => { const item = state.viewerItems[state.viewerIndex]; if (item) showDetails(item.asset_id); });
    $("viewer").addEventListener("click", (event) => { if (event.target === $("viewer")) closeDialog($("viewer")); });
    $("viewer-stage").addEventListener("click", (event) => { if (event.target === $("viewer-stage") || event.target === $("viewer-media")) closeDialog($("viewer")); });
    $("viewer-stage").addEventListener("wheel", (event) => {
      const media = $("viewer-media").querySelector("img.viewer-media");
      if (!media) return;
      const overImage = event.target.closest?.("img.viewer-media");
      if (event.deltaY < 0 && !overImage) return;
      event.preventDefault();
      const rect = $("viewer-stage").getBoundingClientRect();
      const pointX = event.clientX - (rect.left + rect.width / 2); const pointY = event.clientY - (rect.top + rect.height / 2);
      const next = Math.max(1, Math.min(MAX_VIEWER_ZOOM, state.viewerZoom * (event.deltaY < 0 ? 1.2 : 1 / 1.2)));
      if (next === state.viewerZoom) return;
      const zoomingIn = event.deltaY < 0;
      if (zoomingIn) {
        const contentX = (pointX - state.viewerPanX) / state.viewerZoom; const contentY = (pointY - state.viewerPanY) / state.viewerZoom;
        state.viewerZoom = next;
        state.viewerPanX = pointX - contentX * next; state.viewerPanY = pointY - contentY * next;
      } else {
        const progress = state.viewerZoom > 1 ? (next - 1) / (state.viewerZoom - 1) : 0;
        state.viewerZoom = next;
        state.viewerPanX *= progress; state.viewerPanY *= progress;
        if (next === 1) { state.viewerPanX = 0; state.viewerPanY = 0; }
      }
      applyViewerZoom(media);
    }, { passive: false });
    $("details").addEventListener("click", (event) => { if (event.target === $("details")) closeDialog($("details")); });
    $("problems-dialog").addEventListener("click", (event) => { if (event.target === $("problems-dialog")) closeDialog($("problems-dialog")); });
    ["viewer", "details", "problems-dialog"].forEach((id) => $(id).addEventListener("close", () => { if (!["viewer", "details", "problems-dialog"].some((name) => $(name).open)) document.body.classList.remove("modal-open"); }));
     document.addEventListener("keydown", (event) => { if ($("details").open || $("problems-dialog").open || ["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return; const key = event.key.toLowerCase(); if ($("viewer").open) { if ({s: "selected", r: "rejected", u: "undecided"}[key]) { event.preventDefault(); const item = state.viewerItems[state.viewerIndex]; if (item) setDecision(item.asset_id, {s: "selected", r: "rejected", u: "undecided"}[key]); return; } if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); moveViewer(event.key === "ArrowLeft" ? -1 : 1); } return; } if (state.viewMode === "gallery" && {s: "selected", r: "rejected", u: "undecided"}[key]) { const card = document.activeElement.closest?.(".photo-card"); const item = card && state.items[Number(card.dataset.index)]; if (item) { event.preventDefault(); setDecision(item.asset_id, {s: "selected", r: "rejected", u: "undecided"}[key]); } } }); setupFilters();
    if (state.workspace) { loadWorkspace().catch((error) => $("status").textContent = error.message); setInterval(() => { loadJobs(); loadProblemsBadge(); }, 1500); } else { loadHome().catch((error) => $("status").textContent = error.message); }
  
