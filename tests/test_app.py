from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from PIL import Image

from archive_index.app import main


class AppTests(unittest.TestCase):
    def test_doctor_command(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["doctor"])

        self.assertEqual(result, 0)
        self.assertIn("ready", output.getvalue())

    def test_run_command(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["run"])

        self.assertEqual(result, 0)
        self.assertIn("application shell is ready", output.getvalue())

    def test_index_command_creates_and_processes_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (40, 30), color=(100, 140, 200)).save(root / "photo.jpg")
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["index", str(root)])

            self.assertEqual(result, 0)
            self.assertTrue((root / ".archive-index" / "index.sqlite").is_file())
            self.assertIn("Index complete", output.getvalue())


if __name__ == "__main__":
    unittest.main()
