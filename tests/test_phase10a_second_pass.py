from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from archive_index.api.comparison import _mse, comparison_data
from archive_index.file_management import build_dry_run_plan, list_presets, list_profiles, list_rulesets, save_profile, save_ruleset
from archive_index.file_management_previews import bundled_preview_manifest
from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.reconciliation import reconcile_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.quality_provider import LARIQAProvider
from archive_index.workspace import Workspace


class Phase10ASecondPassTests(unittest.TestCase):
    def test_bundled_profile_previews_have_real_metrics_and_assets(self):
        manifest = bundled_preview_manifest()
        self.assertEqual(manifest["version"], "compression-preview-v1")
        self.assertEqual(len(manifest["profiles"]), 3)
        for profile in manifest["profiles"]:
            self.assertGreater(profile["compressed"]["size_bytes"], 0)
            self.assertGreaterEqual(profile["metrics"]["mse"], 0)
            self.assertTrue((Path(__file__).parents[1] / "src" / "archive_index" / "web" / profile["compressed"]["url"].removeprefix("/")).is_file())

    def test_builtin_copy_rules_preserve_subfolders_and_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace.create(Path(directory))
            cleanup = next(item for item in list_rulesets(workspace) if item["id"] == "builtin-archive-cleanup")
            copies = [rule["action"] for rule in cleanup["rules"] if rule["action"].get("operation") == "copy"]
            self.assertEqual([action["preserve_relative_structure"] for action in copies], [True, True])
            self.assertTrue(all(action["rename_on_conflict"] for action in copies))

    def test_copy_collision_uses_windows_suffix_and_can_be_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file.jpg").write_bytes(b"source")
            (root / "out").mkdir()
            (root / "out" / "File.JPG").write_bytes(b"other")
            workspace = Workspace.create(root)
            scan(workspace)
            ruleset = save_ruleset(workspace, name="Copy", rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "destination_dir": "out", "rename_on_conflict": True}}])
            operation = build_dry_run_plan(workspace, ruleset["id"])["operations"][0]
            self.assertEqual(operation["target_relative_path"], "out/file (1).jpg")
            self.assertTrue(operation["renamed_to_avoid_conflict"])
            blocked = save_ruleset(workspace, name="Copy blocked", rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "destination_dir": "out", "rename_on_conflict": False}}])
            operation = build_dry_run_plan(workspace, blocked["id"])["operations"][0]
            self.assertTrue(any("already exists" in conflict for conflict in operation["conflicts"]))

    def test_compression_same_path_is_replace_or_rename_not_unconditional_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ODA8_7608.MP4").write_bytes(b"video")
            workspace = Workspace.create(root)
            scan(workspace)
            profile = save_profile(workspace, name="AV1 test", codec="av1", container="mp4", settings={})
            replacement = save_ruleset(workspace, name="Replace", rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace", "compress_in_place": True}}])
            operation = build_dry_run_plan(workspace, replacement["id"])["operations"][0]
            self.assertTrue(operation["replaces_source_in_place"])
            self.assertFalse(any("same" in conflict.lower() for conflict in operation["conflicts"]))
            keep = save_ruleset(workspace, name="Keep", rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "keep", "compress_in_place": True, "rename_on_conflict": True}}])
            operation = build_dry_run_plan(workspace, keep["id"])["operations"][0]
            self.assertEqual(operation["target_relative_path"], "ODA8_7608 (1).mp4")
            blocked = save_ruleset(workspace, name="Keep blocked", rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "keep", "compress_in_place": True, "rename_on_conflict": False}}])
            operation = build_dry_run_plan(workspace, blocked["id"])["operations"][0]
            self.assertTrue(any("already exists" in conflict for conflict in operation["conflicts"]))

    def test_av1_blocker_is_grouped_once_and_capability_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.mp4").write_bytes(b"one")
            (root / "two.mp4").write_bytes(b"two")
            workspace = Workspace.create(root)
            scan(workspace)
            profile = next(item for item in list_profiles(workspace) if item["codec"] == "av1")
            ruleset = save_ruleset(workspace, name="AV1", rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"]}}])
            plan = build_dry_run_plan(workspace, ruleset["id"])
            self.assertEqual(len(plan["blockers"]), 1)
            self.assertEqual(plan["blockers"][0]["affected_count"], 2)
            self.assertFalse(any("not installed" in conflict["reason"] for conflict in plan["conflicts"]))
    def test_mse_is_true_per_channel_pixel_mean(self):
        left = Image.fromarray(np.array([[[0, 0, 0], [255, 255, 255]]], dtype=np.uint8))
        right = Image.fromarray(np.array([[[0, 0, 0], [255, 0, 0]]], dtype=np.uint8))
        self.assertAlmostEqual(_mse(left, right), (2 * 65025) / 6)
        left.close()
        right.close()

    def test_builtins_expose_three_quality_profiles_and_refresh_code_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace.create(Path(directory))
            profiles = {item["name"] for item in list_profiles(workspace)}
            self.assertTrue({"JXL High Quality", "JXL Balanced", "JXL High Compression"}.issubset(profiles))
            custom = save_ruleset(workspace, name="Custom", rules=[])
            connection = workspace.connect()
            try:
                connection.execute("UPDATE file_management_rule SET match_json = '{}' WHERE ruleset_id = ?", ("builtin-archive-cleanup",))
                connection.commit()
            finally:
                connection.close()
            cleanup = next(item for item in list_rulesets(workspace) if item["id"] == "builtin-archive-cleanup")
            self.assertEqual(len(cleanup["rules"]), 8)
            self.assertEqual(next(item for item in list_rulesets(workspace) if item["id"] == custom["id"])["rules"], [])
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
            self.assertIn("byte_identical", result["metrics"])
            self.assertIn("comparison-preview", result["left"]["preview_url"])

    def test_byte_identical_pair_is_explicitly_marked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pixels = np.full((20, 30, 3), [80, 120, 160], dtype=np.uint8)
            Image.fromarray(pixels).save(root / "photo.jpg", quality=95)
            (root / "copy.jpg").write_bytes((root / "photo.jpg").read_bytes())
            workspace = Workspace.create(root)
            scan(workspace)
            reconcile_workspace(workspace)
            connection = workspace.connect()
            try:
                ids = [row[0] for row in connection.execute("SELECT id FROM physical_file ORDER BY relative_path")]
            finally:
                connection.close()
            result = comparison_data(workspace, ids[0], ids[1], "workspace")
            self.assertTrue(result["metrics"]["byte_identical"])
            self.assertTrue(result["metrics"]["pixel_identical"])

    def test_pixel_identical_non_duplicate_pair_is_distinguished(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pixels = np.full((20, 30, 3), [80, 120, 160], dtype=np.uint8)
            Image.fromarray(pixels).save(root / "photo.png", compress_level=0)
            Image.fromarray(pixels).save(root / "other.png", compress_level=9)
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                rows = connection.execute("SELECT id, logical_asset_id FROM physical_file ORDER BY relative_path").fetchall()
                connection.execute("UPDATE physical_file SET logical_asset_id = ? WHERE id = ?", (rows[0]["logical_asset_id"], rows[1]["id"]))
                connection.commit()
                result = comparison_data(workspace, rows[0]["id"], rows[1]["id"], "workspace")
            finally:
                connection.close()
            self.assertFalse(result["metrics"]["byte_identical"])
            self.assertTrue(result["metrics"]["pixel_identical"])

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
