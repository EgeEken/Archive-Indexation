from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import Image, ImageDraw

from archive_index.indexing import grouping
from archive_index.indexing.grouping import (
    AssetRecord,
    FeatureRecord,
    build_groups,
    extract_visual_features,
    grouping_diagnostics,
)
from archive_index.indexing.scanner import scan
from archive_index.workspace import Workspace


class GroupingTests(unittest.TestCase):
    def test_near_identical_images_within_window_group(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:05+03:00"})
        result = build_groups(workspace)
        self.assertEqual((result.multi_image_groups, result.group_sizes), (1, (2,)))

    def test_dissimilar_images_one_second_apart_do_not_group(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "other"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:01+03:00"})
        self.assertEqual(build_groups(workspace).multi_image_groups, 0)

    def test_dissimilar_images_one_tenth_second_apart_do_not_group(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "other"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:00.100000+03:00"})
        result = build_groups(workspace)
        self.assertEqual((result.multi_image_groups, result.tier_a_pair_relations, result.tier_a_veto_rejections), (0, 1, 1))

    def test_high_speed_burst_chain_allows_non_complete_linkage(self) -> None:
        workspace = Workspace.create(Path(tempfile.mkdtemp()) / "archive")
        def record(asset_id: str, seconds: float, dhash: str, value: float) -> AssetRecord:
            feature = FeatureRecord(asset_id, asset_id, dhash, (value,) * 4, (value,) * 24)
            return AssetRecord(asset_id, None, "exif_offset", seconds, "absolute", feature, None)
        records = [
            record("A", 0.0, "0000000000000000", 0.0),
            record("B", 0.1, "00000000000000ff", 0.1),
            record("C", 0.2, "000000000000ffff", 0.2),
        ]
        with patch.object(grouping, "_asset_records", return_value=records), patch.object(grouping, "_activate_groups"):
            result = build_groups(workspace)
        self.assertEqual((result.group_sizes, result.tier_a_pair_relations, result.tier_a_burst_chains), ((3,), 2, 1))
        self.assertFalse(grouping._visual_match(records[0].feature, records[2].feature))

    def test_identical_images_outside_temporal_window_do_not_group(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:11+03:00"})
        self.assertEqual(build_groups(workspace).multi_image_groups, 0)

    def test_missing_capture_time_is_singleton(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00"})
        result = build_groups(workspace)
        self.assertEqual((result.total_groups, result.no_timestamp), (2, 1))

    def test_unknown_timezone_is_not_compared_as_utc(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene", "c.jpg": "scene"})
        with workspace.transaction() as connection:
            for name, value, kind in (
                ("a.jpg", "2026-09-03T12:00:00", "exif_local_unknown"),
                ("b.jpg", "2026-09-03T12:00:05", "exif_local_unknown"),
                ("c.jpg", "2026-09-03T12:00:05+03:00", "exif_offset"),
            ):
                connection.execute(
                    "UPDATE logical_asset SET capture_time = ?, capture_time_kind = ? WHERE id = (SELECT logical_asset_id FROM physical_file WHERE relative_path = ?)",
                    (value, kind, name),
                )
        result = build_groups(workspace)
        self.assertEqual(result.multi_image_groups, 1)
        self.assertEqual(result.group_sizes, (2, 1))

    def test_complete_linkage_does_not_chain(self) -> None:
        records = [
            self._record("A", 0), self._record("B", 1), self._record("C", 2)
        ]
        pairs = {frozenset(("A", "B")), frozenset(("B", "C"))}

        def match(left, right, similarities=None):
            if similarities is not None:
                similarities.append(.9 if frozenset((left.physical_file_id, right.physical_file_id)) in pairs else .1)
            return frozenset((left.physical_file_id, right.physical_file_id)) in pairs

        groups = []
        with patch.object(grouping, "_visual_match", side_effect=match):
            for record in records:
                grouping._assign_record(groups, record)
        self.assertEqual([[member.asset_id for member in group] for group in groups], [["A", "B"], ["C"]])

    def test_group_assignment_is_deterministic(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene", "c.jpg": "other"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:04+03:00", "c.jpg": "2026-09-03T12:00:05+03:00"})
        build_groups(workspace)
        first = self._members(workspace)
        build_groups(workspace)
        self.assertEqual(first, self._members(workspace))

    def test_candidate_diagnostics_include_all_visual_gates(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "other"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:00.100000+03:00"})
        report = grouping_diagnostics(workspace)
        self.assertEqual(report["candidate_count"], 1)
        self.assertEqual(report["accepted_visual_pair_count"], 0)
        self.assertEqual(report["rejected_temporal_candidate_count"], 1)
        self.assertTrue({"dhash_pass", "luma_pass", "color_pass", "visual_pass"} <= report["borderline_rejected"][0].keys())

    def test_representative_uses_quality_but_quality_does_not_change_membership(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:05+03:00"})
        with workspace.transaction() as connection:
            connection.execute("UPDATE physical_file SET quality_score = .2 WHERE relative_path = 'a.jpg'")
            connection.execute("UPDATE physical_file SET quality_score = .9 WHERE relative_path = 'b.jpg'")
        build_groups(workspace)
        representative = self._active_representative(workspace)
        members = self._members(workspace)
        with workspace.transaction() as connection:
            connection.execute("UPDATE physical_file SET quality_score = .1 WHERE relative_path = 'b.jpg'")
            connection.execute("UPDATE physical_file SET quality_score = .95 WHERE relative_path = 'a.jpg'")
        build_groups(workspace)
        self.assertNotEqual(representative, self._active_representative(workspace))
        self.assertEqual(members, self._members(workspace))

    def test_source_change_invalidates_only_that_feature(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene"})
        with closing(workspace.connect()) as connection:
            before = connection.execute("SELECT id FROM physical_file WHERE relative_path = 'b.jpg'").fetchone()[0]
        (workspace.root / "a.jpg").write_bytes((workspace.root / "b.jpg").read_bytes() + b"change")
        scan(workspace)
        with closing(workspace.connect()) as connection:
            changed = connection.execute("SELECT id FROM physical_file WHERE relative_path = 'a.jpg'").fetchone()[0]
            self.assertIsNone(connection.execute("SELECT 1 FROM visual_feature WHERE physical_file_id = ?", (changed,)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT 1 FROM visual_feature WHERE physical_file_id = ?", (before,)).fetchone())
            self.assertEqual(connection.execute("SELECT status FROM component_state WHERE physical_file_id = ? AND component = 'group_feature'", (changed,)).fetchone()[0], "pending")

    def test_cached_feature_extraction_does_not_decode_again(self) -> None:
        workspace = self._workspace({"a.jpg": "scene"})
        with patch.object(grouping, "load_reduced_image", side_effect=AssertionError("decoded again")):
            result = extract_visual_features(workspace)
        self.assertEqual((result.errors, result.skipped), (0, 1))

    def test_cancelled_rebuild_preserves_previous_run(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:05+03:00"})
        build_groups(workspace)
        old_run = self._active_run(workspace)
        event = Event(); event.set()
        result = build_groups(workspace, cancel_event=event)
        self.assertTrue(result.cancelled)
        self.assertEqual(old_run, self._active_run(workspace))

    def test_failed_rebuild_preserves_previous_run(self) -> None:
        workspace = self._workspace({"a.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00"})
        build_groups(workspace)
        old_run = self._active_run(workspace)
        with patch.object(grouping, "_activate_groups", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                build_groups(workspace)
        self.assertEqual(old_run, self._active_run(workspace))

    def test_successful_rebuild_replaces_old_run(self) -> None:
        workspace = self._workspace({"a.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00"})
        build_groups(workspace)
        old_run = self._active_run(workspace)
        build_groups(workspace)
        self.assertNotEqual(old_run, self._active_run(workspace))
        with closing(workspace.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM grouping_run").fetchone()[0], 1)

    def test_videos_are_excluded(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "clip.mp4": "video"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00"})
        result = build_groups(workspace)
        self.assertEqual((result.eligible_images, result.total_groups), (1, 1))

    def test_feature_decoder_gap_is_failure_isolated_and_singleton(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "camera.arw": "video"})
        result = build_groups(workspace)
        self.assertEqual((result.feature_failures, result.total_groups), (1, 2))

    def _workspace(self, files: dict[str, str]) -> Workspace:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name) / "archive"
        root.mkdir()
        for name, kind in files.items():
            path = root / name
            if kind == "video":
                path.write_bytes(b"video")
            else:
                image = Image.new("RGB", (240, 160), "blue" if kind == "scene" else "green")
                if kind == "scene":
                    ImageDraw.Draw(image).rectangle((35, 25, 170, 120), fill="red")
                image.save(path, format="JPEG")
        workspace = Workspace.create(root)
        scan(workspace)
        extract_visual_features(workspace)
        return workspace

    def _set_times(self, workspace: Workspace, values: dict[str, str]) -> None:
        with workspace.transaction() as connection:
            for path, value in values.items():
                connection.execute(
                    "UPDATE logical_asset SET capture_time = ?, capture_time_kind = 'exif_offset' WHERE id = (SELECT logical_asset_id FROM physical_file WHERE relative_path = ?)",
                    (value, path),
                )

    def _active_run(self, workspace: Workspace):
        with closing(workspace.connect()) as connection:
            return connection.execute("SELECT active_run_id FROM workspace_grouping WHERE id = 1").fetchone()[0]

    def _members(self, workspace: Workspace):
        with closing(workspace.connect()) as connection:
            rows = connection.execute("SELECT group_id, logical_asset_id FROM strict_group_member WHERE run_id = (SELECT active_run_id FROM workspace_grouping WHERE id = 1) ORDER BY group_id, member_order").fetchall()
        return [(row[0], row[1]) for row in rows]

    def _active_representative(self, workspace: Workspace):
        with closing(workspace.connect()) as connection:
            return connection.execute("SELECT representative_logical_asset_id FROM strict_group WHERE run_id = (SELECT active_run_id FROM workspace_grouping WHERE id = 1)").fetchone()[0]

    @staticmethod
    def _record(identifier: str, seconds: float) -> AssetRecord:
        feature = FeatureRecord(identifier, identifier, "0" * 16, (0.0,) * 4, (0.0,) * 24)
        return AssetRecord(identifier, None, "exif_offset", seconds, "absolute", feature, None)


if __name__ == "__main__":
    unittest.main()
