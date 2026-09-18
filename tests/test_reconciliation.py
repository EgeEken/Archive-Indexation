from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event

from PIL import Image

from archive_index.indexing.reconciliation import _capture_value, _pair_evidence, reconcile_workspace
from archive_index.indexing.scanner import scan
from archive_index.workspace import Workspace


class ReconciliationTests(unittest.TestCase):
    def test_mixed_local_and_absolute_capture_time_pairs_with_matching_camera(self):
        raw = {"id": "raw", "capture_time": "2026-09-13T11:47:01", "capture_time_kind": "exif_local_unknown"}
        rendered = {"id": "jpeg", "capture_time": "2026-09-13T11:47:01.453000+02:00", "capture_time_kind": "exif_offset"}
        files = {
            "raw": [{"metadata_json": json.dumps({"exif": {"Make": "Sony", "Model": "ILME-FX3A"}})}],
            "jpeg": [{"metadata_json": json.dumps({"exif": {"Make": "SONY", "Model": "ILME-FX3A"}})}],
        }
        evidence = _pair_evidence(raw, rendered, files)
        self.assertEqual(evidence["rule"], "same_stem+capture_time_mixed_semantics+camera")
        self.assertEqual(evidence["capture_semantics"], {"raw": "local", "rendered": "absolute"})
        self.assertAlmostEqual(evidence["capture_wall_clock_delta_seconds"], 0.453)

    def _workspace(self, root: Path) -> Workspace:
        root.mkdir(parents=True)
        return Workspace.create(root)

    def _image(self, path: Path, color: tuple[int, int, int]) -> None:
        Image.new("RGB", (80, 60), color).save(path, format="JPEG")

    def test_exact_duplicate_copy_merges_but_is_not_a_scanner_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            original = root / "original.jpg"
            self._workspace(root)
            self._image(original, (30, 40, 50))
            workspace = Workspace.open(root)
            scan(workspace)
            (root / "copy.jpg").write_bytes(original.read_bytes())
            scan(workspace)

            with closing(workspace.connect()) as connection:
                before = connection.execute("SELECT COUNT(*) FROM logical_asset").fetchone()[0]
            result = reconcile_workspace(workspace)
            with closing(workspace.connect()) as connection:
                after = connection.execute("SELECT COUNT(*) FROM logical_asset").fetchone()[0]
                relationship = connection.execute(
                    "SELECT relationship_type FROM physical_relationship"
                ).fetchone()[0]
            self.assertEqual((before, after), (2, 1))
            self.assertEqual(relationship, "exact_duplicate")
            self.assertEqual(result.exact_duplicate_files, 1)

    def test_exact_duplicate_relationship_survives_a_later_reconciliation_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            workspace = self._workspace(root)
            original = root / "original.jpg"
            self._image(original, (30, 40, 50))
            scan(workspace)
            (root / "copy.jpg").write_bytes(original.read_bytes())
            scan(workspace)
            first = reconcile_workspace(workspace)

            (root / "unrelated.jpg").write_bytes(b"not an image")
            scan(workspace)
            second = reconcile_workspace(workspace)

            with closing(workspace.connect()) as connection:
                relation_count = connection.execute(
                    "SELECT COUNT(*) FROM physical_relationship WHERE run_id = ? AND relationship_type = 'exact_duplicate'",
                    (second.run_id,),
                ).fetchone()[0]
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertEqual(relation_count, 1)

    def test_unique_raw_jpeg_stem_is_paired_and_roles_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            workspace = self._workspace(root)
            raw = root / "DSC0001.ARW"
            raw.write_bytes(b"raw placeholder")
            self._image(root / "DSC0001.JPG", (100, 110, 120))
            scan(workspace)

            result = reconcile_workspace(workspace)
            with closing(workspace.connect()) as connection:
                rows = connection.execute(
                    "SELECT extension, role, logical_asset_id FROM physical_file ORDER BY extension"
                ).fetchall()
                relation = connection.execute(
                    "SELECT relationship_type, algorithm, version FROM physical_relationship"
                ).fetchone()
            self.assertEqual(result.raw_jpeg_pairs, 1)
            self.assertEqual(len({row[2] for row in rows}), 1)
            self.assertEqual([(row[0], row[1]) for row in rows], [(".arw", "camera_raw"), (".jpg", "camera_jpeg")])
            self.assertEqual(tuple(relation), ("raw_jpeg", "same-stem-with-corroboration", "1"))

    def test_ambiguous_raw_jpeg_stem_is_left_separate_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            workspace = self._workspace(root)
            (root / "a").mkdir()
            (root / "b").mkdir()
            (root / "a" / "DSC0001.ARW").write_bytes(b"raw-a")
            (root / "b" / "DSC0001.ARW").write_bytes(b"raw-b")
            self._image(root / "DSC0001.JPG", (100, 110, 120))
            scan(workspace)

            result = reconcile_workspace(workspace)
            with closing(workspace.connect()) as connection:
                assets = connection.execute("SELECT COUNT(*) FROM logical_asset").fetchone()[0]
                conflicts = connection.execute("SELECT conflict_type FROM reconciliation_conflict").fetchall()
            self.assertEqual(assets, 3)
            self.assertEqual(result.raw_jpeg_pairs, 0)
            self.assertEqual([row[0] for row in conflicts], ["ambiguous_raw_jpeg"])

    def test_manual_decision_conflict_blocks_exact_merge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            workspace = self._workspace(root)
            original = root / "original.jpg"
            self._image(original, (30, 40, 50))
            (root / "copy.jpg").write_bytes(original.read_bytes())
            scan(workspace)
            with workspace.transaction() as connection:
                ids = [row[0] for row in connection.execute("SELECT id FROM logical_asset ORDER BY id")]
                connection.execute("UPDATE logical_asset SET selection_state = 'selected' WHERE id = ?", (ids[0],))
                connection.execute("UPDATE logical_asset SET selection_state = 'rejected' WHERE id = ?", (ids[1],))

            result = reconcile_workspace(workspace)
            with closing(workspace.connect()) as connection:
                assets = connection.execute("SELECT COUNT(*) FROM logical_asset").fetchone()[0]
            self.assertEqual(assets, 2)
            self.assertEqual(result.conflicts, 1)

    def test_cancelled_reconciliation_keeps_previous_group_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            workspace = self._workspace(root)
            original = root / "original.jpg"
            self._image(original, (30, 40, 50))
            scan(workspace)
            first = reconcile_workspace(workspace)
            (root / "copy.jpg").write_bytes(original.read_bytes())
            scan(workspace)
            event = Event()
            event.set()

            result = reconcile_workspace(workspace, cancel_event=event)
            with closing(workspace.connect()) as connection:
                active = connection.execute(
                    "SELECT active_run_id FROM workspace_reconciliation WHERE id = 1"
                ).fetchone()[0]
                status = connection.execute(
                    "SELECT status FROM job WHERE id = ?", (result.job_id,)
                ).fetchone()[0]
            self.assertTrue(result.cancelled)
            self.assertEqual(active, first.run_id)
            self.assertEqual(status, "cancelled")

    def test_reconciliation_is_idempotent_and_local_time_is_not_utc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            workspace = self._workspace(root)
            self._image(root / "photo.jpg", (30, 40, 50))
            scan(workspace)

            first = reconcile_workspace(workspace)
            second = reconcile_workspace(workspace)
            self.assertEqual(first.run_id, second.run_id)
            value, semantics = _capture_value("2026-09-13T12:00:00", "exif_local_unknown")
            expected = 739872 * 86400 + 12 * 3600
        self.assertEqual((value, semantics), (expected, "local"))


if __name__ == "__main__":
    unittest.main()
