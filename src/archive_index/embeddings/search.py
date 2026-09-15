"""Exact cosine retrieval over the active workspace embedding run."""

from __future__ import annotations

from dataclasses import dataclass

from .providers import EmbeddingProvider, create_embedding_provider
from .vector import blob_to_vector, exact_top_k
from ..workspace import Workspace


@dataclass(frozen=True)
class SearchResult:
    asset_id: str
    similarity: float
    best_timestamp: float | None = None


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
    provider = provider or create_embedding_provider(active["active_provider"])
    query = provider.encode_text(text)
    return search_vector(workspace, query, allowed_asset_ids=allowed_asset_ids, top_k=top_k)


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
