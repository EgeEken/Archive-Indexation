from __future__ import annotations

import errno
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from archive_index.file_management import build_dry_run_plan, save_ruleset
from archive_index.file_management_executor import (
    ExecutionConflict,
    OperationFailure,
    discard_execution,
    get_execution,
    prepare_execution,
    retry_failed,
    resume_execution,
    start_execution,
)
from archive_index.indexing.scanner import scan
from archive_index.workspace import Workspace


class Phase10BExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "photo.jpg").write_bytes(b"photo contents")
        self.workspace = Workspace.create(self.root)
        scan(self.workspace)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _prepare(self, operation: str, **action):
        ruleset = save_ruleset(
            self.workspace,
            name=f"{operation}-{time.time_ns()}",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": operation, **action}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        return plan, execution

    def _run(self, execution_id: str):
        start_execution(self.workspace, execution_id)
        for _ in range(300):
            execution = get_execution(self.workspace, execution_id)
            if execution["status"] not in {"running", "cancelling"}:
                return execution
            time.sleep(0.01)
        self.fail("execution did not finish")

    def test_copy_creates_nested_target_without_touching_source(self):
        plan, execution = self._prepare("copy", destination_dir="nested/copies")
        result = self._run(execution["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.root / "photo.jpg").read_bytes(), b"photo contents")
        self.assertEqual((self.root / "nested" / "copies" / "photo.jpg").read_bytes(), b"photo contents")
        self.assertEqual(result["actual_bytes_written"], len(b"photo contents"))
        self.assertEqual(result["operations"][0]["actual_output_sha256"], plan["operations"][0]["source_sha256"])
        self.assertFalse(list(self.root.rglob("*.archive-index-*.tmp")))

    def test_copy_refuses_destination_that_appears_after_confirmation(self):
        _, execution = self._prepare("copy", destination_dir="copies")
        (self.root / "copies").mkdir()
        (self.root / "copies" / "photo.jpg").write_bytes(b"unrelated")
        result = self._run(execution["id"])
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["operations"][0]["status"], "failed")
        self.assertEqual((self.root / "copies" / "photo.jpg").read_bytes(), b"unrelated")

    def test_skip_policy_is_persisted_without_writing(self):
        (self.root / "copies").mkdir()
        (self.root / "copies" / "photo.jpg").write_bytes(b"unrelated")
        _, execution = self._prepare("copy", destination_dir="copies", conflict_policy="skip")
        self.assertEqual(execution["operations"][0]["status"], "skipped")
        result = self._run(execution["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["counts"]["skipped"], 1)
        self.assertEqual((self.root / "copies" / "photo.jpg").read_bytes(), b"unrelated")

    def test_overwrite_requires_reviewed_target_and_rejects_changed_target(self):
        (self.root / "copies").mkdir()
        target = self.root / "copies" / "photo.jpg"
        target.write_bytes(b"old target")
        _, execution = self._prepare("copy", destination_dir="copies", conflict_policy="overwrite")
        self.assertIsNotNone(execution["operations"][0]["target_expected_sha256"])
        target.write_bytes(b"changed after review")
        result = self._run(execution["id"])
        self.assertEqual(result["operations"][0]["status"], "failed")
        self.assertIn("Destination changed since the plan was confirmed.", result["operations"][0]["error_message"])
        self.assertEqual(target.read_bytes(), b"changed after review")

    def test_overwrite_replaces_exact_reviewed_target(self):
        (self.root / "copies").mkdir()
        target = self.root / "copies" / "photo.jpg"
        target.write_bytes(b"old target")
        _, execution = self._prepare("copy", destination_dir="copies", conflict_policy="overwrite")
        result = self._run(execution["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(target.read_bytes(), b"photo contents")

    def test_same_digest_reuses_draft_and_changed_rules_supersede_it(self):
        ruleset = save_ruleset(self.workspace, name=f"draft-{time.time_ns()}", rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "destination_dir": "copies"}}])
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        first = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        second = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        self.assertEqual(first["id"], second["id"])
        changed = save_ruleset(self.workspace, name=ruleset["name"], ruleset_id=ruleset["id"], rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "destination_dir": "other"}}])
        changed_plan = build_dry_run_plan(self.workspace, changed["id"])
        replacement = prepare_execution(self.workspace, changed["id"], changed_plan["plan_digest"])
        self.assertNotEqual(replacement["id"], first["id"])
        self.assertEqual(get_execution(self.workspace, first["id"])["status"], "superseded")
        discard_execution(self.workspace, replacement["id"])
        self.assertEqual(get_execution(self.workspace, replacement["id"])["status"], "abandoned")

    def test_executor_revalidates_windows_target_names(self):
        _, execution = self._prepare("copy", destination_dir="copies")
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE file_management_execution_operation SET target_relative_path = ? WHERE execution_id = ?",
                ("CON.jpg", execution["id"]),
            )
        result = self._run(execution["id"])
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertIn("reserved Windows device name", result["operations"][0]["error_message"])
        self.assertFalse((self.root / "CON.jpg").exists())

    def test_stale_source_is_not_copied_or_deleted(self):
        _, copy_execution = self._prepare("copy", destination_dir="copies")
        (self.root / "photo.jpg").write_bytes(b"changed")
        result = self._run(copy_execution["id"])
        self.assertIn("Source changed since the plan was confirmed.", result["operations"][0]["error_message"])
        self.assertFalse((self.root / "copies" / "photo.jpg").exists())

        _, delete_execution = self._prepare("delete")
        (self.root / "photo.jpg").write_bytes(b"changed again")
        result = self._run(delete_execution["id"])
        self.assertTrue((self.root / "photo.jpg").exists())
        self.assertEqual(result["operations"][0]["status"], "failed")

    def test_move_and_delete_are_exact_and_sequential(self):
        _, move_execution = self._prepare("move", destination_dir="moved")
        result = self._run(move_execution["id"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse((self.root / "photo.jpg").exists())
        self.assertEqual((self.root / "moved" / "photo.jpg").read_bytes(), b"photo contents")

        ruleset = save_ruleset(
            self.workspace,
            name=f"delete-{time.time_ns()}",
            rules=[{"match": {"folder_prefix": "moved", "format": "jpeg"}, "action": {"operation": "delete"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        result = self._run(execution["id"])
        self.assertEqual(result["status"], "completed")
        self.assertFalse((self.root / "moved" / "photo.jpg").exists())

    def test_move_refresh_preserves_asset_identity_and_manual_decision(self):
        with self.workspace.transaction() as connection:
            logical = connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'photo.jpg'").fetchone()[0]
            connection.execute("UPDATE logical_asset SET selection_state = 'selected' WHERE id = ?", (logical,))
        _, execution = self._prepare("move", destination_dir="moved")
        result = self._run(execution["id"])
        self.assertEqual(result["status"], "completed")
        connection = self.workspace.connect()
        try:
            row = connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'moved/photo.jpg' AND is_online = 1").fetchone()
            decision = connection.execute("SELECT selection_state FROM logical_asset WHERE id = ?", (logical,)).fetchone()[0]
        finally:
            connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], logical)
        self.assertEqual(decision, "selected")

    def test_cross_device_move_recovers_when_source_removal_fails(self):
        _, execution = self._prepare("move", destination_dir="moved")
        from archive_index import file_management_executor as executor

        original_finalize = executor._atomic_no_replace

        def force_cross_device(source, target, *, remove_source):
            if not remove_source:
                return original_finalize(source, target, remove_source=remove_source)
            raise OSError(errno.EXDEV, "cross-device link")

        with patch.object(executor, "_atomic_no_replace", side_effect=force_cross_device), patch.object(
            executor, "_unlink_source", side_effect=OperationFailure("Move target completed but source removal failed.")
        ):
            result = self._run(execution["id"])
        self.assertEqual(result["status"], "completed_with_errors")
        self.assertEqual(result["operations"][0]["stage"], "deleting")
        self.assertTrue((self.root / "photo.jpg").exists())
        self.assertTrue((self.root / "moved" / "photo.jpg").exists())
        retry_failed(self.workspace, execution["id"])
        for _ in range(300):
            result = get_execution(self.workspace, execution["id"])
            if result["status"] not in {"running", "cancelling"}:
                break
            time.sleep(0.01)
        self.assertEqual(result["status"], "completed")
        self.assertFalse((self.root / "photo.jpg").exists())
        self.assertTrue((self.root / "moved" / "photo.jpg").exists())

    def test_compression_rows_are_persisted_as_excluded(self):
        profile_ruleset = save_ruleset(
            self.workspace,
            name=f"compress-{time.time_ns()}",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": "missing"}}],
        )
        plan = build_dry_run_plan(self.workspace, profile_ruleset["id"])
        execution = prepare_execution(self.workspace, profile_ruleset["id"], plan["plan_digest"])
        self.assertEqual(execution["operations"][0]["status"], "excluded")
        self.assertIn("Phase 10C", execution["operations"][0]["error_message"])

    def test_plan_digest_changes_when_source_changes(self):
        ruleset = save_ruleset(
            self.workspace,
            name=f"digest-{time.time_ns()}",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "destination_dir": "copies"}}],
        )
        first = build_dry_run_plan(self.workspace, ruleset["id"])
        (self.root / "photo.jpg").write_bytes(b"different")
        scan(self.workspace)
        second = build_dry_run_plan(self.workspace, ruleset["id"])
        self.assertNotEqual(first["plan_digest"], second["plan_digest"])

    def test_prepare_rejects_stale_analyzed_digest(self):
        ruleset = save_ruleset(
            self.workspace,
            name=f"stale-{time.time_ns()}",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "destination_dir": "copies"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        (self.root / "photo.jpg").write_bytes(b"changed")
        scan(self.workspace)
        with self.assertRaisesRegex(ExecutionConflict, "plan changed"):
            prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])

    def test_recovery_marks_orphaned_run_interrupted(self):
        _, execution = self._prepare("copy", destination_dir="copies")
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE file_management_execution SET status = 'running' WHERE id = ?", (execution["id"],))
            connection.execute("UPDATE file_management_execution_operation SET status = 'running', stage = 'copying' WHERE execution_id = ?", (execution["id"],))
        recovered = get_execution(self.workspace, execution["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertEqual(recovered["operations"][0]["status"], "interrupted")

    def test_cancel_removes_owned_temp_and_resume_restarts_from_zero(self):
        (self.root / "photo.jpg").write_bytes(b"x" * (3 * 1024 * 1024))
        scan(self.workspace)
        _, execution = self._prepare("copy", destination_dir="copies")
        original = __import__("archive_index.file_management_executor", fromlist=["_update_progress"])._update_progress
        cancelled = {"done": False}

        def stop_after_first_chunk(workspace, operation_id, value):
            original(workspace, operation_id, value)
            if not cancelled["done"]:
                cancelled["done"] = True
                from archive_index.file_management_executor import cancel_execution
                cancel_execution(workspace, execution["id"])

        with patch("archive_index.file_management_executor._update_progress", side_effect=stop_after_first_chunk), patch("archive_index.file_management_executor.CHUNK_SIZE", 1024 * 1024):
            result = self._run(execution["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(list((self.root / "copies").glob("*.tmp")) if (self.root / "copies").exists() else [])
        result = None
        from archive_index.file_management_executor import resume_execution
        resume_execution(self.workspace, execution["id"])
        for _ in range(300):
            result = get_execution(self.workspace, execution["id"])
            if result["status"] not in {"running", "cancelling"}:
                break
            time.sleep(0.01)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((self.root / "copies" / "photo.jpg").stat().st_size, 3 * 1024 * 1024)

    def test_retry_failed_revalidates_and_completes_only_failed_operation(self):
        _, execution = self._prepare("copy", destination_dir="copies")
        (self.root / "copies").mkdir()
        (self.root / "copies" / "photo.jpg").write_bytes(b"conflict")
        result = self._run(execution["id"])
        self.assertEqual(result["status"], "completed_with_errors")
        (self.root / "copies" / "photo.jpg").unlink()
        retry_failed(self.workspace, execution["id"])
        for _ in range(300):
            result = get_execution(self.workspace, execution["id"])
            if result["status"] not in {"running", "cancelling"}:
                break
            time.sleep(0.01)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["operations"][0]["attempt_count"], 2)


if __name__ == "__main__":
    unittest.main()
