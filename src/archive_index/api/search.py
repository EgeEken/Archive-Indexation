"""Semantic search readiness and query-facing API services."""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Mapping

from ..embeddings.models import OPENCLIP_PROVIDER, model_status
from ..embeddings.search import active_embedding, prepare_provider, provider_state, request_text, search_similar, search_text, select_provider
from ..workspace import Workspace
from .errors import InvalidRequest

MAX_PAGE_SIZE = 180
IMAGE_SIMILARITY_SUMMARY_THRESHOLD = 0.90


def prepare_search(workspace: Workspace, *, model_status_callback=model_status, find_spec_callback=importlib.util.find_spec, select_provider_callback=select_provider) -> None:
    config = workspace.configuration()
    provider = config["embedding_provider"]
    runtime = "open_clip" if provider == OPENCLIP_PROVIDER else "transformers"
    enabled = config["semantic_search_enabled"] and model_status_callback(provider)["installed"] and find_spec_callback(runtime) is not None
    select_provider_callback(provider if enabled else None)


def search_status(workspace: Workspace, *, model_status_callback=model_status, find_spec_callback=importlib.util.find_spec, active_embedding_callback=active_embedding, provider_state_callback=provider_state, prepare_provider_callback=prepare_provider) -> dict[str, object]:
    configuration = workspace.configuration()
    provider = configuration["embedding_provider"]
    label = "OpenCLIP" if provider == OPENCLIP_PROVIDER else "SigLIP2"
    if not configuration["semantic_search_enabled"]:
        return {"state": "unavailable", "message": "Semantic search disabled · Configure to enable", "provider": label}
    if not model_status_callback(provider)["installed"]:
        return {"state": "missing_model", "message": f"{label} model not installed", "provider": label}
    runtime = "open_clip" if provider == OPENCLIP_PROVIDER else "transformers"
    if find_spec_callback(runtime) is None:
        return {"state": "unavailable", "message": f"{label} runtime unavailable", "provider": label}
    active = active_embedding_callback(workspace)
    if active is None or active["active_run_id"] is None or active["status"] != "complete":
        return {"state": "missing_embeddings", "message": "Embeddings not indexed · Re-index required", "provider": label}
    state = provider_state_callback(provider)
    message = {
        "ready": f"Semantic search ready · {label}",
        "available": f"Semantic search available · {label}",
        "loading": f"Preparing semantic search · {label}…",
        "failed": f"Search failed: {label} could not be loaded",
    }[state]
    if state == "failed":
        message = f"Search failed: {prepare_provider_callback(provider).exception()}"
    return {"state": state, "message": message, "provider": label}


def semantic_search(
    workspace: Workspace,
    query: Mapping[str, list[str]],
    handle: str,
    *,
    asset_filter: Callable[[Workspace, Mapping[str, list[str]]], set[str]],
    response_builder: Callable,
    search_text_callback=search_text,
) -> dict[str, object]:
    text = _first(query, "text", "").strip()
    if not text:
        raise InvalidRequest("text query is required")
    if len(text) > 500:
        raise InvalidRequest("text query is too long")
    page = _positive_int(_first(query, "page", "1"), "page")
    page_size = min(_positive_int(_first(query, "page_size", "60"), "page_size"), MAX_PAGE_SIZE)
    allowed_asset_ids = asset_filter(workspace, query)
    try:
        results = search_text_callback(workspace, text, allowed_asset_ids=allowed_asset_ids, top_k=max(len(allowed_asset_ids), page * page_size))
    except (RuntimeError, ValueError) as error:
        raise InvalidRequest(str(error)) from error
    return response_builder(workspace, handle, results, page, page_size, text)


def similar_assets(
    workspace: Workspace,
    asset_id: str,
    handle: str,
    query: Mapping[str, list[str]] | None,
    *,
    asset_filter: Callable[[Workspace, Mapping[str, list[str]]], set[str]],
    response_builder: Callable,
    search_similar_callback=search_similar,
) -> dict[str, object]:
    allowed = asset_filter(workspace, {})
    query = query or {}
    offset = _positive_int(_first(query, "offset", "0"), "offset") if _first(query, "offset", "0") != "0" else 0
    limit = min(_positive_int(_first(query, "limit", "12"), "limit"), MAX_PAGE_SIZE)
    try:
        results = search_similar_callback(workspace, asset_id, allowed_asset_ids=allowed, top_k=len(allowed))
    except (RuntimeError, ValueError) as error:
        raise InvalidRequest(str(error)) from error
    strong_count = sum(result.similarity >= IMAGE_SIMILARITY_SUMMARY_THRESHOLD for result in results)
    if _first(query, "initial", "") == "1":
        response = response_builder(workspace, handle, results, 1, limit, None, start_offset=0, selection_limit=min(strong_count, limit))
    else:
        response = response_builder(workspace, handle, results, 1, limit, None, start_offset=offset)
    response["strong_count"] = strong_count
    return response


def semantic_asset_filter(
    workspace: Workspace,
    query: Mapping[str, list[str]],
    *,
    folder_filter: Callable[[Workspace, str], str],
    current_representatives: Callable[[Workspace], tuple[set[str], bool]],
    current_recommendations: Callable[[Workspace], tuple[set[str], str | None]],
) -> set[str]:
    folder = folder_filter(workspace, _first(query, "folder", "")) if query else ""
    media_type = _first(query, "media_type", "") if query else ""
    if media_type and media_type not in {"image", "video"}:
        raise InvalidRequest("media_type must be image or video")
    selection = _first(query, "selection", "").lower() if query else ""
    if selection not in {"", "all", "representatives", "recommended", "selected", "rejected", "undecided"}:
        raise InvalidRequest("selection is invalid")
    representatives, _ = current_representatives(workspace)
    recommendations, _ = current_recommendations(workspace)
    clauses = ["EXISTS (SELECT 1 FROM physical_file AS pf_scope WHERE pf_scope.logical_asset_id = la.id AND pf_scope.in_scope = 1 AND pf_scope.is_online = 1)"]
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


def _positive_int(value: str, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise InvalidRequest(f"{name} must be an integer") from error
    if number < 1:
        raise InvalidRequest(f"{name} must be positive")
    return number


def _first(query: Mapping[str, list[str]], name: str, default: str) -> str:
    values = query.get(name)
    return values[0] if values else default


def _like_value(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
