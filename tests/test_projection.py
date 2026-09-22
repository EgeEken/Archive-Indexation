from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from archive_index.embeddings.vector import vector_to_blob
from archive_index.api.server import _browser_filtered_assets
from archive_index.api.visualizations import visualization_data
from archive_index.indexing.projection import (
    _fit_pca,
    _load_logical_vectors,
    _active_embedding,
    build_semantic_projection,
    current_projection,
    projection_points,
)
from archive_index.indexing.scanner import scan
from archive_index.workspace import Workspace


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        Image.new("RGB", (30, 20), "red").save(root / "one.jpg")
        Image.new("RGB", (40, 20), "green").save(root / "two.jpg")
        (root / "clip.mp4").write_bytes(b"video fixture")
        self.workspace = Workspace.create(root)
        configuration = self.workspace.configuration()
        configuration.update({"semantic_search_enabled": True, "include_videos_in_semantic_search": True})
        self.workspace.apply_configuration(configuration)
        scan(self.workspace)
        self._seed_embeddings()

    def _seed_embeddings(self):
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        run_id = str(uuid.uuid4())
        provider = "openclip-b16-datacomp-xl"
        version = "projection-test"
        with self.workspace.transaction() as connection:
            connection.execute(
                "INSERT INTO embedding_run(id, provider, model_id, model_version, embedding_dimension, settings_json, status, created_at, completed_at) VALUES (?, ?, 'test', ?, 3, '{}', 'complete', ?, ?)",
                (run_id, provider, version, now, now),
            )
            rows = connection.execute("SELECT id, logical_asset_id, relative_path, media_type FROM physical_file ORDER BY relative_path").fetchall()
            for row in rows:
                connection.execute(
                    "INSERT INTO component_state(physical_file_id, component, status, algorithm, version, input_fingerprint, completed_at) VALUES (?, ?, 'complete', 'semantic-embedding', ?, ?, ?)",
                    (row["id"], f"embedding:{provider}", version, f"source-{row['id']}", now),
                )
                if row["media_type"] == "image":
                    values = [1, 0, 0] if row["relative_path"] == "one.jpg" else [0, 1, 0]
                    blob, dimension = vector_to_blob(values)
                    connection.execute(
                        "INSERT INTO logical_asset_embedding(run_id, logical_asset_id, source_physical_file_id, source_kind, input_fingerprint, embedding, embedding_dimension, created_at, updated_at) VALUES (?, ?, ?, 'rendered', ?, ?, ?, ?, ?)",
                        (run_id, row["logical_asset_id"], row["id"], f"source-{row['id']}", blob, dimension, now, now),
                    )
                else:
                    sample_run = str(uuid.uuid4())
                    connection.execute(
                        "INSERT INTO video_sample_run(id, physical_file_id, sampler_algorithm, sampler_version, settings_json, input_fingerprint, duration_seconds, requested_count, status, aggregate_algorithm, aggregate_version, created_at, completed_at) VALUES (?, ?, 'test', '1', '{}', 'video', 1, 2, 'complete', 'test', '1', ?, ?)",
                        (sample_run, row["id"], now, now),
                    )
                    for index, values in enumerate(([1, 0, 0], [0.6, 0.8, 0])):
                        blob, dimension = vector_to_blob(values)
                        connection.execute(
                            "INSERT INTO video_frame_embedding(run_id, logical_asset_id, physical_file_id, sample_run_id, sample_index, timestamp_seconds, input_fingerprint, embedding, embedding_dimension, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'frame', ?, ?, ?, ?)",
                            (run_id, row["logical_asset_id"], row["id"], sample_run, index, float(index), blob, dimension, now, now),
                        )
            connection.execute(
                "UPDATE workspace_embedding SET active_provider = ?, active_run_id = ?, updated_at = ? WHERE id = 1",
                (provider, run_id, now),
            )

    def test_projection_is_deterministic_and_has_one_point_per_logical_asset(self):
        first = build_semantic_projection(self.workspace)
        self.assertEqual(first["status"], "complete")
        projection, reason = current_projection(self.workspace)
        self.assertIsNone(reason)
        connection = self.workspace.connect()
        try:
            asset_ids = [row["id"] for row in connection.execute("SELECT id FROM logical_asset ORDER BY id")]
        finally:
            connection.close()
        points = projection_points(self.workspace, projection["id"], asset_ids)
        self.assertEqual(len(points), 3)
        self.assertTrue(all(np.isfinite([point["x"], point["y"]]).all() for point in points))
        second = build_semantic_projection(self.workspace)
        self.assertEqual(second["status"], "cached")
        projection_again, _ = current_projection(self.workspace)
        again = projection_points(self.workspace, projection_again["id"], sorted(point["asset_id"] for point in points))
        self.assertEqual([(point["x"], point["y"]) for point in points], [(point["x"], point["y"]) for point in again])

    def test_video_frames_are_meaned_once_without_source_or_model_work(self):
        active = _active_embedding(self.workspace)
        vectors = _load_logical_vectors(self.workspace, active)
        connection = self.workspace.connect()
        try:
            video_id = connection.execute("SELECT id FROM logical_asset WHERE media_type = 'video'").fetchone()[0]
        finally:
            connection.close()
        self.assertTrue(np.allclose(vectors[video_id], np.array([0.8944272, 0.4472136, 0], dtype="float32"), atol=1e-4), vectors[video_id])
        with patch("archive_index.indexing.embeddings.extract_video_frames", side_effect=AssertionError("source decode")):
            result = build_semantic_projection(self.workspace)
        self.assertIn(result["status"], {"complete", "cached"})

    def test_projection_handles_zero_one_two_and_degenerate_vectors(self):
        cases = [
            {},
            {"a": np.array([1], dtype="float32")},
            {"a": np.array([1, 0], dtype="float32"), "b": np.array([0, 1], dtype="float32")},
            {"a": np.array([1, 1], dtype="float32"), "b": np.array([1, 1], dtype="float32")},
        ]
        for vectors in cases:
            result = _fit_pca(vectors, sorted(vectors), 10)
            self.assertTrue(all(np.isfinite(values).all() for values in result.values()))

    def test_source_run_change_invalidates_existing_projection(self):
        build_semantic_projection(self.workspace)
        with self.workspace.transaction() as connection:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            new_run = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO embedding_run(id, provider, model_id, model_version, embedding_dimension, settings_json, status, created_at, completed_at) VALUES (?, 'openclip-b16-datacomp-xl', 'test', 'new', 3, '{}', 'complete', ?, ?)",
                (new_run, now, now),
            )
            connection.execute("UPDATE workspace_embedding SET active_run_id = ?, updated_at = ? WHERE id = 1", (new_run, now))
        projection, reason = current_projection(self.workspace)
        self.assertIsNone(projection)
        self.assertIn("projection", reason.lower())

    def test_vector_response_is_compact_and_filter_intersection_is_shared(self):
        build_semantic_projection(self.workspace)
        data = visualization_data(
            self.workspace,
            {"media_type": ["image"]},
            "test",
            filter_assets=_browser_filtered_assets,
            kind="vector",
        )
        self.assertEqual(data["filtered_asset_count"], 2)
        self.assertEqual(data["represented_point_count"], 2)
        self.assertTrue(all({"asset_id", "x", "y", "media_type"}.issubset(point) for point in data["points"]))
        self.assertTrue(
            all(
                set(point).issubset(
                    {"asset_id", "x", "y", "media_type", "filename", "quality_score", "capture_time", "capture_time_kind", "width", "height"}
                )
                for point in data["points"]
            )
        )
