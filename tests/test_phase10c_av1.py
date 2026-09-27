from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from fractions import Fraction
from pathlib import Path

from archive_index.file_management import build_dry_run_plan, list_profiles, save_ruleset
from archive_index.file_management_executor import get_execution, prepare_execution, start_execution
from archive_index.indexing.scanner import scan
from archive_index.media.av1 import analyze_source, build_command, capped_dimensions, source_blocker
from archive_index.workspace import Workspace


class Phase10CAV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000", "-t", "3", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.root / "clip.mp4")],
            check=True,
        )
        self.workspace = Workspace.create(self.root)
        scan(self.workspace)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _wait(self, execution_id: str):
        for _ in range(600):
            result = get_execution(self.workspace, execution_id)
            if result["status"] not in {"running", "cancelling"}:
                return result
            time.sleep(0.01)
        self.fail("AV1 execution did not finish")

    def test_safe_subset_replaces_source_in_place_and_preserves_audio(self):
        profile = next(item for item in list_profiles(self.workspace) if item["name"] == "AV1 1080p60 Very Fast")
        ruleset = save_ruleset(
            self.workspace,
            name="av1-safe-subset",
            rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        self.assertFalse(plan["operations"][0]["blockers"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        start_execution(self.workspace, execution["id"])
        result = self._wait(execution["id"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue((self.root / "clip.mp4").is_file())
        self.assertEqual(analyze_source(self.root / "clip.mp4")["codec"], "av1")
        self.assertEqual(len(analyze_source(self.root / "clip.mp4")["audio"]), 1)
        self.assertEqual(result["operations"][0]["profile_snapshot"]["settings"]["preset"], 10)

    def test_caps_never_upscale(self):
        info = analyze_source(self.root / "clip.mp4")
        self.assertEqual(capped_dimensions(info, {"settings": {"resolution_cap": [3840, 2160], "fps_cap": 120, "speed_class": "fast"}}), (640, 360))
        self.assertIsNone(source_blocker(self.root / "clip.mp4", {"settings": {"resolution_cap": [1920, 1080], "fps_cap": 60, "speed_class": "fast"}}))

    def test_resolution_and_fps_caps_preserve_portrait_and_rational_rates(self):
        profile_1080 = {"settings": {"resolution_cap": [1920, 1080], "fps_cap": 60, "speed_class": "fast"}}
        profile_4k = {"settings": {"resolution_cap": [3840, 2160], "fps_cap": 120, "speed_class": "very_fast"}}
        low = {"width": 1280, "height": 720, "rotation": 0, "fps": Fraction(24, 1)}
        medium = {"width": 1920, "height": 1080, "rotation": 0, "fps": Fraction(30, 1)}
        high = {"width": 3840, "height": 2160, "rotation": 0, "fps": Fraction(60, 1)}
        ultra = {"width": 3840, "height": 2160, "rotation": 0, "fps": Fraction(120, 1)}
        portrait = {"width": 1080, "height": 1920, "rotation": 90, "fps": Fraction(59_940, 1_001)}
        self.assertEqual(capped_dimensions(low, profile_4k), (1280, 720))
        self.assertEqual(capped_dimensions(medium, profile_4k), (1920, 1080))
        self.assertEqual(capped_dimensions(high, profile_4k), (3840, 2160))
        self.assertEqual(capped_dimensions(ultra, profile_1080), (1920, 1080))
        self.assertEqual(capped_dimensions(portrait, profile_1080), (1080, 1920))
        command = build_command(self.root / "clip.mp4", self.root / "output.tmp", ultra, profile_1080)
        self.assertTrue(any("fps=60" in item for item in command))
