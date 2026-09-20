"""Exact cosine retrieval over the active workspace embedding run."""

from __future__ import annotations

from dataclasses import dataclass
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import RLock
from time import perf_counter
import logging
from pathlib import Path

from .providers import EmbeddingProvider, create_embedding_provider
from .runtime import embedding_runtime
from .vector import blob_to_vector, exact_top_k
from ..workspace import Workspace


@dataclass(frozen=True)
class SearchResult:
    asset_id: str
    similarity: float
    best_timestamp: float | None = None
    source_timestamp: float | None = None


_search_lock = RLock()
_providers = embedding_runtime.providers
_loads = embedding_runtime.loads
_text_vectors = OrderedDict()
_rankings = OrderedDict()
_embedding_matrices = OrderedDict()
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="semantic-search")
_state_lock = RLock()
_requests = OrderedDict()


def _database_fingerprint(workspace: Workspace):
    fingerprint = []
    for path in (workspace.database_path, workspace.database_path.with_name("index.sqlite-wal")):
        try:
            stat_result = Path(path).stat()
        except FileNotFoundError:
            fingerprint.append(None)
        else:
            fingerprint.append((stat_result.st_mtime_ns, stat_result.st_size))
    return tuple(fingerprint)
LAST_SEARCH_TIMINGS = {}


def provider_state(provider_id):
    return embedding_runtime.state(provider_id)


def select_provider(provider_id):
    with _state_lock:
        for future in _requests.values():
            future.cancel()
        _requests.clear()
    release = embedding_runtime.select(provider_id, factory=create_embedding_provider)
    _embedding_matrices.clear()
    if provider_id is None and release is not None:
        return _worker.submit(release.result)
    return release


def prepare_provider(provider_id):
    return embedding_runtime.prepare(provider_id, factory=create_embedding_provider)


def request_text(workspace, text, *, allowed_asset_ids):
    active = active_embedding(workspace)
    if active is None or active["status"] != "complete":
        raise RuntimeError("Embeddings not indexed · Re-index required")
    provider_id = active["active_provider"]
    loading = prepare_provider(provider_id)
    if not loading.done():
        return None, "loading"
    loading.result()
    fingerprint = _database_fingerprint(workspace)
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
    embedding_runtime.release_all().result()
    _rankings.clear()
    _embedding_matrices.clear()


def clear_search_sessions():
    release = select_provider(None)
    if release is not None:
        release.result()
    _worker.submit(_release_providers).result()



def active_embedding(workspace: Workspace):
    connection = workspace.connect()
    try:
        return connection.execute(
            """
            SELECT we.active_provider, we.active_run_id, er.model_id, er.model_version,
                   er.embedding_dimension, er.status, er.settings_json,
                   (SELECT generation FROM browser_revision WHERE id = 1) AS browser_generation
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
            query = embedding_runtime.run(
                active["active_provider"],
                lambda session: session.encode_text(text),
                factory=create_embedding_provider,
            )
            session = embedding_runtime.loaded_provider(active["active_provider"], active["model_version"])
            LAST_SEARCH_TIMINGS["load_stages"] = dict(getattr(session, "last_timings", {}))
            _text_vectors[key] = query
            while len(_text_vectors) > 128:
                _text_vectors.popitem(last=False)
        encode_elapsed = perf_counter() - encode_started
        fingerprint = _database_fingerprint(workspace)
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
    source_vectors = _load_asset_vectors(workspace, active, asset_id)
    if not source_vectors:
        raise RuntimeError("this asset does not have compatible semantic embeddings")
    import numpy as np

    source_matrix = np.vstack([vector for vector, _ in source_vectors]).astype("float32", copy=False)
    cache_key = (
        str(workspace.root),
        active["active_provider"],
        active["active_run_id"],
        int(active["embedding_dimension"]),
        active["browser_generation"],
    )
    with _search_lock:
        cached = _embedding_matrices.get(cache_key)
        if cached is None:
            matrix, records = _load_embedding_matrix(workspace, active, int(active["embedding_dimension"]))
            _embedding_matrices[cache_key] = (matrix, records)
            cached = (matrix, records)
        else:
            _embedding_matrices.move_to_end(cache_key)
    matrix, records = cached
    if not records:
        return []
    scores = source_matrix @ matrix.T
    best: dict[str, SearchResult] = {}
    for candidate_index, (candidate_id, candidate_timestamp) in enumerate(records):
        if allowed_asset_ids is not None and candidate_id not in allowed_asset_ids:
            continue
        if candidate_id == asset_id:
            continue
        source_index = int(np.argmax(scores[:, candidate_index]))
        score = float(scores[source_index, candidate_index])
        old = best.get(candidate_id)
        if old is None or score > old.similarity:
            best[candidate_id] = SearchResult(
                candidate_id,
                score,
                candidate_timestamp,
                source_vectors[source_index][1],
            )
    return sorted(best.values(), key=lambda result: (-result.similarity, result.asset_id))[:top_k]


def _load_asset_vectors(workspace, active, asset_id):
    connection = workspace.connect()
    try:
        image_rows = connection.execute(
            """
            SELECT le.embedding, le.embedding_dimension
            FROM logical_asset_embedding AS le
            JOIN physical_file AS pf ON pf.id = le.source_physical_file_id AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id AND cs.component = ('embedding:' || ?)
            JOIN embedding_run AS er ON er.id = le.run_id AND cs.version = er.model_version
            WHERE le.run_id = ? AND le.logical_asset_id = ? AND cs.status = 'complete'
            """,
            (active["active_provider"], active["active_run_id"], asset_id),
        ).fetchall()
        frame_rows = connection.execute(
            """
            SELECT vfe.timestamp_seconds, vfe.embedding, vfe.embedding_dimension
            FROM video_frame_embedding AS vfe
            JOIN physical_file AS pf ON pf.id = vfe.physical_file_id AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id AND cs.component = ('embedding:' || ?)
            JOIN embedding_run AS er ON er.id = vfe.run_id AND cs.version = er.model_version
            WHERE vfe.run_id = ? AND vfe.logical_asset_id = ? AND cs.status = 'complete'
            ORDER BY vfe.sample_index
            """,
            (active["active_provider"], active["active_run_id"], asset_id),
        ).fetchall()
    finally:
        connection.close()
    vectors = []
    for row in image_rows:
        vectors.append((blob_to_vector(row["embedding"], int(row["embedding_dimension"])), None))
    for row in frame_rows:
        vectors.append((blob_to_vector(row["embedding"], int(row["embedding_dimension"])), row["timestamp_seconds"]))
    return vectors


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
        _embedding_matrices.clear()
        return []
    dimension = int(active["embedding_dimension"])
    if allowed_asset_ids is not None and not allowed_asset_ids:
        return []
    started = perf_counter()
    cache_key = (
        str(workspace.root),
        active["active_provider"],
        active["active_run_id"],
        dimension,
        active["browser_generation"],
    )
    with _search_lock:
        cached = _embedding_matrices.get(cache_key)
        if cached is None:
            matrix, records = _load_embedding_matrix(workspace, active, dimension)
            _embedding_matrices[cache_key] = (matrix, records)
            for old_key in list(_embedding_matrices):
                if old_key[0] == cache_key[0] and old_key != cache_key:
                    del _embedding_matrices[old_key]
            while len(_embedding_matrices) > 2:
                _embedding_matrices.popitem(last=False)
        else:
            matrix, records = cached
            _embedding_matrices.move_to_end(cache_key)
    LAST_SEARCH_TIMINGS.update(matrix=perf_counter() - started, matrix_cached=cached is not None)
    if not records:
        return []
    candidates = exact_top_k(matrix, query, len(records))
    best: dict[str, SearchResult] = {}
    for index, score in candidates:
        asset_id, timestamp = records[index]
        if allowed_asset_ids is not None and asset_id not in allowed_asset_ids:
            continue
        if exclude_asset_id == asset_id:
            continue
        old = best.get(asset_id)
        if old is None or score > old.similarity:
            best[asset_id] = SearchResult(asset_id, score, timestamp)
    return sorted(best.values(), key=lambda result: (-result.similarity, result.asset_id))[:top_k]


def _load_embedding_matrix(workspace, active, dimension):
    connection = workspace.connect()
    try:
        image_rows = connection.execute(
            """
            SELECT le.logical_asset_id, le.embedding, le.embedding_dimension
            FROM logical_asset_embedding AS le
            JOIN physical_file AS pf ON pf.id = le.source_physical_file_id AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id AND cs.component = ('embedding:' || ?)
            JOIN embedding_run AS er ON er.id = le.run_id AND cs.version = er.model_version
            WHERE le.run_id = ? AND cs.status = 'complete' AND cs.input_fingerprint = le.input_fingerprint
            ORDER BY le.logical_asset_id
            """,
            (active["active_provider"], active["active_run_id"]),
        ).fetchall()
        frame_rows = connection.execute(
            """
            SELECT vfe.logical_asset_id, vfe.timestamp_seconds, vfe.embedding, vfe.embedding_dimension
            FROM video_frame_embedding AS vfe
            JOIN physical_file AS pf ON pf.id = vfe.physical_file_id AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id AND cs.component = ('embedding:' || ?)
            JOIN embedding_run AS er ON er.id = vfe.run_id AND cs.version = er.model_version
            WHERE vfe.run_id = ? AND cs.status = 'complete'
            ORDER BY vfe.logical_asset_id, vfe.sample_index
            """,
            (active["active_provider"], active["active_run_id"]),
        ).fetchall()
    finally:
        connection.close()
    records = []
    vectors = []
    import numpy as np
    for row in image_rows:
        vectors.append(blob_to_vector(row["embedding"], int(row["embedding_dimension"])))
        records.append((row["logical_asset_id"], None))
    for row in frame_rows:
        vectors.append(blob_to_vector(row["embedding"], int(row["embedding_dimension"])))
        records.append((row["logical_asset_id"], row["timestamp_seconds"]))
    if not vectors:
        return np.empty((0, dimension), dtype="float32"), records
    matrix = np.vstack(vectors)
    if matrix.shape[1] != dimension:
        raise ValueError("stored embedding dimensions do not match the active model")
    return matrix, records
