from __future__ import annotations

import tempfile
import time
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from archive_index.file_management import build_dry_run_plan, list_profiles, save_ruleset
from archive_index.file_management_executor import get_execution, prepare_execution, start_execution
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
        Image.new("RGBA", (96, 64), (10, 120, 220, 128)).save(self.root / "alpha.png")
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
        finally:
            decoded.close()
        self.assertEqual(result_operation["source_sha256"], png_operation["source_sha256"])

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


if __name__ == "__main__":
    unittest.main()
