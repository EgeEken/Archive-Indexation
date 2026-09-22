from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from archive_index.api.server import _browser_filtered_assets
from archive_index.api.visualizations import visualization_capabilities, visualization_data, wall_clock_coordinate, wall_clock_coordinate_us
from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.scanner import scan
from archive_index.media.quality_provider import OffQualityProvider
from archive_index.workspace import Workspace


class VisualizationDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name in ("gps.jpg", "fallback.jpg", "plain.jpg"):
            Image.new("RGB", (80, 60), "red").save(root / name)
        (root / "fallback.arw").write_bytes(b"raw")
        self.workspace = Workspace.create(root)
        configuration = self.workspace.configuration()
        configuration.update({"semantic_search_enabled": False, "quality_enabled": False, "rendered_quality_provider": "off"})
        self.workspace.apply_configuration(configuration)
        scan(self.workspace)
        index_workspace(self.workspace, components=("metadata", "thumbnail"), quality_provider=OffQualityProvider())
        with self.workspace.transaction() as connection:
            gps_id = connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'gps.jpg'").fetchone()[0]
            fallback_id = connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'fallback.jpg'").fetchone()[0]
            raw_id = connection.execute("SELECT id FROM physical_file WHERE relative_path = 'fallback.arw'").fetchone()[0]
            connection.execute(
                "UPDATE physical_file SET metadata_json = ? WHERE relative_path = 'gps.jpg'",
                (json.dumps({"gps": {"latitude": 48.792146, "longitude": 2.369163}}),),
            )
            connection.execute(
                "UPDATE physical_file SET metadata_json = ? WHERE relative_path = 'fallback.jpg'",
                (json.dumps({"gps": {"latitude": 91, "longitude": 2}}),),
            )
            connection.execute(
                "UPDATE physical_file SET logical_asset_id = ?, metadata_json = ? WHERE id = ?",
                (fallback_id, json.dumps({"gps": {"latitude": 41.0082, "longitude": 28.9784}}), raw_id),
            )
            connection.execute(
                "UPDATE logical_asset SET capture_time = '2026-09-19T12:34:56', capture_time_kind = 'exif_local_unknown' WHERE id = ?",
                (gps_id,),
            )
            connection.execute(
                "UPDATE logical_asset SET capture_time = '2026-09-20T12:34:56+03:00', capture_time_kind = 'exif_offset' WHERE id = ?",
                (fallback_id,),
            )
        self.gps_id = gps_id
        self.fallback_id = fallback_id
        self.handle = "test"

    def _filter(self, query):
        return _browser_filtered_assets(self.workspace, {key: [str(value)] for key, value in query.items()}, self.handle)

    def test_geo_prefers_valid_active_representation_and_filters(self):
        data = visualization_data(self.workspace, {}, self.handle, filter_assets=_browser_filtered_assets, kind="geo")
        self.assertEqual(data["filtered_asset_count"], 3)
        self.assertEqual(data["represented_point_count"], 2)
        points = {point["asset_id"]: point for point in data["points"]}
        self.assertEqual(points[self.gps_id]["latitude"], 48.792146)
        self.assertEqual(points[self.fallback_id]["latitude"], 41.0082)
        self.assertEqual(set(points[self.gps_id]), {"asset_id", "latitude", "longitude", "quality_score"})
        filtered = visualization_data(
            self.workspace,
            {"folders": [json.dumps([""])], "media_type": ["image"]},
            self.handle,
            filter_assets=_browser_filtered_assets,
            kind="geo",
        )
        self.assertEqual(filtered["filtered_asset_count"], 3)

    def test_geo_rejects_invalid_coordinates_and_timeline_omits_missing(self):
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE physical_file SET metadata_json = ? WHERE relative_path = 'fallback.arw'", (json.dumps({"gps": {"latitude": float('nan'), "longitude": 3}}),))
            connection.execute("UPDATE logical_asset SET capture_time = NULL WHERE id = (SELECT logical_asset_id FROM physical_file WHERE relative_path = 'plain.jpg')")
        geo = visualization_data(self.workspace, {}, self.handle, filter_assets=_browser_filtered_assets, kind="geo")
        timeline = visualization_data(self.workspace, {}, self.handle, filter_assets=_browser_filtered_assets, kind="timeline")
        self.assertEqual(geo["represented_point_count"], 1)
        self.assertEqual(timeline["represented_point_count"], 2)
        self.assertEqual(timeline["points"][0]["capture_time"], "2026-09-19T12:34:56")
        self.assertEqual(
            set(timeline["points"][0]),
            {"asset_id", "time", "time_us", "time_precision_us", "time_kind", "capture_time", "capture_time_kind", "file_created_time", "media_type", "quality_score"},
        )

    def test_wall_clock_coordinate_ignores_timezone_suffix(self):
        self.assertEqual(
            wall_clock_coordinate("2026-09-20T12:34:56", "exif_local_unknown"),
            wall_clock_coordinate("2026-09-20T12:34:56+03:00", "exif_offset"),
        )
        self.assertIsNone(wall_clock_coordinate("not-a-date"))

    def test_file_created_timeline_uses_preferred_persisted_representation(self):
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE physical_file SET file_created_time = ? WHERE relative_path = 'gps.jpg'",
                ("2024-01-02T03:04:05+03:00",),
            )
            connection.execute(
                "UPDATE physical_file SET file_created_time = ? WHERE relative_path = 'fallback.jpg'",
                ("2024-01-03T04:05:06+03:00",),
            )
            connection.execute(
                "UPDATE physical_file SET file_created_time = ? WHERE relative_path = 'fallback.arw'",
                ("2024-01-04T05:06:07+03:00",),
            )
            connection.execute(
                "UPDATE physical_file SET file_created_time = NULL WHERE relative_path = 'plain.jpg'",
            )
        data = visualization_data(
            self.workspace,
            {"time_mode": ["file_created"]},
            self.handle,
            filter_assets=_browser_filtered_assets,
            kind="timeline",
        )
        self.assertEqual(data["time_mode"], "file_created")
        self.assertEqual(data["represented_point_count"], 2)
        points = {point["asset_id"]: point for point in data["points"]}
        self.assertEqual(points[self.gps_id]["time_kind"], "file_created")
        self.assertEqual(points[self.fallback_id]["file_created_time"], "2024-01-03T04:05:06+03:00")
        self.assertEqual(points[self.fallback_id]["time"], wall_clock_coordinate("2024-01-03T04:05:06+03:00"))
        self.assertEqual(set(points[self.gps_id]), {"asset_id", "time", "time_us", "time_precision_us", "time_kind", "capture_time", "capture_time_kind", "file_created_time", "media_type", "quality_score"})

    def test_timeline_recovers_subsecond_metadata_without_mutating_logical_time(self):
        with self.workspace.transaction() as connection:
            ids = [
                connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = ?", (name,)).fetchone()[0]
                for name in ("gps.jpg", "fallback.jpg", "plain.jpg")
            ]
            for asset_id, fraction in zip(ids, ("123", "456789", None)):
                metadata = {"exif": {"DateTimeOriginal": "2026:09:10 15:10:59"}}
                if fraction is not None:
                    metadata["exif"]["SubSecTimeOriginal"] = fraction
                connection.execute(
                    "UPDATE physical_file SET metadata_json = ? WHERE logical_asset_id = ?",
                    (json.dumps(metadata), asset_id),
                )
                connection.execute(
                    "UPDATE logical_asset SET capture_time = '2026-09-10T15:10:59', capture_time_kind = 'exif_local_unknown' WHERE id = ?",
                    (asset_id,),
                )
        data = visualization_data(self.workspace, {}, self.handle, filter_assets=_browser_filtered_assets, kind="timeline")
        points = {point["asset_id"]: point for point in data["points"]}
        self.assertEqual(points[self.gps_id]["time_us"], wall_clock_coordinate_us("2026-09-10T15:10:59.123000"))
        self.assertEqual(points[self.gps_id]["time_precision_us"], 1_000)
        self.assertEqual(points[self.fallback_id]["time_us"], wall_clock_coordinate_us("2026-09-10T15:10:59.456789"))
        self.assertEqual(points[self.fallback_id]["time_precision_us"], 1)
        self.assertEqual(points[self.gps_id]["capture_time"], "2026-09-10T15:10:59")

    def test_wall_clock_microseconds_remain_distinct_and_equal_values_remain_equal(self):
        values = ["2026-09-10T15:10:59.000000", "2026-09-10T15:10:59.123000", "2026-09-10T15:10:59.456789"]
        coordinates = [wall_clock_coordinate_us(value) for value in values]
        self.assertEqual(len(set(coordinates)), 3)
        self.assertEqual(wall_clock_coordinate_us(values[0]), wall_clock_coordinate_us("2026-09-10T15:10:59"))

    def test_workspace_visualization_capabilities_use_workspace_state(self):
        capabilities = visualization_capabilities(self.workspace)
        self.assertEqual(capabilities, {"geo": True, "timeline": True, "vector": False})
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE physical_file SET metadata_json = ?",
                (json.dumps({"gps": {"latitude": 95, "longitude": 2}}),),
            )
        self.assertFalse(visualization_capabilities(self.workspace)["geo"])
