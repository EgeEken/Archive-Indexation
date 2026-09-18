"""Loopback-only localhost API and UI server."""

from __future__ import annotations

import importlib.util
import json
import logging
import mimetypes
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from io import BytesIO
from collections.abc import Mapping
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from ..app_state import WorkspaceRegistry, workspace_id
from ..configuration import configuration_from_connection, default_configuration, normalize_configuration, path_in_scope
from ..embeddings.models import (
    OPENCLIP_PROVIDER,
    SIGLIP_PROVIDER,
    model_status,
)
from ..embeddings.search import SearchResult, active_embedding, search_similar, search_text, provider_state, prepare_provider, request_text, select_provider
from ..indexing.embeddings import EMBEDDING_ESTIMATE_SECONDS_PER_VECTOR, index_embeddings
from ..indexing.grouping import build_groups, extract_visual_features
from ..indexing.media_pipeline import index_workspace
from ..indexing.raw_quality import index_raw_quality
from ..indexing.video_quality import index_video_quality, video_quality_details
from ..indexing.recommendation import build_recommendations
from ..indexing.reconciliation import reconcile_workspace
from ..indexing.scanner import scan
from ..media.quality_provider import LAR_IQA_MODEL_ID, LAR_IQA_MODEL_FILENAME, default_model_path
from ..media.raw_preview import extract_embedded_preview
from ..media.metadata import UnsupportedDecoderError
from ..media_types import is_raw_extension
from ..timing import TimingRecorder
from ..jobs.engine import JobStore, SUBSTAGES
from ..planning import analyze_folder, plan_from_analysis
from ..workspace import Workspace, WorkspaceError
from ..indexing.representations import preferred_physical

LOGGER = logging.getLogger(__name__)
MAX_PAGE_SIZE = 180
_folder_picker_lock = threading.Lock()


class WorkspaceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, workspace: Workspace | None = None, registry_path: Path | None = None):
        super().__init__(address, ArchiveRequestHandler)
        self.workspace = workspace
        self.registry = WorkspaceRegistry(registry_path)
        self._workspaces: dict[str, Workspace] = {}
        self.default_handle: str | None = None
        self._active_lock = threading.Lock()
        self._active_threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[tuple[str, str], threading.Event] = {}
        self._recovered_workspaces: set[str] = set()
        if workspace is not None:
            self.default_handle = self._register_workspace(workspace)

    def stop_background_jobs(self, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._active_lock:
                events = list(set(self._cancel_events.values()))
                threads = list(self._active_threads.values())
            for event in events:
                event.set()
            alive = [thread for thread in threads if thread.is_alive()]
            if not alive:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                LOGGER.warning("workspace jobs did not stop before server shutdown")
                return False
            for thread in alive:
                thread.join(timeout=min(0.25, remaining))

    def _register_workspace(self, workspace: Workspace) -> str:
        handle = workspace_id(workspace)
        self._workspaces[handle] = workspace
        if handle not in self._recovered_workspaces:
            JobStore(workspace).recover_interrupted()
            self._recovered_workspaces.add(handle)
        return handle

    def resolve_workspace(self, handle: str | None) -> tuple[str, Workspace]:
        selected = handle or self.default_handle
        if not selected:
            raise InvalidRequest("select a workspace first")
        workspace = self._workspaces.get(selected)
        if workspace is None:
            try:
                workspace = self.registry.open(selected)
            except WorkspaceError as error:
                raise ResourceNotFound("workspace is unavailable") from error
            self._register_workspace(workspace)
        return selected, workspace

    def list_workspaces(self) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        seen: set[str] = set()
        if self.workspace is not None:
            handle = self._register_workspace(self.workspace)
            entries.append(_workspace_entry(handle, self.workspace, recent=False))
            seen.add(handle)
        for entry in self.registry.entries():
            handle = entry.get("id")
            path = entry.get("path")
            if not isinstance(handle, str) or not isinstance(path, str) or handle in seen:
                continue
            try:
                workspace = self._workspaces.get(handle)
                entries.append(
                    _workspace_entry(handle, workspace, recent=True)
                    if workspace is not None
                    else _read_only_workspace_entry(handle, path)
                )
            except (WorkspaceError, OSError, sqlite3.Error, ValueError):
                entries.append({"id": handle, "name": Path(path).name, "path": path, "available": False})
            seen.add(handle)
        return entries

    def open_workspace(self, path: str, create: bool = False) -> dict[str, object]:
        if not isinstance(path, str) or not path.strip():
            raise InvalidRequest("a workspace folder path is required")
        root = Path(path).expanduser().resolve(strict=False)
        indexed = (root / ".archive-index").is_dir()
        try:
            workspace = Workspace.open(root) if indexed else None
        except (WorkspaceError, OSError) as error:
            raise InvalidRequest(str(error)) from error
        if workspace is None:
            try:
                analysis = analyze_folder(root)
            except (ValueError, OSError) as error:
                raise InvalidRequest(str(error)) from error
            return {
                "workspace": None,
                "job_id": None,
                "setup": {
                    "path": str(root),
                    "indexed": False,
                    "analysis": analysis,
                    "configuration": default_configuration(),
                },
            }
        handle = self._register_workspace(workspace)
        self.registry.add(workspace)
        return {"workspace": _workspace_entry(handle, workspace, recent=True), "job_id": None}

    def analyze_workspace(self, path: str) -> dict[str, object]:
        if not isinstance(path, str) or not path.strip():
            raise InvalidRequest("a workspace folder path is required")
        root = Path(path).expanduser().resolve(strict=False)
        if not root.is_dir() or root.name.casefold() == ".archive-index":
            raise InvalidRequest(f"folder is not available: {root}")
        try:
            analysis = analyze_folder(root)
        except (ValueError, OSError) as error:
            raise InvalidRequest(str(error)) from error
        indexed = (root / ".archive-index").is_dir()
        configuration = default_configuration()
        handle = None
        if indexed:
            try:
                workspace = next((w for w in self._workspaces.values() if w.root == root), None) or Workspace.open(root)
                handle = self._register_workspace(workspace)
                configuration = workspace.configuration()
            except (WorkspaceError, OSError) as error:
                raise InvalidRequest(str(error)) from error
        return {"path": str(root), "indexed": indexed, "workspace": handle, "analysis": analysis, "configuration": configuration}

    def plan_workspace_configuration(
        self,
        path: str,
        configuration: dict[str, object],
        analysis: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if not isinstance(path, str) or not path.strip():
            raise InvalidRequest("a workspace folder path is required")
        root = Path(path).expanduser().resolve(strict=False)
        try:
            normalized = normalize_configuration(configuration, root)
            source_analysis = analysis if analysis is not None else analyze_folder(root)
            if not isinstance(source_analysis, dict):
                raise ValueError("analysis must be an object")
            if source_analysis.get("path") != str(root):
                raise ValueError("analysis does not belong to this workspace folder")
            if not isinstance(source_analysis.get("root"), dict):
                raise ValueError("analysis is incomplete")
            existing_workspace = (
                (next((w for w in self._workspaces.values() if w.root == root), None) or Workspace.open(root)) if (root / ".archive-index").is_dir() else None
            )
            plan = plan_from_analysis(source_analysis, normalized, existing_workspace)
            plan.update(_configuration_quality_readiness(normalized))
            embedding_plan = _embedding_plan(normalized, existing_workspace, plan)
            plan.update(embedding_plan)
            eta = dict(plan.get("eta_seconds_by_feature", {}))
            eta["semantic_search"] = (
                embedding_plan["embedding_estimated_seconds"]
                if normalized["semantic_search_enabled"]
                else 0
            )
            for key in ("embedding_initialization", "image_embeddings", "video_embeddings"):
                eta.pop(key, None)
            plan["eta_seconds_by_feature"] = eta
            plan["estimated_seconds"] = round(sum(eta.values()), 1)
        except (ValueError, OSError) as error:
            raise InvalidRequest(str(error)) from error
        return {"path": str(root), "configuration": normalized, "plan": plan}

    def apply_workspace_configuration(
        self,
        configuration: dict[str, object],
        *,
        handle: str | None = None,
        path: str | None = None,
    ) -> dict[str, object]:
        if handle:
            selected, workspace = self.resolve_workspace(handle)
        else:
            if not isinstance(path, str) or not path.strip():
                raise InvalidRequest("a workspace folder path is required")
            root = Path(path).expanduser().resolve(strict=False)
            try:
                workspace = Workspace.open(root) if (root / ".archive-index").is_dir() else Workspace.create(root)
            except (WorkspaceError, OSError) as error:
                raise InvalidRequest(str(error)) from error
            selected = self._register_workspace(workspace)
        try:
            config = workspace.apply_configuration(configuration)
        except (ValueError, WorkspaceError) as error:
            raise InvalidRequest(str(error)) from error
        _prepare_search(workspace)
        self.registry.add(workspace)
        job_id = self.start_indexing(selected)
        return {"workspace": _workspace_entry(selected, workspace, recent=True), "configuration": config, "job_id": job_id}

    def remove_workspace(self, handle: str) -> bool:
        return self.registry.remove(handle)

    def workspace_removal_info(self, handle: str) -> dict[str, object]:
        entry = next((entry for entry in self.registry.entries() if entry.get("id") == handle), None)
        if entry is None:
            raise ResourceNotFound("workspace is unavailable")
        try:
            _, workspace = self.resolve_workspace(handle)
            if not workspace.root.is_dir() or not workspace.database_path.is_file():
                raise WorkspaceError("workspace database is unavailable")
            index_size_bytes = _index_size_bytes(workspace.index_directory)
            active_job = _active_job(workspace)
        except (ResourceNotFound, WorkspaceError, OSError, sqlite3.Error, InvalidRequest):
            self._workspaces.pop(handle, None)
            return {"workspace": handle, "index_size_bytes": 0, "active_job": None, "available": False}
        return {
            "workspace": handle,
            "index_size_bytes": index_size_bytes,
            "active_job": active_job,
            "available": True,
        }

    def remove_workspace_with_index(self, handle: str, delete_index: bool) -> bool:
        if not delete_index:
            self._workspaces.pop(handle, None)
            return self.registry.remove(handle)
        entry = next((entry for entry in self.registry.entries() if entry.get("id") == handle), None)
        if entry is None:
            return False
        path = entry.get("path")
        if not isinstance(path, str):
            self._workspaces.pop(handle, None)
            return self.registry.remove(handle)
        root = Path(path)
        if not root.is_dir() or not (root / ".archive-index" / "index.sqlite").is_file():
            self._workspaces.pop(handle, None)
            return self.registry.remove(handle)
        try:
            _, workspace = self.resolve_workspace(handle)
        except (ResourceNotFound, WorkspaceError, OSError, sqlite3.Error):
            self._workspaces.pop(handle, None)
            return self.registry.remove(handle)
        with self._active_lock:
            thread = self._active_threads.get(handle)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        with self._active_lock:
            if thread is not None and thread.is_alive():
                raise InvalidRequest("the workspace index is still shutting down")
        try:
            active = _active_job(workspace)
        except (OSError, sqlite3.Error):
            self._workspaces.pop(handle, None)
            return self.registry.remove(handle)
        if active is not None:
            raise InvalidRequest("the workspace index cannot be deleted while a job is running")
        _delete_owned_index(workspace)
        self._workspaces.pop(handle, None)
        self._recovered_workspaces.discard(handle)
        return self.registry.remove(handle)

    def start_indexing(self, handle: str) -> str | None:
        _, workspace = self.resolve_workspace(handle)
        with self._active_lock:
            thread = self._active_threads.get(handle)
            if thread is not None and thread.is_alive():
                return None
            job_id = JobStore(workspace).create("scan")
            cancel_event = threading.Event()
            self._cancel_events[(handle, job_id)] = cancel_event
            thread = threading.Thread(
                target=self._run_indexing,
                args=(handle, workspace, job_id, cancel_event),
                name=f"archive-index-work-{handle[:8]}",
                daemon=True,
            )
            self._active_threads[handle] = thread
            thread.start()
            return job_id

    def start_group_rebuild(self, handle: str) -> str | None:
        _, workspace = self.resolve_workspace(handle)
        with self._active_lock:
            thread = self._active_threads.get(handle)
            if thread is not None and thread.is_alive():
                return None
            feature_job_id = JobStore(workspace).create("visual_features")
            cancel_event = threading.Event()
            self._cancel_events[(handle, feature_job_id)] = cancel_event
            thread = threading.Thread(
                target=self._run_grouping_only,
                args=(handle, workspace, feature_job_id, cancel_event),
                name=f"archive-index-groups-{handle[:8]}",
                daemon=True,
            )
            self._active_threads[handle] = thread
            thread.start()
            return feature_job_id

    def start_recommendation_rebuild(self, handle: str) -> str | None:
        _, workspace = self.resolve_workspace(handle)
        with self._active_lock:
            thread = self._active_threads.get(handle)
            if thread is not None and thread.is_alive():
                return None
            job_id = JobStore(workspace).create("recommendations")
            cancel_event = threading.Event()
            self._cancel_events[(handle, job_id)] = cancel_event
            thread = threading.Thread(
                target=self._run_recommendation_only,
                args=(handle, workspace, job_id, cancel_event),
                name=f"archive-index-recommendations-{handle[:8]}",
                daemon=True,
            )
            self._active_threads[handle] = thread
            thread.start()
            return job_id

    def start_embedding_rebuild(self, handle: str) -> str | None:
        _, workspace = self.resolve_workspace(handle)
        with self._active_lock:
            thread = self._active_threads.get(handle)
            if thread is not None and thread.is_alive():
                return None
            job_id = JobStore(workspace).create("embeddings")
            cancel_event = threading.Event()
            self._cancel_events[(handle, job_id)] = cancel_event
            thread = threading.Thread(
                target=self._run_embeddings_only,
                args=(handle, workspace, job_id, cancel_event),
                name=f"archive-index-embeddings-{handle[:8]}",
                daemon=True,
            )
            self._active_threads[handle] = thread
            thread.start()
            return job_id

    def start_reconciliation(self, handle: str) -> str | None:
        _, workspace = self.resolve_workspace(handle)
        with self._active_lock:
            thread = self._active_threads.get(handle)
            if thread is not None and thread.is_alive():
                return None
            job_id = JobStore(workspace).create("reconciliation")
            cancel_event = threading.Event()
            self._cancel_events[(handle, job_id)] = cancel_event
            thread = threading.Thread(
                target=self._run_reconciliation_only,
                args=(handle, workspace, job_id, cancel_event),
                name=f"archive-index-reconciliation-{handle[:8]}",
                daemon=True,
            )
            self._active_threads[handle] = thread
            thread.start()
            return job_id

    def cancel_job(self, handle: str, job_id: str) -> bool:
        with self._active_lock:
            event = self._cancel_events.get((handle, job_id))
            if event is None:
                return False
            event.set()
            return True

    def _run_indexing(
        self,
        handle: str,
        workspace: Workspace,
        job_id: str,
        cancel_event: threading.Event,
    ) -> None:
        job_ids = [job_id]
        current_job_id = job_id
        timings = TimingRecorder()
        try:
            scan_result = scan(workspace, job_id=job_id, cancel_event=cancel_event, timings=timings)
            if scan_result.cancelled:
                return
            media_job_id = JobStore(workspace).create("media_index")
            with self._active_lock:
                self._cancel_events[(handle, media_job_id)] = cancel_event
            job_ids.append(media_job_id)
            current_job_id = media_job_id
            media_result = index_workspace(
                workspace,
                components=("metadata",),
                job_id=media_job_id,
                cancel_event=cancel_event,
                timings=timings,
            )
            if media_result.cancelled:
                return
            reconciliation_job_id = JobStore(workspace).create("reconciliation")
            with self._active_lock:
                self._cancel_events[(handle, reconciliation_job_id)] = cancel_event
            job_ids.append(reconciliation_job_id)
            current_job_id = reconciliation_job_id
            reconciliation_result = reconcile_workspace(
                workspace, job_id=reconciliation_job_id, cancel_event=cancel_event
            )
            if reconciliation_result.cancelled:
                return
            thumbnail_job_id = JobStore(workspace).create("media_thumbnails")
            with self._active_lock:
                self._cancel_events[(handle, thumbnail_job_id)] = cancel_event
            job_ids.append(thumbnail_job_id)
            current_job_id = thumbnail_job_id
            thumbnail_result = index_workspace(
                workspace,
                components=("thumbnail",),
                job_id=thumbnail_job_id,
                cancel_event=cancel_event,
                timings=timings,
            )
            if thumbnail_result.cancelled:
                return
            quality_job_id = JobStore(workspace).create("media_quality")
            with self._active_lock:
                self._cancel_events[(handle, quality_job_id)] = cancel_event
            job_ids.append(quality_job_id)
            current_job_id = quality_job_id
            quality_result = index_workspace(
                workspace,
                components=("quality",),
                job_id=quality_job_id,
                cancel_event=cancel_event,
                timings=timings,
            )
            if quality_result.cancelled:
                return
            raw_quality_job_id = JobStore(workspace).create("raw_quality")
            with self._active_lock:
                self._cancel_events[(handle, raw_quality_job_id)] = cancel_event
            job_ids.append(raw_quality_job_id)
            current_job_id = raw_quality_job_id
            raw_quality_result = index_raw_quality(
                workspace,
                job_id=raw_quality_job_id,
                cancel_event=cancel_event,
            )
            if raw_quality_result.cancelled:
                return
            video_quality_job_id = JobStore(workspace).create("video_quality")
            with self._active_lock:
                self._cancel_events[(handle, video_quality_job_id)] = cancel_event
            job_ids.append(video_quality_job_id)
            current_job_id = video_quality_job_id
            video_quality_result = index_video_quality(
                workspace,
                job_id=video_quality_job_id,
                cancel_event=cancel_event,
            )
            if video_quality_result.cancelled:
                return
            embedding_job_id = JobStore(workspace).create("embeddings")
            with self._active_lock:
                self._cancel_events[(handle, embedding_job_id)] = cancel_event
            job_ids.append(embedding_job_id)
            current_job_id = embedding_job_id
            embedding_result = index_embeddings(
                workspace, job_id=embedding_job_id, cancel_event=cancel_event
            )
            if embedding_result.cancelled:
                return
            feature_job_id = JobStore(workspace).create("visual_features")
            with self._active_lock:
                self._cancel_events[(handle, feature_job_id)] = cancel_event
            job_ids.append(feature_job_id)
            current_job_id = feature_job_id
            with timings.measure("visual_features.total"):
                feature_result = extract_visual_features(
                    workspace, job_id=feature_job_id, cancel_event=cancel_event, timings=timings
                )
            if feature_result.cancelled:
                return
            group_job_id = JobStore(workspace).create("grouping")
            with self._active_lock:
                self._cancel_events[(handle, group_job_id)] = cancel_event
            job_ids.append(group_job_id)
            current_job_id = group_job_id
            with timings.measure("grouping.total"):
                group_result = build_groups(workspace, job_id=group_job_id, cancel_event=cancel_event)
            if group_result.cancelled:
                return
            recommendation_job_id = JobStore(workspace).create("recommendations")
            with self._active_lock:
                self._cancel_events[(handle, recommendation_job_id)] = cancel_event
            job_ids.append(recommendation_job_id)
            current_job_id = recommendation_job_id
            with timings.measure("recommendations.total"):
                recommendation_result = build_recommendations(
                    workspace, job_id=recommendation_job_id, cancel_event=cancel_event
                )
            if recommendation_result.cancelled:
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
            with self._active_lock:
                for active_job_id in job_ids:
                    self._cancel_events.pop((handle, active_job_id), None)
                if self._active_threads.get(handle) is threading.current_thread():
                    self._active_threads.pop(handle, None)

    def _run_embeddings_only(
        self,
        handle: str,
        workspace: Workspace,
        job_id: str,
        cancel_event: threading.Event,
    ) -> None:
        try:
            index_embeddings(workspace, job_id=job_id, cancel_event=cancel_event)
        except Exception:
            LOGGER.exception("embedding rebuild failed")
            try:
                row = JobStore(workspace).get(job_id)
                if row is not None and row["status"] in {"pending", "running"}:
                    JobStore(workspace).fail(job_id)
            except Exception:
                LOGGER.exception("could not mark embedding job failed")
        finally:
            with self._active_lock:
                self._cancel_events.pop((handle, job_id), None)
                if self._active_threads.get(handle) is threading.current_thread():
                    self._active_threads.pop(handle, None)

    def _run_grouping_only(
        self,
        handle: str,
        workspace: Workspace,
        feature_job_id: str,
        cancel_event: threading.Event,
    ) -> None:
        job_ids = [feature_job_id]
        current_job_id = feature_job_id
        try:
            feature_result = extract_visual_features(
                workspace, job_id=feature_job_id, cancel_event=cancel_event
            )
            if feature_result.cancelled:
                return
            group_job_id = JobStore(workspace).create("grouping")
            with self._active_lock:
                self._cancel_events[(handle, group_job_id)] = cancel_event
            job_ids.append(group_job_id)
            current_job_id = group_job_id
            group_result = build_groups(workspace, job_id=group_job_id, cancel_event=cancel_event)
            if group_result.cancelled:
                return
            recommendation_job_id = JobStore(workspace).create("recommendations")
            with self._active_lock:
                self._cancel_events[(handle, recommendation_job_id)] = cancel_event
            job_ids.append(recommendation_job_id)
            current_job_id = recommendation_job_id
            recommendation_result = build_recommendations(
                workspace, job_id=recommendation_job_id, cancel_event=cancel_event
            )
            if recommendation_result.cancelled:
                return
        except Exception:
            LOGGER.exception("workspace grouping failed")
            try:
                row = JobStore(workspace).get(current_job_id)
                if row is not None and row["status"] in {"pending", "running"}:
                    JobStore(workspace).fail(current_job_id)
            except Exception:
                LOGGER.exception("could not mark grouping job failed")
        finally:
            with self._active_lock:
                for active_job_id in job_ids:
                    self._cancel_events.pop((handle, active_job_id), None)
                if self._active_threads.get(handle) is threading.current_thread():
                    self._active_threads.pop(handle, None)

    def _run_recommendation_only(
        self,
        handle: str,
        workspace: Workspace,
        recommendation_job_id: str,
        cancel_event: threading.Event,
    ) -> None:
        try:
            build_recommendations(
                workspace,
                job_id=recommendation_job_id,
                cancel_event=cancel_event,
            )
        except Exception:
            LOGGER.exception("workspace recommendation rebuild failed")
            try:
                row = JobStore(workspace).get(recommendation_job_id)
                if row is not None and row["status"] in {"pending", "running"}:
                    JobStore(workspace).fail(recommendation_job_id)
            except Exception:
                LOGGER.exception("could not mark recommendation job failed")
        finally:
            with self._active_lock:
                self._cancel_events.pop((handle, recommendation_job_id), None)
                if self._active_threads.get(handle) is threading.current_thread():
                    self._active_threads.pop(handle, None)

    def _run_reconciliation_only(
        self,
        handle: str,
        workspace: Workspace,
        reconciliation_job_id: str,
        cancel_event: threading.Event,
    ) -> None:
        try:
            reconcile_workspace(
                workspace,
                job_id=reconciliation_job_id,
                cancel_event=cancel_event,
            )
        except Exception:
            LOGGER.exception("workspace reconciliation failed")
            try:
                row = JobStore(workspace).get(reconciliation_job_id)
                if row is not None and row["status"] in {"pending", "running"}:
                    JobStore(workspace).fail(reconciliation_job_id)
            except Exception:
                LOGGER.exception("could not mark reconciliation job failed")
        finally:
            with self._active_lock:
                self._cancel_events.pop((handle, reconciliation_job_id), None)
                if self._active_threads.get(handle) is threading.current_thread():
                    self._active_threads.pop(handle, None)


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    server: WorkspaceHTTPServer

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        query = parse_qs(request.query, keep_blank_values=True)
        try:
            if request.path == "/":
                self._send_bytes(200, _ui_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if request.path in {"/app.css", "/app.js"}:
                name = request.path.removeprefix("/")
                content_type = "text/css; charset=utf-8" if name == "app.css" else "text/javascript; charset=utf-8"
                self._send_bytes(200, _ui_resource(name).encode("utf-8"), content_type)
                return
            if request.path == "/api/health":
                self._send_json(200, {"status": "ok"})
                return
            if request.path == "/api/workspaces":
                self._send_json(200, {"workspaces": self.server.list_workspaces()})
                return
            if request.path == "/api/embedding-models":
                self._send_json(200, {"models": _embedding_model_statuses()})
                return
            handle, workspace = self._workspace(query)
            if request.path == "/api/workspace":
                _prepare_search(workspace)
                self._send_json(200, _workspace_summary(workspace, handle))
            elif request.path == "/api/workspace/configuration":
                self._send_json(200, {"configuration": workspace.configuration()})
            elif request.path == "/api/folders":
                self._send_json(200, {"folders": _folders(workspace), "counts": _folder_counts(workspace)})
            elif request.path == "/api/assets":
                self._send_json(200, _assets(workspace, query, handle))
            elif request.path == "/api/browser/locate":
                group_id = _first(query, "group_id", "")
                offset = 0
                while True:
                    data = _browser_assets(workspace, {**query, "view": ["groups"], "offset": [str(offset)], "limit": ["180"]}, handle)
                    found = next((i for i, group in enumerate(data["groups"]) if group["group_id"] == group_id), None)
                    if found is not None or not data["has_next"]:
                        self._send_json(200, {"found": found is not None, "page": (offset + found) // 10 + 1 if found is not None else 1})
                        break
                    offset += 180
            elif request.path == "/api/browser":
                self._send_json(200, _browser_assets(workspace, query, handle))
            elif request.path == "/api/search-status":
                self._send_json(200, _search_status(workspace))
            elif request.path == "/api/search":
                self._send_json(200, _semantic_search(workspace, query, handle))
            elif request.path == "/api/jobs":
                self._send_json(200, {"jobs": _jobs(workspace, query), "revision": _browser_revision(workspace)})
            elif request.path == "/api/problems":
                self._send_json(200, {"problems": _problems(workspace, query)})
            elif request.path == "/api/groups":
                self._send_json(200, _groups(workspace, query, handle))
            elif request.path == "/api/groups/locate":
                self._send_json(200, _locate_group(workspace, query))
            elif request.path == "/api/recommendations":
                self._send_json(200, _recommendations(workspace))
            else:
                self._handle_resource_get(request.path, workspace, handle, query)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except InvalidRequest as error:
            self._send_json(400, {"error": str(error)})
        except ResourceNotFound as error:
            self._send_json(404, {"error": str(error)})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except Exception as error:
            LOGGER.exception("GET %s failed", request.path)
            self._send_json(500, {"error": _safe_server_error(error)})

    def do_POST(self) -> None:
        request = urlsplit(self.path)
        query = parse_qs(request.query, keep_blank_values=True)
        try:
            if request.path == "/api/workspaces/pick":
                self._send_json(200, {"path": _pick_workspace_path()})
                return
            if request.path == "/api/workspaces/open":
                self._send_json(200, self.server.open_workspace(**self._json_body()))
                return
            if request.path == "/api/workspaces/analyze":
                body = self._json_body()
                self._send_json(200, self.server.analyze_workspace(body.get("path")))
                return
            if request.path == "/api/workspaces/plan":
                body = self._json_body()
                self._send_json(
                    200,
                    self.server.plan_workspace_configuration(
                        body.get("path"), body.get("configuration"), body.get("analysis")
                    ),
                )
                return
            if request.path == "/api/quality-model/install":
                from ..media.quality_provider import install_lar_iqa_model

                try:
                    path = install_lar_iqa_model()
                except Exception as error:
                    raise InvalidRequest(f"Model installation failed: {error}") from error
                self._send_json(200, {"installed": True, "path": str(path)})
                return
            if request.path == "/api/workspaces/apply":
                body = self._json_body()
                self._send_json(
                    202,
                    self.server.apply_workspace_configuration(
                        body.get("configuration"),
                        handle=body.get("workspace"),
                        path=body.get("path"),
                    ),
                )
                return
            if request.path == "/api/workspaces/remove-info":
                body = self._json_body()
                handle = body.get("workspace") or body.get("id")
                if not isinstance(handle, str):
                    raise InvalidRequest("workspace is required")
                self._send_json(200, self.server.workspace_removal_info(handle))
                return
            if request.path == "/api/workspaces/remove":
                body = self._json_body()
                handle = body.get("workspace") or body.get("id")
                if not isinstance(handle, str):
                    raise InvalidRequest("workspace is required")
                self._send_json(
                    200,
                    {"removed": self.server.remove_workspace_with_index(handle, bool(body.get("delete_index")))},
                )
                return
            handle, workspace = self._workspace(query)
            if request.path == "/api/search/prepare":
                _prepare_search(workspace)
                status = _search_status(workspace)
                self._send_json(200, status)
                return
            if request.path == "/api/recommendation-threshold":
                value = self._json_body().get("threshold")
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
                    raise InvalidRequest("threshold must be between 0 and 1")
                connection = workspace.connect()
                try:
                    with connection:
                        connection.execute("UPDATE workspace_config SET recommendation_threshold = ? WHERE id = 1", (value,))
                finally:
                    connection.close()
                self._send_json(200, {"threshold": value})
                return
            if request.path == "/api/reveal":
                file_id = self._json_body().get("file_id")
                path = workspace.root
                if file_id:
                    connection = workspace.connect()
                    try:
                        row = connection.execute("SELECT relative_path FROM physical_file WHERE id = ? AND in_scope = 1 AND is_online = 1", (file_id,)).fetchone()
                    finally:
                        connection.close()
                    if row is None:
                        raise ResourceNotFound("file is unavailable")
                    path = workspace.absolute_path(row[0])
                if sys.platform != "win32":
                    raise InvalidRequest("Explorer is available on Windows")
                subprocess.Popen(["explorer.exe", "/select,", str(path)] if file_id else ["explorer.exe", str(path)])
                self._send_json(200, {"opened": True})
                return
            if request.path == "/api/embedding-models/install":
                from ..embeddings.models import install_model
                provider = self._json_body().get("provider")
                if provider not in {OPENCLIP_PROVIDER, SIGLIP_PROVIDER}:
                    raise InvalidRequest("unsupported provider")
                try:
                    install_model(provider)
                    with _browser_lock:
                        _browser_cache.clear()
                except Exception as error:
                    raise InvalidRequest(f"Model installation failed: {error}") from error
                config = {**workspace.configuration(), "embedding_provider": provider}
                plan = _embedding_plan(config, workspace)
                active = active_embedding(workspace)
                compatible = bool(active and active["active_provider"] == provider and active["active_run_id"] == plan["embedding_active_run_id"] and active["status"] == "complete" and plan["embedding_total_vectors"] > 0 and plan["embedding_pending_count"] == 0 and plan["embedding_unknown_videos"] == 0)
                self._send_json(200, {"model": _embedding_model_status(provider), "compatible_embeddings": compatible, "search": _search_status(workspace)})
                return
            if request.path == "/api/index":
                job_id = self.server.start_indexing(handle)
                if job_id is None:
                    self._send_json(409, {"error": "indexing is already running"})
                else:
                    self._send_json(202, {"job_id": job_id})
                return
            if request.path == "/api/groups/rebuild":
                job_id = self.server.start_group_rebuild(handle)
                if job_id is None:
                    self._send_json(409, {"error": "a workspace job is already running"})
                else:
                    self._send_json(202, {"job_id": job_id})
                return
            if request.path == "/api/recommendations/rebuild":
                job_id = self.server.start_recommendation_rebuild(handle)
                if job_id is None:
                    self._send_json(409, {"error": "a workspace job is already running"})
                else:
                    self._send_json(202, {"job_id": job_id})
                return
            if request.path == "/api/embeddings/rebuild":
                job_id = self.server.start_embedding_rebuild(handle)
                if job_id is None:
                    self._send_json(409, {"error": "a workspace job is already running"})
                else:
                    self._send_json(202, {"job_id": job_id})
                return
            if request.path == "/api/reconciliation/rebuild":
                job_id = self.server.start_reconciliation(handle)
                if job_id is None:
                    self._send_json(409, {"error": "a workspace job is already running"})
                else:
                    self._send_json(202, {"job_id": job_id})
                return
            if request.path == "/api/quality-provider":
                provider = self._json_body().get("provider")
                if not isinstance(provider, str):
                    raise InvalidRequest("quality provider is required")
                try:
                    workspace.set_quality_provider(provider)
                except WorkspaceError as error:
                    raise InvalidRequest(str(error)) from error
                self._send_json(200, {"quality_provider": workspace.quality_provider()})
                return
            if request.path == "/api/workspace/configuration/plan":
                body = self._json_body()
                configuration = normalize_configuration(body.get("configuration"), workspace.root)
                analysis = body.get("analysis")
                if analysis is not None:
                    if (
                        not isinstance(analysis, dict)
                        or analysis.get("path") != str(workspace.root)
                        or not isinstance(analysis.get("root"), dict)
                    ):
                        raise InvalidRequest("analysis does not belong to this workspace folder")
                    plan = plan_from_analysis(analysis, configuration, workspace)
                    plan.update(_configuration_quality_readiness(configuration))
                    plan.update(_embedding_plan(configuration, workspace, plan))
                else:
                    plan = _workspace_plan(workspace, configuration)
                self._send_json(200, {"configuration": configuration, "plan": plan})
                return
            parts = request.path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "assets"] and parts[3] == "decision":
                decision = self._json_body().get("decision")
                if decision not in {"undecided", "selected", "rejected"}:
                    raise InvalidRequest("decision must be undecided, selected, or rejected")
                self._send_json(200, _set_user_decision(workspace, parts[2], decision))
                return
            prefix = "/api/jobs/"
            if request.path.startswith(prefix) and request.path.endswith("/cancel"):
                job_id = request.path[len(prefix) : -len("/cancel")]
                if not self.server.cancel_job(handle, job_id):
                    raise ResourceNotFound("job is not cancellable")
                self._send_json(202, {"job_id": job_id, "status": "cancellation_requested"})
                return
            raise ResourceNotFound("route not found")
        except InvalidRequest as error:
            self._send_json(400, {"error": str(error)})
        except ResourceNotFound as error:
            self._send_json(404, {"error": str(error)})
        except ValueError as error:
            self._send_json(400, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        except Exception as error:
            LOGGER.exception("POST %s failed", request.path)
            self._send_json(500, {"error": _safe_server_error(error)})

    def _workspace(self, query: Mapping[str, list[str]]) -> tuple[str, Workspace]:
        return self.server.resolve_workspace(_first(query, "workspace", ""))

    def _json_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise InvalidRequest("request body is too large")
            value = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError) as error:
            raise InvalidRequest("request body must be valid JSON") from error
        if not isinstance(value, dict):
            raise InvalidRequest("request body must be an object")
        return value

    def _handle_resource_get(self, path: str, workspace: Workspace, handle: str, query: Mapping[str, list[str]]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "assets"] and parts[3] == "thumbnail":
            self._serve_asset_thumbnail(workspace, parts[2])
            return
        if len(parts) == 3 and parts[:2] == ["api", "assets"]:
            self._send_json(200, _asset_detail(workspace, parts[2], handle))
            return
        if len(parts) == 4 and parts[:2] == ["api", "assets"] and parts[3] == "similar":
            self._send_json(200, _similar_assets(workspace, parts[2], handle, query))
            return
        if len(parts) == 4 and parts[:2] == ["api", "files"] and parts[3] == "original":
            self._serve_original(workspace, parts[2])
            return
        if len(parts) == 4 and parts[:2] == ["api", "files"] and parts[3] == "preview":
            self._serve_raw_preview(workspace, parts[2])
            return
        if len(parts) == 4 and parts[:2] == ["api", "files"] and parts[3] == "thumbnail":
            self._serve_thumbnail(workspace, parts[2])
            return
        raise ResourceNotFound("route not found")

    def _serve_asset_thumbnail(self, workspace: Workspace, asset_id: str) -> None:
        connection = workspace.connect()
        try:
            rows = connection.execute(
                """
                SELECT pf.*, cs.status AS thumbnail_status, cs.output_path AS thumbnail_output_path
                FROM physical_file AS pf
                LEFT JOIN component_state AS cs
                    ON cs.physical_file_id = pf.id AND cs.component = 'thumbnail'
                WHERE pf.logical_asset_id = ? AND pf.in_scope = 1
                """,
                (asset_id,),
            ).fetchall()
        finally:
            connection.close()
        available = [
            row for row in rows
            if row["thumbnail_status"] == "complete"
            and row["thumbnail_output_path"]
            and _valid_index_file(workspace, row["thumbnail_output_path"])
        ]
        candidate = preferred_physical(available, component="thumbnail")
        if candidate is not None:
            self._send_validated_thumbnail(workspace, candidate["thumbnail_output_path"])
            return
        raise ResourceNotFound("thumbnail is not available")

    def _serve_thumbnail(self, workspace: Workspace, physical_id: str) -> None:
        connection = workspace.connect()
        try:
            row = connection.execute(
                """SELECT cs.status, cs.output_path
                   FROM component_state AS cs
                   JOIN physical_file AS pf ON pf.id = cs.physical_file_id AND pf.in_scope = 1
                   WHERE cs.physical_file_id = ? AND cs.component = 'thumbnail'""",
                (physical_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or row["status"] != "complete" or not row["output_path"]:
            raise ResourceNotFound("thumbnail is not available")
        self._send_validated_thumbnail(workspace, row["output_path"])

    def _send_validated_thumbnail(self, workspace: Workspace, output_path: str) -> None:
        try:
            thumbnail = workspace.index_path(output_path)
        except WorkspaceError as error:
            raise ResourceNotFound("invalid thumbnail path") from error
        self._send_file(thumbnail, "image/jpeg")

    def _serve_original(self, workspace: Workspace, physical_id: str) -> None:
        connection = workspace.connect()
        try:
            row = connection.execute(
                "SELECT relative_path, is_online, in_scope FROM physical_file WHERE id = ?", (physical_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None or not row["is_online"] or not row["in_scope"]:
            raise ResourceNotFound("original is offline")
        try:
            source = workspace.absolute_path(row["relative_path"])
        except WorkspaceError as error:
            raise ResourceNotFound("invalid source path") from error
        self._send_file(
            source,
            mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            allow_range=True,
        )

    def _serve_raw_preview(self, workspace: Workspace, physical_id: str) -> None:
        connection = workspace.connect()
        try:
            row = connection.execute(
                "SELECT relative_path, extension, is_online, in_scope FROM physical_file WHERE id = ?",
                (physical_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not row["is_online"] or not row["in_scope"] or not is_raw_extension(row["extension"]):
            raise ResourceNotFound("RAW preview is unavailable")
        try:
            source = workspace.absolute_path(row["relative_path"])
            preview = extract_embedded_preview(source)
        except (WorkspaceError, UnsupportedDecoderError) as error:
            raise ResourceNotFound("RAW embedded preview is unavailable") from error
        try:
            output = BytesIO()
            preview.image.save(output, format="JPEG", quality=90, optimize=True)
            self._send_bytes(200, output.getvalue(), "image/jpeg")
        finally:
            preview.image.close()

    def _send_file(self, path: Path, content_type: str, allow_range: bool = False) -> None:
        if not path.is_file():
            raise ResourceNotFound("file is unavailable")
        try:
            size = path.stat().st_size
            source = path.open("rb")
        except OSError as error:
            raise ResourceNotFound("file is unavailable") from error
        start = 0
        end = size - 1
        partial = False
        if allow_range:
            range_header = self.headers.get("Range")
            if range_header:
                if not range_header.startswith("bytes=") or "," in range_header:
                    self._send_range_not_satisfiable(size)
                    source.close()
                    return
                specification = range_header[6:]
                first, separator, last = specification.partition("-")
                try:
                    if not separator:
                        raise ValueError
                    if first:
                        start = int(first)
                        end = int(last) if last else size - 1
                    else:
                        suffix_length = int(last)
                        if suffix_length <= 0:
                            raise ValueError
                        start = max(size - suffix_length, 0)
                        end = size - 1
                except ValueError:
                    self._send_range_not_satisfiable(size)
                    source.close()
                    return
                if start < 0 or start >= size or end < start:
                    self._send_range_not_satisfiable(size)
                    source.close()
                    return
                end = min(end, size - 1)
                partial = True
        length = end - start + 1 if size else 0
        with source:
            if partial:
                source.seek(start)
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", content_type)
            if allow_range:
                self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "private, max-age=60")
            self.end_headers()
            remaining = length
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _send_range_not_satisfiable(self, size: int) -> None:
        self.send_response(416)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status: int, value: object) -> None:
        self._send_bytes(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)


class InvalidRequest(ValueError):
    pass


class ResourceNotFound(LookupError):
    pass


def _safe_server_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    return (message or error.__class__.__name__)[:300]


def serve(workspace: Workspace | None = None, host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("the localhost server must bind to 127.0.0.1 or localhost")
    server = WorkspaceHTTPServer((host, port), workspace)
    if workspace is not None:
        server.registry.add(workspace)
    suffix = f"?workspace={server.default_handle}" if server.default_handle else ""
    url = f"http://{host}:{server.server_port}/{suffix}"
    print(f"Archive Indexation UI: {url}")
    webbrowser.open(url, new=2)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArchive Indexation UI stopped.", flush=True)
    finally:
        server.stop_background_jobs()
        server.server_close()


def _ui_html() -> str:
    return files("archive_index.web").joinpath("index.html").read_text(encoding="utf-8")


def _ui_resource(name: str) -> str:
    if name not in {"app.css", "app.js"}:
        raise ResourceNotFound("resource not found")
    return files("archive_index.web").joinpath(name).read_text(encoding="utf-8")


def _pick_workspace_path() -> str:
    if not _folder_picker_lock.acquire(blocking=False):
        return ""
    try:
        return _pick_windows_folder()
    finally:
        _folder_picker_lock.release()


def _pick_windows_folder() -> str:
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8)]

            def com_method(interface, index, restype, *argtypes):
                vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
                return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtable[index])

            clsid_file_open_dialog = GUID(0xDC1C5A9C, 0xE88A, 0x4DDE, (0xA5, 0xA1, 0x60, 0xF8, 0x2A, 0x20, 0xAE, 0xF7))
            iid_file_open_dialog = GUID(0xD57C7288, 0xD4AD, 0x4768, (0xBE, 0x02, 0x9D, 0x96, 0x95, 0x32, 0xD9, 0x60))
            ole32 = ctypes.WinDLL("ole32", use_last_error=True)
            ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            ole32.CoInitializeEx.restype = ctypes.c_long
            ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
            ole32.CoCreateInstance.restype = ctypes.c_long
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree.restype = None
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetForegroundWindow.argtypes = []
            user32.GetForegroundWindow.restype = wintypes.HWND
            dialog = ctypes.c_void_p()
            initialized = ole32.CoInitializeEx(None, 0x2)
            if initialized < 0:
                raise OSError(f"COM initialization failed: 0x{initialized & 0xFFFFFFFF:08X}")
            try:
                result = ole32.CoCreateInstance(ctypes.byref(clsid_file_open_dialog), None, 0x1, ctypes.byref(iid_file_open_dialog), ctypes.byref(dialog))
                if result < 0:
                    raise OSError(f"folder picker creation failed: 0x{result & 0xFFFFFFFF:08X}")
                try:
                    options = ctypes.c_uint()
                    result = com_method(dialog, 10, ctypes.c_long, ctypes.POINTER(ctypes.c_uint))(dialog, ctypes.byref(options))
                    if result < 0:
                        raise OSError(f"folder picker options failed: 0x{result & 0xFFFFFFFF:08X}")
                    options.value |= 0x20 | 0x40 | 0x800
                    result = com_method(dialog, 9, ctypes.c_long, ctypes.c_uint)(dialog, options)
                    if result < 0:
                        raise OSError(f"folder picker configuration failed: 0x{result & 0xFFFFFFFF:08X}")
                    com_method(dialog, 17, ctypes.c_long, ctypes.c_wchar_p)(dialog, "Choose archive workspace folder")
                    owner = user32.GetForegroundWindow()
                    result = com_method(dialog, 3, ctypes.c_long, ctypes.c_void_p)(dialog, owner)
                    if result != 0:
                        return ""
                    item = ctypes.c_void_p()
                    result = com_method(dialog, 20, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(dialog, ctypes.byref(item))
                    if result < 0:
                        raise OSError(f"folder picker result failed: 0x{result & 0xFFFFFFFF:08X}")
                    try:
                        path = ctypes.c_wchar_p()
                        result = com_method(item, 5, ctypes.c_long, ctypes.c_uint, ctypes.POINTER(ctypes.c_wchar_p))(item, 0x80058000, ctypes.byref(path))
                        if result < 0:
                            raise OSError(f"folder path lookup failed: 0x{result & 0xFFFFFFFF:08X}")
                        try:
                            return path.value or ""
                        finally:
                            ole32.CoTaskMemFree(path)
                    finally:
                        com_method(item, 2, ctypes.c_ulong)(item)
                finally:
                    com_method(dialog, 2, ctypes.c_ulong)(dialog)
            finally:
                ole32.CoUninitialize()
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            return filedialog.askdirectory(title="Choose archive workspace folder") or ""
        finally:
            root.destroy()
    except Exception as error:
        raise InvalidRequest("the native folder picker is unavailable; enter the path manually") from error


def _active_job(workspace: Workspace) -> dict[str, object] | None:
    connection = workspace.connect()
    try:
        row = connection.execute(
            "SELECT id, kind, status FROM job WHERE status IN ('pending', 'running') ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def _index_size_bytes(path: Path) -> int:
    if not path.is_dir() or path.is_symlink():
        return 0
    total = 0
    for entry in os.scandir(path):
        if _is_reparse(entry):
            raise InvalidRequest("the app-owned index contains a symlink or reparse point")
        if entry.is_dir(follow_symlinks=False):
            total += _index_size_bytes(Path(entry.path))
        elif entry.is_file(follow_symlinks=False):
            total += entry.stat(follow_symlinks=False).st_size
    return total


def _is_reparse(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _delete_owned_index(workspace: Workspace) -> None:
    index_directory = workspace.index_directory
    root = workspace.root.resolve(strict=False)
    expected = (root / ".archive-index")
    if index_directory.resolve(strict=False) != expected or not index_directory.is_dir() or index_directory.is_symlink():
        raise InvalidRequest("the app-owned index location could not be verified")
    _index_size_bytes(index_directory)
    for attempt in range(10):
        try:
            shutil.rmtree(index_directory)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05)


def _workspace_entry(handle: str, workspace: Workspace, recent: bool) -> dict[str, object]:
    summary = _workspace_summary(workspace, handle)
    summary["recent"] = recent
    summary["available"] = True
    return summary


def _workspace_summary(workspace: Workspace, handle: str) -> dict[str, object]:
    connection = workspace.connect()
    try:
        return _workspace_summary_from_connection(connection, handle, workspace.root)
    finally:
        connection.close()


def _read_only_workspace_entry(handle: str, path: str) -> dict[str, object]:
    root = Path(path).expanduser().resolve(strict=False)
    database_path = root / ".archive-index" / "index.sqlite"
    if not database_path.is_file():
        raise WorkspaceError(f"workspace database is missing: {database_path}")
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=0.25)
    connection.row_factory = sqlite3.Row
    try:
        identity = connection.execute("SELECT workspace_id FROM workspace_info WHERE id = 1").fetchone()
        if identity is None or identity["workspace_id"] != handle:
            raise WorkspaceError("workspace identity does not match recent entry")
        summary = _workspace_summary_from_connection(connection, handle, root)
    finally:
        connection.close()
    summary["recent"] = True
    summary["available"] = True
    return summary


def _workspace_summary_from_connection(connection, handle: str, root: Path) -> dict[str, object]:
    assets = connection.execute(
        """SELECT COUNT(DISTINCT la.id) FROM logical_asset AS la
           JOIN physical_file AS pf ON pf.logical_asset_id = la.id
           WHERE pf.in_scope = 1"""
    ).fetchone()[0]
    physical_files = connection.execute("SELECT COUNT(*) FROM physical_file WHERE in_scope = 1").fetchone()[0]
    online = connection.execute("SELECT COUNT(*) FROM physical_file WHERE in_scope = 1 AND is_online = 1").fetchone()[0]
    out_of_scope = connection.execute("SELECT COUNT(*) FROM physical_file WHERE in_scope = 0").fetchone()[0]
    latest = connection.execute("SELECT MAX(finished_at) FROM job WHERE status = 'complete'").fetchone()[0]
    embedding_storage = connection.execute(
        "SELECT COALESCE((SELECT SUM(length(embedding)) FROM logical_asset_embedding), 0) + COALESCE((SELECT SUM(length(embedding)) FROM video_frame_embedding), 0)"
    ).fetchone()[0]
    configuration = configuration_from_connection(connection)
    return {
        "id": handle,
        "name": root.name or str(root),
        "path": str(root),
        "root": str(root),
        "assets": assets,
        "physical_files": physical_files,
        "online_files": online,
        "offline_files": physical_files - online,
        "out_of_scope_files": out_of_scope,
        "last_indexed": latest,
        "quality_provider": configuration["quality_provider"],
        "quality_providers": ["off", "lar-iqa"],
        "rendered_quality_provider": configuration["rendered_quality_provider"],
        "raw_quality_provider": configuration["raw_quality_provider"],
        "video_quality_enabled": configuration["video_quality_enabled"],
        "include_videos_in_semantic_search": configuration["include_videos_in_semantic_search"],
        "semantic_search_enabled": configuration["semantic_search_enabled"],
        "embedding_provider": configuration["embedding_provider"],
        "embedding_model": _embedding_model_status(configuration["embedding_provider"]),
        "embedding_storage_bytes": embedding_storage,
    }


def _workspace_plan(workspace: Workspace, configuration: Mapping[str, object]) -> dict[str, object]:
    analysis = analyze_folder(workspace.root)
    plan = plan_from_analysis(analysis, dict(configuration), workspace)
    plan.update(_configuration_quality_readiness(configuration))
    plan.update(_embedding_plan(configuration, workspace))
    return plan


def _configuration_quality_readiness(configuration: Mapping[str, object]) -> dict[str, object]:
    lar_iqa = _quality_readiness("lar-iqa")
    rendered = _quality_readiness(configuration["rendered_quality_provider"])
    raw = _quality_readiness(configuration["raw_quality_provider"])
    if configuration["raw_quality_provider"] == "lar-iqa" and raw["ready"]:
        try:
            rawpy_ready = importlib.util.find_spec("rawpy") is not None
        except (ImportError, ModuleNotFoundError):
            rawpy_ready = False
        if not rawpy_ready:
            raw = {
                **raw,
                "status": "raw_preview_missing",
                "ready": False,
                "message": "Install the optional raw-preview dependency to assess RAW-only assets.",
            }
    video = _quality_readiness(
        configuration["rendered_quality_provider"]
        if configuration["video_quality_enabled"]
        else "off"
    )
    return {
        "lar_iqa_readiness": lar_iqa,
        "quality_readiness": rendered,
        "rendered_quality_readiness": rendered,
        "raw_quality_readiness": raw,
        "video_quality_readiness": video,
        "embedding_readiness": _embedding_readiness(configuration),
    }


def _embedding_model_status(provider: str) -> dict[str, object]:
    status = model_status(provider)
    return {
        "provider": status["provider"],
        "model_id": status["model_id"],
        "version": status["version"],
        "dimension": status["dimension"],
        "installed": status["installed"],
        "cache_bytes": status["cache_bytes"],
        "expected_download_bytes": status["expected_download_bytes"],
    }


def _embedding_model_statuses() -> list[dict[str, object]]:
    return [_embedding_model_status(provider) for provider in (OPENCLIP_PROVIDER, SIGLIP_PROVIDER)]


def _embedding_readiness(configuration: Mapping[str, object]) -> dict[str, object]:
    provider = str(configuration["embedding_provider"])
    status = _embedding_model_status(provider)
    modules = ("torch", "numpy")
    if provider == OPENCLIP_PROVIDER:
        modules += ("open_clip",)
    else:
        modules += ("transformers", "safetensors")
    try:
        runtime_ready = all(importlib.util.find_spec(module) is not None for module in modules)
    except (ImportError, ModuleNotFoundError):
        runtime_ready = False
    ready = not configuration["semantic_search_enabled"] or (runtime_ready and bool(status["installed"]))
    if not configuration["semantic_search_enabled"]:
        message = "Semantic search is off."
        state = "off"
    elif ready:
        message = "Runtime and model appear ready."
        state = "ready"
    elif not runtime_ready:
        message = "Install the embeddings extra before indexing semantic features."
        state = "runtime_missing"
    else:
        message = "Install the selected embedding model before indexing semantic features."
        state = "model_missing"
    return {"status": state, "ready": ready, "runtime_ready": runtime_ready, "model": status, "message": message}


def _embedding_plan(configuration, workspace=None, filesystem_plan=None):
    from ..embeddings.providers import create_embedding_provider
    from ..indexing.embeddings import _source, _source_units, _input_fingerprint
    provider = create_embedding_provider(configuration["embedding_provider"])
    images = videos = reusable = unknown_videos = 0
    run_id = None
    if workspace is not None:
        connection = workspace.connect()
        try:
            rows = connection.execute("SELECT * FROM physical_file WHERE is_online = 1 ORDER BY logical_asset_id, relative_path").fetchall()
            run = connection.execute("SELECT id FROM embedding_run WHERE provider = ? AND model_version = ? AND settings_json = ? ORDER BY created_at DESC LIMIT 1",
                                     (provider.provider_id, provider.version, json.dumps(provider.settings, ensure_ascii=False, sort_keys=True))).fetchone()
            run_id = run[0] if run else None
            states = {r["physical_file_id"]: r for r in connection.execute("SELECT * FROM component_state WHERE component = ?", (f"embedding:{provider.provider_id}",))}
            image_cache = {(r[0], r[1]): r[2] for r in connection.execute("SELECT logical_asset_id, source_physical_file_id, input_fingerprint FROM logical_asset_embedding WHERE run_id = ?", (run_id,))}
            video_cache = {(r[0], r[1]): r[2] for r in connection.execute("SELECT v.logical_asset_id, v.physical_file_id, COUNT(*) FROM video_frame_embedding v JOIN workspace_video_sample s ON s.physical_file_id=v.physical_file_id AND s.active_run_id=v.sample_run_id WHERE v.run_id = ? GROUP BY v.logical_asset_id,v.physical_file_id", (run_id,))}
        finally:
            connection.close()
        groups = {}
        for row in rows:
            if path_in_scope(row["relative_path"], configuration):
                groups.setdefault(row["logical_asset_id"], []).append(row)
        for asset_id, members in groups.items():
            rendered = [r for r in members if not is_raw_extension(r["extension"])]
            selected = preferred_physical(rendered or members)
            kind = "video" if selected["media_type"] == "video" else "rendered" if rendered else "raw_preview"
            if kind == "video" and not configuration.get("include_videos_in_semantic_search", True):
                continue
            source = _source(selected, asset_id, kind, configuration)
            if kind == "video" and not (selected["duration_seconds"] and selected["duration_seconds"] > 0):
                unknown_videos += 1
                continue
            units = _source_units(source)
            if kind == "video": videos += units
            else: images += 1
            fingerprint = _input_fingerprint(source, provider)
            state = states.get(selected["id"])
            valid = state and state["status"] == "complete" and state["version"] == provider.version and state["input_fingerprint"] == fingerprint
            stored = video_cache.get((asset_id, selected["id"]), 0) == units if kind == "video" else image_cache.get((asset_id, selected["id"])) == fingerprint
            if valid and stored: reusable += units
    elif filesystem_plan:
        categories = filesystem_plan.get("selected_categories", {})
        images = sum(categories.get(k, 0) for k in ("jpeg", "other_image", "raw"))
        unknown_videos = categories.get("video", 0) if configuration.get("include_videos_in_semantic_search", True) else 0
        if not configuration.get("include_videos_in_semantic_search", True):
            videos = 0
    total = images + videos
    pending = total - reusable
    return {"embedding_image_count": images, "embedding_video_sample_count": videos,
            "embedding_total_vectors": total, "embedding_pending_count": pending,
            "embedding_cached_count": reusable, "embedding_unknown_videos": unknown_videos,
            "embedding_counts_estimated": workspace is None,
            "embedding_estimated_storage_bytes": pending * provider.dimension * 2,
            "embedding_image_estimated_seconds": round(images * 0.25, 1),
            "embedding_video_estimated_seconds": round(videos * 0.30, 1),
            "embedding_estimated_seconds": round((8.0 if pending else 0.0) + images * 0.25 + videos * 0.30, 1),
            "embedding_active_run_id": run_id}


def _quality_readiness(provider: object) -> dict[str, object]:
    if provider == "off":
        return {"status": "off", "ready": True}
    modules = ("torch", "torchvision", "timm", "efficient_kan")
    try:
        runtime_ready = all(importlib.util.find_spec(module) is not None for module in modules)
    except (ImportError, ModuleNotFoundError):
        runtime_ready = False
    checkpoint_ready = default_model_path().is_file()
    checkpoint_size = default_model_path().stat().st_size if checkpoint_ready else None
    if runtime_ready and checkpoint_ready:
        status = "ready"
        message = "Runtime and checkpoint appear ready."
    elif not runtime_ready and not checkpoint_ready:
        status = "needs_setup"
        message = "Install the LAR-IQA runtime and checkpoint before indexing."
    elif not runtime_ready:
        status = "runtime_missing"
        message = "The LAR-IQA runtime is not installed in this environment."
    else:
        status = "checkpoint_missing"
        message = "The LAR-IQA checkpoint is not installed."
    return {
        "status": status,
        "ready": runtime_ready and checkpoint_ready,
        "runtime_ready": runtime_ready,
        "checkpoint_ready": checkpoint_ready,
        "model": {
            "provider": "lar-iqa",
            "model_id": LAR_IQA_MODEL_ID,
            "filename": LAR_IQA_MODEL_FILENAME,
            "installed": checkpoint_ready,
            "size_bytes": checkpoint_size,
        },
        "message": message,
    }


def _assets(workspace: Workspace, query: Mapping[str, list[str]], handle: str) -> dict[str, object]:
    page = _positive_int(_first(query, "page", "1"), "page")
    page_size = min(_positive_int(_first(query, "page_size", "60"), "page_size"), MAX_PAGE_SIZE)
    folder = _folder_filter(workspace, _first(query, "folder", ""))
    media_type = _first(query, "media_type", "")
    if media_type and media_type not in {"image", "video"}:
        raise InvalidRequest("media_type must be image or video")
    sort_by, direction = _sort_values(query)
    search = _first(query, "q", "").strip()
    selection_filter = _first(query, "selection", "").lower()
    if selection_filter and selection_filter not in {"all", "representatives", "recommended", "selected", "rejected", "undecided"}:
        raise InvalidRequest("selection must be all, representatives, recommended, selected, rejected, or undecided")
    representatives_only = (
        selection_filter == "representatives"
        or _first(query, "representatives", "").lower() in {"1", "true", "yes"}
    )
    recommended_only = (
        selection_filter == "recommended"
        or _first(query, "recommended", "").lower() in {"1", "true", "yes"}
    )
    decision = selection_filter if selection_filter in {"selected", "rejected", "undecided"} else _first(query, "decision", "").lower()
    if decision and decision not in {"undecided", "selected", "rejected"}:
        raise InvalidRequest("decision must be undecided, selected, or rejected")
    if len(search) > 200:
        raise InvalidRequest("q is too long")

    representative_ids, grouping_available = _current_representatives(workspace)
    recommendation_ids, recommendation_run_id = _current_recommendations(workspace)

    clauses = [
        "EXISTS (SELECT 1 FROM physical_file AS pf_scope "
        "WHERE pf_scope.logical_asset_id = la.id AND pf_scope.in_scope = 1)"
    ]
    params: list[object] = []
    if folder:
        escaped = _like_value(folder)
        clauses.append(
            "EXISTS (SELECT 1 FROM physical_file AS pf_folder "
            "WHERE pf_folder.logical_asset_id = la.id "
            "AND pf_folder.in_scope = 1 "
            "AND (pf_folder.relative_path = ? OR pf_folder.relative_path LIKE ? ESCAPE '\\'))"
        )
        params.extend([folder, f"{escaped}/%"])
    if media_type:
        clauses.append("EXISTS (SELECT 1 FROM physical_file AS pf_type WHERE pf_type.logical_asset_id = la.id AND pf_type.in_scope = 1 AND pf_type.media_type = ?)")
        params.append(media_type)
    if search:
        clauses.append(
            "EXISTS (SELECT 1 FROM physical_file AS pf_search "
            "WHERE pf_search.logical_asset_id = la.id "
            "AND pf_search.in_scope = 1 "
            "AND LOWER(pf_search.filename) LIKE ? ESCAPE '\\')"
        )
        params.append(f"%{_like_value(search.casefold())}%")
    if representatives_only:
        if not representative_ids:
            clauses.append("1 = 0")
        else:
            placeholders = ",".join("?" for _ in representative_ids)
            clauses.append(f"la.id IN ({placeholders})")
            params.extend(sorted(representative_ids))
    if recommended_only:
        if not recommendation_ids:
            clauses.append("1 = 0")
        else:
            placeholders = ",".join("?" for _ in recommendation_ids)
            clauses.append(f"la.id IN ({placeholders})")
            params.extend(sorted(recommendation_ids))
    if decision:
        clauses.append("la.selection_state = ?")
        params.append(decision)
    where = " AND ".join(clauses)
    connection = workspace.connect()
    try:
        total = connection.execute(f"SELECT COUNT(*) FROM logical_asset AS la WHERE {where}", params).fetchone()[0]
        if sort_by == "quality":
            order = f"CASE WHEN quality_score IS NULL THEN 1 ELSE 0 END, quality_score {direction}, la.id"
        elif sort_by == "filename":
            order = f"CASE WHEN filename_sort IS NULL THEN 1 ELSE 0 END, filename_sort {direction}, la.id"
        else:
            order = f"CASE WHEN la.capture_time IS NULL THEN 1 ELSE 0 END, la.capture_time {direction}, la.id"
        rows = connection.execute(
            f"""
            SELECT la.*,
                   (SELECT MAX(pf_quality.quality_score) FROM physical_file AS pf_quality
                    WHERE pf_quality.logical_asset_id = la.id AND pf_quality.in_scope = 1
                      AND (pf_quality.media_type != 'image' OR pf_quality.extension NOT IN ('.arw', '.cr2', '.cr3', '.dng', '.nef', '.raf', '.rw2')
                           OR NOT EXISTS (SELECT 1 FROM physical_file AS pf_rendered
                                          WHERE pf_rendered.logical_asset_id = la.id AND pf_rendered.in_scope = 1
                                            AND pf_rendered.media_type = 'image'
                                            AND pf_rendered.extension NOT IN ('.arw', '.cr2', '.cr3', '.dng', '.nef', '.raf', '.rw2')))) AS quality_score,
                   (SELECT MIN(LOWER(pf_sort.filename)) FROM physical_file AS pf_sort
                    WHERE pf_sort.logical_asset_id = la.id AND pf_sort.in_scope = 1) AS filename_sort
            FROM logical_asset AS la
            WHERE {where}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    finally:
        connection.close()
    physical_by_asset = _physical_rows_for_assets(workspace, [row["id"] for row in rows])
    group_by_asset = _current_group_ids(workspace, [row["id"] for row in rows])
    return {
        "items": [
            _asset_summary(
                workspace,
                row,
                handle,
                physical_by_asset.get(row["id"], []),
                row["id"] in representative_ids,
                row["id"] in recommendation_ids,
                recommendation_run_id,
                group_by_asset.get(row["id"]),
            )
            for row in rows
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_next": page * page_size < total,
        "sort_by": sort_by,
        "direction": direction,
        "representatives_only": representatives_only,
        "grouping_available": grouping_available,
        "recommended_only": recommended_only,
        "decision": decision or None,
        "selection": selection_filter or ("representatives" if representatives_only else "recommended" if recommended_only else decision or "all"),
        "recommendation_run_id": recommendation_run_id,
    }


def _path_revision(path: Path):
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return None
    return stat_result.st_mtime_ns, stat_result.st_size


def _browser_revision(workspace):
    return [_path_revision(path) for path in (workspace.database_path, workspace.database_path.with_name("index.sqlite-wal"))]


def _browser_catalog_revision(workspace):
    connection = workspace.connect()
    try:
        generation = connection.execute(
            "SELECT generation FROM browser_revision WHERE id = 1"
        ).fetchone()[0]
    finally:
        connection.close()
    return generation


_browser_cache = {}
_browser_catalogs = {}
_browser_lock = threading.RLock()


def _prepare_search(workspace):
    config = workspace.configuration()
    provider = config["embedding_provider"]
    runtime = "open_clip" if provider == OPENCLIP_PROVIDER else "transformers"
    enabled = config["semantic_search_enabled"] and model_status(provider)["installed"] and importlib.util.find_spec(runtime) is not None
    select_provider(provider if enabled else None)


def _search_status(workspace):
    configuration = workspace.configuration()
    provider = configuration["embedding_provider"]
    label = "OpenCLIP" if provider == OPENCLIP_PROVIDER else "SigLIP2"
    if not configuration["semantic_search_enabled"]:
        return {"state": "unavailable", "message": "Semantic search disabled · Configure to enable", "provider": label}
    if not model_status(provider)["installed"]:
        return {"state": "missing_model", "message": f"{label} model not installed", "provider": label}
    runtime = "open_clip" if provider == OPENCLIP_PROVIDER else "transformers"
    if importlib.util.find_spec(runtime) is None:
        return {"state": "unavailable", "message": f"{label} runtime unavailable", "provider": label}
    active = active_embedding(workspace)
    if active is None or active["active_run_id"] is None or active["status"] != "complete":
        return {"state": "missing_embeddings", "message": "Embeddings not indexed · Re-index required", "provider": label}
    state = provider_state(provider)
    message = {"ready": f"Semantic search ready · {label}", "available": f"Semantic search available · {label}",
               "loading": f"Preparing semantic search · {label}…", "failed": f"Search failed: {label} could not be loaded"}[state]
    if state == "failed":
        message = f"Search failed: {prepare_provider(provider).exception()}"
    return {"state": state, "message": message, "provider": label}


def _browser_catalog(workspace, handle):
    representative_ids, grouping_available = _current_representatives(workspace)
    recommendation_ids, recommendation_run_id = _current_recommendations(workspace)
    connection = workspace.connect()
    try:
        rows = connection.execute(
            """
            SELECT la.*,
                   (SELECT MAX(pf_quality.quality_score) FROM physical_file AS pf_quality
                    WHERE pf_quality.logical_asset_id = la.id AND pf_quality.in_scope = 1
                      AND (pf_quality.media_type != 'image' OR pf_quality.extension NOT IN ('.arw', '.cr2', '.cr3', '.dng', '.nef', '.raf', '.rw2')
                           OR NOT EXISTS (SELECT 1 FROM physical_file AS pf_rendered
                                          WHERE pf_rendered.logical_asset_id = la.id AND pf_rendered.in_scope = 1
                                            AND pf_rendered.media_type = 'image'
                                            AND pf_rendered.extension NOT IN ('.arw', '.cr2', '.cr3', '.dng', '.nef', '.raf', '.rw2')))) AS quality_score,
                   (SELECT MIN(LOWER(pf_sort.filename)) FROM physical_file AS pf_sort
                    WHERE pf_sort.logical_asset_id = la.id AND pf_sort.in_scope = 1) AS filename_sort
            FROM logical_asset AS la
            WHERE EXISTS (
                SELECT 1 FROM physical_file AS pf_scope
                WHERE pf_scope.logical_asset_id = la.id AND pf_scope.in_scope = 1
            )
            ORDER BY CASE WHEN la.capture_time IS NULL THEN 1 ELSE 0 END,
                     la.capture_time DESC, la.id
            """
        ).fetchall()
        physical_rows = connection.execute(
            """
            SELECT pf.*,
                   metadata.status AS metadata_status, metadata.algorithm AS metadata_algorithm,
                   metadata.version AS metadata_version, metadata.error_message AS metadata_error,
                   thumbnail.status AS thumbnail_status, thumbnail.algorithm AS thumbnail_algorithm,
                   thumbnail.version AS thumbnail_version, thumbnail.error_message AS thumbnail_error,
                   thumbnail.output_path AS thumbnail_output_path,
                   quality.status AS quality_component_status, quality.algorithm AS quality_component_algorithm,
                   quality.version AS quality_component_version, quality.error_message AS quality_component_error
            FROM physical_file AS pf
            JOIN logical_asset AS la ON la.id = pf.logical_asset_id
            LEFT JOIN component_state AS metadata ON metadata.physical_file_id = pf.id AND metadata.component = 'metadata'
            LEFT JOIN component_state AS thumbnail ON thumbnail.physical_file_id = pf.id AND thumbnail.component = 'thumbnail'
            LEFT JOIN component_state AS quality ON quality.physical_file_id = pf.id AND quality.component = 'quality'
            WHERE EXISTS (
                SELECT 1 FROM physical_file AS pf_scope
                WHERE pf_scope.logical_asset_id = la.id AND pf_scope.in_scope = 1
            )
            ORDER BY pf.logical_asset_id, pf.is_online DESC, pf.relative_path
            """
        ).fetchall()
    finally:
        connection.close()
    physical_by_asset = {}
    for row in physical_rows:
        physical_by_asset.setdefault(row["logical_asset_id"], []).append(row)
    group_by_asset = _current_group_ids(workspace, [row["id"] for row in rows])
    return [
        _asset_summary(
            workspace,
            row,
            handle,
            physical_by_asset.get(row["id"], []),
            row["id"] in representative_ids,
            row["id"] in recommendation_ids,
            recommendation_run_id,
            group_by_asset.get(row["id"]),
        )
        for row in rows
    ], grouping_available


def _browser_assets(workspace, query, handle):
    started = time.perf_counter()
    offset = int(_first(query, "offset", "0"))
    limit = min(_positive_int(_first(query, "limit", "60"), "limit"), 180)
    if offset < 0:
        raise InvalidRequest("offset must not be negative")
    text = _first(query, "q", "").strip()
    if len(text) > 500:
        raise InvalidRequest("query is too long")
    threshold = float(_first(query, "threshold", "0.20"))
    if not 0 <= threshold <= 1:
        raise InvalidRequest("threshold must be between 0 and 1")
    signature = tuple(sorted((k, tuple(v)) for k, v in query.items() if k not in {"offset", "limit", "view", "group_id", "async"}))
    catalog_revision = _browser_catalog_revision(workspace)
    key = (str(workspace.root), handle, catalog_revision, signature)
    with _browser_lock:
        cached = _browser_cache.get(key)
    if cached is not None and _first(query, "auto", "all") == "recommended":
        cached = None
    if cached is None:
        catalog_key = (str(workspace.root), handle, catalog_revision)
        with _browser_lock:
            catalog = _browser_catalogs.get(catalog_key)
        items = [] if catalog is None else list(catalog)
        while catalog is None:
            items, _ = _browser_catalog(workspace, handle)
            catalog = items
        with _browser_lock:
            for old_key in list(_browser_catalogs):
                if old_key[:2] == catalog_key[:2] and old_key != catalog_key:
                    del _browser_catalogs[old_key]
            _browser_catalogs[catalog_key] = items
            while len(_browser_catalogs) > 4:
                del _browser_catalogs[next(iter(_browser_catalogs))]
        folders = json.loads(_first(query, "folders", "null"))
        if folders is not None and (not isinstance(folders, list) or not all(isinstance(p, str) for p in folders)):
            raise InvalidRequest("folders must be a list of paths")
        connection = workspace.connect()
        try:
            physical = connection.execute("SELECT logical_asset_id, filename, relative_path FROM physical_file WHERE in_scope = 1").fetchall()
        finally:
            connection.close()
        filename_ids = {r[0] for r in physical if text and text.casefold() in r[1].casefold()}
        folder_ids = None if folders is None else {r[0] for r in physical if (r[2].rsplit("/", 1)[0] if "/" in r[2] else "") in folders}
        media_type = _first(query, "media_type", "")
        layout = _first(query, "layout", "")
        manual = _first(query, "manual", "all")
        auto = _first(query, "auto", "all")
        recommendation_ids, recommendation_run_id = _current_recommendations(workspace)
        items = [
            {**item, "auto_recommended": item["asset_id"] in recommendation_ids,
             "recommendation_run_id": recommendation_run_id}
            for item in items
        ]
        items = [i for i in items if
                 (folder_ids is None or i["asset_id"] in folder_ids)
                 and (not media_type or i["media_type"] == media_type)
                 and (manual == "all" or i["user_decision"] == manual)
                 and (auto == "all" or (auto == "representatives" and i["is_representative"]) or (auto == "recommended" and i["auto_recommended"]))
                 and (not layout or (i["width"] and i["height"] and ((layout == "horizontal" and i["width"] >= i["height"]) or (layout == "vertical" and i["height"] > i["width"]))))]
        status = _search_status(workspace)
        if text:
            scores = {}
            if _first(query, "semantic", "1") == "0":
                status = {**status, "message": "Filename search complete"}
            elif status["state"] in {"ready", "available", "loading", "failed"}:
                try:
                    allowed = {i["asset_id"] for i in items}
                    if _first(query, "async", "0") == "1":
                        results, phase = request_text(workspace, text, allowed_asset_ids=allowed)
                        status = {**status, "state": phase, "message": f"Loading {status['provider']}…" if phase == "loading" else "Searching…" if phase == "searching" else "Search complete"}
                        scores = {r.asset_id: r for r in results or []}
                    else:
                        scores = {r.asset_id: r for r in search_text(workspace, text, allowed_asset_ids=allowed, top_k=len(items))}
                except Exception as error:
                    status = {**status, "state": "failed", "message": f"Search failed: {error}"}
            merged = []
            for item in items:
                result = scores.get(item["asset_id"])
                filename_match = item["asset_id"] in filename_ids
                if filename_match or (result is not None and result.similarity >= threshold):
                    merged.append({**item, "filename_match": filename_match, "similarity": result.similarity if result else None,
                                   "similarity_kind": "text" if result else None,
                                   "best_match_timestamp": result.best_timestamp if result else None})
            items = merged
        sort_by = _first(query, "sort_by", "capture_time")
        descending = _first(query, "direction", "desc") == "desc"
        if sort_by == "search" and text:
            items.sort(key=lambda i: (i["filename_match"], i.get("similarity") if i.get("similarity") is not None else -2), reverse=descending)
        else:
            field = "quality_score" if sort_by == "quality" else "filename" if sort_by == "filename" else "capture_time"
            known = [i for i in items if i[field] is not None]
            key = (lambda i: i[field].casefold()) if field == "filename" else (lambda i: i[field])
            known.sort(key=key, reverse=descending)
            items = known + [i for i in items if i[field] is None]
        cached = (items, status)
        with _browser_lock:
            if status["state"] not in {"failed", "loading", "searching"}:
                _browser_cache[key] = cached
            while len(_browser_cache) > 8:
                del _browser_cache[next(iter(_browser_cache))]
    items, status = cached
    if not text:
        status = _search_status(workspace)
    media_shown = len(items)
    workspace_total = len(_browser_catalogs.get((str(workspace.root), handle, catalog_revision), items))
    result = {"total": media_shown, "media_shown": media_shown, "media_total": media_shown,
              "workspace_total": workspace_total, "query_active": bool(text), "search": status,
              "filename_matches": sum(bool(i.get("filename_match")) for i in items)}
    if _first(query, "view", "gallery") == "groups":
        groups = {}
        for item in items:
            group_id = item["current_group_id"] or item["asset_id"]
            group = groups.setdefault(group_id, {"group_id": group_id, "label": "Video" if item["media_type"] == "video" else "Group", "members": [], "first_capture_time": item["capture_time"]})
            group["members"].append(item)
            group["member_count"] = len(group["members"])
        values = list(groups.values())
        result.update(groups=values[offset:offset + limit], total=len(values), has_next=offset + limit < len(values))
    else:
        result.update(items=items[offset:offset + limit], has_next=offset + limit < len(items))
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _semantic_search(workspace: Workspace, query: Mapping[str, list[str]], handle: str) -> dict[str, object]:
    text = _first(query, "text", "").strip()
    if not text:
        raise InvalidRequest("text query is required")
    if len(text) > 500:
        raise InvalidRequest("text query is too long")
    page = _positive_int(_first(query, "page", "1"), "page")
    page_size = min(_positive_int(_first(query, "page_size", "60"), "page_size"), MAX_PAGE_SIZE)
    allowed_asset_ids = _semantic_asset_filter(workspace, query)
    try:
        results = search_text(
            workspace,
            text,
            allowed_asset_ids=allowed_asset_ids,
            top_k=max(len(allowed_asset_ids), page * page_size),
        )
    except (RuntimeError, ValueError) as error:
        raise InvalidRequest(str(error)) from error
    return _search_response(workspace, handle, results, page, page_size, text)


IMAGE_SIMILARITY_SUMMARY_THRESHOLD = 0.90


def _similar_assets(workspace: Workspace, asset_id: str, handle: str, query: Mapping[str, list[str]] | None = None) -> dict[str, object]:
    allowed = _semantic_asset_filter(workspace, {"media_type": ["image"]})
    offset = _positive_int(_first(query or {}, "offset", "0"), "offset") if _first(query or {}, "offset", "0") != "0" else 0
    limit = min(_positive_int(_first(query or {}, "limit", "12"), "limit"), MAX_PAGE_SIZE)
    try:
        results = search_similar(workspace, asset_id, allowed_asset_ids=allowed, top_k=len(allowed))
    except (RuntimeError, ValueError) as error:
        raise InvalidRequest(str(error)) from error
    strong_count = sum(r.similarity >= IMAGE_SIMILARITY_SUMMARY_THRESHOLD for r in results)
    initial = _first(query or {}, "initial", "") == "1"
    if initial:
        response = _search_response(workspace, handle, results, 1, limit, None, start_offset=0, selection_limit=min(strong_count, limit))
    else:
        response = _search_response(workspace, handle, results, 1, limit, None, start_offset=offset)
    response["strong_count"] = strong_count
    return response


def _search_response(workspace: Workspace, handle: str, results: list[SearchResult], page: int, page_size: int, query_text: str | None, start_offset: int | None = None, selection_limit: int | None = None) -> dict[str, object]:
    total = len(results)
    start = (page - 1) * page_size if start_offset is None else start_offset
    end = start + (page_size if selection_limit is None else selection_limit)
    selected = results[start : end]
    asset_ids = [result.asset_id for result in selected]
    if not asset_ids:
        return {"items": [], "query": query_text, "page": page, "page_size": page_size, "total": total, "has_next": start + len(selected) < total, "similarity_sort": True}
    placeholders = ",".join("?" for _ in asset_ids)
    connection = workspace.connect()
    try:
        assets = connection.execute(f"SELECT * FROM logical_asset WHERE id IN ({placeholders})", asset_ids).fetchall()
    finally:
        connection.close()
    by_id = {asset["id"]: asset for asset in assets}
    physical = _physical_rows_for_assets(workspace, asset_ids)
    representatives, _ = _current_representatives(workspace)
    recommendations, recommendation_run_id = _current_recommendations(workspace)
    groups = _current_group_ids(workspace, asset_ids)
    items = []
    for result in selected:
        asset = by_id.get(result.asset_id)
        if asset is None:
            continue
        item = _asset_summary(
            workspace,
            asset,
            handle,
            physical.get(result.asset_id, []),
            result.asset_id in representatives,
            result.asset_id in recommendations,
            recommendation_run_id,
            groups.get(result.asset_id),
        )
        item["similarity"] = result.similarity
        item["similarity_kind"] = "text" if query_text is not None else "image"
        item["best_match_timestamp"] = result.best_timestamp
        items.append(item)
    return {
        "items": items,
        "query": query_text,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_next": start + len(selected) < total,
        "similarity_sort": True,
    }


def _semantic_asset_filter(workspace: Workspace, query: Mapping[str, list[str]]) -> set[str]:
    folder = _folder_filter(workspace, _first(query, "folder", "")) if query else ""
    media_type = _first(query, "media_type", "") if query else ""
    if media_type and media_type not in {"image", "video"}:
        raise InvalidRequest("media_type must be image or video")
    selection = _first(query, "selection", "").lower() if query else ""
    if selection not in {"", "all", "representatives", "recommended", "selected", "rejected", "undecided"}:
        raise InvalidRequest("selection is invalid")
    representatives, _ = _current_representatives(workspace)
    recommendations, _ = _current_recommendations(workspace)
    clauses = [
        "EXISTS (SELECT 1 FROM physical_file AS pf_scope WHERE pf_scope.logical_asset_id = la.id AND pf_scope.in_scope = 1 AND pf_scope.is_online = 1)"
    ]
    params: list[object] = []
    if folder:
        escaped = _like_value(folder)
        clauses.append("EXISTS (SELECT 1 FROM physical_file AS pf_folder WHERE pf_folder.logical_asset_id = la.id AND pf_folder.in_scope = 1 AND (pf_folder.relative_path = ? OR pf_folder.relative_path LIKE ? ESCAPE '\\'))")
        params.extend([folder, f"{escaped}/%"])
    if media_type:
        clauses.append("EXISTS (SELECT 1 FROM physical_file AS pf_type WHERE pf_type.logical_asset_id = la.id AND pf_type.in_scope = 1 AND pf_type.media_type = ?)")
        params.append(media_type)
    ids = representatives if selection == "representatives" else recommendations if selection == "recommended" else None
    if ids is not None:
        if not ids:
            return set()
        clauses.append(f"la.id IN ({','.join('?' for _ in ids)})")
        params.extend(sorted(ids))
    if selection in {"selected", "rejected", "undecided"}:
        clauses.append("la.selection_state = ?")
        params.append(selection)
    connection = workspace.connect()
    try:
        rows = connection.execute(f"SELECT la.id FROM logical_asset AS la WHERE {' AND '.join(clauses)}", params).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def _groups(workspace: Workspace, query: Mapping[str, list[str]], handle: str) -> dict[str, object]:
    page = _positive_int(_first(query, "page", "1"), "page")
    page_size = min(_positive_int(_first(query, "page_size", "10"), "page_size"), 60)
    filters = _group_filters(workspace, query)
    connection = workspace.connect()
    try:
        active = connection.execute(
            """
            SELECT gr.id, gr.algorithm, gr.version, gr.settings_json
            FROM workspace_grouping AS wg
            JOIN grouping_run AS gr ON gr.id = wg.active_run_id
            WHERE wg.id = 1
            """
        ).fetchone()
        if active is None:
            return {
                "run_id": None, "groups": [], "page": page, "page_size": page_size,
                "total": 0, "has_next": False,
            }
        if filters["media_type"] == "video":
            return _video_groups(workspace, query, handle, filters, active, page, page_size)
        condition, condition_params, order = _group_query_parts(active["id"], filters)
        total = connection.execute(
            f"SELECT COUNT(*) FROM strict_group AS sg WHERE sg.run_id = ? {condition}",
            [active["id"], *condition_params],
        ).fetchone()[0]
        group_rows = connection.execute(
            f"""
            SELECT sg.*,
                   (SELECT MAX(pf.quality_score) FROM physical_file AS pf
                    WHERE pf.logical_asset_id = sg.representative_logical_asset_id AND pf.in_scope = 1
                      AND (pf.media_type != 'image' OR pf.extension NOT IN ('.arw', '.cr2', '.cr3', '.dng', '.nef', '.raf', '.rw2')
                           OR NOT EXISTS (SELECT 1 FROM physical_file AS pf_rendered
                                          WHERE pf_rendered.logical_asset_id = sg.representative_logical_asset_id AND pf_rendered.in_scope = 1
                                            AND pf_rendered.media_type = 'image'
                                            AND pf_rendered.extension NOT IN ('.arw', '.cr2', '.cr3', '.dng', '.nef', '.raf', '.rw2')))) AS representative_quality_score,
                   (SELECT MIN(LOWER(pf.filename)) FROM physical_file AS pf
                    WHERE pf.logical_asset_id = sg.representative_logical_asset_id AND pf.in_scope = 1) AS representative_filename
            FROM strict_group AS sg
            WHERE sg.run_id = ? {condition}
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            [active["id"], *condition_params, page_size, (page - 1) * page_size],
        ).fetchall()
        group_ids = [row["group_id"] for row in group_rows]
        if not group_ids:
            members = []
        else:
            placeholders = ",".join("?" for _ in group_ids)
            members = connection.execute(
                f"""
                SELECT sgm.*, la.*
                FROM strict_group_member AS sgm
                JOIN logical_asset AS la ON la.id = sgm.logical_asset_id
                WHERE sgm.run_id = ? AND sgm.group_id IN ({placeholders})
                  AND EXISTS (
                      SELECT 1 FROM physical_file AS pf_member
                      WHERE pf_member.logical_asset_id = sgm.logical_asset_id
                        AND pf_member.in_scope = 1
                  )
                ORDER BY sgm.group_id, sgm.member_order
                """,
                [active["id"], *group_ids],
            ).fetchall()
    finally:
        connection.close()
    member_asset_ids = [row["logical_asset_id"] for row in members]
    physical_by_asset = _physical_rows_for_assets(workspace, member_asset_ids)
    recommendation_ids, recommendation_run_id = _current_recommendations(workspace)
    members_by_group: dict[str, list[dict[str, object]]] = {group_id: [] for group_id in group_ids}
    for row in members:
        summary = _asset_summary(
            workspace,
            row,
            handle,
            physical_by_asset.get(row["logical_asset_id"], []),
            False,
            row["logical_asset_id"] in recommendation_ids,
            recommendation_run_id,
            row["group_id"],
        )
        members_by_group[row["group_id"]].append({"asset": summary, "member_order": row["member_order"]})
    response_groups = []
    for index, row in enumerate(group_rows, start=(page - 1) * page_size + 1):
        group_members = members_by_group[row["group_id"]]
        representative_id = row["representative_logical_asset_id"]
        response_groups.append(
            {
                "group_id": row["group_id"],
                "label": f"Group {index}",
                "member_count": len(group_members),
                "first_capture_time": row["first_capture_time"],
                "representative_asset_id": representative_id,
                "representative_quality_score": row["representative_quality_score"],
                "members": [
                    {**member["asset"], "is_representative": member["asset"]["asset_id"] == representative_id}
                    for member in group_members
                ],
            }
        )
    return {
        "run_id": active["id"],
        "algorithm": active["algorithm"],
        "version": active["version"],
        "settings": _json_or_none(active["settings_json"]),
        "groups": response_groups,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_next": page * page_size < total,
        "filters": filters,
    }


def _video_groups(workspace: Workspace, query, handle, filters, active, page, page_size):
    clauses = [
        "la.media_type = 'video'",
        "pf.media_type = 'video'",
        "pf.in_scope = 1",
        "pf.is_online = 1",
    ]
    params: list[object] = []
    if filters["search"]:
        clauses.append("LOWER(pf.filename) LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_value(str(filters['search']).casefold())}%")
    if filters["folder"]:
        escaped = _like_value(str(filters["folder"]))
        clauses.append("(pf.relative_path = ? OR pf.relative_path LIKE ? ESCAPE '\\')")
        params.extend([filters["folder"], f"{escaped}/%"])
    connection = workspace.connect()
    try:
        rows = connection.execute(
            f"SELECT DISTINCT la.* FROM logical_asset AS la JOIN physical_file AS pf ON pf.logical_asset_id = la.id WHERE {' AND '.join(clauses)}",
            params,
        ).fetchall()
    finally:
        connection.close()
    representatives, _ = _current_representatives(workspace)
    recommendations, _ = _current_recommendations(workspace)
    if filters["selection"] == "representatives":
        rows = [row for row in rows if row["id"] in representatives]
    elif filters["selection"] == "recommended":
        rows = [row for row in rows if row["id"] in recommendations]
    elif filters["selection"] in {"selected", "rejected", "undecided"}:
        rows = [row for row in rows if row["selection_state"] == filters["selection"]]
    physical_by_asset = _physical_rows_for_assets(workspace, [row["id"] for row in rows])

    def sort_key(row):
        physical = physical_by_asset.get(row["id"], [])
        filename = min((str(item["filename"]).casefold() for item in physical), default="")
        quality = next((item["quality_score"] for item in physical if item["quality_score"] is not None), None)
        value = filename if filters["sort_by"] == "filename" else quality if filters["sort_by"] == "quality" else row["capture_time"]
        return (value is None, value if value is not None else "")

    rows.sort(key=sort_key, reverse=filters["direction"] == "DESC")
    total = len(rows)
    page_rows = rows[(page - 1) * page_size : page * page_size]
    recommendation_ids, recommendation_run_id = _current_recommendations(workspace)
    groups = []
    for row in page_rows:
        summary = _asset_summary(
            workspace,
            row,
            handle,
            physical_by_asset.get(row["id"], []),
            True,
            row["id"] in recommendation_ids,
            recommendation_run_id,
            None,
        )
        groups.append({
            "group_id": f"video:{row['id']}",
            "label": "Video",
            "member_count": 1,
            "first_capture_time": row["capture_time"],
            "representative_asset_id": row["id"],
            "representative_quality_score": summary["quality_score"],
            "members": [{**summary, "is_representative": True}],
        })
    return {
        "run_id": active["id"],
        "algorithm": active["algorithm"],
        "version": active["version"],
        "settings": _json_or_none(active["settings_json"]),
        "groups": groups,
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_next": page * page_size < total,
        "filters": filters,
    }


def _group_filters(workspace: Workspace, query: Mapping[str, list[str]]) -> dict[str, object]:
    search = _first(query, "q", "").strip()
    folder = _folder_filter(workspace, _first(query, "folder", ""))
    media_type = _first(query, "media_type", "")
    if media_type and media_type not in {"image", "video"}:
        raise InvalidRequest("media_type must be image or video")
    selection = _first(query, "selection", "all").lower()
    if selection not in {"all", "representatives", "recommended", "selected", "rejected", "undecided"}:
        raise InvalidRequest("selection must be all, representatives, recommended, selected, rejected, or undecided")
    sort_by, direction = _sort_values(query)
    if len(search) > 200:
        raise InvalidRequest("q is too long")
    return {
        "search": search,
        "folder": folder,
        "media_type": media_type,
        "selection": selection,
        "sort_by": sort_by,
        "direction": direction,
    }


def _group_query_parts(run_id: str, filters: Mapping[str, object]) -> tuple[str, list[object], str]:
    clauses: list[str] = [
        "EXISTS (SELECT 1 FROM strict_group_member AS sgm_scope "
        "JOIN physical_file AS pf_scope ON pf_scope.logical_asset_id = sgm_scope.logical_asset_id "
        "WHERE sgm_scope.run_id = sg.run_id AND sgm_scope.group_id = sg.group_id AND pf_scope.in_scope = 1)"
    ]
    params: list[object] = []
    search = filters["search"]
    folder = filters["folder"]
    selection = filters["selection"]
    if search:
        clauses.append(
            "EXISTS (SELECT 1 FROM strict_group_member AS sgm_filter "
             "JOIN physical_file AS pf_filter ON pf_filter.logical_asset_id = sgm_filter.logical_asset_id AND pf_filter.in_scope = 1 "
            "WHERE sgm_filter.run_id = sg.run_id AND sgm_filter.group_id = sg.group_id "
            "AND LOWER(pf_filter.filename) LIKE ? ESCAPE '\\')"
        )
        params.append(f"%{_like_value(str(search).casefold())}%")
    if folder:
        escaped = _like_value(str(folder))
        clauses.append(
            "EXISTS (SELECT 1 FROM strict_group_member AS sgm_folder "
             "JOIN physical_file AS pf_folder ON pf_folder.logical_asset_id = sgm_folder.logical_asset_id AND pf_folder.in_scope = 1 "
            "WHERE sgm_folder.run_id = sg.run_id AND sgm_folder.group_id = sg.group_id "
            "AND (pf_folder.relative_path = ? OR pf_folder.relative_path LIKE ? ESCAPE '\\'))"
        )
        params.extend([folder, f"{escaped}/%"])
    if selection == "representatives":
        clauses.append("sg.representative_logical_asset_id IS NOT NULL")
    elif selection == "recommended":
        clauses.append(
            "EXISTS (SELECT 1 FROM asset_recommendation AS ar "
            "JOIN workspace_recommendation AS wr ON wr.active_run_id = ar.run_id AND wr.id = 1 "
            "JOIN recommendation_run AS rr ON rr.id = ar.run_id "
            "JOIN workspace_grouping AS wg ON wg.id = 1 "
            "WHERE ar.logical_asset_id = sg.representative_logical_asset_id AND ar.auto_recommended = 1 "
            "AND rr.source_grouping_run_id = wg.active_run_id)"
        )
    elif selection in {"selected", "rejected", "undecided"}:
        clauses.append(
            "EXISTS (SELECT 1 FROM strict_group_member AS sgm_state "
            "JOIN logical_asset AS la_state ON la_state.id = sgm_state.logical_asset_id "
            "WHERE sgm_state.run_id = sg.run_id AND sgm_state.group_id = sg.group_id "
            "AND la_state.selection_state = ?)"
        )
        params.append(selection)
    condition = f" AND {' AND '.join(clauses)}" if clauses else ""
    direction = filters["direction"]
    if filters["sort_by"] == "quality":
        quality = "(SELECT MAX(pf_order.quality_score) FROM physical_file AS pf_order WHERE pf_order.logical_asset_id = sg.representative_logical_asset_id AND pf_order.in_scope = 1)"
        order = f"CASE WHEN {quality} IS NULL THEN 1 ELSE 0 END, {quality} {direction}, sg.group_id"
    elif filters["sort_by"] == "filename":
        filename = "(SELECT MIN(LOWER(pf_order.filename)) FROM physical_file AS pf_order WHERE pf_order.logical_asset_id = sg.representative_logical_asset_id AND pf_order.in_scope = 1)"
        order = f"CASE WHEN {filename} IS NULL THEN 1 ELSE 0 END, {filename} {direction}, sg.group_id"
    else:
        order = f"CASE WHEN sg.first_capture_time IS NULL THEN 1 ELSE 0 END, sg.first_capture_time {direction}, sg.group_id"
    return condition, params, order


def _locate_group(workspace: Workspace, query: Mapping[str, list[str]]) -> dict[str, object]:
    group_id = _first(query, "group_id", "")
    if not group_id:
        raise InvalidRequest("group_id is required")
    page_size = min(_positive_int(_first(query, "page_size", "10"), "page_size"), 60)
    filters = _group_filters(workspace, query)
    connection = workspace.connect()
    try:
        active = connection.execute(
            "SELECT active_run_id FROM workspace_grouping WHERE id = 1"
        ).fetchone()
        if active is None or not connection.execute(
            "SELECT 1 FROM strict_group WHERE run_id = ? AND group_id = ?",
            (active["active_run_id"], group_id),
        ).fetchone():
            return {"found": False}
        condition, condition_params, order = _group_query_parts(active["active_run_id"], filters)
        rows = connection.execute(
            f"""
            SELECT sg.group_id,
                   (SELECT MAX(pf.quality_score) FROM physical_file AS pf
                    WHERE pf.logical_asset_id = sg.representative_logical_asset_id AND pf.in_scope = 1) AS representative_quality_score,
                   (SELECT MIN(LOWER(pf.filename)) FROM physical_file AS pf
                    WHERE pf.logical_asset_id = sg.representative_logical_asset_id AND pf.in_scope = 1) AS representative_filename,
                   ROW_NUMBER() OVER (ORDER BY {order}) AS position
            FROM strict_group AS sg
            WHERE sg.run_id = ? {condition}
            GROUP BY sg.group_id
            """,
            [active["active_run_id"], *condition_params],
        ).fetchall()
    finally:
        connection.close()
    row = next((candidate for candidate in rows if candidate["group_id"] == group_id), None)
    if row is None:
        return {"found": False, "hidden_by_filters": True}
    position = row["position"]
    return {
        "found": True,
        "group_id": group_id,
        "page": ((position - 1) // page_size) + 1,
        "position": position,
        "page_size": page_size,
    }


def _current_representatives(workspace: Workspace) -> tuple[set[str], bool]:
    connection = workspace.connect()
    try:
        active = connection.execute(
            "SELECT active_run_id FROM workspace_grouping WHERE id = 1"
        ).fetchone()
        if active is None:
            videos = connection.execute(
                "SELECT DISTINCT la.id FROM logical_asset la JOIN physical_file pf ON pf.logical_asset_id = la.id WHERE la.media_type = 'video' AND pf.in_scope = 1 AND pf.is_online = 1"
            ).fetchall()
            return {row["id"] for row in videos}, True
        rows = connection.execute(
            """
            SELECT representative_logical_asset_id
            FROM strict_group
            WHERE run_id = ?
            """,
            (active["active_run_id"],),
        ).fetchall()
    finally:
        connection.close()
    video_connection = workspace.connect()
    try:
        videos = video_connection.execute(
            """
            SELECT DISTINCT la.id
            FROM logical_asset AS la
            JOIN physical_file AS pf ON pf.logical_asset_id = la.id
            WHERE la.media_type = 'video' AND pf.media_type = 'video'
              AND pf.in_scope = 1 AND pf.is_online = 1
            """
        ).fetchall()
    finally:
        video_connection.close()
    return {row["representative_logical_asset_id"] for row in rows} | {row["id"] for row in videos}, True


def _current_group_ids(workspace: Workspace, asset_ids: list[str]) -> dict[str, str]:
    if not asset_ids:
        return {}
    connection = workspace.connect()
    try:
        rows = []
        for start in range(0, len(asset_ids), 800):
            chunk = asset_ids[start : start + 800]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(connection.execute(
                f"""
                SELECT sgm.logical_asset_id, sgm.group_id
                FROM workspace_grouping AS wg
                JOIN strict_group_member AS sgm ON sgm.run_id = wg.active_run_id
                WHERE wg.id = 1 AND sgm.logical_asset_id IN ({placeholders})
                """,
                chunk,
            ).fetchall())
    finally:
        connection.close()
    return {row["logical_asset_id"]: row["group_id"] for row in rows}


def _current_recommendations(workspace: Workspace) -> tuple[set[str], str | None]:
    connection = workspace.connect()
    try:
        active = connection.execute(
            """
            SELECT wr.active_run_id, rr.source_grouping_run_id, wg.active_run_id AS grouping_run_id
            FROM workspace_recommendation AS wr
            LEFT JOIN recommendation_run AS rr ON rr.id = wr.active_run_id
            LEFT JOIN workspace_grouping AS wg ON wg.id = 1
            WHERE wr.id = 1
            """
        ).fetchone()
        if (
            active is None
            or active["active_run_id"] is None
            or active["source_grouping_run_id"] != active["grouping_run_id"]
        ):
            return set(), None
        rows = connection.execute(
            """SELECT sg.representative_logical_asset_id AS logical_asset_id
               FROM strict_group sg
               JOIN workspace_config wc ON wc.id = 1
               WHERE sg.run_id = ? AND EXISTS (
                   SELECT 1 FROM physical_file pf
                   WHERE pf.logical_asset_id = sg.representative_logical_asset_id
                     AND pf.in_scope = 1 AND pf.quality_score >= wc.recommendation_threshold
                     AND (pf.extension NOT IN ('.arw','.cr2','.cr3','.dng','.nef','.raf','.rw2')
                          OR NOT EXISTS (SELECT 1 FROM physical_file rendered
                              WHERE rendered.logical_asset_id = pf.logical_asset_id AND rendered.in_scope = 1
                              AND rendered.extension NOT IN ('.arw','.cr2','.cr3','.dng','.nef','.raf','.rw2'))))""",
            (active["grouping_run_id"],),
        ).fetchall()
    finally:
        connection.close()
    video_connection = workspace.connect()
    try:
        video_rows = video_connection.execute(
            """
            SELECT DISTINCT la.id
            FROM logical_asset AS la
            JOIN physical_file AS pf ON pf.logical_asset_id = la.id
            JOIN workspace_config AS wc ON wc.id = 1
            WHERE la.media_type = 'video' AND pf.media_type = 'video'
              AND pf.in_scope = 1 AND pf.is_online = 1
              AND wc.quality_enabled = 1 AND wc.video_quality_enabled = 1
              AND pf.quality_score >= wc.recommendation_threshold
            """
        ).fetchall()
    finally:
        video_connection.close()
    return {row["logical_asset_id"] for row in rows} | {row["id"] for row in video_rows}, active["active_run_id"]


def _recommendations(workspace: Workspace) -> dict[str, object]:
    connection = workspace.connect()
    try:
        active = connection.execute(
            """
            SELECT rr.id, rr.algorithm, rr.version, rr.settings_json,
                   rr.source_grouping_run_id, rr.created_at, rr.completed_at,
                   wg.active_run_id AS grouping_run_id
            FROM workspace_recommendation AS wr
            JOIN recommendation_run AS rr ON rr.id = wr.active_run_id
            LEFT JOIN workspace_grouping AS wg ON wg.id = 1
            WHERE wr.id = 1
            """
        ).fetchone()
        if active is None or active["source_grouping_run_id"] != active["grouping_run_id"]:
            return {"run_id": None, "available": False, "counts": {}}
        counts = {
            "assets": connection.execute(
                "SELECT COUNT(*) FROM asset_recommendation WHERE run_id = ?", (active["id"],)
            ).fetchone()[0],
            "recommended": connection.execute(
                "SELECT COUNT(*) FROM asset_recommendation WHERE run_id = ? AND auto_recommended = 1",
                (active["id"],),
            ).fetchone()[0],
            "selected": connection.execute(
                "SELECT COUNT(*) FROM logical_asset WHERE selection_state = 'selected'"
            ).fetchone()[0],
            "rejected": connection.execute(
                "SELECT COUNT(*) FROM logical_asset WHERE selection_state = 'rejected'"
            ).fetchone()[0],
            "undecided": connection.execute(
                "SELECT COUNT(*) FROM logical_asset WHERE selection_state = 'undecided'"
            ).fetchone()[0],
        }
    finally:
        connection.close()
    return {
        "run_id": active["id"],
        "available": True,
        "algorithm": active["algorithm"],
        "version": active["version"],
        "settings": _json_or_none(active["settings_json"]),
        "created_at": active["created_at"],
        "completed_at": active["completed_at"],
        "source_grouping_run_id": active["source_grouping_run_id"],
        "counts": counts,
    }


def _set_user_decision(workspace: Workspace, asset_id: str, decision: str) -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with workspace.transaction() as connection:
        cursor = connection.execute(
            "UPDATE logical_asset SET selection_state = ?, selection_updated_at = ?, updated_at = ? WHERE id = ?",
            (decision, now, now, asset_id),
        )
        if cursor.rowcount != 1:
            raise ResourceNotFound("asset not found")
    recommendation_ids, recommendation_run_id = _current_recommendations(workspace)
    return {
        "asset_id": asset_id,
        "user_decision": decision,
        "auto_recommended": asset_id in recommendation_ids,
        "recommendation_run_id": recommendation_run_id,
    }


def _sort_values(query: Mapping[str, list[str]]) -> tuple[str, str]:
    legacy = _first(query, "sort", "")
    if legacy in {"quality_desc", "quality_asc"}:
        return "quality", "DESC" if legacy.endswith("desc") else "ASC"
    sort_by = _first(query, "sort_by", "capture_time")
    direction = _first(query, "direction", "desc").lower()
    if sort_by not in {"capture_time", "quality", "filename"}:
        raise InvalidRequest("sort_by must be capture_time, quality, or filename")
    if direction not in {"asc", "desc"}:
        raise InvalidRequest("direction must be asc or desc")
    return sort_by, direction.upper()


def _asset_summary(
    workspace: Workspace,
    asset,
    handle: str,
    physical=None,
    is_representative: bool = False,
    is_recommended: bool = False,
    recommendation_run_id: str | None = None,
    current_group_id: str | None = None,
) -> dict[str, object]:
    physical = _physical_rows(workspace, asset["id"]) if physical is None else physical
    active_physical = [row for row in physical if row["in_scope"]] or physical
    representative = preferred_physical(active_physical)
    online = preferred_physical([row for row in active_physical if row["is_online"]])
    thumbnail = preferred_physical(
        [
            row for row in active_physical
            if row["thumbnail_status"] == "complete"
            and row["thumbnail_output_path"]
            and _valid_index_file(workspace, row["thumbnail_output_path"])
        ],
        component="thumbnail",
    )
    if representative is None:
        raise ResourceNotFound("asset has no physical representation")
    width, height = _effective_dimensions(active_physical)
    rendered_quality = [
        row for row in active_physical
        if row["media_type"] != "image" or not is_raw_extension(row["extension"])
    ]
    quality_rows = rendered_quality or active_physical
    quality_score = next(
        (row["quality_score"] for row in quality_rows if row["quality_score"] is not None),
        None,
    )
    display = preferred_physical(
        [row for row in active_physical if row["is_online"] and (row["media_type"] != "image" or not is_raw_extension(row["extension"]))]
    ) or online
    display_url = None
    if display is not None:
        display_url = _url(
            f"/api/files/{display['id']}/{('preview' if display['media_type'] == 'image' and is_raw_extension(display['extension']) else 'original')}",
            handle,
        )
    return {
        "asset_id": asset["id"],
        "media_type": asset["media_type"],
        "codec": (online or representative)["codec"],
        "capture_time": asset["capture_time"],
        "capture_time_kind": asset["capture_time_kind"],
        "filename": representative["filename"],
        "relative_path": representative["relative_path"],
        "is_online": any(row["is_online"] for row in physical),
        "physical_count": len(physical),
        "online_count": sum(bool(row["is_online"]) for row in physical),
        "thumbnail_url": _url(f"/api/assets/{asset['id']}/thumbnail", handle) if thumbnail else None,
        "original_url": _url(f"/api/files/{online['id']}/original", handle) if online else None,
        "display_url": display_url,
        "quality_score": quality_score,
        "quality_source": _quality_source(next((row for row in quality_rows if row["quality_score"] is not None), None)),
        "issues": _asset_issues(physical),
        "is_representative": is_representative,
        "current_group_id": current_group_id,
        "auto_recommended": is_recommended,
        "user_decision": asset["selection_state"],
        "user_decision_updated_at": asset["selection_updated_at"],
        "recommendation_run_id": recommendation_run_id,
        "preferred_physical_id": representative["id"],
        "width": width,
        "height": height,
    }


def _effective_dimensions(physical):
    active = [row for row in physical if row["in_scope"]] or list(physical)
    preferred = preferred_physical(active)
    candidates = ([preferred] if preferred is not None else []) + [
        row for row in active if row is not preferred
    ]
    for row in candidates:
        if row["width"] and row["height"]:
            width, height = row["width"], row["height"]
            metadata = _json_or_none(row["metadata_json"]) or {}
            orientation = (metadata.get("exif") or {}).get("Orientation")
            try:
                orientation = int(orientation)
            except (TypeError, ValueError):
                orientation = None
            if orientation in {5, 6, 7, 8}:
                width, height = height, width
            return width, height
    return None, None


def _asset_issues(physical) -> list[str]:
    issues: list[str] = []
    active = [row for row in physical if row["in_scope"]] or list(physical)
    relevant = [
        row for row in active
        if row["media_type"] != "image" or not is_raw_extension(row["extension"])
    ] or active
    if not any(row["is_online"] for row in active):
        issues.append("offline")
    statuses = [row[key] for row in relevant for key in ("metadata_status", "thumbnail_status", "quality_component_status")]
    if any(status in {"pending", "running"} for status in statuses):
        issues.append("processing")
    if any(status == "unsupported" for status in statuses):
        issues.append("unsupported")
    if any(status == "failed" for status in statuses):
        issues.append("failed")
    return issues


def _safe_absolute_path(workspace: Workspace, relative_path: str) -> str | None:
    try:
        return str(workspace.absolute_path(relative_path))
    except WorkspaceError:
        return None


def _asset_detail(workspace: Workspace, asset_id: str, handle: str) -> dict[str, object]:
    connection = workspace.connect()
    try:
        asset = connection.execute("SELECT * FROM logical_asset WHERE id = ?", (asset_id,)).fetchone()
    finally:
        connection.close()
    if asset is None:
        raise ResourceNotFound("asset not found")
    physical = _physical_rows(workspace, asset_id)
    preferred = preferred_physical(physical)
    ordered_physical = ([preferred] if preferred is not None else []) + [
        row for row in physical if preferred is None or row["id"] != preferred["id"]
    ]
    relationships = _current_relationships(workspace, [row["id"] for row in physical])
    recommendation_ids, recommendation_run_id = _current_recommendations(workspace)
    current_group_id = _current_group_ids(workspace, [asset_id]).get(asset_id)
    return {
        "asset_id": asset["id"],
        "media_type": asset["media_type"],
        "capture_time": asset["capture_time"],
        "capture_time_kind": asset["capture_time_kind"],
        "auto_recommended": asset_id in recommendation_ids,
        "user_decision": asset["selection_state"],
        "user_decision_updated_at": asset["selection_updated_at"],
        "recommendation_run_id": recommendation_run_id,
        "current_group_id": current_group_id,
        "physical_files": [
            {
                "id": row["id"],
                "relative_path": row["relative_path"],
                "absolute_path": _safe_absolute_path(workspace, row["relative_path"]),
                "filename": row["filename"],
                "extension": row["extension"],
                "media_type": row["media_type"],
                "role": row["role"],
                "is_preferred": preferred is not None and row["id"] == preferred["id"],
                "relationships": relationships.get(row["id"], []),
                "representation_label": _representation_label(row, relationships.get(row["id"], [])),
                "size_bytes": row["size_bytes"],
                "file_created_time": row["file_created_time"],
                "is_online": bool(row["is_online"]),
                "width": row["width"],
                "height": row["height"],
                "duration_seconds": row["duration_seconds"],
                "codec": row["codec"],
                "metadata": _json_or_none(row["metadata_json"]),
                "quality_score": row["quality_score"],
                "quality_source": _quality_source(row),
                "quality_raw": _json_or_none(row["quality_raw_json"]),
                "quality_components": _json_or_none(row["quality_components_json"]),
                "video_quality": video_quality_details(workspace, row["id"])
                if row["media_type"] == "video" else None,
                "original_url": _url(f"/api/files/{row['id']}/original", handle) if row["is_online"] and row["in_scope"] else None,
                "thumbnail_url": _url(f"/api/files/{row['id']}/thumbnail", handle) if row["in_scope"] and row["thumbnail_status"] == "complete" and row["thumbnail_output_path"] and _valid_index_file(workspace, row["thumbnail_output_path"]) else None,
                "in_scope": bool(row["in_scope"]),
                "components": {
                    "metadata": _component_info(row, "metadata"),
                    "thumbnail": _component_info(row, "thumbnail"),
                    "quality": _component_info(row, "quality"),
                },
            }
            for row in ordered_physical
        ],
    }


def _physical_rows(workspace: Workspace, asset_id: str):
    connection = workspace.connect()
    try:
        return connection.execute(
            """
            SELECT pf.*,
                   metadata.status AS metadata_status, metadata.algorithm AS metadata_algorithm,
                   metadata.version AS metadata_version, metadata.error_message AS metadata_error,
                   thumbnail.status AS thumbnail_status, thumbnail.algorithm AS thumbnail_algorithm,
                   thumbnail.version AS thumbnail_version, thumbnail.error_message AS thumbnail_error,
                   thumbnail.output_path AS thumbnail_output_path,
                   quality.status AS quality_component_status, quality.algorithm AS quality_component_algorithm,
                   quality.version AS quality_component_version, quality.error_message AS quality_component_error
            FROM physical_file AS pf
            LEFT JOIN component_state AS metadata ON metadata.physical_file_id = pf.id AND metadata.component = 'metadata'
            LEFT JOIN component_state AS thumbnail ON thumbnail.physical_file_id = pf.id AND thumbnail.component = 'thumbnail'
            LEFT JOIN component_state AS quality ON quality.physical_file_id = pf.id AND quality.component = 'quality'
            WHERE pf.logical_asset_id = ?
            ORDER BY pf.is_online DESC, pf.relative_path
            """,
            (asset_id,),
        ).fetchall()
    finally:
        connection.close()


def _physical_rows_for_assets(workspace: Workspace, asset_ids: list[str]) -> dict[str, list]:
    if not asset_ids:
        return {}
    placeholders = ",".join("?" for _ in asset_ids)
    connection = workspace.connect()
    try:
        rows = connection.execute(
            f"""
            SELECT pf.*,
                   metadata.status AS metadata_status, metadata.algorithm AS metadata_algorithm,
                   metadata.version AS metadata_version, metadata.error_message AS metadata_error,
                   thumbnail.status AS thumbnail_status, thumbnail.algorithm AS thumbnail_algorithm,
                   thumbnail.version AS thumbnail_version, thumbnail.error_message AS thumbnail_error,
                   thumbnail.output_path AS thumbnail_output_path,
                   quality.status AS quality_component_status, quality.algorithm AS quality_component_algorithm,
                   quality.version AS quality_component_version, quality.error_message AS quality_component_error
            FROM physical_file AS pf
            LEFT JOIN component_state AS metadata ON metadata.physical_file_id = pf.id AND metadata.component = 'metadata'
            LEFT JOIN component_state AS thumbnail ON thumbnail.physical_file_id = pf.id AND thumbnail.component = 'thumbnail'
            LEFT JOIN component_state AS quality ON quality.physical_file_id = pf.id AND quality.component = 'quality'
             WHERE pf.logical_asset_id IN ({placeholders}) AND pf.in_scope = 1
            ORDER BY pf.logical_asset_id, pf.is_online DESC, pf.relative_path
            """,
            asset_ids,
        ).fetchall()
    finally:
        connection.close()
    grouped: dict[str, list] = {asset_id: [] for asset_id in asset_ids}
    for row in rows:
        grouped.setdefault(row["logical_asset_id"], []).append(row)
    return grouped


def _current_relationships(workspace: Workspace, physical_ids: list[str]) -> dict[str, list[str]]:
    if not physical_ids:
        return {}
    placeholders = ",".join("?" for _ in physical_ids)
    connection = workspace.connect()
    try:
        rows = connection.execute(
            f"""
            SELECT pr.source_physical_file_id, pr.target_physical_file_id, pr.relationship_type
            FROM physical_relationship AS pr
            JOIN workspace_reconciliation AS wr ON wr.active_run_id = pr.run_id AND wr.id = 1
            WHERE pr.source_physical_file_id IN ({placeholders})
               OR pr.target_physical_file_id IN ({placeholders})
            ORDER BY pr.relationship_type, pr.source_physical_file_id, pr.target_physical_file_id
            """,
            [*physical_ids, *physical_ids],
        ).fetchall()
    finally:
        connection.close()
    relationships: dict[str, list[str]] = {}
    for row in rows:
        label = "Exact duplicate" if row["relationship_type"] == "exact_duplicate" else "RAW/JPEG pair"
        relationships.setdefault(row["source_physical_file_id"], []).append(label)
        relationships.setdefault(row["target_physical_file_id"], []).append(label)
    return {file_id: sorted(set(values)) for file_id, values in relationships.items()}


def _representation_label(row, relationships: list[str]) -> str:
    if row["role"] == "camera_raw":
        return "RAW source"
    if row["role"] == "camera_jpeg":
        return "Camera JPEG"
    if relationships:
        return relationships[0]
    return "Physical file"


def _component_info(row, component: str) -> dict[str, object]:
    prefix = "quality_component_" if component == "quality" else f"{component}_"
    return {"status": row[f"{prefix}status"], "algorithm": row[f"{prefix}algorithm"], "version": row[f"{prefix}version"], "error": row[f"{prefix}error"]}


def _quality_source(row) -> str | None:
    if row is None or row["quality_score"] is None:
        return None
    raw = _json_or_none(row["quality_raw_json"])
    if isinstance(raw, dict) and raw.get("quality_source") == "raw_embedded_preview":
        return "RAW embedded preview"
    if row["media_type"] == "video":
        return "sampled video frames"
    return "rendered image"


def _jobs(workspace: Workspace, query: Mapping[str, list[str]]):
    limit = min(_positive_int(_first(query, "limit", "20"), "limit"), 100)
    connection = workspace.connect()
    try:
        rows = connection.execute("SELECT * FROM job ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    finally:
        connection.close()
    return [{**_row_dict(row), "substage": SUBSTAGES.get(row["id"])} for row in rows]


_PROBLEM_COMPONENTS = {
    "visual_features": ("group_feature",),
    "media_index": ("metadata", "thumbnail"),
    "media_thumbnails": ("thumbnail",),
    "media_quality": ("quality",),
    "raw_quality": ("quality",),
    "video_quality": ("quality",),
}
_RESOLVED_COMPONENT_STATES = frozenset({"complete", "not_requested"})


def _job_error_is_resolved(connection, row) -> bool:
    physical_file_id = row["physical_file_id"]
    if not physical_file_id:
        return False
    components = _PROBLEM_COMPONENTS.get(row["job_kind"])
    if row["job_kind"] == "embeddings":
        states = connection.execute(
            "SELECT status FROM component_state WHERE physical_file_id = ? AND component LIKE 'embedding:%'",
            (physical_file_id,),
        ).fetchall()
    elif components:
        placeholders = ",".join("?" for _ in components)
        states = connection.execute(
            f"SELECT status FROM component_state WHERE physical_file_id = ? AND component IN ({placeholders})",
            (physical_file_id, *components),
        ).fetchall()
    else:
        return False
    return bool(states) and len(states) == (len(components) if components else len(states)) and all(
        state["status"] in _RESOLVED_COMPONENT_STATES for state in states
    )


def _problems(workspace: Workspace, query: Mapping[str, list[str]]):
    limit = min(_positive_int(_first(query, "limit", "100"), "limit"), 500)
    connection = workspace.connect()
    try:
        rows = connection.execute(
            """
            SELECT job_error.*, job.kind AS job_kind, job.created_at AS job_created_at
            FROM job_error
            LEFT JOIN job ON job.id = job_error.job_id
            ORDER BY job_error.id DESC LIMIT ?
            """,
            (500,),
        ).fetchall()
        rows = [row for row in rows if not _job_error_is_resolved(connection, row)]
        conflicts = connection.execute(
            """
            SELECT rc.id, rc.left_logical_asset_id, rc.right_logical_asset_id,
                   rc.conflict_type, rc.message, rc.created_at,
                   GROUP_CONCAT(DISTINCT pf.relative_path) AS paths
            FROM reconciliation_conflict AS rc
            JOIN workspace_reconciliation AS wr ON wr.active_run_id = rc.run_id AND wr.id = 1
            LEFT JOIN physical_file AS pf
              ON pf.logical_asset_id IN (rc.left_logical_asset_id, rc.right_logical_asset_id)
            WHERE (rc.left_logical_asset_id IS NULL OR rc.right_logical_asset_id IS NULL
                   OR rc.left_logical_asset_id <> rc.right_logical_asset_id)
              AND (rc.conflict_type NOT IN ('manual_decision_conflict', 'exact_duplicate_decision_conflict')
                   OR EXISTS (SELECT 1 FROM logical_asset l JOIN logical_asset r
                              ON r.id=rc.right_logical_asset_id WHERE l.id=rc.left_logical_asset_id
                              AND l.selection_state IN ('selected','rejected') AND r.selection_state IN ('selected','rejected')
                              AND l.selection_state <> r.selection_state))
            GROUP BY rc.id
            ORDER BY rc.id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    problems = [_row_dict(row) for row in rows]
    problems.extend(
        {
            "id": f"reconciliation-{row['id']}",
            "job_id": None,
            "job_kind": "reconciliation",
            "physical_file_id": None,
            "relative_path": row["paths"],
            "error_type": row["conflict_type"],
            "message": row["message"],
            "created_at": row["created_at"],
            "retry_count": 0,
        }
        for row in conflicts
    )
    return sorted(problems, key=lambda problem: problem.get("created_at", ""), reverse=True)[:limit]


def _folders(workspace: Workspace) -> list[str]:
    connection = workspace.connect()
    try:
        paths = [row[0] for row in connection.execute("SELECT relative_path FROM physical_file WHERE in_scope = 1")]
    finally:
        connection.close()
    folders: set[str] = {""}
    for path in paths:
        parts = Path(path).parts[:-1]
        for index in range(1, len(parts) + 1):
            folders.add("/".join(parts[:index]))
    return sorted(folders, key=str.casefold)


def _folder_counts(workspace: Workspace) -> dict[str, dict[str, int]]:
    connection = workspace.connect()
    try:
        rows = connection.execute(
            "SELECT relative_path, media_type, size_bytes FROM physical_file WHERE in_scope = 1 AND is_online = 1"
        ).fetchall()
    finally:
        connection.close()
    counts: dict[str, dict[str, int]] = {"": {"files": 0, "images": 0, "videos": 0, "bytes": 0}}
    for row in rows:
        path = row["relative_path"]
        folder = "/".join(Path(path).parts[:-1])
        value = counts.setdefault(folder, {"files": 0, "images": 0, "videos": 0, "bytes": 0})
        value["files"] += 1
        value["images" if row["media_type"] == "image" else "videos"] += 1
        value["bytes"] += int(row["size_bytes"] or 0)
    return counts


def _folder_filter(workspace: Workspace, value: str) -> str:
    if not value:
        return ""
    if "\\" in value or value.startswith("/") or Path(value).is_absolute():
        raise InvalidRequest("folder must use workspace-relative slash paths")
    try:
        return workspace.relative_path(workspace.root / value.strip("/"))
    except WorkspaceError as error:
        raise InvalidRequest("folder is outside the workspace") from error


def _positive_int(value: str, name: str) -> int:
    try:
        number = int(value)
    except ValueError as error:
        raise InvalidRequest(f"{name} must be an integer") from error
    if number < 1:
        raise InvalidRequest(f"{name} must be positive")
    return number


def _first(query: Mapping[str, list[str]], name: str, default: str) -> str:
    values = query.get(name)
    return values[0] if values else default


def _like_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _url(path: str, handle: str) -> str:
    return f"{path}?{urlencode({'workspace': handle})}"


def _valid_index_file(workspace: Workspace, path: str) -> bool:
    try:
        return workspace.index_path(path).is_file()
    except WorkspaceError:
        return False


def _json_or_none(value: str | None):
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _row_dict(row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}
