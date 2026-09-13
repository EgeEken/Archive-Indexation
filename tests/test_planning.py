from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archive_index.configuration import default_configuration
from archive_index.planning import analyze_folder, plan_from_analysis


class PlanningTests(unittest.TestCase):
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
            config["include_videos"] = False
            config["folder_rules"] = [{"path": "skip", "included": False}]

            plan = plan_from_analysis(analyze_folder(root), config)

            self.assertEqual(plan["selected_files"], 1)
            self.assertEqual(plan["selected_bytes"], 1)
            self.assertEqual(plan["selected_extensions"], {".jpg": 1})

    def test_root_index_is_not_a_workspace_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            index_root = Path(temporary_directory) / ".archive-index"
            index_root.mkdir()
            with self.assertRaises(ValueError):
                analyze_folder(index_root)


if __name__ == "__main__":
    unittest.main()
