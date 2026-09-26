from __future__ import annotations

import tempfile
import time
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageOps
from PIL import ImageCms

from archive_index.api.browser import physical_rows
from archive_index.file_management import build_dry_run_plan, list_profiles, list_rulesets, save_ruleset
from archive_index.file_management_executor import get_execution, prepare_execution, start_execution
from archive_index.file_management_provenance import link_managed_derivatives
from archive_index.indexing.reconciliation import reconcile_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.jpegxl import decode, encode
from archive_index.workspace import Workspace


class Phase10CCompressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        Image.effect_noise((800, 600), 100).convert("RGB").save(self.root / "photo.jpg", quality=95)
        self.workspace = Workspace.create(self.root)
        scan(self.workspace)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run(self, action: dict[str, object], source_format: str = "jpeg"):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name=f"jxl-{time.time_ns()}",
            rules=[{"match": {"format": source_format}, "action": {"operation": "compress", "profile_id": profile["id"], **action}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        start_execution(self.workspace, execution["id"])
        for _ in range(600):
            result = get_execution(self.workspace, execution["id"])
            if result["status"] not in {"running", "cancelling"}:
                return plan, result
            time.sleep(0.01)
        self.fail("execution did not finish")

    def _run_profile(self, profile_name: str, action: dict[str, object], source_format: str = "jpeg"):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == profile_name)
        ruleset = save_ruleset(
            self.workspace,
            name=f"jxl-{profile_name}-{time.time_ns()}",
            rules=[{"match": {"format": source_format}, "action": {"operation": "compress", "profile_id": profile["id"], **action}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        start_execution(self.workspace, execution["id"])
        for _ in range(600):
            result = get_execution(self.workspace, execution["id"])
            if result["status"] not in {"running", "cancelling"}:
                return plan, result
            time.sleep(0.01)
        self.fail("execution did not finish")

    def test_keep_source_compresses_and_records_managed_provenance(self):
        plan, result = self._run({"source_disposition": "keep", "destination_dir": "derivatives"})
        self.assertEqual(result["status"], "completed")
        operation = result["operations"][0]
        output = self.root / "derivatives" / "photo.jxl"
        self.assertTrue(output.is_file())
        self.assertTrue((self.root / "photo.jpg").is_file())
        self.assertLess(output.stat().st_size, (self.root / "photo.jpg").stat().st_size)
        self.assertEqual(operation["actual_output_sha256"], __import__("hashlib").sha256(output.read_bytes()).hexdigest())
        connection = self.workspace.connect()
        try:
            derivative = connection.execute("SELECT * FROM managed_derivative").fetchone()
            physical = connection.execute("SELECT logical_asset_id, role FROM physical_file WHERE relative_path = 'derivatives/photo.jxl'").fetchone()
            source = connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'photo.jpg'").fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(derivative)
        self.assertEqual(derivative["source_sha256"], plan["operations"][0]["source_sha256"])
        self.assertEqual(physical["logical_asset_id"], source["logical_asset_id"])
        self.assertEqual(physical["role"], "managed_jxl")
        connection = self.workspace.connect()
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM logical_asset").fetchone()[0], 1)
        finally:
            connection.close()
        first_reconcile = reconcile_workspace(self.workspace)
        self.assertEqual(first_reconcile.run_id, reconcile_workspace(self.workspace).run_id)

    def test_replace_source_is_blocked_when_metadata_cannot_be_preserved(self):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="replace",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        self.assertTrue(any("metadata" in blocker.lower() for blocker in plan["operations"][0]["blockers"]))
        self.assertEqual(plan["summary"]["compress"]["file_count"], 0)
        self.assertTrue((self.root / "photo.jpg").is_file())

    def test_archive_cleanup_retains_replace_source_semantics_and_blocks_it(self):
        cleanup = next(item for item in list_rulesets(self.workspace) if item["name"] == "Archive cleanup")
        action = next(item["action"] for item in cleanup["rules"] if item["match"].get("formats") == ["jpeg", "png"] and item["match"].get("selection_state") == "undecided")
        self.assertEqual(action["source_disposition"], "replace")
        plan = build_dry_run_plan(self.workspace, cleanup["id"])
        operation = next(item for item in plan["operations"] if item["source_relative_path"] == "photo.jpg")
        self.assertTrue(any("metadata" in blocker.lower() for blocker in operation["blockers"]))
        self.assertEqual(plan["summary"]["compress"]["file_count"], 0)

    def test_stale_source_fails_without_writing_derivative(self):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="stale",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "keep", "destination_dir": "derivatives"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        (self.root / "photo.jpg").write_bytes(b"changed")
        start_execution(self.workspace, execution["id"])
        for _ in range(600):
            result = get_execution(self.workspace, execution["id"])
            if result["status"] not in {"running", "cancelling"}:
                break
            time.sleep(0.01)
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertIn("Source changed since the plan was confirmed.", result["operations"][0]["error_message"])
        self.assertFalse((self.root / "derivatives" / "photo.jxl").exists())

    def test_png_alpha_is_preserved_in_keep_source_output(self):
        source = Image.new("RGBA", (96, 64), (10, 120, 220, 0))
        pixels = source.load()
        for y in range(source.height):
            for x in range(source.width):
                pixels[x, y] = (10, 120, 220, (x * 255) // (source.width - 1) if y % 2 else (y * 255) // (source.height - 1))
        source.save(self.root / "alpha.png")
        source.close()
        scan(self.workspace)
        plan, result = self._run({"source_disposition": "keep", "destination_dir": "derivatives"}, source_format="png")
        png_operation = next(item for item in plan["operations"] if item["source_relative_path"] == "alpha.png")
        result_operation = next(item for item in result["operations"] if item["source_relative_path"] == "alpha.png")
        self.assertEqual(result_operation["status"], "completed")
        output = self.root / "derivatives" / "alpha.jxl"
        decoded = decode(output.read_bytes())
        try:
            self.assertEqual(decoded.size, (96, 64))
            self.assertEqual(len(decoded.getbands()), 4)
            with Image.open(self.root / "alpha.png") as original:
                self.assertTrue(np.array_equal(np.asarray(original)[:, :, 3], np.asarray(decoded)[:, :, 3]))
        finally:
            decoded.close()
        self.assertEqual(result_operation["source_sha256"], png_operation["source_sha256"])

    def test_exif_orientation_is_normalized_without_double_rotation(self):
        oriented = Image.effect_noise((600, 800), 100).convert("RGB")
        exif = oriented.getexif()
        exif[274] = 6
        oriented.save(self.root / "oriented.jpg", exif=exif, quality=95)
        oriented.close()
        scan(self.workspace)
        _, result = self._run({"source_disposition": "keep", "destination_dir": "derivatives"})
        output = self.root / "derivatives" / "oriented.jxl"
        decoded = decode(output.read_bytes())
        try:
            with Image.open(self.root / "oriented.jpg") as source:
                self.assertEqual(decoded.size, ImageOps.exif_transpose(source).size)
        finally:
            decoded.close()
        self.assertEqual(next(item for item in result["operations"] if item["source_relative_path"] == "oriented.jpg")["status"], "completed")

    def test_embedded_icc_profile_is_a_planner_blocker(self):
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        with Image.open(self.root / "photo.jpg") as source:
            source.save(self.root / "icc.jpg", icc_profile=profile, quality=95)
        scan(self.workspace)
        balanced = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="icc-blocked",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": balanced["id"], "source_disposition": "keep", "destination_dir": "derivatives"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        operation = next(item for item in plan["operations"] if item["source_relative_path"] == "icc.jpg")
        self.assertTrue(any("ICC color profile" in blocker for blocker in operation["blockers"]))
        self.assertFalse((self.root / "derivatives" / "icc.jxl").exists())

    def test_external_replacement_at_same_path_detaches_managed_lineage(self):
        _, result = self._run({"source_disposition": "keep", "destination_dir": "derivatives"})
        output = self.root / "derivatives" / "photo.jxl"
        original = output.read_bytes()
        replacement_image = Image.new("RGB", (800, 600), (20, 30, 40))
        try:
            output.write_bytes(encode(replacement_image, {"codec": "jpeg-xl", "settings": {"quality": 60, "effort": 7}}))
        finally:
            replacement_image.close()
        self.assertNotEqual(output.read_bytes(), original)
        scan(self.workspace)
        connection = self.workspace.connect()
        try:
            logical_asset_id = connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'derivatives/photo.jxl'").fetchone()[0]
        finally:
            connection.close()
        stale_browser_row = next(row for row in physical_rows(self.workspace, logical_asset_id) if row["relative_path"] == "derivatives/photo.jxl")
        self.assertIsNone(stale_browser_row["managed_derivative_id"])
        link_managed_derivatives(self.workspace)
        reconcile_workspace(self.workspace)
        connection = self.workspace.connect()
        try:
            derivative = connection.execute("SELECT physical_file_id FROM managed_derivative").fetchone()
            output_row = connection.execute("SELECT logical_asset_id, role FROM physical_file WHERE relative_path = 'derivatives/photo.jxl'").fetchone()
        finally:
            connection.close()
        self.assertIsNone(derivative["physical_file_id"])
        self.assertEqual(output_row["role"], "source_original")
        rows = physical_rows(self.workspace, output_row["logical_asset_id"])
        current = next(row for row in rows if row["relative_path"] == "derivatives/photo.jxl")
        self.assertIsNone(current["managed_derivative_id"])

    def test_deleted_and_recreated_output_requires_exact_hash(self):
        _, result = self._run({"source_disposition": "keep", "destination_dir": "derivatives"})
        output = self.root / "derivatives" / "photo.jxl"
        original = output.read_bytes()
        output.unlink()
        scan(self.workspace)
        link_managed_derivatives(self.workspace)
        connection = self.workspace.connect()
        try:
            self.assertIsNone(connection.execute("SELECT physical_file_id FROM managed_derivative").fetchone()["physical_file_id"])
        finally:
            connection.close()
        replacement = Image.new("RGB", (800, 600), (90, 20, 10))
        try:
            output.write_bytes(encode(replacement, {"codec": "jpeg-xl", "settings": {"quality": 60, "effort": 7}}))
        finally:
            replacement.close()
        scan(self.workspace)
        link_managed_derivatives(self.workspace)
        connection = self.workspace.connect()
        try:
            self.assertIsNone(connection.execute("SELECT physical_file_id FROM managed_derivative").fetchone()["physical_file_id"])
        finally:
            connection.close()
        output.write_bytes(original)
        scan(self.workspace)
        link_managed_derivatives(self.workspace)
        connection = self.workspace.connect()
        try:
            self.assertIsNotNone(connection.execute("SELECT physical_file_id FROM managed_derivative").fetchone()["physical_file_id"])
        finally:
            connection.close()

    def test_overwrite_keeps_history_but_only_latest_lineage_is_current(self):
        first_plan, first = self._run_profile("JXL High Quality", {"source_disposition": "keep", "destination_dir": "derivatives"})
        second_plan, second = self._run_profile("JXL Balanced", {"source_disposition": "keep", "destination_dir": "derivatives", "conflict_policy": "overwrite"})
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        connection = self.workspace.connect()
        try:
            rows = connection.execute("SELECT profile_name, physical_file_id FROM managed_derivative ORDER BY created_at, id").fetchall()
            output = connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'derivatives/photo.jxl'").fetchone()
        finally:
            connection.close()
        self.assertEqual(len(rows), 2)
        current_record = next(row for row in rows if row["physical_file_id"] is not None)
        historical_record = next(row for row in rows if row["physical_file_id"] is None)
        self.assertEqual(current_record["profile_name"], "JXL Balanced")
        self.assertEqual(historical_record["profile_name"], "JXL High Quality")
        current = [row for row in physical_rows(self.workspace, output["logical_asset_id"]) if row["relative_path"] == "derivatives/photo.jxl"]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["managed_derivative_profile_name"], "JXL Balanced")

    def test_unsupported_png_bit_depth_is_a_planner_blocker(self):
        Image.new("I;16", (32, 24), 1024).save(self.root / "wide.png")
        scan(self.workspace)
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="unsupported-png",
            rules=[{"match": {"format": "png"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "keep", "destination_dir": "derivatives"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        operation = next(item for item in plan["operations"] if item["source_relative_path"] == "wide.png")
        self.assertTrue(any("8-bit RGB and RGBA" in blocker for blocker in operation["blockers"]))

    def test_rename_conflict_freezes_deterministic_jxl_target(self):
        (self.root / "derivatives").mkdir()
        (self.root / "derivatives" / "photo.jxl").write_bytes(b"external target")
        plan, result = self._run({"source_disposition": "keep", "destination_dir": "derivatives", "conflict_policy": "rename"})
        self.assertEqual(plan["operations"][0]["target_relative_path"], "derivatives/photo (1).jxl")
        self.assertEqual(result["operations"][0]["status"], "completed")
        self.assertEqual((self.root / "derivatives" / "photo.jxl").read_bytes(), b"external target")
        self.assertTrue((self.root / "derivatives" / "photo (1).jxl").is_file())

    def test_skip_conflict_does_not_encode(self):
        (self.root / "derivatives").mkdir()
        target = self.root / "derivatives" / "photo.jxl"
        target.write_bytes(b"external target")
        _, result = self._run({"source_disposition": "keep", "destination_dir": "derivatives", "conflict_policy": "skip"})
        operation = result["operations"][0]
        self.assertEqual(operation["status"], "skipped")
        self.assertIn("destination already exists", operation["error_message"])
        self.assertEqual(target.read_bytes(), b"external target")

    def test_changed_overwrite_target_fails_closed(self):
        (self.root / "derivatives").mkdir()
        target = self.root / "derivatives" / "photo.jxl"
        target.write_bytes(b"reviewed target")
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="overwrite",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "keep", "destination_dir": "derivatives", "conflict_policy": "overwrite"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        target.write_bytes(b"changed after review")
        start_execution(self.workspace, execution["id"])
        for _ in range(600):
            result = get_execution(self.workspace, execution["id"])
            if result["status"] not in {"running", "cancelling"}:
                break
            time.sleep(0.01)
        self.assertEqual(result["operations"][0]["status"], "failed")
        self.assertIn("Destination changed since the plan was confirmed.", result["operations"][0]["error_message"])
        self.assertEqual(target.read_bytes(), b"changed after review")

    def test_non_smaller_output_is_skipped_without_final_file(self):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="no-benefit",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "keep", "destination_dir": "derivatives"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        source_size = plan["operations"][0]["source_size_bytes"]
        with patch("archive_index.file_management_executor.validate_jxl", return_value={"size_bytes": source_size, "sha256": "unused"}):
            start_execution(self.workspace, execution["id"])
            for _ in range(600):
                result = get_execution(self.workspace, execution["id"])
                if result["status"] not in {"running", "cancelling"}:
                    break
                time.sleep(0.01)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["operations"][0]["status"], "skipped")
        self.assertIn("not smaller", result["operations"][0]["error_message"])
        self.assertFalse((self.root / "derivatives" / "photo.jxl").exists())

    def test_finalized_output_recovers_as_completed_after_process_loss(self):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="recovery",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "keep", "destination_dir": "derivatives"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        source = self.root / "photo.jpg"
        source_image = Image.open(source).convert("RGB")
        try:
            encoded = encode(source_image, plan["operations"][0]["profile_snapshot"])
        finally:
            source_image.close()
        target = self.root / "derivatives" / "photo.jxl"
        target.parent.mkdir()
        target.write_bytes(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        connection = self.workspace.connect()
        try:
            operation_id = connection.execute("SELECT id FROM file_management_execution_operation WHERE execution_id = ?", (execution["id"],)).fetchone()[0]
        finally:
            connection.close()
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE file_management_execution SET status = 'running' WHERE id = ?", (execution["id"],))
            connection.execute(
                "UPDATE file_management_execution_operation SET status = 'running', stage = 'finalizing', actual_output_size_bytes = ?, actual_output_sha256 = ? WHERE id = ?",
                (len(encoded), digest, operation_id),
            )
        recovered = get_execution(self.workspace, execution["id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["operations"][0]["status"], "completed")
        self.assertEqual(recovered["operations"][0]["actual_output_sha256"], digest)
        connection = self.workspace.connect()
        try:
            metadata_contract = connection.execute("SELECT metadata_contract_json FROM managed_derivative").fetchone()[0]
        finally:
            connection.close()
        self.assertIn('"metadata_policy": "source-retained"', metadata_contract)
        self.assertIn('"recovered": true', metadata_contract)


if __name__ == "__main__":
    unittest.main()
