from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from archive_index.file_management import build_dry_run_plan, save_profile, save_ruleset
from archive_index.media.codec_benchmark import benchmark_jpeg_xl_decode, codec_capabilities
from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.jxl import decode
from archive_index.media.quality_provider import OffQualityProvider
from archive_index.workspace import Workspace


class Phase10ADataTests(unittest.TestCase):
    def test_jxl_is_decoded_and_gets_an_app_owned_display_preview(self):
        try:
            import imagecodecs
        except ImportError:
            self.skipTest("imagecodecs is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "photo.jxl"
            source.write_bytes(imagecodecs.jpegxl_encode(np.full((20, 30, 3), 80, dtype="uint8"), lossless=True))
            self.assertEqual(decode(source).size, (30, 20))
            workspace = Workspace.create(root)
            scan(workspace)
            result = index_workspace(
                workspace,
                components=("metadata", "thumbnail"),
                quality_provider=OffQualityProvider(),
            )
            self.assertEqual(result.errors, 0)
            connection = workspace.connect()
            try:
                row = connection.execute(
                    """
                    SELECT pf.width, pf.height, cs.status, cs.algorithm,
                           dp.output_path
                    FROM physical_file AS pf
                    JOIN component_state AS cs ON cs.physical_file_id = pf.id
                                               AND cs.component = 'thumbnail'
                    LEFT JOIN display_preview AS dp ON dp.physical_file_id = pf.id
                    WHERE pf.extension = '.jxl'
                    """
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual((row["width"], row["height"]), (30, 20))
            self.assertEqual(row["status"], "complete")
            self.assertEqual(row["algorithm"], "imagecodecs-jxl-jpeg")
            self.assertTrue(workspace.index_path(row["output_path"]).is_file())

    def test_file_management_plan_is_deterministic_and_never_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.jpg").write_bytes(b"one")
            (root / "two.jpg").write_bytes(b"two")
            workspace = Workspace.create(root)
            scan(workspace)
            profile = save_profile(
                workspace,
                name="JXL test",
                codec="jpeg-xl",
                container="jxl",
                settings={"distance": 1.0},
            )
            ruleset = save_ruleset(
                workspace,
                name="Rendered photos",
                rules=[{
                    "match": {"extensions": [".jpg"]},
                    "action": {"operation": "compress", "profile_id": profile["id"]},
                }],
            )
            before = {path.name: path.read_bytes() for path in root.glob("*.jpg")}
            first = build_dry_run_plan(workspace, ruleset["id"])
            second = build_dry_run_plan(workspace, ruleset["id"])
            self.assertEqual(first["operations"], second["operations"])
            self.assertEqual(first["summary"]["candidate_count"], 2)
            self.assertTrue(all(operation["requires_confirmation"] for operation in first["operations"]))
            self.assertFalse(any(path.suffix == ".jxl" for path in root.iterdir()))
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.glob("*.jpg")})

    def test_codec_research_harness_reports_jxl_without_compressing_sources(self):
        try:
            import imagecodecs
        except ImportError:
            self.skipTest("imagecodecs is not installed")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.jxl"
            source.write_bytes(imagecodecs.jpegxl_encode(np.zeros((4, 5, 3), dtype="uint8"), lossless=True))
            report = benchmark_jpeg_xl_decode(source, iterations=2)
            self.assertEqual(report["iterations"], 2)
            self.assertGreaterEqual(report["mean_seconds"], 0)
            self.assertTrue(codec_capabilities()["jpeg_xl"]["available"])
