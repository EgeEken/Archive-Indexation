from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import Image

from archive_index.indexing.scanner import scan
from archive_index.indexing.video_quality import (
    ExtractedVideoFrame,
    VideoQualityError,
    aggregate_scores,
    index_video_quality,
    sample_count,
    sample_timestamps,
)
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


class VideoQualityTests(unittest.TestCase):
    @staticmethod
    def _fake_frames(timestamps):
        return [
            ExtractedVideoFrame(timestamp, Image.new("RGB", (64, 64), "navy"))
            for timestamp in timestamps
        ]

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
