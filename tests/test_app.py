from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

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
        with patch("archive_index.api.server.serve") as serve:
            result = main(["run"])

        self.assertEqual(result, 0)
        serve.assert_called_once_with(None, host="127.0.0.1", port=8765)

    def test_no_command_starts_home_ui(self) -> None:
        with patch("archive_index.api.server.serve") as serve:
            result = main([])

        self.assertEqual(result, 0)
        serve.assert_called_once_with(None, host="127.0.0.1", port=8765)

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
