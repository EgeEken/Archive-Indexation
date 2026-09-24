"""Loopback-only localhost API and UI server."""

from __future__ import annotations

import importlib.util
import json
import logging
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import time
from math import isfinite
import webbrowser
from io import BytesIO
from collections.abc import Mapping
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from ..app_state import WorkspaceRegistry, workspace_id
from ..configuration import normalize_configuration
from ..embeddings.models import (
    OPENCLIP_PROVIDER,
    SIGLIP_PROVIDER,
    model_status,
)
from ..embeddings.search import SearchResult, active_embedding, search_similar, search_text, provider_state, prepare_provider, request_text, select_provider
from ..indexing.media_pipeline import index_workspace
from ..indexing.scanner import scan
from ..media.quality_provider import default_model_path
from ..media.raw_preview import extract_embedded_preview
from ..media.metadata import UnsupportedDecoderError
from ..media_types import is_raw_extension
from ..jobs.engine import JobStore, SUBSTAGES
from ..planning import _historical_rate, analyze_folder, plan_from_analysis
from ..workspace import Workspace, WorkspaceError
from ..file_management import (
    build_dry_run_plan,
    delete_ruleset,
    list_profiles,
    list_presets,
    list_rulesets,
    save_profile,
    save_preset,
    save_ruleset,
    set_active_ruleset,
)
from ..file_management_previews import cached_preview_file, custom_profile_preview
from ..indexing.representations import preferred_physical
from .errors import InvalidRequest, ResourceNotFound
from .comparison import comparison_data, comparison_preview
from .raw_development import raw_development_preview
from .exports import selected_zip
from .workspaces import (
    _active_job,
    _configuration_quality_readiness,
    _delete_owned_index,
    _embedding_model_status,
    _embedding_model_statuses,
    _embedding_plan,
    _embedding_readiness,
    _index_size_bytes,
    _quality_readiness,
    _require_workspace_root,
    _workspace_entry,
    _workspace_plan,
    _workspace_summary,
    analyze_workspace as analyze_workspace_service,
    apply_workspace_configuration as apply_workspace_configuration_service,
    forget_offline_media as forget_offline_media_service,
    home_thumbnail as home_thumbnail_service,
    list_workspaces as list_workspaces_service,
    open_workspace as open_workspace_service,
    plan_workspace_configuration as plan_workspace_configuration_service,
    register_workspace as register_workspace_service,
    remove_workspace as remove_workspace_service,
    remove_workspace_with_index as remove_workspace_with_index_service,
    resolve_workspace as resolve_workspace_service,
    offline_media_info as offline_media_info_service,
    workspace_removal_info as workspace_removal_info_service,
)
from .search import (
    IMAGE_SIMILARITY_SUMMARY_THRESHOLD,
    prepare_search as prepare_search_service,
    search_status as search_status_service,
    semantic_asset_filter as semantic_asset_filter_service,
    semantic_search as semantic_search_service,
    similar_assets as similar_assets_service,
)
from .browser import (
    asset_detail as asset_detail_service,
    asset_summary as asset_summary_service,
    physical_rows as physical_rows_service,
    physical_rows_for_assets as physical_rows_for_assets_service,
)
from .visualizations import visualization_capabilities, visualization_data
from .jobs import (
    cancel_job as cancel_job_service,
    run_embeddings_only as run_embeddings_only_service,
    run_grouping_only as run_grouping_only_service,
    run_indexing as run_indexing_service,
    run_recommendation_only as run_recommendation_only_service,
    run_reconciliation_only as run_reconciliation_only_service,
    start_embedding_rebuild as start_embedding_rebuild_service,
    start_group_rebuild as start_group_rebuild_service,
    start_indexing as start_indexing_service,
    start_recommendation_rebuild as start_recommendation_rebuild_service,
    start_reconciliation as start_reconciliation_service,
    indexing_runtime_status,
)

LOGGER = logging.getLogger(__name__)
MAX_PAGE_SIZE = 180
_UI_RESOURCES = {
    "app.css",
    "app.js",
    "app-shared.js",
    "app-setup.js",
    "app-browser.js",
    "app-viewer.js",
    "app-details.js",
    "app-maintenance.js",
    "app-file-management.js",
    "app-groups.js",
    "app-visualizations.js",
    "app-bootstrap.js",
    "world.json",
    "assets/compression-preview/reference.jpg",
    "assets/compression-preview/manifest.json",
    "assets/compression-preview/jxl-high-quality.webp",
    "assets/compression-preview/jxl-balanced.webp",
    "assets/compression-preview/jxl-high-compression.webp",
}
_UI_BINARY_RESOURCES = {
    "assets/compression-preview/reference.jpg",
    "assets/compression-preview/jxl-high-quality.webp",
    "assets/compression-preview/jxl-balanced.webp",
    "assets/compression-preview/jxl-high-compression.webp",
}
_folder_picker_lock = threading.Lock()


class WorkspaceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, workspace: Workspace | None = None, registry_path: Path | None = None):
        super().__init__(address, ArchiveRequestHandler)
        self.workspace = workspace
        self.registry = WorkspaceRegistry(registry_path)
        self._workspaces: dict[str, Workspace] = {}
        self.default_handle: str | None = None
        self._active_lock = threading.Lock()
        self._active_threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[tuple[str, str], threading.Event] = {}
        self._indexing_runs = {}
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

    def server_close(self) -> None:
        self.stop_background_jobs()
        super().server_close()

    def _register_workspace(self, workspace: Workspace) -> str:
        return register_workspace_service(self, workspace)

    def resolve_workspace(self, handle: str | None) -> tuple[str, Workspace]:
        return resolve_workspace_service(self, handle)

    def list_workspaces(self) -> list[dict[str, object]]:
        return list_workspaces_service(self)

    def home_thumbnail(self, handle: str) -> Path:
        return home_thumbnail_service(self, handle)

    def offline_media_info(self, handle: str) -> dict[str, int]:
        return offline_media_info_service(self, handle)

    def forget_offline_media(self, handle: str) -> dict[str, int]:
        return forget_offline_media_service(self, handle)

    def open_workspace(self, path: str, create: bool = False) -> dict[str, object]:
        return open_workspace_service(self, path, create)

    def analyze_workspace(self, path: str) -> dict[str, object]:
        return analyze_workspace_service(self, path)

    def plan_workspace_configuration(
        self,
        path: str,
        configuration: dict[str, object],
        analysis: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return plan_workspace_configuration_service(self, path, configuration, analysis)

    def apply_workspace_configuration(
        self,
        configuration: dict[str, object],
        *,
        handle: str | None = None,
        path: str | None = None,
    ) -> dict[str, object]:
        return apply_workspace_configuration_service(
            self,
            configuration,
            handle=handle,
            path=path,
            prepare_search_callback=_prepare_search,
        )

    def remove_workspace(self, handle: str) -> bool:
        return remove_workspace_service(self, handle)

    def workspace_removal_info(self, handle: str) -> dict[str, object]:
        return workspace_removal_info_service(self, handle)

    def remove_workspace_with_index(self, handle: str, delete_index: bool) -> bool:
        return remove_workspace_with_index_service(self, handle, delete_index)

    def start_indexing(self, handle: str) -> str | None:
        return start_indexing_service(self, handle)

    def start_group_rebuild(self, handle: str) -> str | None:
        return start_group_rebuild_service(self, handle)

    def start_recommendation_rebuild(self, handle: str) -> str | None:
        return start_recommendation_rebuild_service(self, handle)

    def start_embedding_rebuild(self, handle: str) -> str | None:
        return start_embedding_rebuild_service(self, handle)

    def start_reconciliation(self, handle: str) -> str | None:
        return start_reconciliation_service(self, handle)

    def cancel_job(self, handle: str, job_id: str) -> bool:
        return cancel_job_service(self, handle, job_id)

    def _run_indexing(self, handle: str, workspace: Workspace, job_id: str, cancel_event: threading.Event) -> None:
        return run_indexing_service(
            self,
            handle,
            workspace,
            job_id,
            cancel_event,
            scan_fn=scan,
            index_workspace_fn=index_workspace,
        )

    def _run_embeddings_only(self, handle: str, workspace: Workspace, job_id: str, cancel_event: threading.Event) -> None:
        return run_embeddings_only_service(self, handle, workspace, job_id, cancel_event)

    def _run_grouping_only(self, handle: str, workspace: Workspace, feature_job_id: str, cancel_event: threading.Event) -> None:
        return run_grouping_only_service(self, handle, workspace, feature_job_id, cancel_event)

    def _run_recommendation_only(self, handle: str, workspace: Workspace, recommendation_job_id: str, cancel_event: threading.Event) -> None:
        return run_recommendation_only_service(self, handle, workspace, recommendation_job_id, cancel_event)

    def _run_reconciliation_only(self, handle: str, workspace: Workspace, reconciliation_job_id: str, cancel_event: threading.Event) -> None:
        return run_reconciliation_only_service(self, handle, workspace, reconciliation_job_id, cancel_event)


class ArchiveRequestHandler(BaseHTTPRequestHandler):
    server: WorkspaceHTTPServer

    def do_GET(self) -> None:
        request = urlsplit(self.path)
        query = parse_qs(request.query, keep_blank_values=True)
        try:
            if request.path == "/":
                self._send_bytes(200, _ui_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if request.path.removeprefix("/") in _UI_RESOURCES:
                name = request.path.removeprefix("/")
                if name in _UI_BINARY_RESOURCES:
                    content_type = "image/jpeg" if name.endswith(".jpg") else "image/webp"
                    self._send_bytes(200, _ui_resource_bytes(name), content_type)
                else:
                    content_type = "text/css; charset=utf-8" if name == "app.css" else "application/json; charset=utf-8" if name.endswith(".json") else "text/javascript; charset=utf-8"
                    self._send_bytes(200, _ui_resource(name).encode("utf-8"), content_type)
                return
            if request.path == "/api/health":
                self._send_json(200, {"status": "ok"})
                return
            if request.path == "/api/workspaces":
                self._send_json(200, {"workspaces": self.server.list_workspaces()})
                return
            if request.path == "/api/workspaces/thumbnail":
                self._send_file(self.server.home_thumbnail(_first(query, "workspace", "")), "image/jpeg")
                return
            if request.path == "/api/embedding-models":
                self._send_json(200, {"models": _embedding_model_statuses()})
                return
            handle, workspace = self._workspace(query)
            if request.path == "/api/workspace":
                _prepare_search(workspace)
                self._send_json(200, _workspace_summary(workspace, handle))
            elif request.path == "/api/workspace/visualization-capabilities":
                self._send_json(200, visualization_capabilities(workspace))
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
            elif request.path == "/api/browser/locate-asset":
                asset_id = _first(query, "asset_id", "")
                items, _, _, _ = _browser_filtered_assets(workspace, query, handle)
                found = next((index for index, item in enumerate(items) if item["asset_id"] == asset_id), None)
                self._send_json(200, {
                    "found": found is not None,
                    "index": found if found is not None else 0,
                    "offset": (found // 60) * 60 if found is not None else 0,
                    "total": len(items),
                })
            elif request.path == "/api/browser":
                self._send_json(200, _browser_assets(workspace, query, handle))
            elif request.path == "/api/visualizations/geo":
                self._send_json(200, visualization_data(workspace, query, handle, filter_assets=_browser_filtered_assets, kind="geo"))
            elif request.path == "/api/visualizations/timeline":
                self._send_json(200, visualization_data(workspace, query, handle, filter_assets=_browser_filtered_assets, kind="timeline"))
            elif request.path == "/api/visualizations/vector":
                self._send_json(200, visualization_data(workspace, query, handle, filter_assets=_browser_filtered_assets, kind="vector"))
            elif request.path == "/api/file-management/profiles":
                self._send_json(200, {"profiles": list_profiles(workspace)})
            elif request.path == "/api/file-management/profile-preview":
                profile_id = _first(query, "profile_id", "")
                profile = next((item for item in list_profiles(workspace) if item["id"] == profile_id), None)
                if profile is None:
                    raise ResourceNotFound("compression profile not found")
                self._send_json(200, custom_profile_preview(workspace, profile))
            elif request.path == "/api/file-management/rulesets":
                self._send_json(200, {"rulesets": list_rulesets(workspace)})
            elif request.path == "/api/file-management/presets":
                self._send_json(200, {"presets": list_presets(workspace)})
            elif request.path == "/api/file-management/plan":
                self._send_json(200, build_dry_run_plan(workspace, _first(query, "ruleset_id", "") or None))
            elif request.path == "/api/exports/selected.zip":
                archive, _, _ = selected_zip(workspace)
                try:
                    self._send_file(
                        archive,
                        "application/zip",
                        content_disposition='attachment; filename="selected-assets.zip"',
                    )
                finally:
                    archive.unlink(missing_ok=True)
            elif request.path == "/api/search-status":
                self._send_json(200, _search_status(workspace))
            elif request.path == "/api/search":
                self._send_json(200, _semantic_search(workspace, query, handle))
            elif request.path == "/api/jobs":
                jobs = _jobs(workspace, query)
                payload = {"jobs": jobs, "revision": _browser_revision(workspace)}
                runtime = indexing_runtime_status(self.server, handle, jobs)
                if runtime is not None:
                    payload["indexing"] = runtime
                self._send_json(200, payload)
            elif request.path == "/api/problems":
                self._send_json(200, {"problems": _problems(workspace, query)})
            elif request.path == "/api/offline-media":
                self._send_json(200, self.server.offline_media_info(handle))
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
            if request.path == "/api/file-management/profiles":
                body = self._json_body()
                self._send_json(200, save_profile(
                    workspace,
                    name=str(body.get("name", "")),
                    codec=str(body.get("codec", "")),
                    container=str(body.get("container", "")),
                    settings=body.get("settings") if isinstance(body.get("settings"), dict) else {},
                    profile_id=body.get("id"),
                ))
                return
            if request.path == "/api/file-management/rulesets":
                body = self._json_body()
                self._send_json(200, save_ruleset(
                    workspace,
                    name=str(body.get("name", "")),
                    description=str(body.get("description", "")),
                    rules=body.get("rules") if isinstance(body.get("rules"), list) else [],
                    ruleset_id=body.get("id"),
                ))
                return
            if request.path == "/api/file-management/presets":
                body = self._json_body()
                ruleset_id = body.get("ruleset_id")
                if not isinstance(ruleset_id, str):
                    raise InvalidRequest("ruleset_id is required")
                self._send_json(200, save_preset(
                    workspace,
                    name=str(body.get("name", "")),
                    ruleset_id=ruleset_id,
                    preset_id=body.get("id"),
                ))
                return
            if request.path == "/api/file-management/active":
                value = self._json_body().get("ruleset_id")
                if value is not None and not isinstance(value, str):
                    raise InvalidRequest("ruleset_id must be a string or null")
                set_active_ruleset(workspace, value)
                self._send_json(200, {"active_ruleset_id": value})
                return
            if request.path == "/api/file-management/rulesets/delete":
                ruleset_id = self._json_body().get("id")
                if not isinstance(ruleset_id, str):
                    raise InvalidRequest("id is required")
                delete_ruleset(workspace, ruleset_id)
                self._send_json(200, {"deleted": True})
                return
            if request.path == "/api/offline-media/forget":
                self._send_json(200, self.server.forget_offline_media(handle))
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
        if len(parts) == 4 and parts[:2] == ["api", "files"] and parts[3] == "comparison-preview":
            self._send_bytes(200, comparison_preview(workspace, parts[2]), "image/png")
            return
        if len(parts) == 4 and parts[:3] == ["api", "file-management", "profile-preview"]:
            self._send_file(cached_preview_file(workspace, parts[3]), "image/webp")
            return
        if len(parts) == 4 and parts[:2] == ["api", "files"] and parts[3] == "raw-development-preview":
            try:
                exposure = float(_first(query, "exposure_ev", "0"))
                white_balance = int(_first(query, "white_balance", "0"))
                saturation = int(_first(query, "saturation", "100"))
                highlights = int(_first(query, "highlights", "0"))
                shadows = int(_first(query, "shadows", "0"))
                output, white_balance_status = raw_development_preview(workspace, parts[2], exposure, white_balance, saturation, highlights, shadows)
            except (InvalidRequest, ResourceNotFound):
                raise
            except (OSError, ValueError, WorkspaceError) as error:
                raise ResourceNotFound("RAW development is unavailable for this file") from error
            self._send_bytes(200, output, "image/png", {"X-RAW-White-Balance": white_balance_status})
            return
        if len(parts) == 4 and parts[:2] == ["api", "files"] and parts[3] == "comparison":
            other_id = _first(query, "with_id", "")
            if not other_id:
                raise InvalidRequest("with_id is required")
            self._send_json(200, comparison_data(workspace, parts[2], other_id, handle))
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
                """
                SELECT pf.relative_path, pf.extension, pf.is_online, pf.in_scope,
                       dp.output_path AS display_preview_output_path
                FROM physical_file AS pf
                LEFT JOIN display_preview AS dp ON dp.physical_file_id = pf.id
                WHERE pf.id = ?
                """,
                (physical_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not row["is_online"] or not row["in_scope"]:
            raise ResourceNotFound("RAW preview is unavailable")
        if row["extension"].casefold() == ".jxl":
            if not row["display_preview_output_path"]:
                raise ResourceNotFound("JPEG XL display preview is unavailable")
            self._send_validated_thumbnail(workspace, row["display_preview_output_path"])
            return
        if not is_raw_extension(row["extension"]):
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

    def _send_file(self, path: Path, content_type: str, allow_range: bool = False, content_disposition: str | None = None) -> None:
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
            if content_disposition:
                self.send_header("Content-Disposition", content_disposition)
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

    def _send_bytes(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)


def _safe_server_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    return (message or error.__class__.__name__)[:300]


def _prepare_search(workspace: Workspace) -> None:
    prepare_search_service(
        workspace,
        model_status_callback=model_status,
        find_spec_callback=importlib.util.find_spec,
        select_provider_callback=select_provider,
    )


def _search_status(workspace: Workspace) -> dict[str, object]:
    return search_status_service(
        workspace,
        model_status_callback=model_status,
        find_spec_callback=importlib.util.find_spec,
        active_embedding_callback=active_embedding,
        provider_state_callback=provider_state,
        prepare_provider_callback=prepare_provider,
    )


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
    if name not in _UI_RESOURCES:
        raise ResourceNotFound("resource not found")
    return files("archive_index.web").joinpath(name).read_text(encoding="utf-8")


def _ui_resource_bytes(name: str) -> bytes:
    if name not in _UI_RESOURCES:
        raise ResourceNotFound("resource not found")
    return files("archive_index.web").joinpath(*name.split("/")).read_bytes()


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
    group_details = _current_group_details(workspace, [row["id"] for row in rows])
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
                (group := group_details.get(row["id"], {})).get("group_id"),
                group.get("member_count"),
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
                   display_preview.output_path AS display_preview_output_path,
                   quality.status AS quality_component_status, quality.algorithm AS quality_component_algorithm,
                   quality.version AS quality_component_version, quality.error_message AS quality_component_error
            FROM physical_file AS pf
            JOIN logical_asset AS la ON la.id = pf.logical_asset_id
            LEFT JOIN component_state AS metadata ON metadata.physical_file_id = pf.id AND metadata.component = 'metadata'
            LEFT JOIN component_state AS thumbnail ON thumbnail.physical_file_id = pf.id AND thumbnail.component = 'thumbnail'
            LEFT JOIN display_preview ON display_preview.physical_file_id = pf.id
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
    group_details = _current_group_details(workspace, [row["id"] for row in rows])
    return [
        _asset_summary(
            workspace,
            row,
            handle,
            physical_by_asset.get(row["id"], []),
            row["id"] in representative_ids,
            row["id"] in recommendation_ids,
            recommendation_run_id,
            (group := group_details.get(row["id"], {})).get("group_id"),
            group.get("member_count"),
        )
        for row in rows
    ], grouping_available


def _browser_filtered_assets(workspace, query, handle):
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
    return items, status, catalog_revision, workspace_total


def _browser_assets(workspace, query, handle):
    started = time.perf_counter()
    offset = int(_first(query, "offset", "0"))
    limit = min(_positive_int(_first(query, "limit", "60"), "limit"), 180)
    if offset < 0:
        raise InvalidRequest("offset must not be negative")
    items, status, catalog_revision, workspace_total = _browser_filtered_assets(workspace, query, handle)
    text = _first(query, "q", "").strip()
    media_shown = len(items)
    result = {"total": media_shown, "media_shown": media_shown, "media_total": media_shown,
              "workspace_total": workspace_total, "query_active": bool(text), "search": status,
              "filename_matches": sum(bool(i.get("filename_match")) for i in items)}
    if _first(query, "view", "gallery") == "groups":
        auto = _first(query, "auto", "all")
        if auto in {"representatives", "recommended"}:
            base_query = {**query, "auto": ["all"]}
            qualifying_items, status, catalog_revision, workspace_total = _browser_filtered_assets(workspace, base_query, handle)
            with _browser_lock:
                catalog = list(_browser_catalogs.get((str(workspace.root), handle, catalog_revision), []))
            recommendation_ids, recommendation_run_id = _current_recommendations(workspace)
            qualifying_groups = {
                item["current_group_id"]
                for item in qualifying_items
                if item.get("current_group_id")
                and int(item.get("strict_group_member_count") or 0) >= 2
                and (item["is_representative"] if auto == "representatives" else item.get("asset_id") in recommendation_ids)
            }
            items = [
                {**item, "auto_recommended": item.get("asset_id") in recommendation_ids,
                 "recommendation_run_id": recommendation_run_id}
                for item in catalog if item.get("current_group_id") in qualifying_groups
            ]
            media_shown = len(items)
            result.update(media_shown=media_shown, media_total=media_shown, search=status,
                          filename_matches=sum(bool(i.get("filename_match")) for i in items))
        group_numbers = _browser_group_numbers(workspace)
        groups = {}
        for item in items:
            group_id = item["current_group_id"] or item["asset_id"]
            label = "Video" if item["media_type"] == "video" else f"Group {group_numbers.get(group_id, 0) or 1}"
            group = groups.setdefault(group_id, {"group_id": group_id, "label": label, "members": [], "first_capture_time": item["capture_time"]})
            group["members"].append(item)
            group["member_count"] = len(group["members"])
        values = [group for group in groups.values() if group["member_count"] >= 2]
        result.update(groups=values[offset:offset + limit], total=len(values), has_next=offset + limit < len(values))
    else:
        result.update(items=items[offset:offset + limit], has_next=offset + limit < len(items))
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result


def _browser_group_numbers(workspace: Workspace) -> dict[str, int]:
    connection = workspace.connect()
    try:
        active = connection.execute("SELECT active_run_id FROM workspace_grouping WHERE id = 1").fetchone()
        if not active or not active["active_run_id"]:
            return {}
        rows = connection.execute(
            """
            SELECT group_id
            FROM strict_group
            WHERE run_id = ? AND member_count >= 2
            ORDER BY CASE WHEN first_capture_time IS NULL THEN 1 ELSE 0 END,
                     first_capture_time, group_id
            """,
            (active["active_run_id"],),
        ).fetchall()
        return {row["group_id"]: index for index, row in enumerate(rows, start=1)}
    finally:
        connection.close()

def _semantic_search(workspace: Workspace, query: Mapping[str, list[str]], handle: str) -> dict[str, object]:
    return semantic_search_service(
        workspace,
        query,
        handle,
        asset_filter=_semantic_asset_filter,
        response_builder=_search_response,
        search_text_callback=search_text,
    )


def _similar_assets(workspace: Workspace, asset_id: str, handle: str, query: Mapping[str, list[str]] | None = None) -> dict[str, object]:
    return similar_assets_service(
        workspace,
        asset_id,
        handle,
        query,
        asset_filter=lambda current_workspace, current_query: _semantic_asset_filter(current_workspace, current_query),
        response_builder=_search_response,
        search_similar_callback=search_similar,
    )


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
    group_details = _current_group_details(workspace, asset_ids)
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
            (group := group_details.get(result.asset_id, {})).get("group_id"),
            group.get("member_count"),
        )
        item["similarity"] = result.similarity
        item["similarity_kind"] = "text" if query_text is not None else "image"
        item["best_match_timestamp"] = result.best_timestamp
        item["source_match_timestamp"] = result.source_timestamp
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
    return semantic_asset_filter_service(
        workspace,
        query,
        folder_filter=_folder_filter,
        current_representatives=_current_representatives,
        current_recommendations=_current_recommendations,
    )


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
            return {
                "run_id": active["id"], "algorithm": active["algorithm"],
                "version": active["version"], "settings": _json_or_none(active["settings_json"]),
                "groups": [], "page": page, "page_size": page_size,
                "total": 0, "has_next": False, "filters": filters,
            }
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
    group_member_counts = {row["group_id"]: row["member_count"] for row in group_rows}
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
            group_member_counts[row["group_id"]],
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
            1,
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
        "sg.member_count >= 2",
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


def _current_group_details(workspace: Workspace, asset_ids: list[str]) -> dict[str, dict[str, object]]:
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
                SELECT sgm.logical_asset_id, sgm.group_id, sg.member_count
                FROM workspace_grouping AS wg
                JOIN strict_group_member AS sgm ON sgm.run_id = wg.active_run_id
                JOIN strict_group AS sg ON sg.run_id = sgm.run_id AND sg.group_id = sgm.group_id
                WHERE wg.id = 1 AND sgm.logical_asset_id IN ({placeholders})
                """,
                chunk,
            ).fetchall())
    finally:
        connection.close()
    return {
        row["logical_asset_id"]: {
            "group_id": row["group_id"],
            "member_count": int(row["member_count"]),
        }
        for row in rows
    }


def _current_group_ids(workspace: Workspace, asset_ids: list[str]) -> dict[str, str]:
    return {
        asset_id: details["group_id"]
        for asset_id, details in _current_group_details(workspace, asset_ids).items()
    }


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


def _asset_detail(workspace: Workspace, asset_id: str, handle: str) -> dict[str, object]:
    return asset_detail_service(
        workspace,
        asset_id,
        handle,
        _current_recommendations,
        _current_group_ids,
        _current_group_details,
    )


_asset_summary = asset_summary_service
_physical_rows = physical_rows_service
_physical_rows_for_assets = physical_rows_for_assets_service


def _jobs(workspace: Workspace, query: Mapping[str, list[str]]):
    limit = min(_positive_int(_first(query, "limit", "20"), "limit"), 100)
    connection = workspace.connect()
    try:
        rows = connection.execute("SELECT * FROM job ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    finally:
        connection.close()
    return [
        {
            **_row_dict(row),
            "substage": (substage := SUBSTAGES.get(row["id"])),
            "eta_seconds": _job_eta_seconds(workspace, row, substage),
        }
        for row in rows
    ]


_JOB_ETA_RATES = {
    "scan": ("scan.discovery", "scan.hashing"),
    "media_index": ("metadata.processing",),
    "media_thumbnails": ("thumbnail.image",),
    "media_quality": ("quality.image",),
    "raw_quality": ("quality.raw",),
    "video_quality": ("quality.video_sample",),
    "embeddings": ("embedding.image",),
    "visual_features": ("grouping.image",),
    "grouping": ("grouping.image",),
    "recommendations": ("recommendations.asset",),
    "reconciliation": ("reconciliation",),
}


def _job_eta_seconds(workspace: Workspace, row, substage: dict[str, object] | None) -> float | None:
    if row["status"] not in {"pending", "running"}:
        return None
    if substage is not None and substage.get("eta") is not None and isfinite(float(substage["eta"])):
        return max(0.0, float(substage["eta"]))
    total = max(0, int(row["total_items"] or 0))
    completed = max(0, int(row["completed_items"] or 0))
    started_at = row["started_at"]
    if total > completed > 0 and started_at:
        try:
            started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            elapsed = max(0.001, (datetime.now(timezone.utc) - started).total_seconds())
            return max(0.0, (total - completed) * elapsed / completed)
        except (TypeError, ValueError):
            pass
    rate_names = _JOB_ETA_RATES.get(row["kind"], ())
    if not rate_names:
        return 1.0
    rate = sum(_historical_rate(workspace, name) for name in rate_names)
    return max(1.0, max(1, total - completed) * rate)


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
