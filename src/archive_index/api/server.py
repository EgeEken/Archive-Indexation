"""Loopback-only localhost API and UI server."""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
import webbrowser
from collections.abc import Mapping
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from ..app_state import WorkspaceRegistry, workspace_id
from ..indexing.grouping import build_groups, extract_visual_features
from ..indexing.media_pipeline import index_workspace
from ..indexing.recommendation import build_recommendations
from ..indexing.scanner import scan
from ..jobs.engine import JobStore
from ..workspace import Workspace, WorkspaceError

LOGGER = logging.getLogger(__name__)
MAX_PAGE_SIZE = 180


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
                workspace = self._workspaces.get(handle) or self.registry.open(handle)
                self._register_workspace(workspace)
                entries.append(_workspace_entry(handle, workspace, recent=True))
            except WorkspaceError:
                entries.append({"id": handle, "name": Path(path).name, "path": path, "available": False})
            seen.add(handle)
        return entries

    def open_workspace(self, path: str, create: bool = False) -> dict[str, object]:
        if not isinstance(path, str) or not path.strip():
            raise InvalidRequest("a workspace folder path is required")
        root = Path(path).expanduser().resolve(strict=False)
        try:
            workspace = Workspace.create(root) if not (root / ".archive-index").is_dir() else Workspace.open(root)
        except (WorkspaceError, OSError) as error:
            raise InvalidRequest(str(error)) from error
        handle = self._register_workspace(workspace)
        self.registry.add(workspace)
        job_id = self.start_indexing(handle)
        return {"workspace": _workspace_entry(handle, workspace, recent=True), "job_id": job_id}

    def remove_workspace(self, handle: str) -> bool:
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
        try:
            scan_result = scan(workspace, job_id=job_id, cancel_event=cancel_event)
            if scan_result.cancelled:
                return
            media_job_id = JobStore(workspace).create("media_index")
            with self._active_lock:
                self._cancel_events[(handle, media_job_id)] = cancel_event
            job_ids.append(media_job_id)
            current_job_id = media_job_id
            media_result = index_workspace(workspace, job_id=media_job_id, cancel_event=cancel_event)
            if media_result.cancelled:
                return
            feature_job_id = JobStore(workspace).create("visual_features")
            with self._active_lock:
                self._cancel_events[(handle, feature_job_id)] = cancel_event
            job_ids.append(feature_job_id)
            current_job_id = feature_job_id
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
            LOGGER.exception("workspace indexing failed")
            try:
                row = JobStore(workspace).get(current_job_id)
                if row is not None and row["status"] in {"pending", "running"}:
                    JobStore(workspace).fail(current_job_id)
            except Exception:
                LOGGER.exception("could not mark workspace job failed")
        finally:
            with self._active_lock:
                for active_job_id in job_ids:
                    self._cancel_events.pop((handle, active_job_id), None)
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
            handle, workspace = self._workspace(query)
            if request.path == "/api/workspace":
                self._send_json(200, _workspace_summary(workspace, handle))
            elif request.path == "/api/folders":
                self._send_json(200, {"folders": _folders(workspace)})
            elif request.path == "/api/assets":
                self._send_json(200, _assets(workspace, query, handle))
            elif request.path == "/api/jobs":
                self._send_json(200, {"jobs": _jobs(workspace, query)})
            elif request.path == "/api/problems":
                self._send_json(200, {"problems": _problems(workspace, query)})
            elif request.path == "/api/groups":
                self._send_json(200, _groups(workspace, query, handle))
            elif request.path == "/api/recommendations":
                self._send_json(200, _recommendations(workspace))
            else:
                self._handle_resource_get(request.path, workspace, handle)
        except InvalidRequest as error:
            self._send_json(400, {"error": str(error)})
        except ResourceNotFound as error:
            self._send_json(404, {"error": str(error)})
        except Exception:
            LOGGER.exception("GET %s failed", request.path)
            self._send_json(500, {"error": "internal server error"})

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
            if request.path == "/api/workspaces/remove":
                body = self._json_body()
                handle = body.get("workspace") or body.get("id")
                if not isinstance(handle, str):
                    raise InvalidRequest("workspace is required")
                self._send_json(200, {"removed": self.server.remove_workspace(handle)})
                return
            handle, workspace = self._workspace(query)
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

    def _handle_resource_get(self, path: str, workspace: Workspace, handle: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "assets"] and parts[3] == "thumbnail":
            self._serve_asset_thumbnail(workspace, parts[2])
            return
        if len(parts) == 3 and parts[:2] == ["api", "assets"]:
            self._send_json(200, _asset_detail(workspace, parts[2], handle))
            return
        if len(parts) == 4 and parts[:2] == ["api", "files"] and parts[3] == "original":
            self._serve_original(workspace, parts[2])
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
                SELECT pf.is_online, cs.status, cs.output_path
                FROM physical_file AS pf
                LEFT JOIN component_state AS cs
                    ON cs.physical_file_id = pf.id AND cs.component = 'thumbnail'
                WHERE pf.logical_asset_id = ?
                ORDER BY pf.is_online DESC, pf.id
                """,
                (asset_id,),
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            if row["status"] == "complete" and row["output_path"]:
                self._send_validated_thumbnail(workspace, row["output_path"])
                return
        raise ResourceNotFound("thumbnail is not available")

    def _serve_thumbnail(self, workspace: Workspace, physical_id: str) -> None:
        connection = workspace.connect()
        try:
            row = connection.execute(
                "SELECT status, output_path FROM component_state WHERE physical_file_id = ? AND component = 'thumbnail'",
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
                "SELECT relative_path, is_online FROM physical_file WHERE id = ?", (physical_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None or not row["is_online"]:
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


def serve(workspace: Workspace | None = None, host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("the localhost server must bind to 127.0.0.1 or localhost")
    server = WorkspaceHTTPServer((host, port), workspace)
    if workspace is not None:
        server.registry.add(workspace)
        server.start_indexing(server.default_handle)
    suffix = f"?workspace={server.default_handle}" if server.default_handle else ""
    url = f"http://{host}:{server.server_port}/{suffix}"
    print(f"Archive Indexation UI: {url}")
    webbrowser.open(url, new=2)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _ui_html() -> str:
    return files("archive_index.web").joinpath("index.html").read_text(encoding="utf-8")


def _ui_resource(name: str) -> str:
    if name not in {"app.css", "app.js"}:
        raise ResourceNotFound("resource not found")
    return files("archive_index.web").joinpath(name).read_text(encoding="utf-8")


def _pick_workspace_path() -> str:
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choose archive workspace folder")
        root.destroy()
        return path or ""
    except Exception as error:
        raise InvalidRequest("the native folder picker is unavailable; enter the path manually") from error


def _workspace_entry(handle: str, workspace: Workspace, recent: bool) -> dict[str, object]:
    summary = _workspace_summary(workspace, handle)
    summary["recent"] = recent
    summary["available"] = True
    return summary


def _workspace_summary(workspace: Workspace, handle: str) -> dict[str, object]:
    connection = workspace.connect()
    try:
        assets = connection.execute("SELECT COUNT(*) FROM logical_asset").fetchone()[0]
        physical_files = connection.execute("SELECT COUNT(*) FROM physical_file").fetchone()[0]
        online = connection.execute("SELECT COUNT(*) FROM physical_file WHERE is_online = 1").fetchone()[0]
        latest = connection.execute("SELECT MAX(finished_at) FROM job WHERE status = 'complete'").fetchone()[0]
    finally:
        connection.close()
    return {
        "id": handle,
        "name": workspace.root.name or str(workspace.root),
        "path": str(workspace.root),
        "root": str(workspace.root),
        "assets": assets,
        "physical_files": physical_files,
        "online_files": online,
        "offline_files": physical_files - online,
        "last_indexed": latest,
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

    clauses = ["1 = 1"]
    params: list[object] = []
    if folder:
        escaped = _like_value(folder)
        clauses.append(
            "EXISTS (SELECT 1 FROM physical_file AS pf_folder "
            "WHERE pf_folder.logical_asset_id = la.id "
            "AND (pf_folder.relative_path = ? OR pf_folder.relative_path LIKE ? ESCAPE '\\'))"
        )
        params.extend([folder, f"{escaped}/%"])
    if media_type:
        clauses.append("EXISTS (SELECT 1 FROM physical_file AS pf_type WHERE pf_type.logical_asset_id = la.id AND pf_type.media_type = ?)")
        params.append(media_type)
    if search:
        clauses.append(
            "EXISTS (SELECT 1 FROM physical_file AS pf_search "
            "WHERE pf_search.logical_asset_id = la.id "
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
                    WHERE pf_quality.logical_asset_id = la.id) AS quality_score,
                   (SELECT MIN(LOWER(pf_sort.filename)) FROM physical_file AS pf_sort
                    WHERE pf_sort.logical_asset_id = la.id) AS filename_sort
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


def _groups(workspace: Workspace, query: Mapping[str, list[str]], handle: str) -> dict[str, object]:
    page = _positive_int(_first(query, "page", "1"), "page")
    page_size = min(_positive_int(_first(query, "page_size", "10"), "page_size"), 60)
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
        condition = ""
        total = connection.execute(
            f"SELECT COUNT(*) FROM strict_group WHERE run_id = ? {condition}", (active["id"],)
        ).fetchone()[0]
        group_rows = connection.execute(
            f"""
            SELECT * FROM strict_group
            WHERE run_id = ? {condition}
            ORDER BY CASE WHEN first_capture_time IS NULL THEN 1 ELSE 0 END,
                     first_capture_time, group_id
            LIMIT ? OFFSET ?
            """,
            (active["id"], page_size, (page - 1) * page_size),
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
                "member_count": row["member_count"],
                "first_capture_time": row["first_capture_time"],
                "representative_asset_id": representative_id,
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
    }


def _current_representatives(workspace: Workspace) -> tuple[set[str], bool]:
    connection = workspace.connect()
    try:
        active = connection.execute(
            "SELECT active_run_id FROM workspace_grouping WHERE id = 1"
        ).fetchone()
        if active is None:
            return set(), False
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
    return {row["representative_logical_asset_id"] for row in rows}, True


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
            "SELECT logical_asset_id FROM asset_recommendation WHERE run_id = ? AND auto_recommended = 1",
            (active["active_run_id"],),
        ).fetchall()
    finally:
        connection.close()
    return {row["logical_asset_id"] for row in rows}, active["active_run_id"]


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
) -> dict[str, object]:
    physical = _physical_rows(workspace, asset["id"]) if physical is None else physical
    representative = sorted(
        physical,
        key=lambda row: (not bool(row["is_online"]), row["media_type"] != "image", row["relative_path"]),
    )[0]
    online = next((row for row in physical if row["is_online"]), None)
    thumbnail = next(
        (row for row in physical if row["thumbnail_status"] == "complete" and row["thumbnail_output_path"] and _valid_index_file(workspace, row["thumbnail_output_path"])),
        None,
    )
    return {
        "asset_id": asset["id"],
        "media_type": asset["media_type"],
        "capture_time": asset["capture_time"],
        "capture_time_kind": asset["capture_time_kind"],
        "filename": representative["filename"],
        "relative_path": representative["relative_path"],
        "is_online": any(row["is_online"] for row in physical),
        "physical_count": len(physical),
        "online_count": sum(bool(row["is_online"]) for row in physical),
        "thumbnail_url": _url(f"/api/assets/{asset['id']}/thumbnail", handle) if thumbnail else None,
        "original_url": _url(f"/api/files/{online['id']}/original", handle) if online else None,
        "quality_score": max((row["quality_score"] for row in physical if row["quality_score"] is not None), default=None),
        "issues": _asset_issues(physical),
        "is_representative": is_representative,
        "auto_recommended": is_recommended,
        "user_decision": asset["selection_state"],
        "user_decision_updated_at": asset["selection_updated_at"],
        "recommendation_run_id": recommendation_run_id,
    }


def _asset_issues(physical) -> list[str]:
    issues: list[str] = []
    if not any(row["is_online"] for row in physical):
        issues.append("offline")
    statuses = [row[key] for row in physical for key in ("metadata_status", "thumbnail_status", "quality_component_status")]
    if any(status in {"pending", "running"} for status in statuses):
        issues.append("processing")
    if any(status == "unsupported" for status in statuses):
        issues.append("unsupported")
    if any(status == "failed" for status in statuses):
        issues.append("failed")
    return issues


def _asset_detail(workspace: Workspace, asset_id: str, handle: str) -> dict[str, object]:
    connection = workspace.connect()
    try:
        asset = connection.execute("SELECT * FROM logical_asset WHERE id = ?", (asset_id,)).fetchone()
    finally:
        connection.close()
    if asset is None:
        raise ResourceNotFound("asset not found")
    physical = _physical_rows(workspace, asset_id)
    recommendation_ids, recommendation_run_id = _current_recommendations(workspace)
    return {
        "asset_id": asset["id"],
        "media_type": asset["media_type"],
        "capture_time": asset["capture_time"],
        "capture_time_kind": asset["capture_time_kind"],
        "auto_recommended": asset_id in recommendation_ids,
        "user_decision": asset["selection_state"],
        "user_decision_updated_at": asset["selection_updated_at"],
        "recommendation_run_id": recommendation_run_id,
        "physical_files": [
            {
                "id": row["id"],
                "relative_path": row["relative_path"],
                "filename": row["filename"],
                "extension": row["extension"],
                "media_type": row["media_type"],
                "role": row["role"],
                "size_bytes": row["size_bytes"],
                "is_online": bool(row["is_online"]),
                "width": row["width"],
                "height": row["height"],
                "duration_seconds": row["duration_seconds"],
                "codec": row["codec"],
                "metadata": _json_or_none(row["metadata_json"]),
                "quality_score": row["quality_score"],
                "quality_raw": _json_or_none(row["quality_raw_json"]),
                "quality_components": _json_or_none(row["quality_components_json"]),
                "original_url": _url(f"/api/files/{row['id']}/original", handle) if row["is_online"] else None,
                "thumbnail_url": _url(f"/api/files/{row['id']}/thumbnail", handle) if row["thumbnail_status"] == "complete" and row["thumbnail_output_path"] and _valid_index_file(workspace, row["thumbnail_output_path"]) else None,
                "components": {
                    "metadata": _component_info(row, "metadata"),
                    "thumbnail": _component_info(row, "thumbnail"),
                    "quality": _component_info(row, "quality"),
                },
            }
            for row in physical
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
            WHERE pf.logical_asset_id IN ({placeholders})
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


def _component_info(row, component: str) -> dict[str, object]:
    prefix = "quality_component_" if component == "quality" else f"{component}_"
    return {"status": row[f"{prefix}status"], "algorithm": row[f"{prefix}algorithm"], "version": row[f"{prefix}version"], "error": row[f"{prefix}error"]}


def _jobs(workspace: Workspace, query: Mapping[str, list[str]]):
    limit = min(_positive_int(_first(query, "limit", "20"), "limit"), 100)
    connection = workspace.connect()
    try:
        rows = connection.execute("SELECT * FROM job ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
    finally:
        connection.close()
    return [_row_dict(row) for row in rows]


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
            (limit,),
        ).fetchall()
    finally:
        connection.close()
    return [_row_dict(row) for row in rows]


def _folders(workspace: Workspace) -> list[str]:
    connection = workspace.connect()
    try:
        paths = [row[0] for row in connection.execute("SELECT relative_path FROM physical_file")]
    finally:
        connection.close()
    folders: set[str] = set()
    for path in paths:
        parts = Path(path).parts[:-1]
        for index in range(1, len(parts) + 1):
            folders.add("/".join(parts[:index]))
    return sorted(folders, key=str.casefold)


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
