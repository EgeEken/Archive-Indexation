from __future__ import annotations

import json
import struct
import tempfile
import time
import unittest
import hashlib
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageOps, PngImagePlugin
from PIL import ImageCms
from PIL.TiffImagePlugin import IFDRational

from archive_index.api.browser import physical_rows
from archive_index.file_management import build_dry_run_plan, list_profiles, list_rulesets, save_ruleset, start_plan_analysis, plan_analysis_status
from archive_index.file_management_executor import get_execution, prepare_execution, start_execution
from archive_index.file_management_provenance import link_managed_derivatives
from archive_index.indexing.reconciliation import reconcile_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.jpegxl import decode, encode
from archive_index.media.jpegxl_tools import encode as encode_archival_jxl, metadata_contract, metadata_inventory, metadata_replacement_blocker, validate as validate_archival_jxl
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

    def test_replace_source_uses_metadata_preserving_runtime(self):
        exif = Image.Exif()
        exif[271] = "Archive Test Camera"
        exif[272] = "Phase 10C"
        exif[306] = "2026:09:27 12:34:56"
        exif[36867] = "2026:09:27 12:34:56"
        exif[34853] = {1: "N", 2: (IFDRational(41, 1), IFDRational(0, 1), IFDRational(0, 1)), 3: "E", 4: (IFDRational(29, 1), IFDRational(0, 1), IFDRational(0, 1))}
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB", colorTemp=5000)).tobytes()
        xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:Description test="phase10c"/></x:xmpmeta>'
        with Image.open(self.root / "photo.jpg") as image:
            image.save(self.root / "photo.jpg", exif=exif.tobytes(), icc_profile=icc, xmp=xmp, quality=95)
        scan(self.workspace)
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="replace",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        self.assertFalse(plan["operations"][0]["blockers"])
        self.assertEqual(plan["summary"]["compress"]["file_count"], 1)
        _, result = self._run({"source_disposition": "replace"})
        self.assertEqual(result["status"], "completed")
        self.assertFalse((self.root / "photo.jpg").exists())
        self.assertTrue((self.root / "photo.jxl").is_file())
        connection = self.workspace.connect()
        try:
            contract = connection.execute("SELECT metadata_contract_json FROM managed_derivative").fetchone()[0]
        finally:
            connection.close()
        self.assertIn('"exif": true', contract)
        self.assertIn('"gps": true', contract)
        self.assertIn('"icc": true', contract)
        self.assertIn('"xmp": true', contract)

    def test_archival_jxl_round_trips_metadata_contract(self):
        source = self.root / "metadata.jpg"
        exif = Image.Exif()
        exif[271] = "Archive Test Camera"
        exif[272] = "Phase 10C"
        exif[306] = "2026:09:27 12:34:56"
        exif[34853] = {1: "N", 2: (IFDRational(41, 1), IFDRational(0, 1), IFDRational(0, 1)), 3: "E", 4: (IFDRational(29, 1), IFDRational(0, 1), IFDRational(0, 1))}
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:Description test="phase10c"/></x:xmpmeta>'
        with Image.new("RGB", (800, 600), (90, 120, 160)) as image:
            image.save(source, exif=exif.tobytes(), icc_profile=icc, xmp=xmp, quality=95)
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        output = self.root / "metadata.jxl"
        with tempfile.TemporaryDirectory() as directory:
            result = encode_archival_jxl(source, output, profile, Path(directory))
            validation = validate_archival_jxl(source, output, result)
        self.assertEqual(validation["metadata_contract"]["exif"], True)
        self.assertEqual(validation["metadata_contract"]["gps"], True)
        self.assertEqual(validation["metadata_contract"]["icc"], True)
        self.assertEqual(validation["metadata_contract"]["xmp"], True)
        self.assertEqual(validation["metadata_contract"]["orientation"], 1)

    def test_archive_cleanup_retains_replace_source_semantics_when_runtime_is_ready(self):
        cleanup = next(item for item in list_rulesets(self.workspace) if item["name"] == "Archive cleanup")
        action = next(item["action"] for item in cleanup["rules"] if item["match"].get("formats") == ["jpeg", "png"] and item["match"].get("selection_state") == "undecided")
        self.assertEqual(action["source_disposition"], "replace")
        plan = build_dry_run_plan(self.workspace, cleanup["id"])
        operation = next(item for item in plan["operations"] if item["source_relative_path"] == "photo.jpg")
        self.assertFalse(operation["blockers"])
        self.assertEqual(plan["summary"]["compress"]["file_count"], 1)

    def test_repeated_plan_reuses_cached_jxl_source_analysis(self):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="cached-plan",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        with patch("archive_index.compression_analysis.metadata_inventory", wraps=metadata_inventory) as inventory:
            build_dry_run_plan(self.workspace, ruleset["id"])
            build_dry_run_plan(self.workspace, ruleset["id"])
        self.assertEqual(inventory.call_count, 1)

    def test_source_change_invalidates_only_its_cached_analysis(self):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="cache-invalidation",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        with patch("archive_index.compression_analysis.metadata_inventory", wraps=metadata_inventory) as inventory:
            build_dry_run_plan(self.workspace, ruleset["id"])
            (self.root / "photo.jpg").write_bytes((self.root / "photo.jpg").read_bytes() + b"changed")
            scan(self.workspace)
            build_dry_run_plan(self.workspace, ruleset["id"])
        self.assertEqual(inventory.call_count, 2)

    def test_runtime_contract_change_invalidates_cached_analysis(self):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="runtime-invalidation",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        with patch("archive_index.compression_analysis.runtime_fingerprint", side_effect=["runtime-a", "runtime-b"]), patch("archive_index.compression_analysis.metadata_inventory", wraps=metadata_inventory) as inventory:
            build_dry_run_plan(self.workspace, ruleset["id"])
            build_dry_run_plan(self.workspace, ruleset["id"])
        self.assertEqual(inventory.call_count, 2)

    def test_review_freezes_completed_analysis_without_rebuilding_plan(self):
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        ruleset = save_ruleset(
            self.workspace,
            name="review-session",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "keep", "destination_dir": "derivatives"}}],
        )
        session_id = start_plan_analysis(self.workspace, ruleset["id"])
        for _ in range(200):
            status = plan_analysis_status(self.workspace, session_id)
            if status["status"] == "complete":
                break
            time.sleep(0.01)
        self.assertEqual(status["status"], "complete")
        plan = status["result"]
        with patch("archive_index.file_management_executor.build_dry_run_plan", side_effect=AssertionError("review rebuilt the plan")):
            execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"], session_id)
        self.assertEqual(execution["status"], "draft")

    def test_streaming_metadata_inventory_stops_before_jpeg_scan_data(self):
        source = self.root / "large.jpg"
        original = (self.root / "photo.jpg").read_bytes()
        payload = b"UNKNOWN-APP2" + b"x" * 100
        segment = b"\xff\xe2" + (len(payload) + 2).to_bytes(2, "big") + payload
        source.write_bytes(original[:2] + segment + original[2:] + b"x" * (20 * 1024 * 1024))
        inventory = metadata_inventory(source)
        self.assertIn("APP2", inventory["unsupported"][0])
        self.assertLess(inventory["bytes_read"], source.stat().st_size // 100)

    def test_app2_classification_reports_mpf_and_unknown_signatures(self):
        source = self.root / "app2.jpg"
        original = (self.root / "photo.jpg").read_bytes()
        mpf = b"MPF\x00MM\x00*\x00\x00\x00\x08\x00\x01\xb0\x01\x00\x04\x00\x00\x00\x01\x00\x00\x00\x02\x00\x00\x00\x00\x00\x00\x00"
        segment = b"\xff\xe2" + (len(mpf) + 2).to_bytes(2, "big") + mpf
        source.write_bytes(original[:2] + segment + original[2:])
        reason = metadata_replacement_blocker(source)
        self.assertIn("MPF multi-picture", reason)
        unknown = self.root / "unknown-app2.jpg"
        payload = b"CAMERA-BLOB" + b"x" * 4
        segment = b"\xff\xe2" + (len(payload) + 2).to_bytes(2, "big") + payload
        unknown.write_bytes(original[:2] + segment + original[2:])
        reason = metadata_replacement_blocker(unknown)
        self.assertIn("Unsupported JPEG APP2 metadata", reason)
        self.assertIn("CAMERA-BLOB", reason)

    def test_streaming_png_inventory_skips_large_idat_payload(self):
        source = self.root / "large.png"
        def chunk(kind, payload):
            import zlib
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", b"x" * (20 * 1024 * 1024)) + chunk(b"IEND", b"")
        source.write_bytes(data)
        inventory = metadata_inventory(source)
        self.assertLess(inventory["bytes_read"], source.stat().st_size // 100)

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
        source = Image.effect_noise((800, 600), 100).convert("RGBA")
        pixels = source.load()
        for y in range(source.height):
            for x in range(source.width):
                pixels[x, y] = (*pixels[x, y][:3], (x * 255) // (source.width - 1) if y % 2 else (y * 255) // (source.height - 1))
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
            self.assertEqual(decoded.size, (800, 600))
            self.assertEqual(len(decoded.getbands()), 4)
            with Image.open(self.root / "alpha.png") as original:
                self.assertTrue(np.array_equal(np.asarray(original)[:, :, 3], np.asarray(decoded)[:, :, 3]))
        finally:
            decoded.close()
        self.assertEqual(result_operation["source_sha256"], png_operation["source_sha256"])

    def test_png_alpha_is_preserved_before_replace_source_deletion(self):
        source = Image.effect_noise((800, 600), 100).convert("RGBA")
        pixels = source.load()
        for y in range(source.height):
            for x in range(source.width):
                pixels[x, y] = (*pixels[x, y][:3], (x * 255) // (source.width - 1) if y % 2 else (y * 255) // (source.height - 1))
        expected_alpha = np.asarray(source)[:, :, 3].copy()
        source.save(self.root / "replace-alpha.png")
        source.close()
        scan(self.workspace)
        plan, result = self._run({"source_disposition": "replace"}, source_format="png")
        result_operation = next(item for item in result["operations"] if item["source_relative_path"] == "replace-alpha.png")
        self.assertEqual(result_operation["status"], "completed")
        self.assertFalse((self.root / "replace-alpha.png").exists())
        decoded = decode((self.root / "replace-alpha.jxl").read_bytes())
        try:
            self.assertEqual(decoded.getbands(), ("R", "G", "B", "A"))
            self.assertTrue(np.array_equal(expected_alpha, np.asarray(decoded)[:, :, 3]))
        finally:
            decoded.close()

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

    def test_replace_source_normalizes_orientation_in_standalone_metadata(self):
        oriented = Image.effect_noise((600, 800), 100).convert("RGB")
        exif = oriented.getexif()
        exif[274] = 6
        oriented.save(self.root / "replace-oriented.jpg", exif=exif, quality=95)
        oriented.close()
        scan(self.workspace)
        _, result = self._run({"source_disposition": "replace"})
        operation = next(item for item in result["operations"] if item["source_relative_path"] == "replace-oriented.jpg")
        self.assertEqual(operation["status"], "completed")
        self.assertFalse((self.root / "replace-oriented.jpg").exists())
        self.assertEqual(json.loads(operation["metadata_contract_json"])["output"]["metadata_contract"]["orientation"], 1)

    def test_jpeg_comments_block_source_replacement(self):
        source = self.root / "commented.jpg"
        original = (self.root / "photo.jpg").read_bytes()
        source.write_bytes(original[:2] + b"\xff\xfe\x00\x09comment\x00" + original[2:])
        self.assertIn("JPEG comment", metadata_replacement_blocker(source))

    def test_png_text_metadata_blocks_source_replacement(self):
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Description", "must not disappear")
        Image.new("RGB", (32, 24), (30, 40, 50)).save(self.root / "text.png", pnginfo=metadata)
        self.assertIn("PNG contains text metadata", metadata_replacement_blocker(self.root / "text.png"))

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

    def test_non_srgb_icc_round_trips_when_official_profile_is_available(self):
        profile_path = Path(r"C:\Windows\System32\spool\drivers\color\AdobeRGB1998.icc")
        if not profile_path.is_file():
            self.skipTest("Windows Adobe RGB profile is not installed")
        source = self.root / "adobe-rgb.jpg"
        with Image.effect_noise((320, 240), 100).convert("RGB") as image:
            image.save(source, icc_profile=profile_path.read_bytes(), quality=95)
        profile = next(profile for profile in list_profiles(self.workspace) if profile["name"] == "JXL Balanced")
        output = self.root / "adobe-rgb.jxl"
        with tempfile.TemporaryDirectory() as directory:
            result = encode_archival_jxl(source, output, profile, Path(directory))
            validation = validate_archival_jxl(source, output, result)
        self.assertTrue(validation["metadata_contract"]["icc"])

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

    def test_relinking_unchanged_output_is_idempotent(self):
        self._run({"source_disposition": "keep", "destination_dir": "derivatives"})
        connection = self.workspace.connect()
        try:
            before = connection.execute("SELECT physical_file_id, updated_at FROM managed_derivative").fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(before["physical_file_id"])
        link_managed_derivatives(self.workspace)
        connection = self.workspace.connect()
        try:
            after = connection.execute("SELECT physical_file_id, updated_at FROM managed_derivative").fetchone()
        finally:
            connection.close()
        self.assertEqual(dict(after), dict(before))

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

    def test_identical_historical_outputs_have_one_current_lineage(self):
        first_plan, first = self._run_profile("JXL Balanced", {"source_disposition": "keep", "destination_dir": "derivatives"})
        second_plan, second = self._run_profile("JXL Balanced", {"source_disposition": "keep", "destination_dir": "derivatives", "conflict_policy": "overwrite"})
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(first["operations"][0]["actual_output_sha256"], second["operations"][0]["actual_output_sha256"])
        connection = self.workspace.connect()
        try:
            rows = connection.execute("SELECT output_relative_path, output_sha256, physical_file_id FROM managed_derivative ORDER BY created_at, id").fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["physical_file_id"] is not None for row in rows), 1)
        self.assertEqual({row["output_relative_path"] for row in rows}, {"derivatives/photo.jxl"})
        self.assertEqual({row["output_sha256"] for row in rows}, {first["operations"][0]["actual_output_sha256"]})
        physical = self.workspace.connect()
        try:
            asset_id = physical.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'derivatives/photo.jxl'").fetchone()[0]
        finally:
            physical.close()
        self.assertEqual(len([row for row in physical_rows(self.workspace, asset_id) if row["relative_path"] == "derivatives/photo.jxl"]), 1)

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
