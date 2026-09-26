"""Workspace registry, setup, configuration, and cleanup services."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from ..configuration import configuration_from_connection, default_configuration, normalize_configuration, path_in_scope
from ..embeddings.models import OPENCLIP_PROVIDER, SIGLIP_PROVIDER, model_status
from ..app_state import workspace_id
from ..indexing.representations import preferred_physical
from ..jobs.engine import JobStore
from ..media.quality_provider import LAR_IQA_MODEL_FILENAME, LAR_IQA_MODEL_ID, default_model_path
from ..media_types import is_raw_extension
from ..planning import analyze_folder, plan_from_analysis
from ..workspace import Workspace, WorkspaceError
from .errors import InvalidRequest, ResourceNotFound


def register_workspace(host, workspace: Workspace) -> str:
    handle = workspace_id(workspace)
    host._workspaces[handle] = workspace
    from ..file_management_executor import recover_workspace

    recover_workspace(workspace)
    if handle not in host._recovered_workspaces:
        JobStore(workspace).recover_interrupted()
        host._recovered_workspaces.add(handle)
    return handle


def resolve_workspace(host, handle: str | None) -> tuple[str, Workspace]:
    selected = handle or host.default_handle
    if not selected:
        raise InvalidRequest("select a workspace first")
    workspace = host._workspaces.get(selected)
    if workspace is None:
        try:
            workspace = host.registry.open(selected)
        except WorkspaceError as error:
            raise ResourceNotFound("workspace is unavailable") from error
        register_workspace(host, workspace)
    return selected, workspace


def list_workspaces(host) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    if host.workspace is not None:
        handle = register_workspace(host, host.workspace)
        entries.append(_workspace_entry(handle, host.workspace, recent=False))
        seen.add(handle)
    for entry in host.registry.entries():
        handle = entry.get("id")
        path = entry.get("path")
        if not isinstance(handle, str) or not isinstance(path, str) or handle in seen:
            continue
        try:
            workspace = host._workspaces.get(handle)
            entries.append(
                _workspace_entry(handle, workspace, recent=True)
                if workspace is not None
                else _read_only_workspace_entry(handle, path)
            )
        except (WorkspaceError, OSError, sqlite3.Error, ValueError):
            entries.append({"id": handle, "name": Path(path).name, "path": path, "available": False})
        seen.add(handle)
    return entries


def home_thumbnail(host, handle: str) -> Path:
    workspace = host._workspaces.get(handle)
    root = workspace.root if workspace is not None else None
    if root is None:
        entry = next((candidate for candidate in host.registry.entries() if candidate.get("id") == handle), None)
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ResourceNotFound("workspace is unavailable")
        root = Path(entry["path"]).expanduser().resolve(strict=False)
    database_path = root / ".archive-index" / "index.sqlite"
    if not database_path.is_file():
        raise ResourceNotFound("workspace database is unavailable")
    connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True, timeout=0.25)
    connection.row_factory = sqlite3.Row
    try:
        identity = connection.execute("SELECT workspace_id FROM workspace_info WHERE id = 1").fetchone()
        if identity is None or identity["workspace_id"] != handle:
            raise ResourceNotFound("workspace identity does not match recent entry")
        output_path = _home_thumbnail_path(connection, root)
    finally:
        connection.close()
    if output_path is None:
        raise ResourceNotFound("thumbnail is not available")
    return Workspace(root).index_path(output_path)


def offline_media_info(host, handle: str) -> dict[str, int]:
    _, workspace = resolve_workspace(host, handle)
    _require_workspace_root(workspace)
    connection = workspace.connect()
    try:
        with connection:
            rows = _offline_media_candidates(workspace, connection)
    finally:
        connection.close()
    return _offline_media_counts(rows)


def forget_offline_media(host, handle: str) -> dict[str, int]:
    _, workspace = resolve_workspace(host, handle)
    _require_workspace_root(workspace)
    with host._active_lock:
        thread = host._active_threads.get(handle)
    if thread is not None and thread.is_alive():
        raise InvalidRequest("offline media cannot be forgotten while a job is running")
    if _active_job(workspace) is not None:
        raise InvalidRequest("offline media cannot be forgotten while a job is running")
    connection = workspace.connect()
    try:
        with connection:
            rows = _offline_media_candidates(workspace, connection)
            counts = _offline_media_counts(rows)
            if not rows:
                return {"removed": 0, **counts}
            connection.execute("CREATE TEMP TABLE offline_cleanup_assets (id TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO offline_cleanup_assets SELECT DISTINCT logical_asset_id FROM physical_file WHERE is_online = 0")
            connection.execute("DELETE FROM job_error WHERE physical_file_id IN (SELECT id FROM physical_file WHERE is_online = 0)")
            connection.execute("DELETE FROM reconciliation_conflict WHERE left_logical_asset_id IN (SELECT id FROM offline_cleanup_assets) OR right_logical_asset_id IN (SELECT id FROM offline_cleanup_assets)")
            connection.execute("DELETE FROM physical_file WHERE is_online = 0")
            connection.execute("DELETE FROM logical_asset WHERE id IN (SELECT id FROM offline_cleanup_assets) AND NOT EXISTS (SELECT 1 FROM physical_file WHERE physical_file.logical_asset_id = logical_asset.id)")
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            connection.execute("UPDATE workspace_grouping SET active_run_id = NULL, updated_at = ? WHERE id = 1", (now,))
            connection.execute("UPDATE workspace_recommendation SET active_run_id = NULL, updated_at = ? WHERE id = 1", (now,))
            connection.execute("UPDATE workspace_reconciliation SET active_run_id = NULL, updated_at = ? WHERE id = 1", (now,))
            connection.execute("UPDATE workspace_embedding SET active_provider = NULL, active_run_id = NULL, updated_at = ? WHERE id = 1", (now,))
    finally:
        connection.close()
    return {"removed": len(rows), **counts}


def open_workspace(host, path: str, create: bool = False) -> dict[str, object]:
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
    handle = register_workspace(host, workspace)
    host.registry.add(workspace)
    return {"workspace": _workspace_entry(handle, workspace, recent=True), "job_id": None}


def analyze_workspace(host, path: str) -> dict[str, object]:
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
            workspace = next((w for w in host._workspaces.values() if w.root == root), None) or Workspace.open(root)
            handle = register_workspace(host, workspace)
            configuration = workspace.configuration()
        except (WorkspaceError, OSError) as error:
            raise InvalidRequest(str(error)) from error
    return {"path": str(root), "indexed": indexed, "workspace": handle, "analysis": analysis, "configuration": configuration}


def plan_workspace_configuration(host, path: str, configuration: dict[str, object], analysis: dict[str, object] | None = None) -> dict[str, object]:
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
            (next((w for w in host._workspaces.values() if w.root == root), None) or Workspace.open(root))
            if (root / ".archive-index").is_dir()
            else None
        )
        plan = plan_from_analysis(source_analysis, normalized, existing_workspace)
        plan.update(_configuration_quality_readiness(normalized))
        embedding_plan = _embedding_plan(normalized, existing_workspace, plan)
        plan.update(embedding_plan)
        eta = dict(plan.get("eta_seconds_by_feature", {}))
        eta["semantic_search"] = embedding_plan["embedding_estimated_seconds"] if normalized["semantic_search_enabled"] else 0
        for key in ("embedding_initialization", "image_embeddings", "video_embeddings"):
            eta.pop(key, None)
        plan["eta_seconds_by_feature"] = eta
        plan["estimated_seconds"] = round(sum(eta.values()), 1)
    except (ValueError, OSError) as error:
        raise InvalidRequest(str(error)) from error
    return {"path": str(root), "configuration": normalized, "plan": plan}


def apply_workspace_configuration(host, configuration: dict[str, object], *, handle: str | None = None, path: str | None = None, prepare_search_callback) -> dict[str, object]:
    if handle:
        selected, workspace = resolve_workspace(host, handle)
    else:
        if not isinstance(path, str) or not path.strip():
            raise InvalidRequest("a workspace folder path is required")
        root = Path(path).expanduser().resolve(strict=False)
        try:
            workspace = Workspace.open(root) if (root / ".archive-index").is_dir() else Workspace.create(root)
        except (WorkspaceError, OSError) as error:
            raise InvalidRequest(str(error)) from error
        selected = register_workspace(host, workspace)
    try:
        config = workspace.apply_configuration(configuration)
    except (ValueError, WorkspaceError) as error:
        raise InvalidRequest(str(error)) from error
    prepare_search_callback(workspace)
    host.registry.add(workspace)
    job_id = host.start_indexing(selected)
    return {"workspace": _workspace_entry(selected, workspace, recent=True), "configuration": config, "job_id": job_id}


def remove_workspace(host, handle: str) -> bool:
    return host.registry.remove(handle)


def workspace_removal_info(host, handle: str) -> dict[str, object]:
    entry = next((entry for entry in host.registry.entries() if entry.get("id") == handle), None)
    if entry is None:
        raise ResourceNotFound("workspace is unavailable")
    try:
        _, workspace = resolve_workspace(host, handle)
        if not workspace.root.is_dir() or not workspace.database_path.is_file():
            raise WorkspaceError("workspace database is unavailable")
        index_size_bytes = _index_size_bytes(workspace.index_directory)
        active_job = _active_job(workspace)
    except (ResourceNotFound, WorkspaceError, OSError, sqlite3.Error, InvalidRequest):
        host._workspaces.pop(handle, None)
        return {"workspace": handle, "index_size_bytes": 0, "active_job": None, "available": False}
    return {"workspace": handle, "index_size_bytes": index_size_bytes, "active_job": active_job, "available": True}


def remove_workspace_with_index(host, handle: str, delete_index: bool) -> bool:
    if not delete_index:
        host._workspaces.pop(handle, None)
        return host.registry.remove(handle)
    entry = next((entry for entry in host.registry.entries() if entry.get("id") == handle), None)
    if entry is None:
        return False
    path = entry.get("path")
    if not isinstance(path, str):
        host._workspaces.pop(handle, None)
        return host.registry.remove(handle)
    root = Path(path)
    if not root.is_dir() or not (root / ".archive-index" / "index.sqlite").is_file():
        host._workspaces.pop(handle, None)
        return host.registry.remove(handle)
    try:
        _, workspace = resolve_workspace(host, handle)
    except (ResourceNotFound, WorkspaceError, OSError, sqlite3.Error):
        host._workspaces.pop(handle, None)
        return host.registry.remove(handle)
    with host._active_lock:
        thread = host._active_threads.get(handle)
    if thread is not None and thread.is_alive():
        thread.join(timeout=5)
    with host._active_lock:
        if thread is not None and thread.is_alive():
            raise InvalidRequest("the workspace index is still shutting down")
    try:
        active = _active_job(workspace)
    except (OSError, sqlite3.Error):
        host._workspaces.pop(handle, None)
        return host.registry.remove(handle)
    if active is not None:
        raise InvalidRequest("the workspace index cannot be deleted while a job is running")
    _delete_owned_index(workspace)
    host._workspaces.pop(handle, None)
    host._recovered_workspaces.discard(handle)
    return host.registry.remove(handle)


def _active_job(workspace: Workspace) -> dict[str, object] | None:
    connection = workspace.connect()
    try:
        row = connection.execute("SELECT id, kind, status FROM job WHERE status IN ('pending', 'running') ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        connection.close()
    return dict(row) if row is not None else None


def _require_workspace_root(workspace: Workspace) -> None:
    if not workspace.root.is_dir() or not os.access(workspace.root, os.R_OK):
        raise InvalidRequest("the workspace root is unavailable or unreadable")


def _offline_media_candidates(workspace: Workspace, connection) -> list[sqlite3.Row]:
    rows = connection.execute("SELECT id, logical_asset_id, media_type, relative_path FROM physical_file WHERE is_online = 0").fetchall()
    reconnected = []
    for row in rows:
        try:
            if workspace.absolute_path(row["relative_path"]).is_file():
                reconnected.append(row["id"])
        except (OSError, WorkspaceError, ValueError):
            continue
    if reconnected:
        placeholders = ",".join("?" for _ in reconnected)
        connection.execute(
            f"UPDATE physical_file SET is_online = 1, updated_at = ? WHERE id IN ({placeholders})",
            [datetime.now(timezone.utc).isoformat(timespec="seconds"), *reconnected],
        )
    reconnected_ids = set(reconnected)
    return [row for row in rows if row["id"] not in reconnected_ids]


def _offline_media_counts(rows) -> dict[str, int]:
    return {"images": sum(row["media_type"] == "image" for row in rows), "videos": sum(row["media_type"] == "video" for row in rows), "count": len(rows)}


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
    expected = root / ".archive-index"
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
    assets = connection.execute("SELECT COUNT(DISTINCT la.id) FROM logical_asset AS la JOIN physical_file AS pf ON pf.logical_asset_id = la.id WHERE pf.in_scope = 1").fetchone()[0]
    physical_files = connection.execute("SELECT COUNT(*) FROM physical_file WHERE in_scope = 1").fetchone()[0]
    online = connection.execute("SELECT COUNT(*) FROM physical_file WHERE in_scope = 1 AND is_online = 1").fetchone()[0]
    out_of_scope = connection.execute("SELECT COUNT(*) FROM physical_file WHERE in_scope = 0").fetchone()[0]
    latest = connection.execute("SELECT MAX(finished_at) FROM job WHERE status = 'complete'").fetchone()[0]
    embedding_storage = connection.execute("SELECT COALESCE((SELECT SUM(length(embedding)) FROM logical_asset_embedding), 0) + COALESCE((SELECT SUM(length(embedding)) FROM video_frame_embedding), 0)").fetchone()[0]
    configuration = configuration_from_connection(connection)
    thumbnail = _home_thumbnail_path(connection, root)
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
        "thumbnail_url": _url("/api/workspaces/thumbnail", handle) if thumbnail else None,
    }


def _home_thumbnail_path(connection, root: Path) -> str | None:
    workspace = Workspace(root)
    rows = connection.execute(
        """
        SELECT pf.logical_asset_id, thumbnails.output_path,
               (SELECT MAX(pf_quality.quality_score)
                  FROM physical_file AS pf_quality
                 WHERE pf_quality.logical_asset_id = pf.logical_asset_id
                   AND pf_quality.media_type = 'image'
                   AND pf_quality.in_scope = 1
                   AND pf_quality.is_online = 1) AS asset_quality,
               pf.relative_path, pf.id
          FROM physical_file AS pf
          JOIN component_state AS thumbnails
            ON thumbnails.physical_file_id = pf.id
           AND thumbnails.component = 'thumbnail'
           AND thumbnails.status = 'complete'
         WHERE pf.media_type = 'image'
           AND pf.in_scope = 1
           AND pf.is_online = 1
           AND thumbnails.output_path IS NOT NULL
         ORDER BY asset_quality IS NULL, asset_quality DESC, pf.relative_path, pf.id
        """
    ).fetchall()
    for row in rows:
        try:
            if workspace.index_path(row["output_path"]).is_file():
                return row["output_path"]
        except WorkspaceError:
            continue
    return None


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
            raw = {**raw, "status": "raw_preview_missing", "ready": False, "message": "Install the optional raw-preview dependency to assess RAW-only assets."}
    video = _quality_readiness(configuration["rendered_quality_provider"] if configuration["video_quality_enabled"] else "off")
    return {"lar_iqa_readiness": lar_iqa, "quality_readiness": rendered, "rendered_quality_readiness": rendered, "raw_quality_readiness": raw, "video_quality_readiness": video, "embedding_readiness": _embedding_readiness(configuration)}


def _embedding_model_status(provider: str) -> dict[str, object]:
    status = model_status(provider)
    return {"provider": status["provider"], "model_id": status["model_id"], "version": status["version"], "dimension": status["dimension"], "installed": status["installed"], "cache_bytes": status["cache_bytes"], "expected_download_bytes": status["expected_download_bytes"]}


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
    from ..indexing.embeddings import _input_fingerprint, _source, _source_units

    provider = create_embedding_provider(configuration["embedding_provider"])
    images = videos = reusable = unknown_videos = 0
    run_id = None
    if workspace is not None:
        connection = workspace.connect()
        try:
            rows = connection.execute("SELECT * FROM physical_file WHERE is_online = 1 ORDER BY logical_asset_id, relative_path").fetchall()
            run = connection.execute("SELECT id FROM embedding_run WHERE provider = ? AND model_version = ? AND settings_json = ? ORDER BY created_at DESC LIMIT 1", (provider.provider_id, provider.version, json.dumps(provider.settings, ensure_ascii=False, sort_keys=True))).fetchone()
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
            if kind == "video":
                videos += units
            else:
                images += 1
            fingerprint = _input_fingerprint(source, provider)
            state = states.get(selected["id"])
            valid = state and state["status"] == "complete" and state["version"] == provider.version and state["input_fingerprint"] == fingerprint
            stored = video_cache.get((asset_id, selected["id"]), 0) == units if kind == "video" else image_cache.get((asset_id, selected["id"])) == fingerprint
            if valid and stored:
                reusable += units
    elif filesystem_plan:
        categories = filesystem_plan.get("selected_categories", {})
        images = sum(categories.get(k, 0) for k in ("jpeg", "other_image", "raw"))
        unknown_videos = categories.get("video", 0) if configuration.get("include_videos_in_semantic_search", True) else 0
        if not configuration.get("include_videos_in_semantic_search", True):
            videos = 0
    total = images + videos
    pending = total - reusable
    return {"embedding_image_count": images, "embedding_video_sample_count": videos, "embedding_total_vectors": total, "embedding_pending_count": pending, "embedding_cached_count": reusable, "embedding_unknown_videos": unknown_videos, "embedding_counts_estimated": workspace is None, "embedding_estimated_storage_bytes": pending * provider.dimension * 2, "embedding_image_estimated_seconds": round(images * 0.25, 1), "embedding_video_estimated_seconds": round(videos * 0.30, 1), "embedding_estimated_seconds": round((8.0 if pending else 0.0) + images * 0.25 + videos * 0.30, 1), "embedding_active_run_id": run_id}


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
    return {"status": status, "ready": runtime_ready and checkpoint_ready, "runtime_ready": runtime_ready, "checkpoint_ready": checkpoint_ready, "model": {"provider": "lar-iqa", "model_id": LAR_IQA_MODEL_ID, "filename": LAR_IQA_MODEL_FILENAME, "installed": checkpoint_ready, "size_bytes": checkpoint_size}, "message": message}


def _url(path: str, handle: str) -> str:
    from urllib.parse import urlencode

    return f"{path}?{urlencode({'workspace': handle})}"
