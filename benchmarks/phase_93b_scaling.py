"""Synthetic Phase 9.3B browser and semantic-search scaling benchmark."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import tracemalloc
from pathlib import Path

import numpy as np

from archive_index.api import server
from archive_index.embeddings.search import search_vector
from archive_index.embeddings.vector import vector_to_blob
from archive_index.workspace import Workspace


SIZES = (10_000, 50_000, 100_000)
DIMENSION = 512


def _timestamp(index: int) -> str:
    return f"2020-01-{(index % 28) + 1:02d}T12:{(index // 28) % 60:02d}:00+00:00"


def _create_fixture(root: Path, size: int, *, embeddings: bool) -> Workspace:
    workspace = Workspace.create(root)
    now = "2026-09-18T00:00:00+00:00"
    rng = np.random.default_rng(93_000 + size)
    logical_rows = []
    physical_rows = []
    embedding_rows = []
    state_rows = []
    vectors = rng.normal(size=(size, DIMENSION)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    run_id = "benchmark-run" if embeddings else None
    with workspace.transaction() as connection:
        for index in range(size):
            asset_id = f"asset-{index:06d}"
            file_id = f"file-{index:06d}"
            relative_path = f"folder-{index % 100:03d}/image-{index:06d}.jpg"
            logical_rows.append((asset_id, "image", _timestamp(index), "absolute", now, now))
            physical_rows.append(
                (
                    file_id,
                    asset_id,
                    relative_path,
                    Path(relative_path).name,
                    ".jpg",
                    "image",
                    "source_original",
                    1_000_000 + index,
                    index,
                    1,
                    now,
                    now,
                    1600,
                    1200,
                    0.4 + (index % 600) / 1000,
                )
            )
            if embeddings:
                embedding_rows.append(
                    (
                        run_id,
                        asset_id,
                        file_id,
                        "rendered",
                        "benchmark-input",
                        vector_to_blob(vectors[index])[0],
                        DIMENSION,
                        now,
                        now,
                    )
                )
                state_rows.append((file_id, "embedding:benchmark", "complete", "benchmark", "v1", "benchmark-input", now))
        connection.executemany(
            "INSERT INTO logical_asset(id, media_type, capture_time, capture_time_kind, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            logical_rows,
        )
        connection.executemany(
            """
            INSERT INTO physical_file(
                id, logical_asset_id, relative_path, filename, extension, media_type,
                role, size_bytes, mtime_ns, is_online, created_at, updated_at,
                width, height, quality_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            physical_rows,
        )
        if embeddings:
            connection.execute(
                "INSERT INTO embedding_run(id, provider, model_id, model_version, embedding_dimension, settings_json, status, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, 'complete', ?, ?)",
                (run_id, "benchmark", "benchmark", "v1", DIMENSION, "{}", now, now),
            )
            connection.execute(
                "UPDATE workspace_config SET semantic_search_enabled = 1, embedding_provider = 'benchmark' WHERE id = 1"
            )
            connection.execute(
                "UPDATE workspace_embedding SET active_provider = 'benchmark', active_run_id = ?, updated_at = ? WHERE id = 1",
                (run_id, now),
            )
            connection.executemany(
                "INSERT INTO component_state(physical_file_id, component, status, algorithm, version, input_fingerprint, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                state_rows,
            )
            connection.executemany(
                "INSERT INTO logical_asset_embedding(run_id, logical_asset_id, source_physical_file_id, source_kind, input_fingerprint, embedding, embedding_dimension, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                embedding_rows,
            )
    return workspace


def _timed(function):
    started = time.perf_counter()
    value = function()
    return value, time.perf_counter() - started


def _browser_benchmark(workspace: Workspace) -> dict[str, object]:
    server._browser_cache.clear()
    server._browser_catalogs.clear()
    _, cold = _timed(lambda: server._browser_assets(workspace, {}, "benchmark"))
    _, warm = _timed(lambda: server._browser_assets(workspace, {}, "benchmark"))
    _, folder = _timed(
        lambda: server._browser_assets(
            workspace,
            {"folders": [json.dumps(["folder-050"])]},
            "benchmark",
        )
    )
    _, filename = _timed(
        lambda: server._browser_assets(
            workspace,
            {"sort_by": ["filename"], "direction": ["asc"]},
            "benchmark",
        )
    )
    _, quality = _timed(
        lambda: server._browser_assets(
            workspace,
            {"sort_by": ["quality"], "direction": ["desc"]},
            "benchmark",
        )
    )
    return {
        "cold_first_request_seconds": cold,
        "warm_repeated_request_seconds": warm,
        "filtered_folder_request_seconds": folder,
        "filename_sort_seconds": filename,
        "quality_sort_seconds": quality,
        "catalog_items": len(next(iter(server._browser_catalogs.values()))),
    }


def _semantic_benchmark(workspace: Workspace, size: int) -> dict[str, object]:
    query = np.zeros(DIMENSION, dtype=np.float32)
    query[0] = 1.0
    _, cold = _timed(lambda: search_vector(workspace, query, top_k=20))
    _, warm = _timed(lambda: search_vector(workspace, query, top_k=20))
    current, peak = tracemalloc.get_traced_memory()
    return {
        "cold_query_seconds": cold,
        "warm_query_seconds": warm,
        "result_count": 20,
        "tracemalloc_current_mb": current / 1_000_000,
        "tracemalloc_peak_mb": peak / 1_000_000,
        "embedding_bytes": size * DIMENSION * 2,
    }


def benchmark_size(size: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix=f"archive-index-benchmark-{size}-") as temporary_directory:
        browser_workspace = _create_fixture(Path(temporary_directory) / "browser", size, embeddings=False)
        tracemalloc.start()
        browser = _browser_benchmark(browser_workspace)
        browser_current, browser_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        semantic_workspace = _create_fixture(Path(temporary_directory) / "semantic", size, embeddings=True)
        tracemalloc.start()
        semantic = _semantic_benchmark(semantic_workspace, size)
        semantic["browser_tracemalloc_current_mb"] = browser_current / 1_000_000
        semantic["browser_tracemalloc_peak_mb"] = browser_peak / 1_000_000
        tracemalloc.stop()
        return {"assets": size, "browser": browser, "semantic": semantic}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=SIZES)
    args = parser.parse_args()
    results = []
    for size in args.sizes:
        result = benchmark_size(size)
        results.append(result)
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
