from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PIL import Image

from archive_index.configuration import default_configuration, normalize_configuration
from archive_index.indexing.reconciliation import reconcile_workspace
from archive_index.indexing.scanner import scan
from archive_index.planning import analyze_folder, plan_from_analysis
from archive_index.workspace import Workspace


class PlanningTests(unittest.TestCase):
    def test_configuration_normalizes_legacy_extension_filters_to_supported_categories(self) -> None:
        configuration = normalize_configuration({"image_extensions": [".jpg"], "video_extensions": [".mp4"]})

        self.assertIn(".png", configuration["image_extensions"])
        self.assertIn(".arw", configuration["image_extensions"])
        self.assertIn(".mov", configuration["video_extensions"])

    def test_analysis_is_non_destructive_and_excludes_app_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            (root / "nested").mkdir(parents=True)
            (root / ".archive-index").mkdir()
            (root / "photo.JPG").write_bytes(b"jpg")
            (root / "nested" / "clip.MP4").write_bytes(b"video")
            (root / "notes.txt").write_bytes(b"ignored")
            (root / ".archive-index" / "should-not-appear.jpg").write_bytes(b"ignored")
            before = (root / "photo.JPG").stat()

            analysis = analyze_folder(root)
            plan = plan_from_analysis(analysis, default_configuration())

            self.assertEqual(analysis["totals"]["files"], 3)
            self.assertEqual(analysis["totals"]["recognized_files"], 2)
            self.assertEqual(plan["selected_files"], 2)
            self.assertEqual(plan["selected_bytes"], len(b"jpg") + len(b"video"))
            after = (root / "photo.JPG").stat()
            self.assertEqual((before.st_size, before.st_mtime_ns), (after.st_size, after.st_mtime_ns))

    def test_plan_applies_nested_folder_and_extension_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            (root / "keep").mkdir(parents=True)
            (root / "skip").mkdir()
            (root / "keep" / "a.jpg").write_bytes(b"a")
            (root / "skip" / "b.jpg").write_bytes(b"bb")
            (root / "keep" / "c.mp4").write_bytes(b"ccc")
            config = default_configuration()
            config["folder_rules"] = [{"path": "skip", "included": False}]

            plan = plan_from_analysis(analyze_folder(root), config)

            self.assertEqual(plan["selected_files"], 2)
            self.assertEqual(plan["selected_bytes"], 4)
            self.assertEqual(plan["selected_extensions"], {".jpg": 1, ".mp4": 1})

    def test_root_index_is_not_a_workspace_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            index_root = Path(temporary_directory) / ".archive-index"
            index_root.mkdir()
            with self.assertRaises(ValueError):
                analyze_folder(index_root)

    def test_existing_reconciled_raw_jpeg_is_counted_once_for_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (100, 80), "navy").save(root / "image.jpg")
            (root / "image.arw").write_bytes(b"raw")
            workspace = Workspace.create(root)
            scan(workspace)
            reconcile_workspace(workspace)
            configuration = workspace.configuration()
            configuration["raw_quality_provider"] = "lar-iqa"
            plan = plan_from_analysis(analyze_folder(root), configuration, workspace)

            self.assertEqual(plan["quality_rendered_image_count"], 1)
            self.assertEqual(plan["quality_raw_candidate_count"], 0)
            self.assertEqual(plan["quality_image_count"], 1)


if __name__ == "__main__":
    unittest.main()
