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
from archive_index.api import raw_development as raw_module
from archive_index.api.errors import ResourceNotFound
from archive_index.workspace import Workspace
from archive_index.indexing.scanner import scan


class _FakeRaw:
    calls = []
    camera_whitebalance = [2.0, 1.0, 1.5, 1.0]

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

    def test_display_gamma_and_native_orientation_are_not_overridden(self):
        source = Path(__file__).parents[1] / "src" / "archive_index" / "api" / "raw_development.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn("gamma=(1, 1)", text)
        self.assertNotIn("user_flip=0", text)

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
                negative, _ = raw_development_preview(workspace, physical_id, -1)
                positive, _ = raw_development_preview(workspace, physical_id, 1)
            self.assertEqual([round(call["exp_shift"], 3) for call in _FakeRaw.calls], [0.5, 2.0])
            self.assertEqual(source.read_bytes(), before)
            with Image.open(__import__("io").BytesIO(negative)) as image:
                self.assertLessEqual(max(image.size), 2560)
            self.assertNotEqual(negative, positive)
            self.assertTrue(all(call["use_camera_wb"] and call["no_auto_bright"] for call in _FakeRaw.calls))
            self.assertTrue(all("gamma" not in call and "user_flip" not in call for call in _FakeRaw.calls))

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

    def test_extended_exposure_range_clamps_rawpy_shift_and_uses_bright_residual(self):
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
            with patch.dict(sys.modules, {"rawpy": fake_rawpy}):
                raw_development_preview(workspace, physical_id, -5)
                raw_development_preview(workspace, physical_id, 5)
            self.assertEqual(round(_FakeRaw.calls[0]["exp_shift"], 3), 0.25)
            self.assertEqual(round(_FakeRaw.calls[0]["bright"], 3), 0.125)
            self.assertEqual(round(_FakeRaw.calls[1]["exp_shift"], 3), 8)
            self.assertEqual(round(_FakeRaw.calls[1]["bright"], 3), 4)

    def test_extended_exposure_mapping_is_monotonic(self):
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
            exposures = [-5, -3, -2, 0, 3, 5]
            with patch.dict(sys.modules, {"rawpy": fake_rawpy}):
                for exposure in exposures:
                    raw_development_preview(workspace, physical_id, exposure)
            effective_shifts = [call["exp_shift"] * call["bright"] for call in _FakeRaw.calls]
            self.assertEqual(effective_shifts, sorted(effective_shifts))

    def test_offline_raw_is_rejected_before_decode_and_cache_is_keyed_by_ev(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "camera.arw"
            source.write_bytes(b"raw source")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                physical_id = connection.execute("SELECT id FROM physical_file WHERE extension = '.arw'").fetchone()[0]
                connection.execute("UPDATE physical_file SET is_online = 0 WHERE id = ?", (physical_id,))
                connection.commit()
            finally:
                connection.close()
            fake_rawpy = SimpleNamespace(
                ColorSpace=SimpleNamespace(sRGB="sRGB"),
                imread=lambda _: _FakeRaw(),
            )
            with patch.dict(sys.modules, {"rawpy": fake_rawpy}):
                with self.assertRaises(ResourceNotFound):
                    raw_development_preview(workspace, physical_id, 0)

    def test_as_shot_white_balance_is_neutral_and_relative_adjustments_are_bounded(self):
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
            fake_rawpy = SimpleNamespace(ColorSpace=SimpleNamespace(sRGB="sRGB"), imread=lambda _: _FakeRaw())
            _FakeRaw.calls = []
            with patch.dict(sys.modules, {"rawpy": fake_rawpy}):
                _, baseline = raw_development_preview(workspace, physical_id, 0)
                raw_development_preview(workspace, physical_id, 0, white_balance=100)
                raw_development_preview(workspace, physical_id, 0, white_balance=-100)
                raw_development_preview(workspace, physical_id, 0, saturation=125, highlights=-30, shadows=40)
            self.assertEqual(baseline, "Camera/as-shot WB")
            self.assertTrue(_FakeRaw.calls[0]["use_camera_wb"])
            self.assertIsNone(_FakeRaw.calls[0]["user_wb"])
            self.assertGreater(_FakeRaw.calls[1]["user_wb"][0], _FakeRaw.calls[1]["user_wb"][2])
            self.assertLess(_FakeRaw.calls[2]["user_wb"][0], _FakeRaw.calls[2]["user_wb"][2])
            self.assertFalse(_FakeRaw.calls[1]["use_camera_wb"])

    def test_missing_camera_white_balance_reports_rawpy_fallback(self):
        class NoCameraWB(_FakeRaw):
            camera_whitebalance = None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "camera.arw").write_bytes(b"raw source")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                physical_id = connection.execute("SELECT id FROM physical_file WHERE extension = '.arw'").fetchone()[0]
            finally:
                connection.close()
            fake_rawpy = SimpleNamespace(ColorSpace=SimpleNamespace(sRGB="sRGB"), imread=lambda _: NoCameraWB())
            with patch.dict(sys.modules, {"rawpy": fake_rawpy}):
                _, status = raw_development_preview(workspace, physical_id, 0)
            self.assertEqual(status, "Rawpy auto-WB fallback (camera WB unavailable)")
            self.assertTrue(NoCameraWB.calls[-1]["use_auto_wb"])

    def test_all_controls_participate_in_cache_and_invalid_ranges_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "camera.arw").write_bytes(b"raw source")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                physical_id = connection.execute("SELECT id FROM physical_file WHERE extension = '.arw'").fetchone()[0]
            finally:
                connection.close()
            fake_rawpy = SimpleNamespace(ColorSpace=SimpleNamespace(sRGB="sRGB"), imread=lambda _: _FakeRaw())
            _FakeRaw.calls = []
            with patch.dict(sys.modules, {"rawpy": fake_rawpy}):
                for settings in ({}, {"white_balance": 5}, {"saturation": 105}, {"highlights": -5}, {"shadows": 5}):
                    raw_development_preview(workspace, physical_id, 0, **settings)
                raw_development_preview(workspace, physical_id, 0)
                with self.assertRaises(ValueError):
                    raw_development_preview(workspace, physical_id, 0.25)
                with self.assertRaises(ValueError):
                    raw_development_preview(workspace, physical_id, 0, white_balance=1)
                with self.assertRaises(ValueError):
                    raw_development_preview(workspace, physical_id, 0, saturation=101)
                with self.assertRaises(ValueError):
                    raw_development_preview(workspace, physical_id, 0, highlights=1)
                with self.assertRaises(ValueError):
                    raw_development_preview(workspace, physical_id, 0, shadows=1)
                with self.assertRaises(ValueError):
                    raw_development_preview(workspace, physical_id, 0, saturation=151)
            self.assertEqual(len(_FakeRaw.calls), 5)

    def test_raw_cache_obeys_byte_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "camera.arw").write_bytes(b"raw source")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                physical_id = connection.execute("SELECT id FROM physical_file WHERE extension = '.arw'").fetchone()[0]
            finally:
                connection.close()
            with patch.object(raw_module, "MAX_CACHE_BYTES", 100), patch.object(raw_module, "_decode_and_resize", return_value=(b"x" * 80, "Camera/as-shot WB")):
                raw_development_preview(workspace, physical_id, 0)
                raw_development_preview(workspace, physical_id, 0.5)
                self.assertEqual(len(raw_module._cache), 1)
                self.assertLessEqual(raw_module._cache_bytes, 100)


if __name__ == "__main__":
    unittest.main()
