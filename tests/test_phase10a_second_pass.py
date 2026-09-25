from __future__ import annotations

import tempfile
import sys
import types
import threading
import time
import unittest
from io import BytesIO
from math import log1p
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

from archive_index.api.comparison import DIFFERENCE_PNG_COMPRESS_LEVEL, _difference_map, _encode_difference, _mse, comparison_data, comparison_difference
from archive_index.file_management import build_dry_run_plan, cancel_plan_analysis, list_presets, list_profiles, list_rulesets, plan_analysis_status, save_profile, save_ruleset, start_plan_analysis
from archive_index.file_management_previews import _encode, bundled_preview_manifest, custom_profile_preview
from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.reconciliation import reconcile_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.quality_provider import LARIQAProvider
from archive_index.workspace import Workspace


class Phase10ASecondPassTests(unittest.TestCase):
    def test_planner_scales_without_reenumerating_the_same_destination_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace.create(Path(directory))
            now = "2026-09-25T00:00:00+00:00"
            count = 2500
            with workspace.transaction() as connection:
                connection.executemany(
                    "INSERT INTO logical_asset(id, media_type, selection_state, created_at, updated_at) VALUES (?, 'image', 'selected', ?, ?)",
                    ((f"asset-{index}", now, now) for index in range(count)),
                )
                connection.executemany(
                    "INSERT INTO physical_file(id, logical_asset_id, relative_path, filename, extension, media_type, role, size_bytes, is_online, in_scope, created_at, updated_at) VALUES (?, ?, ?, ?, '.jpg', 'image', 'source_original', 1024, 1, 1, ?, ?)",
                    ((f"file-{index}", f"asset-{index}", f"photos/{index:04}.jpg", f"{index:04}.jpg", now, now) for index in range(count)),
                )
            ruleset = save_ruleset(workspace, name="Copy all", rules=[{
                "match": {"format": "jpeg"},
                "action": {"operation": "copy", "destination_dir": "sorted", "preserve_relative_structure": False},
            }])
            original_iterdir = Path.iterdir
            directory_reads = 0
            phases = set()

            def counted_iterdir(path):
                nonlocal directory_reads
                directory_reads += 1
                return original_iterdir(path)

            with patch.object(Path, "iterdir", counted_iterdir):
                plan = build_dry_run_plan(workspace, ruleset["id"], progress=lambda phase, completed, total: phases.add(phase))
            self.assertEqual(plan["summary"]["copy"]["file_count"], count)
            self.assertEqual(plan["summary"]["candidate_count"], count)
            self.assertLess(directory_reads, 5)
            self.assertTrue({"Loading indexed files", "Matching rules", "Resolving targets", "Checking destination conflicts", "Summarizing plan"} <= phases)

    def test_planner_session_reports_and_honors_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace.create(Path(directory))
            started = threading.Event()

            def wait_for_cancel(workspace, ruleset_id, *, progress, cancelled):
                progress("Matching rules", 0, 1)
                started.set()
                while not cancelled.wait(.01):
                    pass
                raise InterruptedError("Plan analysis was cancelled.")

            with patch("archive_index.file_management.build_dry_run_plan", side_effect=wait_for_cancel):
                session_id = start_plan_analysis(workspace)
                self.assertTrue(started.wait(2))
                self.assertEqual(plan_analysis_status(workspace, session_id)["phase"], "Matching rules")
                self.assertTrue(cancel_plan_analysis(workspace, session_id))
                deadline = time.monotonic() + 2
                while plan_analysis_status(workspace, session_id)["status"] == "running" and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertEqual(plan_analysis_status(workspace, session_id)["status"], "cancelled")

    def test_bundled_profile_previews_have_real_metrics_and_assets(self):
        manifest = bundled_preview_manifest()
        self.assertEqual(manifest["version"], "compression-preview-v1")
        self.assertEqual(len(manifest["profiles"]), 3)
        for profile in manifest["profiles"]:
            self.assertEqual(set(profile["settings"]), {"quality", "effort"})
            self.assertNotIn("quality", profile)
            self.assertNotIn("effort", profile)
            self.assertGreater(profile["compressed"]["size_bytes"], 0)
            self.assertGreaterEqual(profile["metrics"]["mse"], 0)
            self.assertTrue((Path(__file__).parents[1] / "src" / "archive_index" / "web" / profile["compressed"]["url"].removeprefix("/")).is_file())

    def test_avif_preview_maps_quality_to_imagecodecs_level(self):
        try:
            import imagecodecs
        except ImportError:
            self.skipTest("imagecodecs is not installed")
        image = Image.new("RGB", (8, 8), (80, 120, 160))
        try:
            encoder = Mock(return_value=b"encoded")
            fake_imagecodecs = types.SimpleNamespace(avif_encode=encoder)
            with patch.dict(sys.modules, {"imagecodecs": fake_imagecodecs}):
                _encode(image, {"codec": "avif", "settings": {"quality": 23, "effort": 4}})
            self.assertEqual(encoder.call_args.kwargs, {"level": 23, "speed": 4})
        finally:
            image.close()

    def test_avif_quality_changes_actual_encoded_output(self):
        try:
            import imagecodecs
        except ImportError:
            self.skipTest("imagecodecs is not installed")
        try:
            imagecodecs.avif_encode
        except AttributeError:
            self.skipTest("AVIF encoding is not available")
        pixels = np.zeros((64, 64, 3), dtype=np.uint8)
        pixels[..., 0] = np.arange(64, dtype=np.uint8)[:, None]
        pixels[..., 1] = np.arange(64, dtype=np.uint8)[None, :]
        image = Image.fromarray(pixels)
        try:
            low = _encode(image, {"codec": "avif", "settings": {"quality": 20, "effort": 7}})
            high = _encode(image, {"codec": "avif", "settings": {"quality": 80, "effort": 7}})
        finally:
            image.close()
        self.assertNotEqual(low, high)

    def test_custom_preview_cache_writes_complete_files_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace.create(Path(directory))
            profile = {"id": "custom-jxl", "name": "Custom", "codec": "jpeg-xl", "container": "jxl", "settings": {"quality": 60, "effort": 7}}
            with patch("archive_index.file_management_previews.load_full_image", side_effect=lambda _: Image.new("RGB", (8, 8), (80, 120, 160))), patch("archive_index.file_management_previews._encode", return_value=b"encoded"), patch("archive_index.file_management_previews._decode", side_effect=lambda *_: Image.new("RGB", (8, 8), (80, 120, 160))):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(lambda _: custom_profile_preview(workspace, profile), range(4)))
            self.assertEqual({result["profile_id"] for result in results}, {"custom-jxl"})
            cache = workspace.index_directory / "compression-previews"
            self.assertEqual(len(list(cache.glob("*.json"))), 1)
            self.assertEqual(len(list(cache.glob("*.webp"))), 1)
            self.assertFalse(list(cache.glob("*.tmp")))

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

    def test_file_management_targets_are_contained_before_conflict_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "photo.jpg").write_bytes(b"photo")
            workspace = Workspace.create(root)
            scan(workspace)
            valid = ["compressed/file.jxl", "photos/day1/file.jxl", "raws/camera/file.ARW"]
            for target in valid:
                ruleset = save_ruleset(workspace, name=f"Valid {target}", rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "target_template": target}}])
                operation = build_dry_run_plan(workspace, ruleset["id"])["operations"][0]
                self.assertEqual(operation["target_relative_path"], target)
                self.assertFalse(operation["conflicts"])
            invalid = [
                "../outside/file.jxl", "folder/../../outside/file.jxl", "C:/outside/file.jxl",
                r"C:\outside\file.jxl", r"\\server\share\file.jxl", "/archive/path",
                ".archive-index/file", "foo/.archive-index/file", "/.archive-index/file", ".ARCHIVE-INDEX/file",
            ]
            for target in invalid:
                ruleset = save_ruleset(workspace, name=f"Invalid {target}", rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "target_template": target}}])
                operation = build_dry_run_plan(workspace, ruleset["id"])["operations"][0]
                self.assertIsNone(operation["target_relative_path"], target)
                self.assertTrue(operation["conflicts"], target)
                self.assertTrue(any("Destination" in conflict for conflict in operation["conflicts"]), target)
            outside = root.parent / f"outside-target-{root.name}"
            outside.mkdir()
            link = root / "escape-link"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                pass
            else:
                ruleset = save_ruleset(workspace, name="Symlink escape", rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "target_template": "escape-link/file.jxl"}}])
                operation = build_dry_run_plan(workspace, ruleset["id"])["operations"][0]
                self.assertIsNone(operation["target_relative_path"])
                self.assertIn("escapes the workspace", " ".join(operation["conflicts"]))

    def test_planned_same_content_target_is_coalesced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.jpg").write_bytes(b"same")
            (root / "two.jpg").write_bytes(b"same")
            workspace = Workspace.create(root)
            scan(workspace)
            ruleset = save_ruleset(workspace, name="Coalesce", rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "target_template": "out/file.jpg"}}])
            operations = build_dry_run_plan(workspace, ruleset["id"])["operations"]
            self.assertEqual([item["target_relative_path"] for item in operations], ["out/file.jpg", "out/file.jpg"])
            self.assertTrue(operations[1]["coalesced_by_planned_target"])
            self.assertEqual(operations[1]["destination_status"], "already_satisfied")

    def test_planned_different_content_target_renames_or_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.jpg").write_bytes(b"one")
            (root / "two.jpg").write_bytes(b"two")
            workspace = Workspace.create(root)
            scan(workspace)
            renamed = save_ruleset(workspace, name="Rename", rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "target_template": "out/file.jpg", "rename_on_conflict": True}}])
            operations = build_dry_run_plan(workspace, renamed["id"])["operations"]
            self.assertEqual([item["target_relative_path"] for item in operations], ["out/file.jpg", "out/file (1).jpg"])
            blocked = save_ruleset(workspace, name="Blocked", rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "target_template": "out/file.jpg", "rename_on_conflict": False}}])
            operations = build_dry_run_plan(workspace, blocked["id"])["operations"]
            self.assertTrue(any("planned output" in conflict for conflict in operations[1]["conflicts"]))

    def test_executable_summary_excludes_blocked_and_conflicted_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            safe = root / "safe.jpg"
            safe.write_bytes(b"safe")
            (root / "blocked.mp4").write_bytes(b"blocked")
            conflicted = root / "conflicted.png"
            conflicted.write_bytes(b"conflicted")
            workspace = Workspace.create(root)
            scan(workspace)
            av1 = next(item for item in list_profiles(workspace) if item["codec"] == "av1")
            ruleset = save_ruleset(workspace, name="Mixed", rules=[
                {"match": {"format": "jpeg"}, "action": {"operation": "delete"}},
                {"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": av1["id"], "source_disposition": "replace"}},
                {"match": {"format": "png"}, "action": {"operation": "copy", "target_template": "copies/{filename}"}},
                {"match": {"format": "png"}, "action": {"operation": "delete"}},
            ])
            plan = build_dry_run_plan(workspace, ruleset["id"])
            summary = plan["summary"]
            self.assertEqual(summary["candidate_count"], 4)
            self.assertEqual(summary["executable_count"], 1)
            self.assertGreaterEqual(summary["blocked_count"], 1)
            self.assertGreaterEqual(summary["conflicted_count"], 1)
            self.assertEqual(summary["estimated_storage_delta_bytes"], -safe.stat().st_size)
            self.assertEqual(summary["executable_storage_delta_bytes"], -safe.stat().st_size)
            self.assertNotEqual(summary["candidate_storage_delta_bytes"], summary["executable_storage_delta_bytes"])
            self.assertEqual(summary["temporary_space_upper_bound_bytes"], 0)
            self.assertEqual(summary["compress"]["file_count"], 0)
            self.assertEqual(summary["compress"]["candidate_file_count"], 1)

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

    def test_normalized_difference_map_uses_absolute_per_pixel_mse(self):
        left = Image.new("RGB", (2, 2), (0, 0, 0))
        right = Image.fromarray(np.array([[[0, 0, 0], [3, 0, 0]], [[0, 6, 0], [0, 0, 9]]], dtype=np.uint8))
        difference, mse, maximum = _difference_map(left, right)
        pixels = np.asarray(difference)
        self.assertAlmostEqual(mse, (0 + 3 + 12 + 27) / 4)
        self.assertEqual(maximum, 27)
        self.assertEqual(pixels[0, 0].tolist(), [0, 0, 0])
        self.assertEqual(pixels[1, 1].tolist(), [255, 0, 0])
        self.assertEqual(pixels[0, 1, 0], round(log1p(3) / log1p(27) * 255))
        self.assertTrue(0 < pixels[0, 1, 0] < pixels[1, 0, 0] < pixels[1, 1, 0])
        difference.close()
        left.close()
        right.close()

    def test_identical_difference_map_is_black_with_zero_metrics(self):
        left = Image.new("RGB", (3, 2), (80, 120, 160))
        right = left.copy()
        difference, mse, maximum = _difference_map(left, right)
        self.assertEqual(mse, 0)
        self.assertEqual(maximum, 0)
        self.assertTrue(np.array_equal(np.asarray(difference), np.zeros((2, 3, 3), dtype=np.uint8)))
        difference.close()
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
            self.assertNotIn("max_pixel_mse", result["metrics"])
            self.assertIn("byte_identical", result["metrics"])
            self.assertIn("/original", result["left"]["preview_url"])
            self.assertTrue("/preview" in result["right"]["preview_url"] or "comparison-preview" in result["right"]["preview_url"])

    def test_comparison_cache_avoids_redecoding_an_unchanged_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (20, 30), (80, 120, 160)).save(root / "photo.png")
            Image.new("RGB", (20, 30), (80, 120, 160)).save(root / "other.png")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                rows = connection.execute("SELECT id, logical_asset_id FROM physical_file ORDER BY relative_path").fetchall()
                connection.execute("UPDATE physical_file SET logical_asset_id = ? WHERE id = ?", (rows[0]["logical_asset_id"], rows[1]["id"]))
                connection.commit()
            finally:
                connection.close()
            from archive_index.api import comparison as comparison_module
            with patch.object(comparison_module, "_decode", wraps=comparison_module._decode) as decoder:
                first = comparison_data(workspace, rows[0]["id"], rows[1]["id"], "workspace")
                second = comparison_data(workspace, rows[0]["id"], rows[1]["id"], "workspace")
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(decoder.call_count, 2)
            self.assertIn("total", first["timings_ms"])
            self.assertIn("cache_lookup", second["timings_ms"])

    def test_normal_comparison_does_not_compute_or_encode_difference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (20, 30), (80, 120, 160)).save(root / "photo.png")
            Image.new("RGB", (20, 30), (80, 120, 161)).save(root / "other.png")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                rows = connection.execute("SELECT id, logical_asset_id FROM physical_file ORDER BY relative_path").fetchall()
                connection.execute("UPDATE physical_file SET logical_asset_id = ? WHERE id = ?", (rows[0]["logical_asset_id"], rows[1]["id"]))
                connection.commit()
            finally:
                connection.close()
            from archive_index.api import comparison as comparison_module
            with patch.object(comparison_module, "_difference_map", side_effect=AssertionError("Difference map was requested")) as difference_map, patch.object(comparison_module, "_encode_difference") as encoder:
                result = comparison_data(workspace, rows[0]["id"], rows[1]["id"], "workspace")
            self.assertIsNotNone(result["metrics"]["mse"])
            self.assertNotIn("max_pixel_mse", result["metrics"])
            difference_map.assert_not_called()
            encoder.assert_not_called()

    def test_difference_encoding_uses_fast_png_settings(self):
        difference = Image.new("RGB", (2, 2), (128, 0, 0))
        original_save = Image.Image.save
        try:
            with patch.object(Image.Image, "save", autospec=True) as save:
                save.side_effect = original_save
                _encode_difference(difference)
            self.assertEqual(save.call_args.kwargs["compress_level"], DIFFERENCE_PNG_COMPRESS_LEVEL)
            self.assertNotIn("optimize", save.call_args.kwargs)
        finally:
            difference.close()

    def test_difference_cache_is_separate_and_reuses_encoded_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (20, 30), (80, 120, 160)).save(root / "photo.png")
            Image.new("RGB", (20, 30), (80, 120, 161)).save(root / "other.png")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                rows = connection.execute("SELECT id, logical_asset_id FROM physical_file ORDER BY relative_path").fetchall()
                connection.execute("UPDATE physical_file SET logical_asset_id = ? WHERE id = ?", (rows[0]["logical_asset_id"], rows[1]["id"]))
                connection.commit()
            finally:
                connection.close()
            from archive_index.api import comparison as comparison_module
            with patch.object(comparison_module, "_decode", wraps=comparison_module._decode) as decoder:
                first, first_timings = comparison_difference(workspace, rows[0]["id"], rows[1]["id"])
                second, second_timings = comparison_difference(workspace, rows[0]["id"], rows[1]["id"])
            self.assertEqual(first, second)
            self.assertFalse(first_timings["cache_hit"])
            self.assertTrue(second_timings["cache_hit"])
            self.assertEqual(decoder.call_count, 2)

    def test_difference_uses_decoded_dimensions_when_index_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (20, 30), (80, 120, 160)).save(root / "photo.png")
            Image.new("RGB", (20, 30), (80, 120, 161)).save(root / "other.png")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                rows = connection.execute("SELECT id, logical_asset_id FROM physical_file ORDER BY relative_path").fetchall()
                connection.execute("UPDATE physical_file SET logical_asset_id = ?, width = 1, height = 1 WHERE id = ?", (rows[0]["logical_asset_id"], rows[1]["id"]))
                connection.commit()
            finally:
                connection.close()
            body, _ = comparison_difference(workspace, rows[0]["id"], rows[1]["id"])
            with Image.open(BytesIO(body)) as difference:
                self.assertEqual(difference.size, (20, 30))

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

    def test_comparison_difference_png_matches_actual_pair_pixels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = np.zeros((12, 16, 3), dtype=np.uint8)
            changed = original.copy()
            changed[4, 7] = [120, 40, 8]
            Image.fromarray(original).save(root / "preferred.png")
            Image.fromarray(changed).save(root / "other.png")
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                rows = connection.execute("SELECT id, logical_asset_id FROM physical_file ORDER BY relative_path").fetchall()
                connection.execute("UPDATE physical_file SET logical_asset_id = ? WHERE id = ?", (rows[0]["logical_asset_id"], rows[1]["id"]))
                connection.commit()
                unequal = comparison_data(workspace, rows[0]["id"], rows[1]["id"], "workspace")
                identical = comparison_data(workspace, rows[0]["id"], rows[0]["id"], "workspace")
            finally:
                connection.close()
            diff_bytes, timings = comparison_difference(workspace, rows[0]["id"], rows[1]["id"])
            with Image.open(BytesIO(diff_bytes)) as diff:
                pixels = np.asarray(diff)
            self.assertEqual(pixels.shape, (12, 16, 3))
            self.assertTrue(np.array_equal(pixels[0, 0], [0, 0, 0]))
            self.assertEqual(pixels[4, 7].tolist(), [255, 0, 0])
            self.assertGreater(unequal["metrics"]["mse"], 0)
            expected_max = (120 ** 2 + 40 ** 2 + 8 ** 2) / 3
            self.assertEqual(unequal["metrics"]["mse"], round(expected_max / (12 * 16), 6))
            self.assertNotIn("max_pixel_mse", unequal["metrics"])
            self.assertEqual(timings["max_pixel_mse"], round(expected_max, 6))
            identical_bytes, identical_timings = comparison_difference(workspace, rows[0]["id"], rows[0]["id"])
            identical_pixels = np.asarray(Image.open(BytesIO(identical_bytes)))
            self.assertFalse(np.any(identical_pixels))
            self.assertNotIn("max_pixel_mse", identical["metrics"])
            self.assertIn("difference_compute", timings)
            self.assertIn("difference_encode", timings)
            self.assertFalse(identical_timings["cache_hit"])

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
