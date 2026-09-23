from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from archive_index.api.raw_development import clear_raw_development_cache, raw_development_preview
from archive_index.workspace import Workspace
from archive_index.indexing.scanner import scan


class _FakeRaw:
    calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def postprocess(self, **kwargs):
        self.calls.append(kwargs)
        value = int(round(80 * kwargs["exp_shift"]))
        return np.full((3000, 4000, 3), min(255, value), dtype=np.uint8)


class RawDevelopmentTests(unittest.TestCase):
    def tearDown(self):
        clear_raw_development_cache()

    def test_sensor_preview_uses_actual_rawpy_and_exposure_shift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "camera.arw"
            source.write_bytes(b"raw source")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                physical_id = connection.execute("SELECT id FROM physical_file WHERE extension = '.arw'").fetchone()[0]
            finally:
                connection.close()
            fake_rawpy = SimpleNamespace(
                ColorSpace=SimpleNamespace(sRGB="sRGB"),
                imread=lambda _: _FakeRaw(),
            )
            _FakeRaw.calls = []
            before = source.read_bytes()
            with patch.dict(sys.modules, {"rawpy": fake_rawpy}):
                negative = raw_development_preview(workspace, physical_id, -1)
                positive = raw_development_preview(workspace, physical_id, 1)
            self.assertEqual([round(call["exp_shift"], 3) for call in _FakeRaw.calls], [0.5, 2.0])
            self.assertEqual(source.read_bytes(), before)
            with Image.open(__import__("io").BytesIO(negative)) as image:
                self.assertLessEqual(max(image.size), 2560)
            self.assertNotEqual(negative, positive)
            self.assertTrue(all(call["use_camera_wb"] and call["no_auto_bright"] for call in _FakeRaw.calls))

    def test_exposure_range_is_rejected_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "camera.arw"
            source.write_bytes(b"raw source")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                physical_id = connection.execute("SELECT id FROM physical_file WHERE extension = '.arw'").fetchone()[0]
            finally:
                connection.close()
            with self.assertRaises(ValueError):
                raw_development_preview(workspace, physical_id, 3.1)


if __name__ == "__main__":
    unittest.main()
