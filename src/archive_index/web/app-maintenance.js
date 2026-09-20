async function loadJobs() {
  const requestId = ++state.jobsRequest;
  try {
    const data = await api("/api/jobs?limit=10");
    if (requestId !== state.jobsRequest) return;
    const revision = JSON.stringify(data.revision);
    if (state.browserRevision && state.browserRevision !== revision) state.renderKeys = {};
    state.browserRevision = revision;
    if (!$("search").value.trim() && !$("workspace-view").classList.contains("hidden")) {
      const readiness = await api("/api/search-status");
      if (requestId !== state.jobsRequest) return;
      if (!$("search").value.trim()) showSearchStatus(readiness);
    }
    const active = data.jobs.find((job) => ["pending", "running"].includes(job.status));
    if (!active) {
      $("job-banner").classList.add("hidden");
      if (state.activeJobId) {
        state.activeJobId = null;
        await api("/api/workspace");
        if (requestId !== state.jobsRequest) return;
        await loadVisualizationCapabilities();
        if (requestId !== state.jobsRequest) return;
        state.renderKeys = {};
        await loadCurrentView();
        await loadProblemsBadge();
      }
      return;
    }
    state.activeJobId = active.id;
    const percent = active.total_items ? (100 * active.completed_items / active.total_items).toFixed(1) : "0.0";
    const elapsed = active.started_at ? Math.max((Date.now() - Date.parse(active.started_at)) / 1000, .001) : .001;
    const rate = (active.completed_items / elapsed).toFixed(1);
    const jobEta = active.eta_seconds == null ? "" : ` · ETA ${formatEta(active.eta_seconds)}`;
    $("job-banner").classList.remove("hidden");
    $("job-copy").textContent = `${active.stage || active.kind} · ${active.completed_items}/${active.total_items} (${percent}%) · ${rate}/s${jobEta} · ${active.failed_items || 0} failed · ${active.skipped_items || 0} skipped`;
    if (active.substage) {
      const sub = active.substage;
      const outerCurrent = sub.outer_total ? sub.outer_completed : active.completed_items;
      const outerTotal = sub.outer_total || active.total_items;
      const frameRate = sub.rate == null ? "" : ` · ${sub.rate.toFixed(1)} frames/s`;
      const stageEtaSeconds = sub.eta ?? active.eta_seconds;
      const stageEta = stageEtaSeconds == null ? "" : ` · ETA ${formatEta(stageEtaSeconds)}`;
      $("job-copy").textContent = `${active.kind.replaceAll("_", " ")} · ${outerCurrent}/${outerTotal} videos · ${sub.item || ""} — ${sub.stage} · ${sub.current}/${sub.total}${frameRate}${stageEta}`;
    }
    $("job-progress").max = Math.max(active.total_items || 1, 1);
    $("job-progress").value = active.completed_items;
    $("cancel-job").onclick = async () => { await api(`/api/jobs/${active.id}/cancel`, { method: "POST" }); };
  } catch (error) {
    showToast(`Job status request failed: ${error.message}`);
  }
}

async function loadProblemsBadge() {
  try {
    const data = await api("/api/problems?limit=500");
    $("problems-button").classList.remove("hidden");
    $("problem-count").textContent = data.problems.length ? `(${data.problems.length})` : "";
  } catch (error) {
    showToast(`Diagnostics request failed: ${error.message}`);
  }
}

async function showProblems() {
  try {
    const data = await api("/api/problems?limit=100");
    $("problems-dialog").innerHTML = `<div class="dialog-inner"><div class="dialog-header"><h2 id="problems-title">Problems${data.problems.length ? ` (${data.problems.length})` : ""}</h2><button id="problems-close" class="secondary" type="button">Close</button></div>${data.problems.map((problem) => `<div class="file-card"><strong>${escapeHtml(problem.relative_path || "Workspace")}</strong><div class="muted">${escapeHtml(problem.job_kind || "Processing")} · ${escapeHtml(problem.job_created_at || problem.created_at || "")}</div><p class="error">${escapeHtml(problem.message)}</p></div>`).join("") || `<div class="empty">No problems recorded.</div>`}<div class="recent-actions"><button id="problems-index" type="button">Run index again</button></div></div>`;
    $("problems-dialog").showModal();
    document.body.classList.add("modal-open");
    $("problems-close").addEventListener("click", () => closeDialog($("problems-dialog")));
    $("problems-index").addEventListener("click", async () => { closeDialog($("problems-dialog")); await startIndex(); });
  } catch (error) {
    showToast(`Diagnostics request failed: ${error.message}`);
  }
}

async function showOfflineCleanup() {
  try {
    const data = await api("/api/offline-media");
    if (!data.count) {
      showToast("There is no offline media to forget.");
      return;
    }
    const noun = data.count === 1 ? "entry" : "entries";
    $("offline-dialog").innerHTML = `<div class="dialog-inner removal-dialog"><div class="dialog-header"><h2 id="offline-title">Forget ${data.count} offline media ${noun}?</h2><button id="offline-close" class="icon" type="button" aria-label="Close">×</button></div><p>${data.images} image${data.images === 1 ? "" : "s"} · ${data.videos} video${data.videos === 1 ? "" : "s"}</p><p class="muted">This does not delete files from disk.</p><div class="removal-actions"><button id="offline-cancel" class="secondary" type="button">Cancel</button><button id="offline-forget" class="danger-button" type="button">Forget</button></div></div>`;
    const dialog = $("offline-dialog");
    dialog.showModal();
    document.body.classList.add("modal-open");
    $("offline-close").addEventListener("click", () => closeDialog(dialog));
    $("offline-cancel").addEventListener("click", () => closeDialog(dialog));
    $("offline-forget").addEventListener("click", forgetOfflineMedia);
  } catch (error) {
    showToast(`Offline media request failed: ${error.message}`);
  }
}

async function forgetOfflineMedia() {
  try {
    const result = await api("/api/offline-media/forget", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    closeDialog($("offline-dialog"));
    state.renderKeys = {};
    await renderSetupPlan();
    await loadProblemsBadge();
    showToast(`${result.removed} offline media ${result.removed === 1 ? "entry" : "entries"} forgotten.`);
  } catch (error) {
    showToast(`Offline media cleanup failed: ${error.message}`);
  }
}

async function startIndex() {
  try { await api("/api/index", { method: "POST" }); $("status").textContent = ""; await loadJobs(); }
  catch (error) { showToast(`Index request failed: ${error.message}`); }
}
