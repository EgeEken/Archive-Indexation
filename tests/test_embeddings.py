from __future__ import annotations

import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from archive_index.embeddings.models import OPENCLIP_PROVIDER, SIGLIP_PROVIDER
from archive_index.embeddings.providers import EmbeddingProvider
from archive_index.embeddings.search import search_similar, search_text
from archive_index.embeddings.vector import blob_to_vector, exact_top_k, vector_to_blob
from archive_index.indexing.embeddings import index_embeddings
from archive_index.indexing.scanner import scan
from archive_index.media.raw_preview import RawPreview
from archive_index.workspace import Workspace


class FakeProvider(EmbeddingProvider):
    def __init__(self, provider_id=OPENCLIP_PROVIDER, version="fake-v1", dimension=3):
        self.provider_id = provider_id
        self.model_id = f"fake:{provider_id}"
        self.version = version
        self.dimension = dimension
        self.batch_size = 8
        self.calls = 0
        self.preflight_calls = 0

    @property
    def settings(self):
        return {
            "model_id": self.model_id,
            "version": self.version,
            "dimension": self.dimension,
            "normalization": "unit_l2",
            "similarity": "cosine_dot_product",
            "image_preprocessing": "fake",
            "text_preprocessing": "fake",
        }

    def preflight(self):
        self.preflight_calls += 1

    def encode_images(self, images):
        self.calls += 1
        return [
            np.array([image.width, image.height, 1.0], dtype=np.float32)
            for image in images
        ]

    def encode_text(self, text):
        return np.eye(self.dimension, dtype=np.float32)[0]


class EmbeddingTests(unittest.TestCase):
    def test_float16_vectors_are_normalized_and_dimension_checked(self):
        blob, dimension = vector_to_blob([3.0, 4.0])
        value = blob_to_vector(blob, dimension)
        self.assertEqual(dimension, 2)
        self.assertAlmostEqual(float(np.linalg.norm(value)), 1.0, places=5)
        self.assertEqual(exact_top_k(np.array([[1.0, 0.0], [0.0, 1.0]]), [1.0, 0.0], 2)[0][0], 0)
        with self.assertRaises(ValueError):
            blob_to_vector(blob, 3)

    def test_logical_asset_is_embedded_once_and_cached(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            for name in ("photo.jpg", "copy.jpg"):
                Image.new("RGB", (20, 30), color=(20, 40, 60)).save(root / name)
            workspace = Workspace.create(root)
            scan(workspace)
            with workspace.transaction() as connection:
                rows = connection.execute("SELECT id, logical_asset_id FROM physical_file ORDER BY relative_path").fetchall()
                connection.execute(
                    "UPDATE physical_file SET logical_asset_id = ? WHERE id = ?",
                    (rows[0][1], rows[1][0]),
                )
                configuration = workspace.configuration()
                configuration["semantic_search_enabled"] = True
                workspace_config = configuration
            workspace.apply_configuration(workspace_config)

            provider = FakeProvider()
            first = index_embeddings(workspace, provider=provider)
            second_provider = FakeProvider()
            second = index_embeddings(workspace, provider=second_provider)

            self.assertEqual((first.succeeded, first.errors), (1, 0))
            self.assertEqual(second.skipped, 1)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(second_provider.calls, 0)
            with closing(workspace.connect()) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM logical_asset_embedding").fetchone()[0], 1)

    def test_raw_jpeg_logical_asset_uses_rendered_source_and_raw_only_uses_preview(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            jpeg = root / "photo.jpg"
            Image.new("RGB", (40, 20), color=(20, 40, 60)).save(jpeg)
            raw = root / "photo.arw"
            raw.write_bytes(b"raw")
            workspace = Workspace.create(root)
            scan(workspace)
            with closing(workspace.connect()) as connection:
                rows = connection.execute("SELECT id, logical_asset_id, extension FROM physical_file ORDER BY extension").fetchall()
            rendered_asset = next(row[1] for row in rows if row[2] == ".jpg")
            with workspace.transaction() as connection:
                connection.execute(
                    "UPDATE physical_file SET logical_asset_id = ? WHERE extension = '.arw'",
                    (rendered_asset,),
                )
                configuration = workspace.configuration()
                configuration["semantic_search_enabled"] = True
            workspace.apply_configuration(configuration)
            provider = FakeProvider()
            result = index_embeddings(workspace, provider=provider)
            self.assertEqual(result.succeeded, 1)
            with closing(workspace.connect()) as connection:
                source = connection.execute("SELECT source_kind FROM logical_asset_embedding").fetchone()[0]
            self.assertEqual(source, "rendered")

            raw_only = root / "only.arw"
            raw_only.write_bytes(b"raw-only")
            scan(workspace)
            with patch(
                "archive_index.indexing.embeddings.extract_embedded_preview",
                return_value=RawPreview(Image.new("RGB", (40, 20)), "fake", 40, 20),
            ):
                index_embeddings(workspace, provider=FakeProvider(version="fake-v2"))
            with closing(workspace.connect()) as connection:
                kinds = {row[0] for row in connection.execute("SELECT source_kind FROM logical_asset_embedding").fetchall()}
            self.assertIn("raw_preview", kinds)

    def test_provider_switch_keeps_compatible_previous_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (20, 30)).save(root / "photo.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            configuration = workspace.configuration()
            configuration["semantic_search_enabled"] = True
            workspace.apply_configuration(configuration)
            openclip = FakeProvider()
            siglip = FakeProvider(SIGLIP_PROVIDER, "siglip-fake-v1", 3)
            index_embeddings(workspace, provider=openclip)
            workspace.apply_configuration({**workspace.configuration(), "embedding_provider": SIGLIP_PROVIDER})
            index_embeddings(workspace, provider=siglip)
            workspace.apply_configuration({**workspace.configuration(), "embedding_provider": OPENCLIP_PROVIDER})
            restored = FakeProvider()
            result = index_embeddings(workspace, provider=restored)
            self.assertEqual(result.skipped, 1)
            self.assertEqual(restored.calls, 0)
            with closing(workspace.connect()) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(DISTINCT provider) FROM embedding_run").fetchone()[0], 2)

    def test_provider_version_change_recomputes_only_embedding_state(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (20, 30)).save(root / "photo.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            workspace.apply_configuration({**workspace.configuration(), "semantic_search_enabled": True})
            with workspace.transaction() as connection:
                connection.execute("UPDATE physical_file SET quality_score = 0.8")
            first = FakeProvider(version="fake-v1")
            second = FakeProvider(version="fake-v2")
            index_embeddings(workspace, provider=first)
            result = index_embeddings(workspace, provider=second)
            self.assertEqual((result.succeeded, result.errors), (1, 0))
            with closing(workspace.connect()) as connection:
                self.assertEqual(connection.execute("SELECT quality_score FROM physical_file").fetchone()[0], 0.8)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM embedding_run").fetchone()[0], 2)

    def test_search_collapses_video_frames_by_max_similarity(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Workspace.create(Path(temporary_directory) / "archive")
            configuration = workspace.configuration()
            configuration["semantic_search_enabled"] = True
            workspace.apply_configuration(configuration)
            now = "now"
            run_id = "run"
            image_id, video_id = "image", "video"
            image_file, video_file = "image-file", "video-file"
            with workspace.transaction() as connection:
                for asset_id, media_type in ((image_id, "image"), (video_id, "video")):
                    connection.execute("INSERT INTO logical_asset(id, media_type, created_at, updated_at) VALUES (?, ?, ?, ?)", (asset_id, media_type, now, now))
                for file_id, asset_id, name, media_type in ((image_file, image_id, "image.jpg", "image"), (video_file, video_id, "clip.mp4", "video")):
                    connection.execute("INSERT INTO physical_file(id, logical_asset_id, relative_path, filename, extension, media_type, is_online, in_scope, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?, ?)", (file_id, asset_id, name, name, Path(name).suffix, media_type, now, now))
                connection.execute("INSERT INTO embedding_run(id, provider, model_id, model_version, embedding_dimension, settings_json, status, created_at) VALUES (?, ?, ?, ?, 2, '{}', 'complete', ?)", (run_id, OPENCLIP_PROVIDER, "fake", "fake", now))
                connection.execute("UPDATE workspace_embedding SET active_provider = ?, active_run_id = ?, updated_at = ? WHERE id = 1", (OPENCLIP_PROVIDER, run_id, now))
                for file_id in (image_file, video_file):
                    connection.execute("INSERT INTO component_state(physical_file_id, component, status, algorithm, version, input_fingerprint) VALUES (?, ?, 'complete', 'embedding', 'fake', ?)", (file_id, f"embedding:{OPENCLIP_PROVIDER}", file_id))
                image_blob, _ = vector_to_blob([0.0, 1.0])
                video_low, _ = vector_to_blob([0.0, 1.0])
                video_high, _ = vector_to_blob([1.0, 0.0])
                connection.execute("INSERT INTO logical_asset_embedding(run_id, logical_asset_id, source_physical_file_id, source_kind, input_fingerprint, embedding, embedding_dimension, created_at, updated_at) VALUES (?, ?, ?, 'rendered', ?, ?, 2, ?, ?)", (run_id, image_id, image_file, image_file, image_blob, now, now))
                connection.execute("INSERT INTO video_sample_run(id, physical_file_id, sampler_algorithm, sampler_version, settings_json, input_fingerprint, duration_seconds, requested_count, status, aggregate_algorithm, aggregate_version, created_at) VALUES ('sample-run', ?, 'fake', '1', '{}', 'video', 2, 2, 'complete', 'max', '1', ?)", (video_file, now))
                connection.execute("INSERT INTO workspace_video_sample(physical_file_id, active_run_id) VALUES (?, 'sample-run')", (video_file,))
                for index, blob in enumerate((video_low, video_high)):
                    connection.execute("INSERT INTO video_frame_embedding(run_id, logical_asset_id, physical_file_id, sample_run_id, sample_index, timestamp_seconds, input_fingerprint, embedding, embedding_dimension, created_at, updated_at) VALUES (?, ?, ?, 'sample-run', ?, ?, ?, ?, 2, ?, ?)", (run_id, video_id, video_file, index, float(index), video_file, blob, now, now))
            results = search_text(workspace, "query", provider=FakeProvider(dimension=2))
            self.assertEqual(results[0].asset_id, video_id)
            self.assertAlmostEqual(results[0].best_timestamp, 1.0)
            similar = search_similar(workspace, image_id, top_k=10)
            self.assertNotIn(image_id, {result.asset_id for result in similar})

    def test_video_embeddings_run_when_video_quality_is_off(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"video")
            workspace = Workspace.create(root)
            scan(workspace)
            with workspace.transaction() as connection:
                connection.execute("UPDATE physical_file SET duration_seconds = 1.0 WHERE relative_path = 'clip.mp4'")
            configuration = {**workspace.configuration(), "semantic_search_enabled": True, "video_quality_enabled": False}
            workspace.apply_configuration(configuration)
            from archive_index.indexing.video_quality import ExtractedVideoFrame

            frames = [
                ExtractedVideoFrame(float(index), Image.new("RGB", (20, 20), color=(index, 0, 0)))
                for index in range(2)
            ]
            with patch("archive_index.indexing.embeddings.extract_video_frames", return_value=frames):
                result = index_embeddings(workspace, provider=FakeProvider(dimension=3))
            self.assertEqual((result.succeeded, result.errors), (1, 0))
            with closing(workspace.connect()) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM video_frame_embedding").fetchone()[0], 2)

    def test_disabled_search_does_not_call_provider(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (20, 30)).save(root / "photo.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            provider = FakeProvider()
            result = index_embeddings(workspace, provider=provider)
            self.assertEqual(result.skipped, 1)
            self.assertEqual(provider.calls, 0)
            with self.assertRaises(RuntimeError):
                search_text(workspace, "photo", provider=provider)

    def test_parallel_preparation_is_failure_isolated(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (20, 30), color=(20, 40, 60)).save(root / "good-1.jpg")
            (root / "broken.jpg").write_bytes(b"not an image")
            Image.new("RGB", (24, 18), color=(60, 40, 20)).save(root / "good-2.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            workspace.apply_configuration({**workspace.configuration(), "semantic_search_enabled": True})

            result = index_embeddings(workspace, provider=FakeProvider(), preparation_workers=4, batch_size=2)

            self.assertEqual((result.succeeded, result.errors), (2, 1))
            with closing(workspace.connect()) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM logical_asset_embedding"
                    ).fetchone()[0],
                    2,
                )

    def test_cancelled_preparation_stops_workers_and_resume_skips_completed_rows(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            for index in range(8):
                Image.new("RGB", (20 + index, 30), color=(20, 40, 60)).save(root / f"photo-{index}.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            workspace.apply_configuration({**workspace.configuration(), "semantic_search_enabled": True})
            cancel_event = threading.Event()

            def cancel_after_first(progress):
                if progress.processed >= 1:
                    cancel_event.set()

            cancelled = index_embeddings(
                workspace,
                provider=FakeProvider(),
                preparation_workers=4,
                batch_size=2,
                cancel_event=cancel_event,
                progress=cancel_after_first,
            )

            self.assertTrue(cancelled.cancelled)
            self.assertGreaterEqual(cancelled.succeeded, 1)
            self.assertFalse(any(thread.name.startswith("archive-index-embedding-prep") for thread in threading.enumerate()))

            resumed = index_embeddings(
                workspace,
                provider=FakeProvider(),
                preparation_workers=2,
                batch_size=2,
            )

            self.assertGreaterEqual(resumed.skipped, 1)
            self.assertEqual(resumed.errors, 0)


if __name__ == "__main__":
    unittest.main()
