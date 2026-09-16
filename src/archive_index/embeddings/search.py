"""Exact cosine retrieval over the active workspace embedding run."""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import RLock
from time import perf_counter
import logging

from .providers import EmbeddingProvider, create_embedding_provider
from .vector import blob_to_vector, exact_top_k
from ..workspace import Workspace


@dataclass(frozen=True)
class SearchResult:
    asset_id: str
    similarity: float
    best_timestamp: float | None = None


_search_lock = RLock()
_providers = {}
_text_vectors = OrderedDict()
_rankings = OrderedDict()
_active_provider = None
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="semantic-search")
_state_lock = RLock()
_loads = {}
_requests = OrderedDict()
LAST_SEARCH_TIMINGS = {}


def provider_state(provider_id):
    with _state_lock:
        future = _loads.get(provider_id)
        if future is None:
            return "available"
        if not future.done():
            return "loading"
        return "failed" if future.exception() else "ready"


def select_provider(provider_id):
    global _active_provider
    with _state_lock:
        if _active_provider != provider_id:
            _active_provider = provider_id
            for future in (*_loads.values(), *_requests.values()):
                future.cancel()
            _loads.clear()
            _requests.clear()
            _worker.submit(_release_providers)
        if provider_id is not None and provider_id not in _loads:
            _loads[provider_id] = _worker.submit(_warm_provider, provider_id)
        return _loads.get(provider_id)


def prepare_provider(provider_id):
    return select_provider(provider_id)


def _warm_provider(provider_id):
    with _search_lock:
        candidate = create_embedding_provider(provider_id)
        session = _providers.get((provider_id, candidate.version), candidate)
        started = perf_counter()
        session.preflight()
        _providers[(provider_id, session.version)] = session
        LAST_SEARCH_TIMINGS["provider_load"] = perf_counter() - started
        LAST_SEARCH_TIMINGS["load_stages"] = dict(getattr(session, "last_timings", {}))


def request_text(workspace, text, *, allowed_asset_ids):
    active = active_embedding(workspace)
    if active is None or active["status"] != "complete":
        raise RuntimeError("Embeddings not indexed · Re-index required")
    provider_id = active["active_provider"]
    loading = prepare_provider(provider_id)
    if not loading.done():
        return None, "loading"
    loading.result()
    fingerprint = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                        for p in (workspace.database_path, workspace.database_path.with_name("index.sqlite-wal")))
    key = (str(workspace.root), active["active_run_id"], fingerprint, text, frozenset(allowed_asset_ids))
    with _state_lock:
        future = _requests.get(key)
        if future is None:
            future = _worker.submit(search_text, workspace, text, allowed_asset_ids=allowed_asset_ids, top_k=2**31)
            _requests[key] = future
        while len(_requests) > 16:
            _, old = _requests.popitem(last=False)
            old.cancel()
    try:
        return future.result(timeout=0.04), "complete"
    except TimeoutError:
        return None, "searching"


def _release_providers():
    with _search_lock:
        runtimes = [p._torch for p in _providers.values() if getattr(p, "_torch", None) is not None]
        _providers.clear()
        _rankings.clear()
        for runtime in runtimes:
            if runtime.cuda.is_available():
                runtime.cuda.empty_cache()


def clear_search_sessions():
    select_provider(None)
    _worker.submit(_release_providers).result()



def active_embedding(workspace: Workspace):
    connection = workspace.connect()
    try:
        return connection.execute(
            """
            SELECT we.active_provider, we.active_run_id, er.model_id, er.model_version,
                   er.embedding_dimension, er.status, er.settings_json
            FROM workspace_embedding AS we
            JOIN workspace_config AS wc
              ON wc.id = 1 AND wc.semantic_search_enabled = 1
             AND wc.embedding_provider = we.active_provider
            LEFT JOIN embedding_run AS er ON er.id = we.active_run_id
            WHERE we.id = 1
            """
        ).fetchone()
    finally:
        connection.close()


def search_text(
    workspace: Workspace,
    text: str,
    *,
    allowed_asset_ids: set[str] | None = None,
    top_k: int = 60,
    provider: EmbeddingProvider | None = None,
) -> list[SearchResult]:
    active = active_embedding(workspace)
    if active is None or active["active_run_id"] is None or active["status"] != "complete":
        raise RuntimeError("semantic embeddings are not available; enable search and index the workspace")
    started = perf_counter()
    with _search_lock:
        key = (active["active_provider"], active["model_version"], text)
        if provider is not None:
            query = provider.encode_text(text)
            return search_vector(workspace, query, allowed_asset_ids=allowed_asset_ids, top_k=top_k)
        query = _text_vectors.get(key)
        encode_started = perf_counter()
        if query is None:
            session = _providers.get(key[:2])
            if session is None:
                session = create_embedding_provider(active["active_provider"])
                _providers[key[:2]] = session
            query = session.encode_text(text)
            _text_vectors[key] = query
            while len(_text_vectors) > 128:
                _text_vectors.popitem(last=False)
        encode_elapsed = perf_counter() - encode_started
        fingerprint = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                            for p in (workspace.database_path, workspace.database_path.with_name("index.sqlite-wal")))
        rank_key = (str(workspace.root), active["active_run_id"], fingerprint, key,
                    None if allowed_asset_ids is None else frozenset(allowed_asset_ids))
        results = _rankings.get(rank_key)
        cached = results is not None
        rank_started = perf_counter()
        if results is None:
            results = search_vector(workspace, query, allowed_asset_ids=allowed_asset_ids, top_k=2**31)
            _rankings[rank_key] = results
            while len(_rankings) > 16:
                _rankings.popitem(last=False)
        logging.getLogger(__name__).info("semantic search %.4fs ranking_cache=%s", perf_counter() - started, cached)
        LAST_SEARCH_TIMINGS.update(encode=encode_elapsed, rank=perf_counter() - rank_started, total=perf_counter() - started, ranking_cached=cached)
        return results[:top_k]



def search_similar(
    workspace: Workspace,
    asset_id: str,
    *,
    allowed_asset_ids: set[str] | None = None,
    top_k: int = 60,
) -> list[SearchResult]:
    active = active_embedding(workspace)
    if active is None or active["active_run_id"] is None or active["status"] != "complete":
        raise RuntimeError("semantic embeddings are not available; enable search and index the workspace")
    connection = workspace.connect()
    try:
        row = connection.execute(
            "SELECT embedding, embedding_dimension FROM logical_asset_embedding WHERE run_id = ? AND logical_asset_id = ?",
            (active["active_run_id"], asset_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise RuntimeError("this image does not have a compatible semantic embedding")
    query = blob_to_vector(row["embedding"], row["embedding_dimension"])
    return search_vector(
        workspace,
        query,
        allowed_asset_ids=allowed_asset_ids,
        top_k=top_k,
        exclude_asset_id=asset_id,
    )


def search_vector(
    workspace: Workspace,
    query,
    *,
    allowed_asset_ids: set[str] | None = None,
    top_k: int = 60,
    exclude_asset_id: str | None = None,
) -> list[SearchResult]:
    active = active_embedding(workspace)
    if active is None or active["active_run_id"] is None or active["status"] != "complete":
        return []
    run_id = active["active_run_id"]
    dimension = int(active["embedding_dimension"])
    connection = workspace.connect()
    try:
        params: list[object] = [run_id]
        asset_clause = ""
        if allowed_asset_ids is not None:
            if not allowed_asset_ids:
                return []
            placeholders = ",".join("?" for _ in allowed_asset_ids)
            asset_clause = f" AND le.logical_asset_id IN ({placeholders})"
            params.extend(sorted(allowed_asset_ids))
        image_rows = connection.execute(
            f"""
            SELECT le.logical_asset_id, le.embedding, le.embedding_dimension
            FROM logical_asset_embedding AS le
            JOIN physical_file AS pf ON pf.id = le.source_physical_file_id AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id AND cs.component = ('embedding:' || ?)
            JOIN embedding_run AS er ON er.id = le.run_id AND cs.version = er.model_version
            WHERE le.run_id = ? AND cs.status = 'complete' AND cs.input_fingerprint = le.input_fingerprint {asset_clause}
            """,
            [active["active_provider"], *params],
        ).fetchall()
        frame_params: list[object] = [active["active_provider"], run_id]
        if allowed_asset_ids is not None:
            frame_params.extend(sorted(allowed_asset_ids))
        frame_rows = connection.execute(
            f"""
            SELECT vfe.logical_asset_id, vfe.timestamp_seconds, vfe.embedding, vfe.embedding_dimension
            FROM video_frame_embedding AS vfe
            JOIN physical_file AS pf ON pf.id = vfe.physical_file_id AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id AND cs.component = ('embedding:' || ?)
            JOIN embedding_run AS er ON er.id = vfe.run_id AND cs.version = er.model_version
            WHERE vfe.run_id = ? AND cs.status = 'complete' {('AND vfe.logical_asset_id IN (' + ','.join('?' for _ in allowed_asset_ids) + ')' if allowed_asset_ids is not None else '')}
            """,
            frame_params,
        ).fetchall()
    finally:
        connection.close()
    records = []
    vectors = []
    import numpy as np

    for row in image_rows:
        if exclude_asset_id == row["logical_asset_id"]:
            continue
        vectors.append(blob_to_vector(row["embedding"], int(row["embedding_dimension"])))
        records.append((row["logical_asset_id"], None))
    for row in frame_rows:
        if exclude_asset_id == row["logical_asset_id"]:
            continue
        vectors.append(blob_to_vector(row["embedding"], int(row["embedding_dimension"])))
        records.append((row["logical_asset_id"], row["timestamp_seconds"]))
    if not vectors:
        return []
    matrix = np.vstack(vectors)
    if matrix.shape[1] != dimension:
        raise ValueError("stored embedding dimensions do not match the active model")
    candidates = exact_top_k(matrix, query, len(records))
    best: dict[str, SearchResult] = {}
    for index, score in candidates:
        asset_id, timestamp = records[index]
        old = best.get(asset_id)
        if old is None or score > old.similarity:
            best[asset_id] = SearchResult(asset_id, score, timestamp)
    return sorted(best.values(), key=lambda result: (-result.similarity, result.asset_id))[:top_k]
