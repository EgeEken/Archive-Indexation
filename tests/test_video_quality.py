from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

import numpy as np
from PIL import Image

from archive_index.embeddings.providers import EmbeddingProvider
from archive_index.indexing.embeddings import index_embeddings
from archive_index.indexing.scanner import scan
from archive_index.indexing.video_quality import (
    ExtractedVideoFrame,
    VideoQualityError,
    aggregate_scores,
    index_video_quality,
    prepare_shared_video_quality,
    sample_count,
    sample_timestamps,
)
from archive_index.jobs.engine import JobStore
from archive_index.media.quality_provider import OffQualityProvider, ProviderResult
from archive_index.workspace import Workspace


class FakeVideoProvider:
    enabled = True
    algorithm = "fake-lar-iqa"
    version = "2"
    batch_size = 2
    settings = {"model": "fake"}

    def __init__(self, scores=None):
        self.scores = iter(scores or [])
        self.preflight_calls = 0
        self.image_calls = 0

    def preflight(self):
        self.preflight_calls += 1

    def score_images(self, images):
        self.image_calls += 1
        return [ProviderResult({"model_output": score}, score) for score in [next(self.scores) for _ in images]]


class FakeEmbeddingProvider(EmbeddingProvider):
    provider_id = "fake-video-embedding"
    model_id = "fake-video-embedding"
    version = "1"
    dimension = 3
    batch_size = 8

    @property
    def settings(self):
        return {
            "model_id": self.model_id,
            "version": self.version,
            "dimension": self.dimension,
            "normalization": "unit_l2",
        }

    def preflight(self):
        return None

    def encode_images(self, images):
        return [np.array([image.width, image.height, 1.0], dtype=np.float32) for image in images]

    def encode_text(self, text):
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)


class VideoQualityTests(unittest.TestCase):
    @staticmethod
    def _fake_frames(timestamps):
        return [
            ExtractedVideoFrame(timestamp, Image.new("RGB", (64, 64), "navy"))
            for timestamp in timestamps
        ]

    @staticmethod
    def _video_workspace(temporary_directory):
        root = Path(temporary_directory) / "archive"
        root.mkdir()
        (root / "clip.mp4").write_bytes(b"video")
        workspace = Workspace.create(root)
        scan(workspace)
        with workspace.transaction() as connection:
            connection.execute("UPDATE physical_file SET duration_seconds = 2.0 WHERE relative_path = 'clip.mp4'")
        workspace.apply_configuration({**workspace.configuration(), "semantic_search_enabled": True, "include_videos_in_semantic_search": True})
        return workspace

    def test_shared_video_frames_feed_quality_and_embeddings_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = self._video_workspace(temporary_directory)
            quality_job_id = JobStore(workspace).create("video_quality")
            quality = prepare_shared_video_quality(
                workspace,
                job_id=quality_job_id,
                quality_provider=FakeVideoProvider([0.2, 0.4, 0.8, 0.6]),
            )

            def fake_extract(source, timestamps, cancel_event=None, **kwargs):
                return self._fake_frames(timestamps)

            with patch("archive_index.indexing.embeddings.extract_video_frames", side_effect=fake_extract) as extractor:
                embeddings = index_embeddings(
                    workspace,
                    provider=FakeEmbeddingProvider(),
                    video_frame_consumer=quality.consume,
                )
                video_quality = quality.finish()

            self.assertEqual((embeddings.errors, video_quality.errors, extractor.call_count), (0, 0, 1))
            with closing(workspace.connect()) as connection:
                states = connection.execute(
                    "SELECT component, status FROM component_state WHERE component IN ('quality', 'embedding:fake-video-embedding') ORDER BY component"
                ).fetchall()
                frame_count = connection.execute("SELECT COUNT(*) FROM video_frame_embedding").fetchone()[0]
                score = connection.execute("SELECT quality_score FROM physical_file").fetchone()[0]
            self.assertEqual([tuple(row) for row in states], [("embedding:fake-video-embedding", "complete"), ("quality", "complete")])
            self.assertEqual(frame_count, 4)
            self.assertAlmostEqual(score, 0.8)

    def test_shared_video_quality_skips_cached_quality_while_embedding_decodes(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = self._video_workspace(temporary_directory)
            provider = FakeVideoProvider([0.2, 0.4, 0.8, 0.6])
            with patch("archive_index.indexing.video_quality.extract_video_frames", side_effect=lambda source, timestamps, cancel_event=None, **kwargs: self._fake_frames(timestamps)):
                index_video_quality(workspace, quality_provider=provider)
            quality_job_id = JobStore(workspace).create("video_quality")
            cached_quality_provider = FakeVideoProvider()
            quality = prepare_shared_video_quality(
                workspace,
                job_id=quality_job_id,
                quality_provider=cached_quality_provider,
            )
            with patch(
                "archive_index.indexing.embeddings.extract_video_frames",
                side_effect=lambda source, timestamps, cancel_event=None, **kwargs: self._fake_frames(timestamps),
            ) as extractor:
                embeddings = index_embeddings(
                    workspace,
                    provider=FakeEmbeddingProvider(),
                    video_frame_consumer=quality.consume,
                )
                video_quality = quality.finish()
            self.assertEqual((embeddings.errors, video_quality.skipped, extractor.call_count), (0, 1, 1))
            self.assertEqual(cached_quality_provider.preflight_calls, 0)

    def test_cached_embeddings_leave_quality_to_its_normal_decode_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = self._video_workspace(temporary_directory)
            with patch(
                "archive_index.indexing.embeddings.extract_video_frames",
                side_effect=lambda source, timestamps, cancel_event=None, **kwargs: self._fake_frames(timestamps),
            ):
                index_embeddings(workspace, provider=FakeEmbeddingProvider())
            quality_job_id = JobStore(workspace).create("video_quality")
            quality = prepare_shared_video_quality(
                workspace,
                job_id=quality_job_id,
                quality_provider=FakeVideoProvider([0.2, 0.4, 0.8, 0.6]),
            )
            with patch(
                "archive_index.indexing.video_quality.extract_video_frames",
                side_effect=lambda source, timestamps, cancel_event=None, **kwargs: self._fake_frames(timestamps),
            ) as extractor:
                video_quality = quality.finish()
            self.assertEqual((video_quality.errors, video_quality.succeeded, extractor.call_count), (0, 1, 1))

    def test_quality_failure_does_not_fail_shared_embeddings(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = self._video_workspace(temporary_directory)
            quality_job_id = JobStore(workspace).create("video_quality")
            quality = prepare_shared_video_quality(
                workspace,
                job_id=quality_job_id,
                quality_provider=FakeVideoProvider([0.8]),
            )
            with patch(
                "archive_index.indexing.embeddings.extract_video_frames",
                side_effect=lambda source, timestamps, cancel_event=None, **kwargs: self._fake_frames(timestamps),
            ):
                embeddings = index_embeddings(
                    workspace,
                    provider=FakeEmbeddingProvider(),
                    video_frame_consumer=quality.consume,
                )
                video_quality = quality.finish()
            self.assertEqual((embeddings.errors, video_quality.errors), (0, 1))
            with closing(workspace.connect()) as connection:
                status = connection.execute(
                    "SELECT status FROM component_state WHERE component = 'quality'"
                ).fetchone()[0]
            self.assertEqual(status, "failed")

    def test_sampling_rule_and_top_quartile_aggregation(self):
        self.assertEqual(sample_count(1.01, 2, 2, 32), 3)
        self.assertEqual(sample_count(30, 2, 2, 32), 32)
        self.assertEqual(sample_timestamps(10, 4), [1.25, 3.75, 6.25, 8.75])
        self.assertEqual((round(aggregate_scores([0.2, 0.4, 0.8, 0.6, 0.9], 5)[0], 2), 5, False), (0.85, 5, False))

    def test_lar_video_provenance_includes_checkpoint_identity_before_preflight(self):
        from archive_index.indexing.video_quality import video_quality_provenance

        provider = FakeVideoProvider()
        provider.algorithm = "lar-iqa"
        algorithm, version, settings = video_quality_provenance(
            provider,
            {
                "video_sampling_fps": 2.0,
                "video_sampling_min_frames": 2,
                "video_sampling_max_frames": 32,
            },
        )
        self.assertEqual((algorithm, version), ("lar-iqa-video-top-quartile-mean", "1"))
        self.assertEqual(settings["sampler_version"], "3")
        self.assertEqual(settings["long_video_strategy"], "per-frame-input-seek")
        self.assertEqual(settings["provider_settings"]["checkpoint_sha256"], "70c243d7324c76df43df8ab6a44eb535ee9f4f3acb928e5dfe9deb2bb3b7b0ab")

    def test_video_quality_persists_samples_and_cached_rerun_skips(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            source = root / "clip.mp4"
            source.write_bytes(b"video")
            workspace = Workspace.create(root)
            scan(workspace)
            with workspace.transaction() as connection:
                connection.execute("UPDATE physical_file SET duration_seconds = 2.0 WHERE relative_path = 'clip.mp4'")
            provider = FakeVideoProvider([0.2, 0.4, 0.8, 0.6])
            frames = []

            def fake_extract(source, timestamps, cancel_event=None):
                frames.extend(timestamps)
                return self._fake_frames(timestamps)

            with patch("archive_index.indexing.video_quality.extract_video_frames", side_effect=fake_extract) as extractor:
                result = index_video_quality(workspace, quality_provider=provider)

            self.assertEqual((result.errors, result.skipped), (0, 0))
            self.assertEqual(len(frames), 4)
            self.assertEqual(provider.preflight_calls, 1)
            with closing(workspace.connect()) as connection:
                score, algorithm, version = connection.execute(
                    "SELECT quality_score, quality_algorithm, quality_version FROM physical_file"
                ).fetchone()
                sample_run = connection.execute(
                    "SELECT status, requested_count, successful_count FROM video_sample_run"
                ).fetchone()
                timestamp = connection.execute(
                    "SELECT requested_timestamp, actual_timestamp, timestamp_error_seconds FROM video_sample ORDER BY sample_index LIMIT 1"
                ).fetchone()
                sample_count_value = connection.execute("SELECT COUNT(*) FROM video_sample").fetchone()[0]
            self.assertEqual((round(score, 3), algorithm, version), (0.8, "lar-iqa-video-top-quartile-mean", "1"))
            self.assertEqual(tuple(sample_run), ("complete", 4, 4))
            self.assertEqual(tuple(timestamp), (0.25, 0.25, 0.0))
            self.assertEqual(sample_count_value, 4)

            with patch("archive_index.indexing.video_quality.extract_video_frames", side_effect=AssertionError("decoded cached video")) as extractor:
                cached_provider = FakeVideoProvider()
                cached = index_video_quality(workspace, quality_provider=cached_provider)
            self.assertEqual((cached.errors, cached.skipped), (0, 1))
            extractor.assert_not_called()
            self.assertEqual(cached_provider.preflight_calls, 0)

            with patch("archive_index.indexing.video_quality.extract_video_frames", side_effect=AssertionError("decoded disabled video")):
                disabled = index_video_quality(workspace, quality_provider=OffQualityProvider())
            self.assertEqual((disabled.errors, disabled.skipped), (0, 1))
            with closing(workspace.connect()) as connection:
                state, preserved_score = connection.execute(
                    "SELECT component_state.status, physical_file.quality_score FROM component_state JOIN physical_file ON physical_file.id = component_state.physical_file_id"
                ).fetchone()
            self.assertEqual(state, "complete")
            self.assertEqual(round(preserved_score, 3), 0.8)

    def test_partial_video_result_is_published_when_three_quarters_succeed(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"video")
            workspace = Workspace.create(root)
            scan(workspace)
            with workspace.transaction() as connection:
                connection.execute("UPDATE physical_file SET duration_seconds = 2.0 WHERE relative_path = 'clip.mp4'")
            provider = FakeVideoProvider([0.2, 0.4, 0.8])

            def fake_extract(source, timestamps, cancel_event=None):
                return self._fake_frames(timestamps[:3])

            with patch("archive_index.indexing.video_quality.extract_video_frames", side_effect=fake_extract):
                result = index_video_quality(workspace, quality_provider=provider)

            self.assertEqual(result.errors, 0)
            with closing(workspace.connect()) as connection:
                run = connection.execute("SELECT status, successful_count FROM video_sample_run").fetchone()
                score = connection.execute("SELECT quality_score FROM physical_file").fetchone()[0]
            self.assertEqual(tuple(run), ("partial", 3))
            self.assertIsNotNone(score)

    def test_insufficient_samples_fail_without_an_active_result(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"video")
            workspace = Workspace.create(root)
            scan(workspace)
            with workspace.transaction() as connection:
                connection.execute("UPDATE physical_file SET duration_seconds = 2.0 WHERE relative_path = 'clip.mp4'")
            provider = FakeVideoProvider([0.2])
            with patch(
                "archive_index.indexing.video_quality.extract_video_frames",
                return_value=self._fake_frames([0.25]),
            ):
                result = index_video_quality(workspace, quality_provider=provider)
            self.assertEqual(result.errors, 1)
            with closing(workspace.connect()) as connection:
                status, score, run_status = connection.execute(
                    "SELECT component_state.status, physical_file.quality_score, video_sample_run.status FROM component_state JOIN physical_file ON physical_file.id = component_state.physical_file_id JOIN video_sample_run ON video_sample_run.physical_file_id = physical_file.id"
                ).fetchone()
            self.assertEqual(status, "failed")
            self.assertIsNone(score)
            self.assertEqual(run_status, "failed")

    def test_cancellation_does_not_activate_partial_sample_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"video")
            workspace = Workspace.create(root)
            scan(workspace)
            with workspace.transaction() as connection:
                connection.execute("UPDATE physical_file SET duration_seconds = 2.0 WHERE relative_path = 'clip.mp4'")
            cancel = Event()

            def fake_extract(source, timestamps, cancel_event=None):
                cancel.set()
                return self._fake_frames(timestamps[:1])

            with patch("archive_index.indexing.video_quality.extract_video_frames", side_effect=fake_extract):
                result = index_video_quality(workspace, quality_provider=FakeVideoProvider([0.7]), cancel_event=cancel)
            self.assertTrue(result.cancelled)
            with closing(workspace.connect()) as connection:
                run_status, active = connection.execute(
                    "SELECT video_sample_run.status, workspace_video_sample.active_run_id FROM video_sample_run LEFT JOIN workspace_video_sample ON workspace_video_sample.physical_file_id = video_sample_run.physical_file_id"
                ).fetchone()
            self.assertEqual(run_status, "cancelled")
            self.assertIsNone(active)

    def test_unknown_duration_is_a_recorded_failure(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"video")
            workspace = Workspace.create(root)
            scan(workspace)
            result = index_video_quality(workspace, quality_provider=FakeVideoProvider())
            self.assertEqual(result.errors, 1)
            with closing(workspace.connect()) as connection:
                status = connection.execute("SELECT status FROM component_state WHERE component = 'quality'").fetchone()[0]
            self.assertEqual(status, "failed")

    def test_long_video_uses_cancellable_input_seek_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"video")
            workspace = Workspace.create(root)
            scan(workspace)
            with workspace.transaction() as connection:
                connection.execute("UPDATE physical_file SET duration_seconds = 31.0 WHERE relative_path = 'clip.mp4'")
            provider = FakeVideoProvider([0.7] * 32)
            calls = []

            def fake_extract(source, timestamps, cancel_event=None, **kwargs):
                calls.append(kwargs)
                return self._fake_frames(timestamps)

            with patch("archive_index.indexing.video_quality.extract_video_frames", side_effect=fake_extract):
                result = index_video_quality(workspace, quality_provider=provider)

            self.assertEqual(result.errors, 0)
            self.assertEqual(calls, [{"seek_per_frame": True}])


if __name__ == "__main__":
    unittest.main()
