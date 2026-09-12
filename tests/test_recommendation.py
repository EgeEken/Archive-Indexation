from __future__ import annotations

import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import Image, ImageDraw

from archive_index.indexing.grouping import build_groups, extract_visual_features
from archive_index.indexing.recommendation import (
    Candidate,
    VisualFeature,
    _recommend_group,
    build_recommendations,
)
from archive_index.indexing.scanner import scan
from archive_index.workspace import Workspace


class RecommendationTests(unittest.TestCase):
    def test_singletons_use_conservative_quality_threshold(self) -> None:
        workspace = self._workspace({"good.jpg": "scene", "bad.jpg": "other"})
        self._set_times(workspace, {"good.jpg": "2026-09-03T12:00:00+03:00", "bad.jpg": "2026-09-03T12:01:00+03:00"})
        self._set_quality(workspace, {"good.jpg": .70, "bad.jpg": .69})
        build_groups(workspace)
        result = build_recommendations(workspace)
        self.assertEqual((result.total_assets, result.auto_recommended, result.singleton_recommendations), (2, 1, 1))
        with closing(workspace.connect()) as connection:
            rows = connection.execute(
                "SELECT la.id, ar.auto_recommended FROM logical_asset AS la JOIN asset_recommendation AS ar ON ar.logical_asset_id = la.id WHERE ar.run_id = (SELECT active_run_id FROM workspace_recommendation WHERE id = 1) ORDER BY la.id"
            ).fetchall()
        self.assertEqual(sum(row[1] for row in rows), 1)

    def test_recommendation_is_only_the_current_representative(self) -> None:
        base = VisualFeature("0" * 16, (0.0, 0.0), (0.0, 0.0, 0.0))
        distinct_one = VisualFeature("000000000000001f", (0.09, 0.0), (0.09, 0.0, 0.0))
        distinct_two = VisualFeature("000000000000003f", (0.18, 0.0), (0.18, 0.0, 0.0))
        candidates = [
            Candidate("group", "a", 0, .80, base, True),
            Candidate("group", "b", 1, .75, distinct_one),
            Candidate("group", "c", 2, .73, distinct_two),
        ]
        decisions = _recommend_group("group", candidates)
        self.assertEqual([row[1] for row in decisions if row[2]], ["a"])

        below_threshold = [Candidate("group", "a", 0, .59, base, True)]
        self.assertEqual(sum(row[2] for row in _recommend_group("group", below_threshold)), 0)

    def test_manual_decision_survives_recommendation_rebuild(self) -> None:
        workspace = self._workspace({"a.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00"})
        self._set_quality(workspace, {"a.jpg": .8})
        build_groups(workspace)
        first = build_recommendations(workspace)
        with closing(workspace.connect()) as connection:
            asset_id = connection.execute("SELECT id FROM logical_asset").fetchone()[0]
            grouping_run_id = connection.execute("SELECT active_run_id FROM workspace_grouping WHERE id = 1").fetchone()[0]
            source_grouping_run_id = connection.execute("SELECT source_grouping_run_id FROM recommendation_run WHERE id = (SELECT active_run_id FROM workspace_recommendation WHERE id = 1)").fetchone()[0]
        self.assertEqual(source_grouping_run_id, grouping_run_id)
        with workspace.transaction() as connection:
            connection.execute("UPDATE logical_asset SET selection_state = 'rejected' WHERE id = ?", (asset_id,))
        second = build_recommendations(workspace)
        with closing(workspace.connect()) as connection:
            decision = connection.execute("SELECT selection_state FROM logical_asset WHERE id = ?", (asset_id,)).fetchone()[0]
            version = connection.execute("SELECT version FROM recommendation_run WHERE id = (SELECT active_run_id FROM workspace_recommendation WHERE id = 1)").fetchone()[0]
        self.assertEqual((first.auto_recommended, second.auto_recommended, decision, version), (1, 1, "rejected", "3"))

    def test_group_rebuild_invalidates_old_recommendations_without_erasing_decision(self) -> None:
        workspace = self._workspace({"a.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00"})
        self._set_quality(workspace, {"a.jpg": .8})
        build_groups(workspace)
        build_recommendations(workspace)
        with closing(workspace.connect()) as connection:
            asset_id = connection.execute("SELECT id FROM logical_asset").fetchone()[0]
        with workspace.transaction() as connection:
            connection.execute("UPDATE logical_asset SET selection_state = 'selected' WHERE id = ?", (asset_id,))
        build_groups(workspace)
        with closing(workspace.connect()) as connection:
            active_recommendation = connection.execute("SELECT active_run_id FROM workspace_recommendation WHERE id = 1").fetchone()[0]
            decision = connection.execute("SELECT selection_state FROM logical_asset WHERE id = ?", (asset_id,)).fetchone()[0]
        self.assertIsNone(active_recommendation)
        self.assertEqual(decision, "selected")

    def test_cancelled_rebuild_preserves_previous_recommendation_run(self) -> None:
        workspace = self._workspace({"a.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00"})
        self._set_quality(workspace, {"a.jpg": .8})
        build_groups(workspace)
        build_recommendations(workspace)
        with closing(workspace.connect()) as connection:
            old_run = connection.execute("SELECT active_run_id FROM workspace_recommendation WHERE id = 1").fetchone()[0]
        event = Event()
        event.set()
        result = build_recommendations(workspace, cancel_event=event)
        with closing(workspace.connect()) as connection:
            active_run = connection.execute("SELECT active_run_id FROM workspace_recommendation WHERE id = 1").fetchone()[0]
        self.assertTrue(result.cancelled)
        self.assertEqual(old_run, active_run)

    def test_representative_flag_is_separate_from_recommendation_state(self) -> None:
        workspace = self._workspace({"a.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00"})
        self._set_quality(workspace, {"a.jpg": .8})
        build_groups(workspace)
        build_recommendations(workspace)
        with closing(workspace.connect()) as connection:
            recommendation = connection.execute("SELECT auto_recommended FROM asset_recommendation").fetchone()[0]
        with workspace.transaction() as connection:
            connection.execute("UPDATE strict_group_member SET is_representative = 0")
        with closing(workspace.connect()) as connection:
            still_recommended = connection.execute("SELECT auto_recommended FROM asset_recommendation").fetchone()[0]
        self.assertEqual((recommendation, still_recommended), (1, 1))

    def test_recommendation_rebuild_is_deterministic_and_does_not_decode_sources(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:01+03:00"})
        self._set_quality(workspace, {"a.jpg": .8, "b.jpg": .7})
        build_groups(workspace)
        first = build_recommendations(workspace)
        with closing(workspace.connect()) as connection:
            first_rows = connection.execute("SELECT logical_asset_id, auto_recommended, recommendation_rank, reason FROM asset_recommendation ORDER BY logical_asset_id").fetchall()
            grouping_run = connection.execute("SELECT active_run_id FROM workspace_grouping WHERE id = 1").fetchone()[0]
        with patch("archive_index.indexing.grouping.load_reduced_image", side_effect=AssertionError("source decoded")):
            second = build_recommendations(workspace)
        with closing(workspace.connect()) as connection:
            second_rows = connection.execute("SELECT logical_asset_id, auto_recommended, recommendation_rank, reason FROM asset_recommendation ORDER BY logical_asset_id").fetchall()
            self.assertEqual(grouping_run, connection.execute("SELECT active_run_id FROM workspace_grouping WHERE id = 1").fetchone()[0])
        self.assertEqual(first_rows, second_rows)
        self.assertEqual((first.auto_recommended, second.auto_recommended), (1, 1))

    def test_recommendation_version_rebuilds_only_recommendation_state(self) -> None:
        workspace = self._workspace({"a.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00"})
        self._set_quality(workspace, {"a.jpg": .8})
        build_groups(workspace)
        build_recommendations(workspace)
        with closing(workspace.connect()) as connection:
            grouping_run = connection.execute("SELECT active_run_id FROM workspace_grouping WHERE id = 1").fetchone()[0]
            quality_score = connection.execute("SELECT quality_score FROM physical_file").fetchone()[0]
        with patch("archive_index.indexing.recommendation.RECOMMENDATION_VERSION", "3"):
            build_recommendations(workspace)
        with closing(workspace.connect()) as connection:
            self.assertEqual(grouping_run, connection.execute("SELECT active_run_id FROM workspace_grouping WHERE id = 1").fetchone()[0])
            self.assertEqual(quality_score, connection.execute("SELECT quality_score FROM physical_file").fetchone()[0])
            self.assertEqual(connection.execute("SELECT version FROM recommendation_run WHERE id = (SELECT active_run_id FROM workspace_recommendation WHERE id = 1)").fetchone()[0], "3")

    def test_one_bad_cached_feature_does_not_abort_recommendations(self) -> None:
        workspace = self._workspace({"a.jpg": "scene", "b.jpg": "scene"})
        self._set_times(workspace, {"a.jpg": "2026-09-03T12:00:00+03:00", "b.jpg": "2026-09-03T12:00:01+03:00"})
        self._set_quality(workspace, {"a.jpg": .8, "b.jpg": .7})
        build_groups(workspace)
        with workspace.transaction() as connection:
            connection.execute(
                "UPDATE visual_feature SET luma_json = 'not-json' WHERE physical_file_id = (SELECT id FROM physical_file WHERE relative_path = 'b.jpg')"
            )
        result = build_recommendations(workspace)
        self.assertFalse(result.cancelled)
        with closing(workspace.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM asset_recommendation").fetchone()[0], 2)

    def _workspace(self, files: dict[str, str]) -> Workspace:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name) / "archive"
        root.mkdir()
        for name, kind in files.items():
            image = Image.new("RGB", (240, 160), "blue" if kind == "scene" else "green")
            if kind == "scene":
                ImageDraw.Draw(image).rectangle((35, 25, 170, 120), fill="red")
            image.save(root / name, format="JPEG")
        workspace = Workspace.create(root)
        workspace.set_quality_provider("lar-iqa")
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

    def _set_quality(self, workspace: Workspace, values: dict[str, float]) -> None:
        with workspace.transaction() as connection:
            for path, value in values.items():
                connection.execute("UPDATE physical_file SET quality_score = ?, quality_algorithm = 'test', quality_version = '1' WHERE relative_path = ?", (value, path))


if __name__ == "__main__":
    unittest.main()
