from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


class FrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is required for frontend formatter tests")
    def test_capture_formatter_keeps_offsets_and_limits_fractional_seconds(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app.js").read_text(encoding="utf-8")
        start = source.index("function formatCapture")
        end = source.index("\n}\n\nfunction renderReviewFilters", start) + 2
        function = source[start:end]
        script = f"const formatCapture = ({function}); console.log(JSON.stringify([formatCapture('2026-09-11T12:28:43.341000+02:00', ''), formatCapture('2026-09-11T12:28:43.000000+02:00', ''), formatCapture('2026-09-11T12:28:43', 'exif_local_unknown')]));"
        result = subprocess.run([shutil.which("node"), "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        self.assertEqual(
            json.loads(result.stdout),
            [
                "2026-09-11 12:28:43.34+02:00",
                "2026-09-11 12:28:43+02:00",
                "2026-09-11 12:28:43 · local time; timezone unknown",
            ],
        )
