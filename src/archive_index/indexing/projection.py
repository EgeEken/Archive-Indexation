"""Persisted deterministic 2D projections of the active semantic run."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from math import isfinite

from ..embeddings.vector import blob_to_vector
from ..workspace import Workspace

LOGGER = logging.getLogger(__name__)
PROJECTION_ALGORITHM = "pca-2d-v1"
PROJECTION_VERSION = "1"
MAX_FIT_ASSETS = 10_000


def build_semantic_projection(workspace: Workspace, *, max_fit_assets: int = MAX_FIT_ASSETS) -> dict[str, object]:
    active = _active_embedding(workspace)
    if active is None:
        return {"status": "unavailable", "reason": "Semantic search is disabled."}
    if active["active_run_id"] is None or active["status"] != "complete":
        return {"status": "unavailable", "reason": "Semantic embeddings are not indexed."}
    try:
        vectors = _load_logical_vectors(workspace, active)
        asset_ids = sorted(vectors)
        fingerprint = asset_set_fingerprint(asset_ids)
        settings_json = json.dumps(
            {
                "fit_sample_policy": "sha256(logical_asset_id) ascending",
                "max_fit_assets": max_fit_assets,
                "video_aggregation": "l2-normalized frame mean, then l2-normalized",
            },
            sort_keys=True,
        )
        existing = _matching_projection(workspace, active["active_run_id"], fingerprint, len(asset_ids))
        if existing is not None:
            with workspace.transaction() as connection:
                connection.execute(
                    "UPDATE workspace_semantic_projection SET active_run_id = ?, updated_at = ? WHERE id = 1",
                    (existing["id"], _timestamp()),
                )
            return {"status": "cached", "run_id": existing["id"], "asset_count": len(asset_ids)}
        coordinates = _fit_pca(vectors, asset_ids, max_fit_assets)
    except Exception as error:
        LOGGER.exception("semantic projection build failed")
        return {"status": "failed", "reason": str(error)}

    run_id = str(uuid.uuid4())
    now = _timestamp()
    try:
        with workspace.transaction() as connection:
            connection.execute(
                """
                INSERT INTO semantic_projection_run(
                    id, source_embedding_run_id, algorithm, version, settings_json,
                    asset_count, asset_set_fingerprint, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'building', ?)
                """,
                (
                    run_id,
                    active["active_run_id"],
                    PROJECTION_ALGORITHM,
                    PROJECTION_VERSION,
                    settings_json,
                    len(asset_ids),
                    fingerprint,
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO semantic_projection_point(run_id, logical_asset_id, x, y) VALUES (?, ?, ?, ?)",
                ((run_id, asset_id, coordinates[asset_id][0], coordinates[asset_id][1]) for asset_id in asset_ids),
            )
            connection.execute(
                "UPDATE semantic_projection_run SET status = 'complete', completed_at = ? WHERE id = ?",
                (_timestamp(), run_id),
            )
            connection.execute(
                "UPDATE workspace_semantic_projection SET active_run_id = ?, updated_at = ? WHERE id = 1",
                (run_id, _timestamp()),
            )
    except Exception as error:
        LOGGER.exception("semantic projection activation failed")
        with workspace.transaction() as connection:
            connection.execute(
                "UPDATE semantic_projection_run SET status = 'failed', completed_at = ?, error_message = ? WHERE id = ?",
                (_timestamp(), str(error), run_id),
            )
        return {"status": "failed", "reason": str(error)}
    return {"status": "complete", "run_id": run_id, "asset_count": len(asset_ids)}


def current_projection(workspace: Workspace) -> tuple[dict[str, object] | None, str | None]:
    active = _active_embedding(workspace)
    if active is None:
        return None, "Semantic search is disabled."
    if active["active_run_id"] is None or active["status"] != "complete":
        return None, "Semantic embeddings are not indexed · Re-index required."
    connection = workspace.connect()
    try:
        row = connection.execute(
            """
            SELECT sp.active_run_id, pr.*
            FROM workspace_semantic_projection AS sp
            LEFT JOIN semantic_projection_run AS pr ON pr.id = sp.active_run_id
            WHERE sp.id = 1
            """
        ).fetchone()
    finally:
        connection.close()
    if row is None or row["active_run_id"] is None or row["status"] != "complete":
        return None, "Vector Cloud projection not built · Re-index to build it from existing embeddings."
    if row["source_embedding_run_id"] != active["active_run_id"]:
        return None, "Vector Cloud projection is stale · Re-index to rebuild it."
    ids = _vector_asset_ids(workspace, active)
    if row["asset_count"] != len(ids) or row["asset_set_fingerprint"] != asset_set_fingerprint(ids):
        return None, "Vector Cloud projection is stale · Re-index to rebuild it."
    return dict(row), None


def projection_points(workspace: Workspace, run_id: str, asset_ids: list[str]) -> list[dict[str, object]]:
    if not asset_ids:
        return []
    points = []
    connection = workspace.connect()
    try:
        for start in range(0, len(asset_ids), 500):
            chunk = asset_ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            points.extend(
                connection.execute(
                    f"""
                    SELECT p.logical_asset_id AS asset_id, p.x, p.y, la.media_type
                    FROM semantic_projection_point AS p
                    JOIN logical_asset AS la ON la.id = p.logical_asset_id
                    WHERE p.run_id = ? AND p.logical_asset_id IN ({placeholders})
                    ORDER BY p.logical_asset_id
                    """,
                    [run_id, *chunk],
                ).fetchall()
            )
    finally:
        connection.close()
    return [dict(point) for point in points]


def asset_set_fingerprint(asset_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(asset_ids)).encode("utf-8")).hexdigest()


def _active_embedding(workspace: Workspace):
    connection = workspace.connect()
    try:
        return connection.execute(
            """
            SELECT we.active_provider, we.active_run_id, er.status, er.embedding_dimension
            FROM workspace_embedding AS we
            JOIN workspace_config AS wc ON wc.id = 1 AND wc.semantic_search_enabled = 1
                                         AND wc.embedding_provider = we.active_provider
            LEFT JOIN embedding_run AS er ON er.id = we.active_run_id
            WHERE we.id = 1
            """
        ).fetchone()
    finally:
        connection.close()


def _vector_asset_ids(workspace: Workspace, active) -> list[str]:
    connection = workspace.connect()
    try:
        image_rows = connection.execute(
            """
            SELECT DISTINCT le.logical_asset_id
            FROM logical_asset_embedding AS le
            JOIN physical_file AS pf ON pf.id = le.source_physical_file_id
                                      AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id
                                      AND cs.component = ? AND cs.status = 'complete'
                                      AND cs.version = (SELECT model_version FROM embedding_run WHERE id = ?)
                                      AND cs.input_fingerprint = le.input_fingerprint
            WHERE le.run_id = ?
            """,
            (f"embedding:{active['active_provider']}", active["active_run_id"], active["active_run_id"]),
        ).fetchall()
        video_rows = connection.execute(
            """
            SELECT DISTINCT vfe.logical_asset_id
            FROM video_frame_embedding AS vfe
            JOIN physical_file AS pf ON pf.id = vfe.physical_file_id
                                      AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id
                                      AND cs.component = ? AND cs.status = 'complete'
                                      AND cs.version = (SELECT model_version FROM embedding_run WHERE id = ?)
            WHERE vfe.run_id = ?
            """,
            (f"embedding:{active['active_provider']}", active["active_run_id"], active["active_run_id"]),
        ).fetchall()
    finally:
        connection.close()
    return sorted({row[0] for row in [*image_rows, *video_rows]})


def _load_logical_vectors(workspace: Workspace, active) -> dict[str, object]:
    connection = workspace.connect()
    try:
        image_rows = connection.execute(
            """
            SELECT le.logical_asset_id, le.embedding, le.embedding_dimension
            FROM logical_asset_embedding AS le
            JOIN physical_file AS pf ON pf.id = le.source_physical_file_id
                                      AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id
                                      AND cs.component = ? AND cs.status = 'complete'
                                      AND cs.version = (SELECT model_version FROM embedding_run WHERE id = ?)
                                      AND cs.input_fingerprint = le.input_fingerprint
            WHERE le.run_id = ?
            ORDER BY le.logical_asset_id
            """,
            (f"embedding:{active['active_provider']}", active["active_run_id"], active["active_run_id"]),
        ).fetchall()
        frame_rows = connection.execute(
            """
            SELECT vfe.logical_asset_id, vfe.embedding, vfe.embedding_dimension
            FROM video_frame_embedding AS vfe
            JOIN physical_file AS pf ON pf.id = vfe.physical_file_id
                                      AND pf.in_scope = 1 AND pf.is_online = 1
            JOIN component_state AS cs ON cs.physical_file_id = pf.id
                                      AND cs.component = ? AND cs.status = 'complete'
                                      AND cs.version = (SELECT model_version FROM embedding_run WHERE id = ?)
            WHERE vfe.run_id = ?
            ORDER BY vfe.logical_asset_id, vfe.sample_index
            """,
            (f"embedding:{active['active_provider']}", active["active_run_id"], active["active_run_id"]),
        ).fetchall()
    finally:
        connection.close()
    vectors = {}
    for row in image_rows:
        try:
            vectors[row["logical_asset_id"]] = _finite_vector(blob_to_vector(row["embedding"], row["embedding_dimension"]))
        except ValueError:
            continue
    video_frames: dict[str, list] = {}
    for row in frame_rows:
        try:
            video_frames.setdefault(row["logical_asset_id"], []).append(_finite_vector(blob_to_vector(row["embedding"], row["embedding_dimension"])))
        except ValueError:
            continue
    for asset_id, frames in video_frames.items():
        if frames:
            import numpy as np

            mean = np.mean(np.vstack(frames), axis=0, dtype=np.float32)
            norm = float(np.linalg.norm(mean))
            if isfinite(norm) and norm > 0:
                vectors[asset_id] = (mean / norm).astype("float32")
    return vectors


def _finite_vector(vector):
    import numpy as np

    value = np.asarray(vector, dtype="float32")
    if value.ndim != 1 or not value.size or not np.isfinite(value).all():
        raise ValueError("stored embedding is non-finite")
    norm = float(np.linalg.norm(value))
    if not isfinite(norm) or norm <= 0:
        raise ValueError("stored embedding has zero norm")
    return (value / norm).astype("float32")


def _fit_pca(vectors, asset_ids: list[str], max_fit_assets: int) -> dict[str, tuple[float, float]]:
    import numpy as np

    if not asset_ids:
        return {}
    matrix = np.vstack([vectors[asset_id] for asset_id in asset_ids]).astype("float32")
    sample_ids = sorted(asset_ids, key=lambda asset_id: hashlib.sha256(asset_id.encode("utf-8")).hexdigest())[:max_fit_assets]
    sample = np.vstack([vectors[asset_id] for asset_id in sample_ids]).astype("float32")
    mean = np.mean(sample, axis=0, dtype=np.float64).astype("float32")
    centered = sample.astype("float64") - mean.astype("float64")
    components = np.zeros((2, matrix.shape[1]), dtype="float64")
    if centered.shape[0] > 1 and np.any(np.abs(centered) > 0):
        _, _, right = np.linalg.svd(centered, full_matrices=False)
        components[: min(2, right.shape[0])] = right[:2]
    for axis in range(2):
        loading = components[axis]
        if not np.any(np.abs(loading) > 0):
            continue
        index = int(np.argmax(np.abs(loading)))
        if loading[index] < 0:
            components[axis] *= -1
    projected = (matrix.astype("float64") - mean.astype("float64")) @ components.T
    projected = np.nan_to_num(projected, nan=0.0, posinf=0.0, neginf=0.0)
    return {
        asset_id: (float(projected[index, 0]), float(projected[index, 1]))
        for index, asset_id in enumerate(asset_ids)
    }


def _matching_projection(workspace, source_run_id: str, fingerprint: str, asset_count: int):
    connection = workspace.connect()
    try:
        return connection.execute(
            """
            SELECT pr.*
            FROM semantic_projection_run AS pr
            WHERE pr.source_embedding_run_id = ? AND pr.algorithm = ? AND pr.version = ?
              AND pr.status = 'complete' AND pr.asset_count = ? AND pr.asset_set_fingerprint = ?
            ORDER BY pr.created_at DESC LIMIT 1
            """,
            (source_run_id, PROJECTION_ALGORITHM, PROJECTION_VERSION, asset_count, fingerprint),
        ).fetchone()
    finally:
        connection.close()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
