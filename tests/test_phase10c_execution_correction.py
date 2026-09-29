from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_index.file_management import BUILTIN_ARCHIVE_CLEANUP_ID, build_dry_run_plan
from archive_index.file_management import list_profiles, save_ruleset
from archive_index.file_management_executor import get_execution, prepare_execution, start_execution
from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.av1 import analyze_source
from archive_index.media.video_proxy import PROXY_CONTRACT_VERSION, _lock, _workers, proxy_path, proxy_status, request_proxy
from archive_index.workspace import Workspace


class Phase10CExecutionCorrectionTests(unittest.TestCase):
    def _execute_hevc_source(self, pixel_format: str):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        source = root / "camera.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24", "-t", "2",
                "-c:v", "libx265", "-crf", "10", "-pix_fmt", pixel_format,
                "-x265-params", "log-level=error", str(source),
            ],
            check=True,
        )
        workspace = Workspace.create(root)
        scan(workspace)
        profile = next(item for item in list_profiles(workspace) if item["name"] == "AV1 4K120 Very Fast")
        ruleset = save_ruleset(
            workspace,
            name=f"av1-{pixel_format}",
            rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        plan = build_dry_run_plan(workspace, ruleset["id"])
        self.assertFalse(plan["operations"][0]["blockers"])
        execution = prepare_execution(workspace, ruleset["id"], plan["plan_digest"])
        start_execution(workspace, execution["id"])
        for _ in range(3000):
            result = get_execution(workspace, execution["id"])
            if result["status"] not in {"running", "cancelling"}:
                break
            time.sleep(0.01)
        try:
            return directory, workspace, source, result
        except Exception:
            directory.cleanup()
            raise

    def test_hevc_10_bit_replaces_source_with_av1(self):
        directory, workspace, source, result = self._execute_hevc_source("yuv420p10le")
        try:
            self.assertEqual(result["status"], "completed")
            self.assertEqual(analyze_source(source, deep_timing=False)["codec"], "av1")
            self.assertEqual(analyze_source(source, deep_timing=False)["pix_fmt"], "yuv420p10le")
        finally:
            directory.cleanup()

    def test_hevc_422_is_converted_to_validated_av1_420(self):
        directory, workspace, source, result = self._execute_hevc_source("yuv422p10le")
        try:
            self.assertEqual(result["status"], "completed")
            self.assertEqual(analyze_source(source, deep_timing=False)["codec"], "av1")
            self.assertIn("yuv420", analyze_source(source, deep_timing=False)["pix_fmt"])
            report = result["operations"][0]["preservation_report"]
            self.assertTrue(any(item["kind"] == "chroma_subsampling" for item in report["changed"]))
        finally:
            directory.cleanup()

    def test_selected_archive_cleanup_persists_copy_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "photo.jpg").write_bytes(b"selected photo")
            workspace = Workspace.create(root)
            scan(workspace)
            with workspace.transaction() as connection:
                connection.execute("UPDATE logical_asset SET selection_state = 'selected'")

            plan = build_dry_run_plan(workspace, BUILTIN_ARCHIVE_CLEANUP_ID)
            operations = [item for item in plan["operations"] if item["source_relative_path"] == "photo.jpg"]
            self.assertEqual([item["operation"] for item in operations], ["copy", "compress"])
            self.assertEqual(operations[0]["rule_id"], f"{BUILTIN_ARCHIVE_CLEANUP_ID}-4")
            self.assertEqual(operations[1]["rule_id"], f"{BUILTIN_ARCHIVE_CLEANUP_ID}-5")
            self.assertEqual(operations[1]["dependency_key"], f"{BUILTIN_ARCHIVE_CLEANUP_ID}-4:{operations[0]['physical_file_id']}")
            execution = prepare_execution(workspace, BUILTIN_ARCHIVE_CLEANUP_ID, plan["plan_digest"])
            persisted = [item for item in execution["operations"] if item["source_relative_path"] == "photo.jpg"]
            self.assertEqual(persisted[1]["dependency_operation_id"], persisted[0]["id"])

    def test_failed_operation_records_stage_and_does_not_abort_later_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.jpg").write_bytes(b"a")
            (root / "b.jpg").write_bytes(b"b")
            workspace = Workspace.create(root)
            scan(workspace)
            ruleset = save_ruleset(
                workspace,
                name="copy-failure-continues",
                rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "destination_dir": "copies"}}],
            )
            plan = build_dry_run_plan(workspace, ruleset["id"])
            execution = prepare_execution(workspace, ruleset["id"], plan["plan_digest"])
            from archive_index import file_management_executor as executor

            original_copy = executor._copy

            def fail_first(workspace_arg, execution_id, operation, cancel):
                if operation["filename"] == "a.jpg":
                    raise RuntimeError("synthetic output validation failure")
                return original_copy(workspace_arg, execution_id, operation, cancel)

            with patch.object(executor, "_copy", side_effect=fail_first):
                start_execution(workspace, execution["id"])
                for _ in range(3000):
                    result = get_execution(workspace, execution["id"])
                    if result["status"] not in {"running", "cancelling"}:
                        break
                    time.sleep(0.01)
            self.assertEqual(result["status"], "completed_with_errors")
            failed = next(item for item in result["operations"] if item["filename"] == "a.jpg")
            completed = next(item for item in result["operations"] if item["filename"] == "b.jpg")
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failure_stage"], "starting")
            self.assertIn("synthetic output validation failure", failed["failure_detail"])
            self.assertEqual(completed["status"], "completed")
            self.assertTrue((root / "copies" / "b.jpg").is_file())

    def test_hevc_playback_proxy_is_reused_and_invalidated_by_source_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "camera.mp4"
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24", "-t", "1",
                    "-c:v", "libx265", "-pix_fmt", "yuv420p", "-x265-params", "log-level=error",
                    str(source),
                ],
                check=True,
            )
            workspace = Workspace.create(root)
            scan(workspace)
            connection = workspace.connect()
            try:
                row = connection.execute("SELECT id, codec FROM physical_file WHERE relative_path = 'camera.mp4'").fetchone()
            finally:
                connection.close()
            index_workspace(workspace, components=("metadata",))
            connection = workspace.connect()
            try:
                row = connection.execute("SELECT id, codec FROM physical_file WHERE relative_path = 'camera.mp4'").fetchone()
            finally:
                connection.close()
            self.assertEqual(row["codec"], "hevc")
            self.assertEqual(proxy_status(workspace, row["id"])["status"], "pending")
            self.assertEqual(request_proxy(workspace, row["id"])["status"], "running")
            result = None
            for _ in range(160):
                result = proxy_status(workspace, row["id"])
                if result["status"] in {"complete", "failed", "cancelled"}:
                    break
                time.sleep(0.05)
            self.assertEqual(result["status"], "complete", result)
            for _ in range(100):
                with _lock:
                    active_workers = [thread for thread, _ in _workers.values() if thread.is_alive()]
                if not active_workers:
                    break
                time.sleep(0.05)
            output = proxy_path(workspace, row["id"])
            self.assertTrue(output.is_file())
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,pix_fmt", "-of", "default=nw=1", str(output)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("codec_name=h264", probe)
            self.assertIn("pix_fmt=yuv420p", probe)
            source.write_bytes(source.read_bytes() + b"changed")
            scan(workspace)
            connection = workspace.connect()
            try:
                current = connection.execute("SELECT sha256, size_bytes, mtime_ns FROM physical_file WHERE id = ?", (row["id"],)).fetchone()
                proxy = connection.execute("SELECT source_sha256, source_size_bytes, source_mtime_ns FROM video_playback_proxy WHERE physical_file_id = ?", (row["id"],)).fetchone()
            finally:
                connection.close()
            self.assertNotEqual(current["sha256"], proxy["source_sha256"])
            self.assertEqual(proxy_status(workspace, row["id"])["status"], "pending")
            connection = workspace.connect()
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT contract_version FROM video_playback_proxy WHERE physical_file_id = ?",
                        (row["id"],),
                    ).fetchone()["contract_version"],
                    PROXY_CONTRACT_VERSION,
                )
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
