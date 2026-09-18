from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from archive_index.app import main
from archive_index.api.server import serve


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

    def test_serve_handles_keyboard_interrupt_without_traceback(self) -> None:
        output = io.StringIO()
        with patch("archive_index.api.server.WorkspaceHTTPServer") as server_class, patch(
            "archive_index.api.server.webbrowser.open"
        ):
            server = server_class.return_value
            server.server_port = 8765
            server.default_handle = None
            server.serve_forever.side_effect = KeyboardInterrupt
            with redirect_stdout(output):
                serve()

        server.stop_background_jobs.assert_called_once_with()
        server.server_close.assert_called_once_with()
        self.assertEqual(output.getvalue(), "Archive Indexation UI: http://127.0.0.1:8765/\n\nArchive Indexation UI stopped.\n")


if __name__ == "__main__":
    unittest.main()
