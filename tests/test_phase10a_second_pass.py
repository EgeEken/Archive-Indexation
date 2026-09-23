from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from archive_index.api.comparison import comparison_data
from archive_index.file_management import build_dry_run_plan, list_presets, list_rulesets, save_ruleset
from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.reconciliation import reconcile_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.quality_provider import LARIQAProvider
from archive_index.workspace import Workspace


class Phase10ASecondPassTests(unittest.TestCase):
    def test_builtin_presets_are_seeded_and_delete_is_planned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "photo.jpg"
            source.write_bytes(b"photo")
            workspace = Workspace.create(root)
            scan(workspace)
            preset_names = {item["name"] for item in list_presets(workspace)}
            rulesets = list_rulesets(workspace)
            self.assertEqual(preset_names, {"Archive cleanup", "Keep selected only"})
            ruleset = save_ruleset(
                workspace,
                name="Delete test",
                rules=[{"match": {"format": "jpeg"}, "action": {"operation": "delete"}}],
            )
            plan = build_dry_run_plan(workspace, ruleset["id"])
            self.assertEqual(plan["summary"]["delete"]["file_count"], 1)
            self.assertIsNone(plan["operations"][0]["target_relative_path"])
            self.assertFalse(plan["executor"]["available"])
            self.assertEqual(len(rulesets), 2)

    def test_all_matching_rules_report_copy_delete_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "photo.jpg").write_bytes(b"photo")
            workspace = Workspace.create(root)
            scan(workspace)
            ruleset = save_ruleset(
                workspace,
                name="Conflict test",
                rules=[
                    {"match": {"format": "jpeg"}, "action": {"operation": "copy", "target_template": "copies/{filename}"}},
                    {"match": {"format": "jpeg"}, "action": {"operation": "delete"}},
                ],
            )
            plan = build_dry_run_plan(workspace, ruleset["id"])
            self.assertEqual(plan["summary"]["candidate_count"], 2)
            self.assertEqual(plan["summary"]["conflict_count"], 2)
            self.assertTrue(all("copied and deleted" in conflict["reason"] for conflict in plan["conflicts"]))

    def test_external_jxl_peer_merges_without_derived_lineage(self):
        try:
            import imagecodecs
        except ImportError:
            self.skipTest("imagecodecs is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pixels = np.full((20, 30, 3), [80, 120, 160], dtype=np.uint8)
            Image.fromarray(pixels).save(root / "photo.jpg", quality=95)
            (root / "photo.jxl").write_bytes(imagecodecs.jpegxl_encode(pixels, lossless=True))
            workspace = Workspace.create(root)
            scan(workspace)
            reconcile_workspace(workspace)
            connection = workspace.connect()
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM logical_asset").fetchone()[0], 1)
                relation = connection.execute("SELECT relationship_type, algorithm FROM physical_relationship").fetchone()
            finally:
                connection.close()
            self.assertEqual(tuple(relation), ("external_rendered_peer", "same-stem-visual-identity"))

    def test_comparison_metrics_use_actual_physical_pair(self):
        try:
            import imagecodecs
        except ImportError:
            self.skipTest("imagecodecs is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pixels = np.full((20, 30, 3), [80, 120, 160], dtype=np.uint8)
            Image.fromarray(pixels).save(root / "photo.jpg", quality=95)
            (root / "photo.jxl").write_bytes(imagecodecs.jpegxl_encode(pixels, lossless=True))
            workspace = Workspace.create(root)
            scan(workspace)
            reconcile_workspace(workspace)
            connection = workspace.connect()
            try:
                ids = [row[0] for row in connection.execute("SELECT id FROM physical_file ORDER BY relative_path")]
            finally:
                connection.close()
            result = comparison_data(workspace, ids[0], ids[1], "workspace")
            self.assertIsNotNone(result["metrics"]["mse"])
            self.assertIn("comparison-preview", result["left"]["preview_url"])

    def test_lar_iqa_preparation_uses_canonical_loader(self):
        provider = object.__new__(LARIQAProvider)
        provider._transforms = type("Transforms", (), {"authentic": staticmethod(lambda image: "authentic"), "synthetic": staticmethod(lambda image: "synthetic")})()
        image = Image.new("RGB", (4, 4), "red")
        with patch("archive_index.media.quality_provider.load_full_image", return_value=image) as loader:
            result = provider._prepare_path(Path("photo.jxl"))
        loader.assert_called_once_with(Path("photo.jxl"))
        self.assertEqual(result, ("authentic", "synthetic"))

    def test_combined_jxl_metadata_thumbnail_pass_reuses_one_full_decode(self):
        try:
            import imagecodecs
        except ImportError:
            self.skipTest("imagecodecs is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pixels = np.full((20, 30, 3), 80, dtype=np.uint8)
            (root / "photo.jxl").write_bytes(imagecodecs.jpegxl_encode(pixels, lossless=True))
            workspace = Workspace.create(root)
            scan(workspace)
            from archive_index.media.image_decode import load_full_image

            with patch("archive_index.indexing.media_pipeline.load_full_image", wraps=load_full_image) as loader:
                result = index_workspace(workspace, components=("metadata", "thumbnail"))
            self.assertEqual(result.errors, 0)
            self.assertEqual(loader.call_count, 1)


if __name__ == "__main__":
    unittest.main()
