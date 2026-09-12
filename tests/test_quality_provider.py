from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.quality_provider import (
    OffQualityProvider,
    ProviderResult,
    QualityProviderUnavailable,
    LARIQAProvider,
    default_quality_preparation_workers,
)
from archive_index.workspace import Workspace


class FakeProvider:
    enabled = True

    def __init__(self, version: str = "1", fail_name: str | None = None) -> None:
        self.algorithm = "fake-quality"
        self.version = version
        self.fail_name = fail_name
        self.preflight_calls = 0
        self.path_calls = 0
        self.image_calls = 0
        self.prepared_calls = 0

    def preflight(self) -> None:
        self.preflight_calls += 1

    def score_paths(self, paths):
        self.path_calls += 1
        results = []
        for path in paths:
            if path.name == self.fail_name:
                raise RuntimeError("simulated provider failure")
            results.append(ProviderResult({"model_output": 0.73}, 0.73, None))
        return results

    def score_images(self, images):
        self.image_calls += 1
        if self.fail_name and self.image_calls == 1:
            raise RuntimeError("simulated provider failure")
        return [ProviderResult({"model_output": 0.73}, 0.73, None) for _ in images]

    def score_prepared_images(self, images):
        self.prepared_calls += 1
        if self.fail_name and self.prepared_calls == 1:
            raise RuntimeError("simulated provider failure")
        return [ProviderResult({"model_output": 0.73}, 0.73, None) for _ in images]


class BatchSessionProvider(FakeProvider):
    def __init__(self, workspace) -> None:
        super().__init__()
        self.workspace = workspace
        self.session_status = None

    @contextmanager
    def batch_session(self):
        with closing(self.workspace.connect()) as connection:
            self.session_status = connection.execute(
                "SELECT status FROM job ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()[0]
        yield self


class QualityProviderTests(unittest.TestCase):
    def test_cpu_quality_preparation_default_is_two_workers(self) -> None:
        with patch("torch.cuda.is_available", return_value=False):
            self.assertEqual(default_quality_preparation_workers(), 2)
        with patch("torch.cuda.is_available", return_value=True):
            self.assertEqual(default_quality_preparation_workers(), 8)
        with patch(
            "archive_index.media.quality_provider.default_quality_preparation_workers",
            return_value=2,
        ):
            self.assertEqual(LARIQAProvider(model_path=Path("missing.pt")).preparation_workers, 2)

    def test_provider_off_preserves_cached_quality_and_recommendations_are_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (120, 80), "navy").save(root / "photo.jpg")
            workspace = Workspace.create(root)
            workspace.set_quality_provider("lar-iqa")
            scan(workspace)
            provider = FakeProvider()
            index_workspace(workspace, components=("quality",), quality_provider=provider)

            with closing(workspace.connect()) as connection:
                before = connection.execute(
                    "SELECT quality_score, quality_raw_json FROM physical_file"
                ).fetchone()
                state_before = connection.execute(
                    "SELECT status, input_fingerprint FROM component_state WHERE component = 'quality'"
                ).fetchone()

            workspace.set_quality_provider("off")
            with patch(
                "archive_index.indexing.media_pipeline._prepare_component_states",
                side_effect=AssertionError("quality-off state preparation should be skipped"),
            ):
                result = index_workspace(workspace, components=("quality",), quality_provider=OffQualityProvider())
            with closing(workspace.connect()) as connection:
                after = connection.execute(
                    "SELECT quality_score, quality_raw_json FROM physical_file"
                ).fetchone()
                state_after = connection.execute(
                    "SELECT status, input_fingerprint FROM component_state WHERE component = 'quality'"
                ).fetchone()
                active_row = connection.execute(
                    "SELECT active_run_id FROM workspace_recommendation WHERE id = 1"
                ).fetchone()

            self.assertEqual(result.skipped, 1)
            self.assertEqual(tuple(before), tuple(after))
            self.assertEqual(tuple(state_before), tuple(state_after))
            self.assertTrue(active_row is None or active_row[0] is None)

            workspace.set_quality_provider("lar-iqa")
            cached_provider = FakeProvider()
            resumed = index_workspace(workspace, components=("quality",), quality_provider=cached_provider)
            self.assertEqual(resumed.skipped, 1)
            self.assertEqual(cached_provider.preflight_calls, 0)

    def test_fake_provider_preflight_and_raw_canonical_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (120, 80), "navy").save(root / "photo.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            provider = FakeProvider()

            result = index_workspace(workspace, components=("quality",), quality_provider=provider)

            self.assertEqual((result.succeeded, result.errors), (1, 0))
            self.assertEqual((provider.preflight_calls, provider.image_calls, provider.prepared_calls), (1, 0, 1))
            with closing(workspace.connect()) as connection:
                row = connection.execute(
                    "SELECT quality_raw_json, quality_components_json, quality_score, quality_algorithm, quality_version FROM physical_file"
                ).fetchone()
                state = connection.execute(
                    "SELECT algorithm, version, settings_json, status FROM component_state WHERE component = 'quality'"
                ).fetchone()
            self.assertEqual(json.loads(row[0])["model_output"], 0.73)
            self.assertIsNone(row[1])
            self.assertEqual(tuple(row[2:]), (0.73, "fake-quality", "1"))
            self.assertEqual((state[0], state[1], state[3]), ("fake-quality", "1", "complete"))

    def test_combined_thumbnail_and_quality_uses_one_prepared_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (120, 80), "navy").save(root / "photo.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            provider = FakeProvider()

            result = index_workspace(
                workspace,
                components=("thumbnail", "quality"),
                quality_provider=provider,
            )

            self.assertEqual((result.succeeded, result.errors), (1, 0))
            self.assertEqual((provider.path_calls, provider.image_calls, provider.prepared_calls), (0, 0, 1))

    def test_cached_quality_does_not_preflight_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (120, 80), "navy").save(root / "photo.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            index_workspace(workspace, components=("quality",), quality_provider=FakeProvider())
            cached_provider = FakeProvider()

            result = index_workspace(
                workspace,
                components=("quality",),
                quality_provider=cached_provider,
                quality_batch_size=4,
            )

            self.assertEqual((result.succeeded, result.skipped), (1, 1))
            self.assertEqual(cached_provider.preflight_calls, 0)

    def test_quality_job_is_running_before_batch_session_initializes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (120, 80), "navy").save(root / "photo.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            provider = BatchSessionProvider(workspace)

            result = index_workspace(
                workspace,
                components=("quality",),
                quality_provider=provider,
                quality_batch_size=2,
            )

            self.assertEqual((result.errors, result.skipped, provider.session_status), (0, 0, "running"))

    def test_unavailable_lar_batch_marks_items_failed_instead_of_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (120, 80), "navy").save(root / "photo.jpg")
            workspace = Workspace.create(root)
            scan(workspace)

            result = index_workspace(
                workspace,
                components=("quality",),
                quality_provider=LARIQAProvider(model_path=root / "missing.pt"),
                quality_batch_size=2,
            )

            self.assertEqual((result.errors, result.skipped), (1, 0))
            with closing(workspace.connect()) as connection:
                state = connection.execute(
                    "SELECT status FROM component_state WHERE component = 'quality'"
                ).fetchone()[0]
            self.assertEqual(state, "failed")

    def test_provider_failure_isolated_to_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (120, 80), "navy").save(root / "good.jpg")
            Image.new("RGB", (120, 80), "red").save(root / "bad.jpg")
            workspace = Workspace.create(root)
            scan(workspace)

            result = index_workspace(
                workspace,
                components=("quality",),
                quality_provider=FakeProvider(fail_name="bad.jpg"),
            )

            self.assertEqual((result.succeeded, result.errors), (1, 1))
            with closing(workspace.connect()) as connection:
                states = connection.execute(
                    """
                    SELECT physical_file.relative_path, component_state.status
                    FROM physical_file JOIN component_state ON component_state.physical_file_id = physical_file.id
                    WHERE component_state.component = 'quality' ORDER BY physical_file.relative_path
                    """
                ).fetchall()
            self.assertEqual([tuple(row) for row in states], [("bad.jpg", "failed"), ("good.jpg", "complete")])

    def test_quality_batch_path_is_resumable_and_isolates_failed_batch_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (120, 80), "navy").save(root / "good.jpg")
            Image.new("RGB", (120, 80), "red").save(root / "bad.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            result = index_workspace(
                workspace,
                components=("quality",),
                quality_provider=FakeProvider(fail_name="bad.jpg"),
                quality_batch_size=2,
            )
            self.assertEqual((result.succeeded, result.errors), (1, 1))
            resumed = index_workspace(
                workspace,
                components=("quality",),
                quality_provider=FakeProvider(fail_name="bad.jpg"),
                quality_batch_size=2,
            )
            self.assertEqual((resumed.succeeded, resumed.errors, resumed.skipped), (1, 1, 1))

    def test_quality_batch_path_leaves_videos_not_requested(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"not a real video")
            workspace = Workspace.create(root)
            scan(workspace)
            provider = FakeProvider()

            result = index_workspace(
                workspace,
                components=("quality",),
                quality_provider=provider,
                quality_batch_size=2,
            )

            self.assertEqual((result.succeeded, result.errors), (1, 0))
            self.assertEqual((provider.path_calls, provider.image_calls, provider.prepared_calls), (0, 0, 0))
            with closing(workspace.connect()) as connection:
                status = connection.execute(
                    "SELECT status FROM component_state WHERE component = 'quality'"
                ).fetchone()[0]
            self.assertEqual(status, "not_requested")

    def test_quality_version_recomputes_only_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (120, 80), "navy").save(root / "photo.jpg")
            workspace = Workspace.create(root)
            scan(workspace)
            index_workspace(
                workspace,
                components=("metadata", "thumbnail", "quality"),
                quality_provider=FakeProvider(version="1"),
            )
            index_workspace(workspace, components=("quality",), quality_provider=FakeProvider(version="2"))

            with closing(workspace.connect()) as connection:
                states = {
                    row[0]: row[1:]
                    for row in connection.execute(
                        "SELECT component, status, version FROM component_state ORDER BY component"
                    ).fetchall()
                }
            self.assertEqual(states["metadata"], ("complete", "4"))
            self.assertEqual(states["thumbnail"], ("complete", "pillow-jpeg-v2"))
            self.assertEqual(states["quality"], ("complete", "2"))

    def test_missing_lar_dependencies_or_checkpoint_fails_preflight_once(self) -> None:
        provider = LARIQAProvider(model_path=Path("missing-lar-iqa-checkpoint.pt"))
        with self.assertRaises(QualityProviderUnavailable):
            provider.preflight()


if __name__ == "__main__":
    unittest.main()
