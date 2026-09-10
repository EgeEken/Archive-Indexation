from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.quality import measure_quality, score_from_raw
from archive_index.workspace import Workspace


class QualityTests(unittest.TestCase):
    def test_blur_clipping_noise_and_resolution_behave_sensibly(self) -> None:
        sharp = _scene()
        blurred = sharp.filter(ImageFilter.GaussianBlur(5))
        clipped = ImageChops.add(sharp, Image.new("RGB", sharp.size, (180, 180, 180)))
        noisy = Image.effect_noise(sharp.size, 30).convert("RGB")

        sharp_result = measure_quality(sharp)
        self.assertGreater(sharp_result.raw["focus_gradient"], measure_quality(blurred).raw["focus_gradient"])
        self.assertGreater(
            sharp_result.components["focus"], measure_quality(blurred).components["focus"]
        )
        self.assertGreater(
            sharp_result.components["exposure"], measure_quality(clipped).components["exposure"]
        )
        self.assertGreater(
            measure_quality(noisy).raw["noise_mad"], sharp_result.raw["noise_mad"]
        )

        resized_large = measure_quality(sharp.resize((1280, 960), Image.Resampling.NEAREST))
        resized_small = measure_quality(sharp.resize((640, 480), Image.Resampling.NEAREST))
        self.assertLess(abs(resized_large.score - resized_small.score), 0.08)

    def test_formula_recomputes_from_persisted_raw_measurements(self) -> None:
        result = measure_quality(_scene())
        recomputed = score_from_raw(result.raw)
        self.assertEqual(recomputed.components, result.components)
        self.assertEqual(recomputed.score, result.score)

    def test_shared_thumbnail_and_quality_path_decodes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            _scene().save(root / "photo.jpg", format="JPEG")
            workspace = Workspace.create(root)
            scan(workspace)

            from archive_index.indexing import media_pipeline

            real_loader = media_pipeline.load_reduced_image
            with patch(
                "archive_index.indexing.media_pipeline.load_reduced_image",
                side_effect=real_loader,
            ) as loader:
                result = index_workspace(workspace, components=("thumbnail", "quality"))

            self.assertEqual((result.succeeded, result.errors), (1, 0))
            loader.assert_called_once()

    def test_score_version_recomputes_from_raw_without_source_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            _scene().save(root / "photo.jpg", format="JPEG")
            workspace = Workspace.create(root)
            scan(workspace)
            first = index_workspace(workspace, components=("quality",))
            self.assertEqual((first.succeeded, first.errors), (1, 0))

            from archive_index.indexing import media_pipeline

            with patch.object(media_pipeline, "QUALITY_SCORE_VERSION", "2"), patch.object(
                media_pipeline,
                "load_reduced_image",
                side_effect=AssertionError("formula-only change decoded source"),
            ):
                second = index_workspace(workspace, components=("quality",))
            self.assertEqual((second.succeeded, second.errors), (1, 0))

            with closing(workspace.connect()) as connection:
                state = connection.execute(
                    "SELECT status, version FROM component_state WHERE component = 'quality'"
                ).fetchone()
            self.assertEqual(tuple(state), ("complete", "2"))

    def test_quality_failure_does_not_abort_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            _scene().save(root / "good.jpg", format="JPEG")
            (root / "bad.jpg").write_bytes(b"not an image")
            workspace = Workspace.create(root)
            scan(workspace)
            result = index_workspace(workspace, components=("quality",))

            self.assertEqual((result.succeeded, result.errors), (1, 1))
            with closing(workspace.connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT physical_file.relative_path, component_state.status
                    FROM physical_file
                    JOIN component_state ON component_state.physical_file_id = physical_file.id
                    WHERE component_state.component = 'quality'
                    ORDER BY physical_file.relative_path
                    """
                ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in rows], [("bad.jpg", "failed"), ("good.jpg", "complete")])


def _scene(size: tuple[int, int] = (640, 480)) -> Image.Image:
    image = Image.new("RGB", size, (70, 90, 120))
    draw = ImageDraw.Draw(image)
    for x in range(0, size[0], 40):
        draw.line((x, 0, x, size[1]), fill=(230, 230, 230), width=4)
    for y in range(0, size[1], 40):
        draw.line((0, y, size[0], y), fill=(30, 30, 30), width=4)
    draw.rectangle((100, 100, 400, 300), outline=(255, 180, 30), width=12)
    return image


if __name__ == "__main__":
    unittest.main()
