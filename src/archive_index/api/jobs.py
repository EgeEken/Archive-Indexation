"""Background indexing job orchestration for the HTTP API."""

from __future__ import annotations

import logging
import threading

from ..indexing.embeddings import index_embeddings
from ..indexing.grouping import build_groups, extract_visual_features
from ..indexing.media_pipeline import index_workspace
from ..indexing.raw_quality import index_raw_quality
from ..indexing.recommendation import build_recommendations
from ..indexing.reconciliation import reconcile_workspace
from ..indexing.scanner import scan
from ..indexing.video_quality import index_video_quality, prepare_shared_video_quality
from ..jobs.engine import JobStore
from ..timing import TimingRecorder
from ..workspace import Workspace

LOGGER = logging.getLogger(__name__)


def start_indexing(host, handle: str) -> str | None:
    _, workspace = host.resolve_workspace(handle)
    with host._active_lock:
        thread = host._active_threads.get(handle)
        if thread is not None and thread.is_alive():
            return None
        job_id = JobStore(workspace).create("scan")
        cancel_event = threading.Event()
        host._cancel_events[(handle, job_id)] = cancel_event
        thread = threading.Thread(target=run_indexing, args=(host, handle, workspace, job_id, cancel_event), name=f"archive-index-work-{handle[:8]}", daemon=True)
        host._active_threads[handle] = thread
        thread.start()
        return job_id


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
        current_job_id = media_job_id
        media_result = index_workspace_fn(workspace, components=("metadata",), job_id=media_job_id, cancel_event=cancel_event, timings=timings)
        if media_result.cancelled:
            return
        reconciliation_job_id = JobStore(workspace).create("reconciliation")
        with host._active_lock:
            host._cancel_events[(handle, reconciliation_job_id)] = cancel_event
        job_ids.append(reconciliation_job_id)
        current_job_id = reconciliation_job_id
        if reconcile_workspace(workspace, job_id=reconciliation_job_id, cancel_event=cancel_event).cancelled:
            return
        thumbnail_job_id = JobStore(workspace).create("media_thumbnails")
        with host._active_lock:
            host._cancel_events[(handle, thumbnail_job_id)] = cancel_event
        job_ids.append(thumbnail_job_id)
        current_job_id = thumbnail_job_id
        if index_workspace_fn(workspace, components=("thumbnail",), job_id=thumbnail_job_id, cancel_event=cancel_event, timings=timings).cancelled:
            return
        quality_job_id = JobStore(workspace).create("media_quality")
        with host._active_lock:
            host._cancel_events[(handle, quality_job_id)] = cancel_event
        job_ids.append(quality_job_id)
        current_job_id = quality_job_id
        if index_workspace_fn(workspace, components=("quality",), job_id=quality_job_id, cancel_event=cancel_event, timings=timings).cancelled:
            return
        raw_quality_job_id = JobStore(workspace).create("raw_quality")
        with host._active_lock:
            host._cancel_events[(handle, raw_quality_job_id)] = cancel_event
        job_ids.append(raw_quality_job_id)
        current_job_id = raw_quality_job_id
        if index_raw_quality(workspace, job_id=raw_quality_job_id, cancel_event=cancel_event).cancelled:
            return
        video_quality_job_id = JobStore(workspace).create("video_quality")
        with host._active_lock:
            host._cancel_events[(handle, video_quality_job_id)] = cancel_event
        job_ids.append(video_quality_job_id)
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
        current_job_id = embedding_job_id
        embedding_result = index_embeddings(workspace, job_id=embedding_job_id, cancel_event=cancel_event, video_frame_consumer=shared_video_quality.consume if shared_video_quality is not None else None)
        if embedding_result.cancelled:
            if shared_video_quality is not None:
                shared_video_quality.finish()
            return
        if shared_video_quality is not None:
            shared_video_quality.finish()
        feature_job_id = JobStore(workspace).create("visual_features")
        with host._active_lock:
            host._cancel_events[(handle, feature_job_id)] = cancel_event
        job_ids.append(feature_job_id)
        current_job_id = feature_job_id
        with timings.measure("visual_features.total"):
            if extract_visual_features(workspace, job_id=feature_job_id, cancel_event=cancel_event, timings=timings).cancelled:
                return
        group_job_id = JobStore(workspace).create("grouping")
        with host._active_lock:
            host._cancel_events[(handle, group_job_id)] = cancel_event
        job_ids.append(group_job_id)
        current_job_id = group_job_id
        with timings.measure("grouping.total"):
            if build_groups(workspace, job_id=group_job_id, cancel_event=cancel_event).cancelled:
                return
        recommendation_job_id = JobStore(workspace).create("recommendations")
        with host._active_lock:
            host._cancel_events[(handle, recommendation_job_id)] = cancel_event
        job_ids.append(recommendation_job_id)
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


def run_embeddings_only(host, handle: str, workspace: Workspace, job_id: str, cancel_event: threading.Event) -> None:
    try:
        index_embeddings(workspace, job_id=job_id, cancel_event=cancel_event)
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
