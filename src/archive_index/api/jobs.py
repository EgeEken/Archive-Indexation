"""Background indexing job orchestration for the HTTP API."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from math import isfinite
from time import monotonic

from ..indexing.embeddings import index_embeddings
from ..indexing.grouping import build_groups, extract_visual_features
from ..indexing.media_pipeline import index_workspace
from ..indexing.raw_quality import index_raw_quality
from ..indexing.recommendation import build_recommendations
from ..indexing.reconciliation import reconcile_workspace
from ..indexing.scanner import scan
from ..indexing.projection import build_semantic_projection
from ..indexing.video_quality import index_video_quality, prepare_shared_video_quality
from ..jobs.engine import JobStore, _timestamp
from ..timing import TimingRecorder
from ..workspace import Workspace

LOGGER = logging.getLogger(__name__)

PIPELINE_STAGES = (
    "scan", "media_index", "reconciliation", "media_thumbnails", "media_quality",
    "raw_quality", "video_quality", "embeddings", "semantic_projection", "visual_features", "grouping", "recommendations",
)
PLANNER_STAGE_KEYS = {
    "scan": ("scan", "hashing"),
    "media_index": ("metadata",),
    "reconciliation": ("reconciliation",),
    "media_thumbnails": ("thumbnails",),
    "media_quality": ("rendered_quality",),
    "raw_quality": ("raw_quality",),
    "video_quality": ("video_quality",),
    "embeddings": ("embedding_initialization", "image_embeddings", "video_embeddings", "semantic_search"),
    "semantic_projection": ("semantic_projection",),
    "visual_features": ("visual_features",),
    "grouping": ("strict_grouping",),
    "recommendations": ("recommendations",),
}


@dataclass
class IndexingRunContext:
    mode: str
    started_at: str
    started_monotonic: float
    planner_estimates: dict[str, float]
    job_ids: list[str] = field(default_factory=list)
    current_job_id: str | None = None
    current_kind: str = "scan"
    stage_started_monotonic: float = 0.0
    completed_stage_seconds: float = 0.0
    last_projected_total: float | None = None


def _planner_stage_estimates(plan: dict[str, object]) -> dict[str, float]:
    eta = plan.get("eta_seconds_by_feature") or {}
    estimates = {
        stage: sum(max(0.0, float(eta.get(key, 0) or 0)) for key in keys)
        for stage, keys in PLANNER_STAGE_KEYS.items()
    }
    if not estimates["embeddings"]:
        estimates["embeddings"] = max(0.0, float(plan.get("embedding_estimated_seconds", 0) or 0))
    return estimates


def _workspace_has_indexed_media(workspace: Workspace) -> bool:
    connection = workspace.connect()
    try:
        return connection.execute("SELECT EXISTS(SELECT 1 FROM physical_file)").fetchone()[0] == 1
    finally:
        connection.close()


def _indexing_mode(workspace: Workspace) -> str:
    return "reindexing" if _workspace_has_indexed_media(workspace) else "indexing"


def _new_indexing_context(workspace: Workspace, plan: dict[str, object] | None = None) -> IndexingRunContext:
    if plan is None:
        from .workspaces import _workspace_plan
        plan = _workspace_plan(workspace, workspace.configuration())
    estimates = _planner_stage_estimates(plan)
    now = monotonic()
    return IndexingRunContext(
        mode=_indexing_mode(workspace),
        started_at=_timestamp(),
        started_monotonic=now,
        planner_estimates=estimates,
        stage_started_monotonic=now,
    )


def start_indexing(host, handle: str) -> str | None:
    _, workspace = host.resolve_workspace(handle)
    try:
        from .workspaces import _workspace_plan
        plan = _workspace_plan(workspace, workspace.configuration())
    except Exception:
        LOGGER.exception("could not prepare indexing runtime estimate")
        plan = {"eta_seconds_by_feature": {}}
    context = _new_indexing_context(workspace, plan)
    with host._active_lock:
        thread = host._active_threads.get(handle)
        if thread is not None and thread.is_alive():
            return None
        job_id = JobStore(workspace).create("scan")
        cancel_event = threading.Event()
        host._cancel_events[(handle, job_id)] = cancel_event
        context.job_ids.append(job_id)
        context.current_job_id = job_id
        host._indexing_runs[handle] = context
        thread = threading.Thread(target=run_indexing, args=(host, handle, workspace, job_id, cancel_event), name=f"archive-index-work-{handle[:8]}", daemon=True)
        host._active_threads[handle] = thread
        thread.start()
        return job_id


def _advance_indexing_stage(host, handle: str, job_id: str, kind: str) -> None:
    with host._active_lock:
        context = host._indexing_runs.get(handle)
        if context is None:
            return
        now = monotonic()
        context.completed_stage_seconds += max(0.0, now - context.stage_started_monotonic)
        context.current_job_id = job_id
        context.current_kind = kind
        context.stage_started_monotonic = now
        context.job_ids.append(job_id)


def _advance_inline_indexing_stage(host, handle: str, kind: str) -> None:
    with host._active_lock:
        context = host._indexing_runs.get(handle)
        if context is None:
            return
        now = monotonic()
        context.completed_stage_seconds += max(0.0, now - context.stage_started_monotonic)
        context.current_job_id = None
        context.current_kind = kind
        context.stage_started_monotonic = now


def indexing_runtime_status(host, handle: str, jobs: list[dict[str, object]]) -> dict[str, object] | None:
    with host._active_lock:
        context = host._indexing_runs.get(handle)
        if context is None:
            return None
        now = monotonic()
        current = next((job for job_id in reversed(context.job_ids) if (job := next((candidate for candidate in jobs if candidate["id"] == job_id), None)) and job["status"] in {"pending", "running"}), None)
        current_kind = current["kind"] if current else context.current_kind
        stage_elapsed = max(0.0, now - context.stage_started_monotonic)
        current_remaining = context.planner_estimates.get(current_kind, 0.0) if current or current_kind == "semantic_projection" else 0.0
        remaining_source = "planner"
        if current:
            substage = current.get("substage") or {}
            if substage.get("eta") is not None and isfinite(float(substage["eta"])):
                current_remaining = max(0.0, float(substage["eta"]))
                remaining_source = "substage"
            elif current.get("eta_seconds") is not None and isfinite(float(current["eta_seconds"])):
                current_remaining = max(0.0, float(current["eta_seconds"]))
                remaining_source = "job"
            elif int(current.get("total_items") or 0) > 0:
                total = int(current["total_items"])
                completed = min(total, max(0, int(current.get("completed_items") or 0)))
                current_remaining *= max(0.0, (total - completed) / total)
                remaining_source = "planner_progress"
        try:
            stage_index = PIPELINE_STAGES.index(current_kind)
        except ValueError:
            stage_index = len(PIPELINE_STAGES) - 1
        future = sum(context.planner_estimates.get(stage, 0.0) for stage in PIPELINE_STAGES[stage_index + 1:])
        elapsed = max(0.0, now - context.started_monotonic)
        raw_total = max(elapsed, context.completed_stage_seconds + stage_elapsed + current_remaining + future)
        if context.last_projected_total is not None and abs(raw_total - context.last_projected_total) < 2.0:
            projected_total = context.last_projected_total
        else:
            projected_total = raw_total
            context.last_projected_total = projected_total
        return {
            "mode": context.mode,
            "started_at": context.started_at,
            "elapsed_seconds": elapsed,
            "projected_total_seconds": max(elapsed, projected_total),
            "initial_estimated_seconds": sum(context.planner_estimates.values()),
            "current_stage": current_kind,
            "current_remaining_seconds": current_remaining,
            "remaining_source": remaining_source,
        }


def start_group_rebuild(host, handle: str) -> str | None:
    _, workspace = host.resolve_workspace(handle)
    with host._active_lock:
        thread = host._active_threads.get(handle)
        if thread is not None and thread.is_alive():
            return None
        job_id = JobStore(workspace).create("visual_features")
        cancel_event = threading.Event()
        host._cancel_events[(handle, job_id)] = cancel_event
        thread = threading.Thread(target=run_grouping_only, args=(host, handle, workspace, job_id, cancel_event), name=f"archive-index-groups-{handle[:8]}", daemon=True)
        host._active_threads[handle] = thread
        thread.start()
        return job_id


def start_recommendation_rebuild(host, handle: str) -> str | None:
    _, workspace = host.resolve_workspace(handle)
    with host._active_lock:
        thread = host._active_threads.get(handle)
        if thread is not None and thread.is_alive():
            return None
        job_id = JobStore(workspace).create("recommendations")
        cancel_event = threading.Event()
        host._cancel_events[(handle, job_id)] = cancel_event
        thread = threading.Thread(target=run_recommendation_only, args=(host, handle, workspace, job_id, cancel_event), name=f"archive-index-recommendations-{handle[:8]}", daemon=True)
        host._active_threads[handle] = thread
        thread.start()
        return job_id


def start_embedding_rebuild(host, handle: str) -> str | None:
    _, workspace = host.resolve_workspace(handle)
    with host._active_lock:
        thread = host._active_threads.get(handle)
        if thread is not None and thread.is_alive():
            return None
        job_id = JobStore(workspace).create("embeddings")
        cancel_event = threading.Event()
        host._cancel_events[(handle, job_id)] = cancel_event
        thread = threading.Thread(target=run_embeddings_only, args=(host, handle, workspace, job_id, cancel_event), name=f"archive-index-embeddings-{handle[:8]}", daemon=True)
        host._active_threads[handle] = thread
        thread.start()
        return job_id


def start_reconciliation(host, handle: str) -> str | None:
    _, workspace = host.resolve_workspace(handle)
    with host._active_lock:
        thread = host._active_threads.get(handle)
        if thread is not None and thread.is_alive():
            return None
        job_id = JobStore(workspace).create("reconciliation")
        cancel_event = threading.Event()
        host._cancel_events[(handle, job_id)] = cancel_event
        thread = threading.Thread(target=run_reconciliation_only, args=(host, handle, workspace, job_id, cancel_event), name=f"archive-index-reconciliation-{handle[:8]}", daemon=True)
        host._active_threads[handle] = thread
        thread.start()
        return job_id


def cancel_job(host, handle: str, job_id: str) -> bool:
    with host._active_lock:
        event = host._cancel_events.get((handle, job_id))
        if event is None:
            return False
        event.set()
        return True


def run_indexing(
    host,
    handle: str,
    workspace: Workspace,
    job_id: str,
    cancel_event: threading.Event,
    *,
    scan_fn=scan,
    index_workspace_fn=index_workspace,
) -> None:
    job_ids = [job_id]
    current_job_id = job_id
    timings = TimingRecorder()
    try:
        scan_result = scan_fn(workspace, job_id=job_id, cancel_event=cancel_event, timings=timings)
        if scan_result.cancelled:
            return
        media_job_id = JobStore(workspace).create("media_index")
        with host._active_lock:
            host._cancel_events[(handle, media_job_id)] = cancel_event
        job_ids.append(media_job_id)
        _advance_indexing_stage(host, handle, media_job_id, "media_index")
        current_job_id = media_job_id
        media_result = index_workspace_fn(workspace, components=("metadata",), job_id=media_job_id, cancel_event=cancel_event, timings=timings)
        if media_result.cancelled:
            return
        reconciliation_job_id = JobStore(workspace).create("reconciliation")
        with host._active_lock:
            host._cancel_events[(handle, reconciliation_job_id)] = cancel_event
        job_ids.append(reconciliation_job_id)
        _advance_indexing_stage(host, handle, reconciliation_job_id, "reconciliation")
        current_job_id = reconciliation_job_id
        if reconcile_workspace(workspace, job_id=reconciliation_job_id, cancel_event=cancel_event).cancelled:
            return
        thumbnail_job_id = JobStore(workspace).create("media_thumbnails")
        with host._active_lock:
            host._cancel_events[(handle, thumbnail_job_id)] = cancel_event
        job_ids.append(thumbnail_job_id)
        _advance_indexing_stage(host, handle, thumbnail_job_id, "media_thumbnails")
        current_job_id = thumbnail_job_id
        if index_workspace_fn(workspace, components=("thumbnail",), job_id=thumbnail_job_id, cancel_event=cancel_event, timings=timings).cancelled:
            return
        quality_job_id = JobStore(workspace).create("media_quality")
        with host._active_lock:
            host._cancel_events[(handle, quality_job_id)] = cancel_event
        job_ids.append(quality_job_id)
        _advance_indexing_stage(host, handle, quality_job_id, "media_quality")
        current_job_id = quality_job_id
        if index_workspace_fn(workspace, components=("quality",), job_id=quality_job_id, cancel_event=cancel_event, timings=timings).cancelled:
            return
        raw_quality_job_id = JobStore(workspace).create("raw_quality")
        with host._active_lock:
            host._cancel_events[(handle, raw_quality_job_id)] = cancel_event
        job_ids.append(raw_quality_job_id)
        _advance_indexing_stage(host, handle, raw_quality_job_id, "raw_quality")
        current_job_id = raw_quality_job_id
        if index_raw_quality(workspace, job_id=raw_quality_job_id, cancel_event=cancel_event).cancelled:
            return
        video_quality_job_id = JobStore(workspace).create("video_quality")
        with host._active_lock:
            host._cancel_events[(handle, video_quality_job_id)] = cancel_event
        job_ids.append(video_quality_job_id)
        _advance_indexing_stage(host, handle, video_quality_job_id, "video_quality")
        current_job_id = video_quality_job_id
        shared_video_quality = None
        configuration = workspace.configuration()
        if configuration["include_videos_in_semantic_search"] and configuration["semantic_search_enabled"]:
            shared_video_quality = prepare_shared_video_quality(workspace, job_id=video_quality_job_id, cancel_event=cancel_event, timings=timings)
        if shared_video_quality is None and index_video_quality(workspace, job_id=video_quality_job_id, cancel_event=cancel_event).cancelled:
            return
        embedding_job_id = JobStore(workspace).create("embeddings")
        with host._active_lock:
            host._cancel_events[(handle, embedding_job_id)] = cancel_event
        job_ids.append(embedding_job_id)
        _advance_indexing_stage(host, handle, embedding_job_id, "embeddings")
        current_job_id = embedding_job_id
        embedding_result = index_embeddings(workspace, job_id=embedding_job_id, cancel_event=cancel_event, video_frame_consumer=shared_video_quality.consume if shared_video_quality is not None else None)
        if embedding_result.cancelled:
            if shared_video_quality is not None:
                shared_video_quality.finish()
            return
        if shared_video_quality is not None:
            shared_video_quality.finish()
        _advance_inline_indexing_stage(host, handle, "semantic_projection")
        projection_started = monotonic()
        with timings.measure("semantic_projection.total"):
            projection_result = build_semantic_projection(workspace)
        projection_assets = int(projection_result.get("asset_count", 0) or 0)
        timings.add("semantic_projection.rate_sample", monotonic() - projection_started, projection_assets)
        if projection_result.get("status") == "failed":
            LOGGER.warning("semantic projection unavailable after indexing: %s", projection_result.get("reason"))
        feature_job_id = JobStore(workspace).create("visual_features")
        with host._active_lock:
            host._cancel_events[(handle, feature_job_id)] = cancel_event
        job_ids.append(feature_job_id)
        _advance_indexing_stage(host, handle, feature_job_id, "visual_features")
        current_job_id = feature_job_id
        with timings.measure("visual_features.total"):
            if extract_visual_features(workspace, job_id=feature_job_id, cancel_event=cancel_event, timings=timings).cancelled:
                return
        group_job_id = JobStore(workspace).create("grouping")
        with host._active_lock:
            host._cancel_events[(handle, group_job_id)] = cancel_event
        job_ids.append(group_job_id)
        _advance_indexing_stage(host, handle, group_job_id, "grouping")
        current_job_id = group_job_id
        connection = workspace.connect()
        try:
            grouping_asset_count = connection.execute("SELECT COUNT(DISTINCT logical_asset_id) FROM physical_file WHERE media_type = 'image' AND in_scope = 1 AND is_online = 1").fetchone()[0]
        finally:
            connection.close()
        grouping_started = monotonic()
        with timings.measure("grouping.total"):
            if build_groups(workspace, job_id=group_job_id, cancel_event=cancel_event).cancelled:
                return
        timings.add("strict_grouping.rate_sample", monotonic() - grouping_started, grouping_asset_count)
        recommendation_job_id = JobStore(workspace).create("recommendations")
        with host._active_lock:
            host._cancel_events[(handle, recommendation_job_id)] = cancel_event
        job_ids.append(recommendation_job_id)
        _advance_indexing_stage(host, handle, recommendation_job_id, "recommendations")
        current_job_id = recommendation_job_id
        with timings.measure("recommendations.total"):
            if build_recommendations(workspace, job_id=recommendation_job_id, cancel_event=cancel_event).cancelled:
                return
    except Exception:
        LOGGER.exception("workspace indexing failed")
        try:
            row = JobStore(workspace).get(current_job_id)
            if row is not None and row["status"] in {"pending", "running"}:
                JobStore(workspace).fail(current_job_id)
        except Exception:
            LOGGER.exception("could not mark workspace job failed")
    finally:
        LOGGER.info("workspace indexing timings: %s", timings.summary())
        try:
            JobStore(workspace).set_timing_summary(job_id, timings.summary())
        except Exception:
            LOGGER.exception("could not persist workspace indexing timings")
        with host._active_lock:
            for active_job_id in job_ids:
                host._cancel_events.pop((handle, active_job_id), None)
            if host._active_threads.get(handle) is threading.current_thread():
                host._active_threads.pop(handle, None)
            if host._indexing_runs.get(handle) is not None:
                host._indexing_runs.pop(handle, None)


def run_embeddings_only(host, handle: str, workspace: Workspace, job_id: str, cancel_event: threading.Event) -> None:
    try:
        result = index_embeddings(workspace, job_id=job_id, cancel_event=cancel_event)
        if not result.cancelled:
            projection_result = build_semantic_projection(workspace)
            if projection_result.get("status") == "failed":
                LOGGER.warning("semantic projection unavailable after embedding rebuild: %s", projection_result.get("reason"))
    except Exception:
        LOGGER.exception("embedding rebuild failed")
        _fail_if_running(workspace, job_id, "could not mark embedding job failed")
    finally:
        _finish_thread(host, handle, job_id)


def run_grouping_only(host, handle: str, workspace: Workspace, feature_job_id: str, cancel_event: threading.Event) -> None:
    job_ids = [feature_job_id]
    current_job_id = feature_job_id
    try:
        if extract_visual_features(workspace, job_id=feature_job_id, cancel_event=cancel_event).cancelled:
            return
        group_job_id = JobStore(workspace).create("grouping")
        with host._active_lock:
            host._cancel_events[(handle, group_job_id)] = cancel_event
        job_ids.append(group_job_id)
        current_job_id = group_job_id
        if build_groups(workspace, job_id=group_job_id, cancel_event=cancel_event).cancelled:
            return
        recommendation_job_id = JobStore(workspace).create("recommendations")
        with host._active_lock:
            host._cancel_events[(handle, recommendation_job_id)] = cancel_event
        job_ids.append(recommendation_job_id)
        current_job_id = recommendation_job_id
        if build_recommendations(workspace, job_id=recommendation_job_id, cancel_event=cancel_event).cancelled:
            return
    except Exception:
        LOGGER.exception("workspace grouping failed")
        _fail_if_running(workspace, current_job_id, "could not mark grouping job failed")
    finally:
        with host._active_lock:
            for active_job_id in job_ids:
                host._cancel_events.pop((handle, active_job_id), None)
            if host._active_threads.get(handle) is threading.current_thread():
                host._active_threads.pop(handle, None)


def run_recommendation_only(host, handle: str, workspace: Workspace, job_id: str, cancel_event: threading.Event) -> None:
    try:
        build_recommendations(workspace, job_id=job_id, cancel_event=cancel_event)
    except Exception:
        LOGGER.exception("workspace recommendation rebuild failed")
        _fail_if_running(workspace, job_id, "could not mark recommendation job failed")
    finally:
        _finish_thread(host, handle, job_id)


def run_reconciliation_only(host, handle: str, workspace: Workspace, job_id: str, cancel_event: threading.Event) -> None:
    try:
        reconcile_workspace(workspace, job_id=job_id, cancel_event=cancel_event)
    except Exception:
        LOGGER.exception("workspace reconciliation failed")
        _fail_if_running(workspace, job_id, "could not mark reconciliation job failed")
    finally:
        _finish_thread(host, handle, job_id)


def _fail_if_running(workspace: Workspace, job_id: str, message: str) -> None:
    try:
        row = JobStore(workspace).get(job_id)
        if row is not None and row["status"] in {"pending", "running"}:
            JobStore(workspace).fail(job_id)
    except Exception:
        LOGGER.exception(message)


def _finish_thread(host, handle: str, job_id: str) -> None:
    with host._active_lock:
        host._cancel_events.pop((handle, job_id), None)
        if host._active_threads.get(handle) is threading.current_thread():
            host._active_threads.pop(handle, None)
