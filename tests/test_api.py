from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from PIL import Image

from archive_index.api.server import WorkspaceHTTPServer, _asset_detail, _configuration_quality_readiness, _path_revision, _pick_workspace_path, _quality_readiness
from archive_index.app_state import workspace_id
from archive_index.configuration import default_configuration
from archive_index.indexing.media_pipeline import index_workspace as run_index_workspace
from archive_index.indexing.grouping import build_groups, extract_visual_features
from archive_index.indexing.recommendation import build_recommendations
from archive_index.indexing.reconciliation import reconcile_workspace
from archive_index.indexing.scanner import _file_created_time, scan
from archive_index.jobs.engine import JobStore
from archive_index.workspace import Workspace
from archive_index.media.quality_provider import LegacyPillowProvider, OffQualityProvider
from archive_index.embeddings.search import SearchResult


def index_workspace(workspace, **kwargs):
    kwargs.setdefault("quality_provider", LegacyPillowProvider())
    return run_index_workspace(workspace, **kwargs)


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name) / "archive"
        (root / "nested").mkdir(parents=True)
        _write_image(root / "root.jpg", (640, 480))
        _write_image(root / "nested" / "nested.jpg", (320, 240))
        (root / "clip.mp4").write_bytes(b"not a real video")
        self.workspace = Workspace.create(root)
        self.workspace.set_quality_provider("lar-iqa")
        scan(self.workspace)
        index_workspace(self.workspace, components=("metadata", "thumbnail", "quality"))
        self.server = WorkspaceHTTPServer(("127.0.0.1", 0), self.workspace)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary_directory.cleanup()

    def _mark_offline(self, *relative_paths: str) -> None:
        with self.workspace.transaction() as connection:
            for relative_path in relative_paths:
                connection.execute(
                    "UPDATE physical_file SET is_online = 0 WHERE relative_path = ?",
                    (relative_path,),
                )

    def _add_offline_error(self, relative_path: str) -> str:
        with self.workspace.transaction() as connection:
            physical_id = connection.execute(
                "SELECT id FROM physical_file WHERE relative_path = ?", (relative_path,)
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO job_error(job_id, physical_file_id, error_type, message, created_at) VALUES (NULL, ?, 'visual_features', 'decoder failed', '2026-01-01T00:00:00+00:00')",
                (physical_id,),
            )
        return physical_id

    def _pair_raw_with_jpeg(self) -> None:
        (self.workspace.root / "paired.ARW").write_bytes(b"raw")
        scan(self.workspace)
        with self.workspace.transaction() as connection:
            jpeg_asset_id = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'root.jpg'"
            ).fetchone()[0]
            raw = connection.execute(
                "SELECT id, logical_asset_id FROM physical_file WHERE relative_path = 'paired.ARW'"
            ).fetchone()
            connection.execute(
                "UPDATE physical_file SET logical_asset_id = ? WHERE id = ?",
                (jpeg_asset_id, raw["id"]),
            )
            connection.execute("DELETE FROM logical_asset WHERE id = ?", (raw["logical_asset_id"],))

    def test_explicit_model_install_does_not_index(self):
        body = json.dumps({"provider": "openclip-b16-datacomp-xl"}).encode()
        with patch("archive_index.embeddings.models.install_model") as install, patch.object(self.server, "start_indexing") as indexing:
            request = Request(self.base_url + "/api/embedding-models/install", data=body, headers={"Content-Type":"application/json"}, method="POST")
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
            install.assert_called_once_with("openclip-b16-datacomp-xl")
            indexing.assert_not_called()

    def test_forget_offline_media_removes_an_offline_asset_and_diagnostics(self):
        self._mark_offline("root.jpg")
        physical_id = self._add_offline_error("root.jpg")
        original = (self.workspace.root / "root.jpg").read_bytes()
        with patch.object(Path, "is_file", return_value=False), patch.object(Path, "unlink", side_effect=AssertionError("original deleted")):
            result = self.server.forget_offline_media(self.server.default_handle)
        self.assertEqual(result, {"removed": 1, "images": 1, "videos": 0, "count": 1})
        with closing(self.workspace.connect()) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM physical_file WHERE relative_path = 'root.jpg'").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM logical_asset WHERE id NOT IN (SELECT logical_asset_id FROM physical_file)").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM job_error WHERE physical_file_id = ?", (physical_id,)).fetchone())
        self.assertEqual((self.workspace.root / "root.jpg").read_bytes(), original)

    def test_forget_offline_media_keeps_logical_asset_for_online_pair(self):
        self._pair_raw_with_jpeg()
        self._mark_offline("paired.ARW")
        with patch.object(Path, "is_file", return_value=False):
            result = self.server.forget_offline_media(self.server.default_handle)
        self.assertEqual(result["removed"], 1)
        with closing(self.workspace.connect()) as connection:
            row = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'root.jpg'"
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(connection.execute("SELECT 1 FROM physical_file WHERE relative_path = 'paired.ARW'").fetchone())
            self.assertIsNotNone(connection.execute("SELECT 1 FROM logical_asset WHERE id = ?", (row[0],)).fetchone())

    def test_forget_offline_media_removes_fully_offline_pair(self):
        self._pair_raw_with_jpeg()
        self._mark_offline("root.jpg", "paired.ARW")
        with patch.object(Path, "is_file", return_value=False):
            result = self.server.forget_offline_media(self.server.default_handle)
        self.assertEqual(result, {"removed": 2, "images": 2, "videos": 0, "count": 2})
        with closing(self.workspace.connect()) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM physical_file WHERE relative_path IN ('root.jpg', 'paired.ARW')").fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM logical_asset WHERE id NOT IN (SELECT logical_asset_id FROM physical_file)").fetchone()[0], 0)

    def test_forget_offline_media_never_removes_online_files(self):
        self._mark_offline("root.jpg")
        original = (self.workspace.root / "nested" / "nested.jpg").read_bytes()
        with patch.object(Path, "is_file", return_value=False), patch.object(Path, "unlink", side_effect=AssertionError("original deleted")):
            self.server.forget_offline_media(self.server.default_handle)
        self.assertTrue((self.workspace.root / "nested" / "nested.jpg").is_file())
        self.assertEqual((self.workspace.root / "nested" / "nested.jpg").read_bytes(), original)
        with closing(self.workspace.connect()) as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM physical_file WHERE relative_path = 'nested/nested.jpg' AND is_online = 1").fetchone())

    def test_forget_offline_media_refuses_unavailable_root(self):
        self._mark_offline("root.jpg")
        with patch("archive_index.api.server.os.access", return_value=False):
            with self.assertRaises(ValueError):
                self.server.forget_offline_media(self.server.default_handle)

    def test_offline_media_info_rechecks_a_reconnected_file(self):
        self._mark_offline("root.jpg")
        result = self.server.offline_media_info(self.server.default_handle)
        self.assertEqual(result, {"images": 0, "videos": 0, "count": 0})
        with closing(self.workspace.connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT is_online FROM physical_file WHERE relative_path = 'root.jpg'"
                ).fetchone()[0],
                1,
            )

    def test_forget_offline_media_invalidates_browser_catalog(self):
        before = _get_json(self.base_url, "/api/browser")[1]
        self._mark_offline("root.jpg")
        with patch.object(Path, "is_file", return_value=False):
            self.server.forget_offline_media(self.server.default_handle)
        after = _get_json(self.base_url, "/api/browser")[1]
        self.assertEqual((before["total"], after["total"]), (3, 2))

    def test_home_summary_filters_and_pagination(self) -> None:
        status, home = _get_json(self.base_url, "/api/workspace")
        self.assertEqual(status, 200)
        self.assertEqual((home["assets"], home["online_files"]), (3, 3))
        self.assertEqual(home["embedding_storage_bytes"], 0)

        status, nested = _get_json(self.base_url, "/api/assets?folder=nested")
        self.assertEqual(status, 200)
        self.assertEqual((nested["total"], nested["items"][0]["filename"]), (1, "nested.jpg"))

        status, images = _get_json(self.base_url, "/api/assets?media_type=image&page_size=1")
        self.assertEqual(status, 200)
        self.assertEqual((images["total"], len(images["items"]), images["has_next"]), (2, 1, True))
        status, videos = _get_json(self.base_url, "/api/assets?media_type=video")
        self.assertEqual(status, 200)
        self.assertNotIn("processing", videos["items"][0]["issues"])
        status, ranked = _get_json(self.base_url, "/api/assets?sort=quality_desc")
        self.assertEqual(status, 200)
        self.assertIsNotNone(ranked["items"][0]["quality_score"])
        status, lowest = _get_json(self.base_url, "/api/assets?sort=quality_asc")
        self.assertEqual(status, 200)
        self.assertIsNone(lowest["items"][-1]["quality_score"])
        status, filenames = _get_json(
            self.base_url, "/api/assets?sort_by=filename&direction=asc&media_type=image&page_size=60"
        )
        self.assertEqual(status, 200)
        self.assertEqual(filenames["items"][0]["filename"], "nested.jpg")

        status, html = _get_bytes(self.base_url, "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Archive Indexation", html)
        self.assertIn(b"<dialog", html)
        self.assertIn(b"problems-dialog", html)
        self.assertNotIn(b"page-size", html)
        self.assertIn(b"Choose folder", html)
        status, css = _get_bytes(self.base_url, "/app.css")
        self.assertEqual(status, 200)
        self.assertIn(b"aspect-ratio: 1 / 1", css)
        status, js = _get_bytes(self.base_url, "/app.js")
        self.assertEqual(status, 200)
        for resource in (
            "app-shared.js",
            "app-setup.js",
            "app-browser.js",
            "app-viewer.js",
            "app-details.js",
            "app-maintenance.js",
            "app-groups.js",
            "app-bootstrap.js",
        ):
            resource_status, resource_body = _get_bytes(self.base_url, f"/{resource}")
            self.assertEqual(resource_status, 200)
            js += b"\n" + resource_body
        status, models = _get_json(self.base_url, "/api/embedding-models")
        self.assertEqual(status, 200)
        self.assertEqual({model["provider"] for model in models["models"]}, {"openclip-b16-datacomp-xl", "siglip2-base-patch16-224"})
        self.assertIn(b">Groupings<", html)
        self.assertNotIn(b">Selection<", html)
        self.assertNotIn(b">Strict groups<", html)
        self.assertNotIn(b"Workspaces</a>", html)
        self.assertNotIn(b"Rebuild groups", html)
        self.assertNotIn(b"Rebuild recommendations", html)
        self.assertIn(b"groups-pager-top", html)
        self.assertIn(b"groups-pager-bottom", html)
        self.assertIn(b"Recommended", html)
        self.assertIn(b"viewer-selection", html)
        self.assertIn(b"viewer-info", html)
        self.assertIn(b"viewer-stage", html)
        self.assertIn(b"quality-unsupported", js)
        self.assertIn(b"representative", js)
        self.assertIn(b"quality-chip", js)
        self.assertIn(b"selection-button select", js)
        self.assertNotIn(b">Clear<", js)
        self.assertIn(b"viewer-smooth", html)
        self.assertIn(b"const progress = state.viewerZoom > 1", js)
        self.assertIn(b'"viewer-media-pane"', js)
        self.assertIn(b"setPointerCapture", js)
        self.assertIn(b"qualityColor", js)
        self.assertIn(b"technical-reading", js)
        self.assertIn(b"recommended-card", js)
        self.assertIn(b"viewerClickSuppressed", js)
        self.assertIn(b"bindBackdropClose", js)
        self.assertIn(b"pressedOutside", js)
        self.assertIn(b"model install lar-iqa", js)

        self.assertNotIn(b'$("details").addEventListener("click"', js)
        self.assertIn(b"#details .details-content", css)
        self.assertIn(b"focused-group .group-heading", css)
        self.assertIn(b"range-label-top", html)
        self.assertNotIn("aria-label=\"Previous page\"".encode(), html)
        self.assertNotIn(b">Previous<", html)
        self.assertNotIn(b">Next<", html)
        self.assertNotIn(b"Open a workspace to browse", html)
        self.assertNotIn(b"Create / index folder", html)
        self.assertNotIn(b"Files / representations", html)
        self.assertNotIn(b"Advanced measurements", html)
        self.assertNotIn(b"lower is better", html)
        self.assertNotIn(b"Include singletons", html)
        self.assertNotIn(b">Apply<", html)
        self.assertNotIn(b"Open thumbnail", html)
        self.assertNotIn(b"scrollIntoView", html)
        self.assertNotIn(b'id="selection-filter"', html)
        for value in (b"all", b"representatives", b"recommended"):
            self.assertIn(b'data-auto="' + value + b'"', html)
        for value in (b"all", b"selected", b"rejected", b"undecided"):
            self.assertIn(b'data-manual="' + value + b'"', html)
        self.assertIn(b"function formatCapture", js)
        self.assertIn(b"slice(1, 3)", js)
        self.assertIn(b'data-detail-thumbnail', js)
        self.assertIn(b'renderDetails(state.viewerDetail', js)
        self.assertIn(b'manual: state.manual', js)

    def test_semantic_search_returns_similarity_results_through_the_same_asset_shape(self) -> None:
        with closing(self.workspace.connect()) as connection:
            asset_id = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'root.jpg'"
            ).fetchone()[0]
        status, unavailable = _get_json(self.base_url, "/api/search?text=red%20flower")
        self.assertEqual(status, 400)
        self.assertIn("semantic embeddings", unavailable["error"])
        configuration = {**self.workspace.configuration(), "semantic_search_enabled": True}
        self.workspace.apply_configuration(configuration)
        with patch(
            "archive_index.api.server.search_text",
            return_value=[SearchResult(asset_id, 0.8123)],
        ):
            status, result = _get_json(self.base_url, "/api/search?text=red%20flower")
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["asset_id"], asset_id)
        self.assertAlmostEqual(result["items"][0]["similarity"], 0.8123)

    def test_similar_route_accepts_paging_query(self) -> None:
        with closing(self.workspace.connect()) as connection:
            asset_ids = [row[0] for row in connection.execute("SELECT logical_asset_id FROM physical_file ORDER BY logical_asset_id")]
        asset_id = asset_ids[0]
        results = [SearchResult(other_id, 0.81 - index * 0.01, 12.3, 8.1) for index, other_id in enumerate(asset_ids[1:])]
        with patch(
            "archive_index.api.server.search_similar",
            return_value=results,
        ):
            pages = []
            for offset in (0, 1):
                status, result = _get_json(self.base_url, f"/api/assets/{asset_id}/similar?offset={offset}&limit=1")
                self.assertEqual(status, 200)
                pages.append(result)
        self.assertEqual([page["items"][0]["asset_id"] for page in pages], asset_ids[1:])
        self.assertEqual([page["total"] for page in pages], [len(results), len(results)])
        self.assertEqual(pages[0]["items"][0]["best_match_timestamp"], 12.3)
        self.assertEqual(pages[0]["items"][0]["source_match_timestamp"], 8.1)

    def test_similar_initial_page_caps_strong_results_and_keeps_alternatives(self) -> None:
        with closing(self.workspace.connect()) as connection:
            asset_ids = [row[0] for row in connection.execute("SELECT logical_asset_id FROM physical_file ORDER BY logical_asset_id")]
        asset_id = asset_ids[0]
        other_id = asset_ids[1]
        cases = [
            ([0.89], 0, 0, True),
            ([0.95], 1, 1, False),
            ([0.95] * 7 + [0.89], 7, 7, True),
            ([0.95] * 14 + [0.89], 14, 12, True),
        ]
        for scores, expected_strong, expected_items, expected_next in cases:
            results = [SearchResult(other_id, score) for score in scores]
            with patch("archive_index.api.server.search_similar", return_value=results):
                status, initial = _get_json(self.base_url, f"/api/assets/{asset_id}/similar?offset=0&limit=12&initial=1")
            self.assertEqual(status, 200)
            self.assertEqual((initial["strong_count"], len(initial["items"]), initial["has_next"]), (expected_strong, expected_items, expected_next))

    def test_unexpected_server_errors_keep_a_concise_safe_message(self) -> None:
        with patch("archive_index.api.server._browser_assets", side_effect=RuntimeError("catalog query failed")):
            status, error = _get_json(self.base_url, "/api/browser")
        self.assertEqual(status, 500)
        self.assertEqual(error["error"], "catalog query failed")

    def test_configuration_plan_and_scoped_gallery(self) -> None:
        status, configuration = _get_json(self.base_url, "/api/workspace/configuration")
        self.assertEqual(status, 200)
        draft = configuration["configuration"]
        self.assertEqual(draft["configuration_version"], 5)
        draft["folder_rules"] = [{"path": "", "included": False}, {"path": "nested", "included": True}]
        status, plan = _post_json(self.base_url, "/api/workspace/configuration/plan", {"configuration": draft})
        self.assertEqual(status, 200)
        self.assertEqual(plan["plan"]["selected_files"], 1)
        invalid = dict(draft)
        invalid["quality_provider"] = "future-provider"
        status, error = _post_json(self.base_url, "/api/workspace/configuration/plan", {"configuration": invalid})
        self.assertEqual(status, 400)
        self.assertIn("quality_provider", error["error"])
        self.workspace.apply_configuration(draft)
        status, summary = _get_json(self.base_url, "/api/workspace")
        self.assertEqual((status, summary["assets"], summary["out_of_scope_files"]), (200, 1, 2))
        status, videos = _get_json(self.base_url, "/api/assets?media_type=video")
        self.assertEqual((status, videos["total"]), (200, 0))

    def test_logical_asset_detail_exposes_physical_and_component_state(self) -> None:
        with closing(self.workspace.connect()) as connection:
            asset_id = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'root.jpg'"
            ).fetchone()[0]

        status, detail = _get_json(self.base_url, f"/api/assets/{asset_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["physical_files"]), 1)
        physical = detail["physical_files"][0]
        self.assertEqual(physical["relative_path"], "root.jpg")
        self.assertEqual(physical["absolute_path"], str(self.workspace.root / "root.jpg"))
        self.assertEqual(physical["file_created_time"], _file_created_time((self.workspace.root / "root.jpg").stat()))
        self.assertEqual(physical["components"]["metadata"]["status"], "complete")
        self.assertEqual(physical["components"]["thumbnail"]["status"], "complete")
        self.assertEqual(physical["components"]["quality"]["status"], "complete")
        self.assertIsNotNone(physical["quality_score"])
        self.assertIsNotNone(physical["original_url"])
        self.assertIsNotNone(physical["thumbnail_url"])

    def test_missing_wal_is_a_normal_revision_state(self) -> None:
        wal = self.workspace.database_path.with_name("index.sqlite-wal")
        self.assertFalse(wal.exists())
        self.assertIsNone(_path_revision(wal))

    def test_detail_exposes_multiple_physical_representations_and_preferred_view(self) -> None:
        original = self.workspace.root / "root.jpg"
        (self.workspace.root / "root-copy.jpg").write_bytes(original.read_bytes())
        scan(self.workspace)
        reconcile_workspace(self.workspace)
        with closing(self.workspace.connect()) as connection:
            asset_id = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'root.jpg'"
            ).fetchone()[0]

        status, detail = _get_json(self.base_url, f"/api/assets/{asset_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["physical_files"]), 2)
        self.assertEqual(sum(file["is_preferred"] for file in detail["physical_files"]), 1)
        self.assertTrue(
            all("Exact duplicate" in file["relationships"] for file in detail["physical_files"])
        )

    def test_detail_location_uses_valid_coordinates_and_raw_fallback(self) -> None:
        with closing(self.workspace.connect()) as connection:
            asset_id = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'root.jpg'"
            ).fetchone()[0]
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE physical_file SET metadata_json = ? WHERE relative_path = 'root.jpg'",
                (json.dumps({"gps": {"latitude": 40.987654, "longitude": -73.123456}}),),
            )
        detail = _asset_detail(self.workspace, asset_id, "test")
        self.assertEqual(detail["location"], {"latitude": 40.987654, "longitude": -73.123456})

        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE physical_file SET metadata_json = ? WHERE relative_path = 'root.jpg'",
                (json.dumps({"exif": {}}),),
            )
        self.assertIsNone(_asset_detail(self.workspace, asset_id, "test")["location"])

        _write_image(self.workspace.root / "paired.jpg", (80, 60))
        (self.workspace.root / "paired.arw").write_bytes(b"raw")
        scan(self.workspace)
        with self.workspace.transaction() as connection:
            jpeg_id, raw_id, pair_asset_id = connection.execute(
                """
                SELECT jpeg.id, raw.id, jpeg.logical_asset_id
                FROM physical_file AS jpeg
                JOIN physical_file AS raw ON raw.relative_path = 'paired.arw'
                WHERE jpeg.relative_path = 'paired.jpg'
                """
            ).fetchone()
            connection.execute(
                "UPDATE physical_file SET logical_asset_id = ?, role = 'camera_jpeg', metadata_json = ? WHERE id = ?",
                (pair_asset_id, json.dumps({"exif": {}}), jpeg_id),
            )
            connection.execute(
                "UPDATE physical_file SET logical_asset_id = ?, role = 'camera_raw', metadata_json = ? WHERE id = ?",
                (pair_asset_id, json.dumps({"gps": {"latitude": -33.8688, "longitude": 151.2093}}), raw_id),
            )
        self.assertEqual(
            _asset_detail(self.workspace, pair_asset_id, "test")["location"],
            {"latitude": -33.8688, "longitude": 151.2093},
        )

    def test_original_and_thumbnail_routes_do_not_accept_arbitrary_paths(self) -> None:
        with closing(self.workspace.connect()) as connection:
            file_id = connection.execute(
                "SELECT id FROM physical_file WHERE relative_path = 'root.jpg'"
            ).fetchone()[0]
            asset_id = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE id = ?", (file_id,)
            ).fetchone()[0]

        status, original = _get_bytes(self.base_url, f"/api/files/{file_id}/original")
        self.assertEqual(status, 200)
        self.assertGreater(len(original), 10)
        status, thumbnail = _get_bytes(self.base_url, f"/api/assets/{asset_id}/thumbnail")
        self.assertEqual(status, 200)
        self.assertGreater(len(thumbnail), 10)

        outside = Path(self.temporary_directory.name) / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        with self.workspace.transaction() as connection:
            connection.execute(
                """
                INSERT INTO logical_asset(id, media_type, created_at, updated_at)
                VALUES ('malicious-asset', 'image', 'now', 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO physical_file(
                    id, logical_asset_id, relative_path, filename, extension, media_type,
                    created_at, updated_at
                ) VALUES ('malicious-file', 'malicious-asset', '../outside.txt', 'outside.txt', '.txt', 'image', 'now', 'now')
                """
            )
            connection.execute(
                """
                INSERT INTO component_state(
                    physical_file_id, component, status, output_path
                ) VALUES ('malicious-file', 'thumbnail', 'complete', '../index.sqlite')
                """
            )

        status, _ = _get_json(self.base_url, "/api/assets/malicious-asset")
        self.assertEqual(status, 200)
        status, _ = _get_bytes(self.base_url, "/api/files/malicious-file/original")
        self.assertEqual(status, 404)
        status, _ = _get_bytes(self.base_url, "/api/files/malicious-file/thumbnail")
        self.assertEqual(status, 404)
        status, _ = _get_json(self.base_url, "/api/assets?folder=../")
        self.assertEqual(status, 400)
        status, _ = _get_bytes(self.base_url, "/.archive-index/index.sqlite")
        self.assertEqual(status, 404)

    def test_missing_media_placeholder_and_problem_data(self) -> None:
        missing = self.workspace.root / "nested" / "nested.jpg"
        missing.unlink()
        scan(self.workspace)
        with closing(self.workspace.connect()) as connection:
            missing_asset = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'nested/nested.jpg'"
            ).fetchone()[0]

        status, assets = _get_json(self.base_url, "/api/assets?folder=nested")
        self.assertEqual(status, 200)
        self.assertFalse(assets["items"][0]["is_online"])
        self.assertIsNotNone(assets["items"][0]["thumbnail_url"])
        status, detail = _get_json(self.base_url, f"/api/assets/{missing_asset}")
        self.assertEqual(status, 200)
        self.assertIsNone(detail["physical_files"][0]["original_url"])

        job_id = JobStore(self.workspace).create("test")
        JobStore(self.workspace).record_error(
            job_id, RuntimeError("problem"), relative_path="nested/nested.jpg"
        )
        status, problems = _get_json(self.base_url, "/api/problems")
        self.assertEqual(status, 200)
        self.assertEqual(problems["problems"][0]["relative_path"], "nested/nested.jpg")
        status, jobs = _get_json(self.base_url, "/api/jobs")
        self.assertEqual(status, 200)
        self.assertEqual(jobs["jobs"][0]["id"], job_id)

    def test_index_endpoint_starts_and_reports_shared_jobs(self) -> None:
        status, payload = _post_json(self.base_url, "/api/index")
        self.assertEqual(status, 202)
        scan_job_id = payload["job_id"]

        observed = None
        for _ in range(50):
            status, jobs = _get_json(self.base_url, "/api/jobs?limit=10")
            self.assertEqual(status, 200)
            observed = next(job for job in jobs["jobs"] if job["id"] == scan_job_id)
            if observed["status"] in {"complete", "failed", "cancelled"}:
                break
            time.sleep(0.1)

        self.assertEqual(observed["status"], "complete")
        self.assertTrue({"stage", "failed_items", "skipped_items"} <= observed.keys())
        self.assertTrue(any(job["kind"] == "media_index" for job in jobs["jobs"]))

    def test_active_jobs_include_eta_when_live_rate_is_unavailable(self) -> None:
        job_id = JobStore(self.workspace).create("recommendations", total_items=5)
        JobStore(self.workspace).start(job_id)
        try:
            status, payload = _get_json(self.base_url, "/api/jobs?limit=10")
            self.assertEqual(status, 200)
            job = next(job for job in payload["jobs"] if job["id"] == job_id)
            self.assertIsInstance(job["eta_seconds"], (int, float))
            self.assertGreaterEqual(job["eta_seconds"], 1)
        finally:
            JobStore(self.workspace).complete(job_id)

    def test_media_failure_does_not_rewrite_completed_scan(self) -> None:
        scan_job_id = JobStore(self.workspace).create("scan")
        JobStore(self.workspace).complete(scan_job_id)
        handle = self.server.default_handle
        with patch("archive_index.api.server.scan", return_value=type("Scan", (), {"cancelled": False})()), patch(
            "archive_index.api.server.index_workspace", side_effect=RuntimeError("media stop")
        ):
            self.server._run_indexing(handle, self.workspace, scan_job_id, threading.Event())
        self.assertEqual(JobStore(self.workspace).get(scan_job_id)["status"], "complete")
        with closing(self.workspace.connect()) as connection:
            media = connection.execute(
                "SELECT status FROM job WHERE kind = 'media_index' ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(media["status"], "failed")

    def test_gallery_page_uses_bulk_physical_lookup(self) -> None:
        with patch("archive_index.api.server._physical_rows", side_effect=AssertionError("N+1 lookup")):
            status, assets = _get_json(self.base_url, "/api/assets?page_size=60")
        self.assertEqual(status, 200)
        self.assertEqual(len(assets["items"]), 3)

    def test_group_api_uses_bulk_physical_lookup(self) -> None:
        extract_visual_features(self.workspace)
        build_groups(self.workspace)
        with patch("archive_index.api.server._physical_rows", side_effect=AssertionError("N+1 group lookup")):
            status, groups = _get_json(self.base_url, "/api/groups")
        self.assertEqual(status, 200)
        self.assertEqual(len(groups["groups"]), 2)
        self.assertTrue(all(group["members"] for group in groups["groups"]))
        self.assertTrue(
            all(
                member["current_group_id"] == group["group_id"]
                for group in groups["groups"]
                for member in group["members"]
            )
        )
        status, filename_groups = _get_json(self.base_url, "/api/groups?sort_by=filename&direction=asc")
        self.assertEqual(status, 200)
        self.assertEqual(
            [group["members"][0]["filename"] for group in filename_groups["groups"]],
            ["nested.jpg", "root.jpg"],
        )
        status, reverse_filename_groups = _get_json(self.base_url, "/api/groups?sort_by=filename&direction=desc")
        self.assertEqual(status, 200)
        self.assertEqual(
            [group["members"][0]["filename"] for group in reverse_filename_groups["groups"]],
            ["root.jpg", "nested.jpg"],
        )

        status, filtered = _get_json(self.base_url, "/api/groups?q=root.jpg")
        self.assertEqual((status, filtered["total"], len(filtered["groups"][0]["members"])), (200, 1, 1))
        status, video_groups = _get_json(self.base_url, "/api/groups?media_type=video")
        self.assertEqual((status, video_groups["total"]), (200, 1))
        group_id = groups["groups"][0]["group_id"]
        status, location = _get_json(self.base_url, f"/api/groups/locate?group_id={group_id}")
        self.assertEqual((status, location["found"], location["page"]), (200, True, 1))

        status, representatives = _get_json(self.base_url, "/api/assets?representatives=1")
        self.assertEqual(status, 200)
        self.assertEqual(representatives["total"], 3)
        self.assertTrue(all(item["is_representative"] for item in representatives["items"]))

    def test_group_rebuild_refreshes_recommendations_for_new_grouping_run(self) -> None:
        extract_visual_features(self.workspace)
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE logical_asset SET capture_time = '2026-09-03T12:00:00+03:00', capture_time_kind = 'exif_offset'"
            )
            connection.execute("UPDATE physical_file SET quality_score = .8 WHERE media_type = 'image'")
        status, payload = _post_json(self.base_url, "/api/groups/rebuild")
        self.assertEqual(status, 202)
        recommendation_job = None
        for _ in range(100):
            _, jobs = _get_json(self.base_url, "/api/jobs?limit=20")
            recommendation_job = next((job for job in jobs["jobs"] if job["kind"] == "recommendations"), None)
            if recommendation_job and recommendation_job["status"] in {"complete", "failed", "cancelled"}:
                break
            time.sleep(.1)
        self.assertIsNotNone(recommendation_job)
        self.assertEqual(recommendation_job["status"], "complete")
        status, recommendations = _get_json(self.base_url, "/api/recommendations")
        self.assertEqual((status, recommendations["available"]), (200, True))
        with closing(self.workspace.connect()) as connection:
            grouping_run = connection.execute("SELECT active_run_id FROM workspace_grouping WHERE id = 1").fetchone()[0]
            source_run = connection.execute("SELECT source_grouping_run_id FROM recommendation_run WHERE id = (SELECT active_run_id FROM workspace_recommendation WHERE id = 1)").fetchone()[0]
        self.assertEqual(source_run, grouping_run)

    def test_video_original_supports_safe_byte_ranges(self) -> None:
        with closing(self.workspace.connect()) as connection:
            file_id = connection.execute(
                "SELECT id FROM physical_file WHERE relative_path = 'clip.mp4'"
            ).fetchone()[0]

        status, headers, body = _get_response(
            self.base_url,
            f"/api/files/{file_id}/original",
            {"Range": "bytes=0-3"},
        )
        self.assertEqual(status, 206)
        self.assertEqual(len(body), 4)
        self.assertEqual(headers["Content-Type"], "video/mp4")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Content-Range"], "bytes 0-3/16")

        status, headers, _ = _get_response(
            self.base_url,
            f"/api/files/{file_id}/original",
            {"Range": "bytes=100-"},
        )
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */16")

    def test_recommendation_filters_and_workspace_scoped_decisions(self) -> None:
        with closing(self.workspace.connect()) as connection:
            root_id = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'root.jpg'"
            ).fetchone()[0]
            nested_id = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'nested/nested.jpg'"
            ).fetchone()[0]
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE physical_file SET quality_score = .8 WHERE logical_asset_id = ?", (root_id,))
            connection.execute("UPDATE physical_file SET quality_score = .2 WHERE logical_asset_id = ?", (nested_id,))
        extract_visual_features(self.workspace)
        build_groups(self.workspace)
        build_recommendations(self.workspace)

        status, recommended = _get_json(self.base_url, "/api/assets?recommended=1")
        self.assertEqual(status, 200)
        self.assertEqual(recommended["total"], 1)
        self.assertTrue(recommended["items"][0]["auto_recommended"])
        self.assertEqual(recommended["items"][0]["user_decision"], "undecided")

        status, decision = _post_json(
            self.base_url,
            f"/api/assets/{root_id}/decision",
            {"decision": "selected"},
        )
        self.assertEqual((status, decision["user_decision"]), (200, "selected"))
        status, selected = _get_json(self.base_url, "/api/assets?decision=selected")
        self.assertEqual((status, selected["total"]), (200, 1))
        self.assertEqual((selected["items"][0]["user_decision"], selected["items"][0]["auto_recommended"]), ("selected", True))
        status, undecided = _get_json(self.base_url, "/api/assets?decision=undecided")
        self.assertEqual((status, undecided["total"]), (200, 2))
        status, invalid = _post_json(
            self.base_url,
            f"/api/assets/{root_id}/decision",
            {"decision": "maybe"},
        )
        self.assertEqual(status, 400)
        status, invalid_scope = _post_json(
            self.base_url,
            f"/api/assets/{root_id}/decision?workspace=not-a-workspace",
            {"decision": "rejected"},
        )
        self.assertEqual(status, 404)
        self.assertEqual(invalid_scope["error"], "workspace is unavailable")
        status, info = _get_json(self.base_url, "/api/recommendations")
        self.assertEqual((status, info["available"], info["counts"]["recommended"]), (200, True, 1))
        status, rebuild = _post_json(self.base_url, "/api/recommendations/rebuild")
        self.assertEqual(status, 202)
        for _ in range(50):
            status, jobs = _get_json(self.base_url, "/api/jobs?limit=10")
            recommendation_job = next(job for job in jobs["jobs"] if job["id"] == rebuild["job_id"])
            if recommendation_job["status"] in {"complete", "failed", "cancelled"}:
                break
            time.sleep(.1)
        self.assertEqual(recommendation_job["status"], "complete")


class WorkspaceHomeApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        base = Path(self.temporary_directory.name)
        self.root = base / "new archive"
        self.root.mkdir()
        Image.new("RGB", (80, 60), color=(100, 140, 200)).save(self.root / "photo.jpg")
        self.home_root = base / "home workspace"
        self.home_root.mkdir()
        Image.new("RGB", (80, 60), color=(40, 80, 120)).save(self.home_root / "home.jpg")
        Image.new("RGB", (80, 60), color=(120, 80, 40)).save(self.home_root / "home-second.jpg")
        self.workspace = Workspace.create(self.home_root)
        scan(self.workspace)
        run_index_workspace(self.workspace, components=("metadata", "thumbnail"), quality_provider=OffQualityProvider())
        self.registry_path = base / "recent.json"
        self.server = WorkspaceHTTPServer(("127.0.0.1", 0), registry_path=self.registry_path)
        self.quality_provider_patch = patch(
            "archive_index.indexing.media_pipeline.create_quality_provider",
            return_value=OffQualityProvider(),
        )
        self.quality_provider_patch.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def test_quality_readiness_reports_ready_when_runtime_and_checkpoint_exist(self) -> None:
        checkpoint = self.root / "checkpoint.pt"
        checkpoint.write_bytes(b"test checkpoint")
        with patch("archive_index.api.workspaces.importlib.util.find_spec", return_value=object()), patch(
            "archive_index.api.workspaces.default_model_path", return_value=checkpoint
        ):
            readiness = _quality_readiness("lar-iqa")
        self.assertEqual(readiness["status"], "ready")
        self.assertTrue(readiness["ready"])

    def test_quality_readiness_reports_missing_checkpoint(self) -> None:
        checkpoint = self.root / "missing-checkpoint.pt"
        with patch("archive_index.api.workspaces.importlib.util.find_spec", return_value=object()), patch(
            "archive_index.api.workspaces.default_model_path", return_value=checkpoint
        ):
            readiness = _quality_readiness("lar-iqa")
        self.assertEqual(readiness["status"], "checkpoint_missing")
        self.assertFalse(readiness["ready"])
        self.assertFalse(readiness["model"]["installed"])

    def test_quality_model_installation_truth_is_independent_of_quality_checkbox(self) -> None:
        installed = {
            "status": "ready",
            "ready": True,
            "runtime_ready": True,
            "checkpoint_ready": True,
            "model": {"provider": "lar-iqa", "model_id": "lar-iqa", "installed": True, "size_bytes": 123},
            "message": "ready",
        }
        off = {"status": "off", "ready": True}

        def readiness(provider):
            return off if provider == "off" else installed

        configuration = default_configuration()
        configuration["rendered_quality_provider"] = "off"
        configuration["raw_quality_provider"] = "off"
        configuration["video_quality_enabled"] = False
        with patch("archive_index.api.workspaces._quality_readiness", side_effect=readiness), patch(
            "archive_index.api.workspaces._embedding_readiness", return_value={}
        ):
            disabled = _configuration_quality_readiness(configuration)
            configuration["rendered_quality_provider"] = "lar-iqa"
            enabled = _configuration_quality_readiness(configuration)

        self.assertTrue(disabled["lar_iqa_readiness"]["model"]["installed"])
        self.assertTrue(enabled["lar_iqa_readiness"]["model"]["installed"])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.quality_provider_patch.stop()
        self.temporary_directory.cleanup()

    def test_open_create_switch_and_remove_recent_workspace_without_server_restart(self) -> None:
        status, home = _get_json(self.base_url, "/api/workspaces")
        self.assertEqual((status, home["workspaces"]), (200, []))

        status, opened = _post_json(
            self.base_url,
            "/api/workspaces/open",
            {"path": str(self.root)},
        )
        self.assertEqual((status, opened["workspace"], opened["job_id"]), (200, None, None))
        self.assertFalse((self.root / ".archive-index").exists())
        status, planned = _post_json(
            self.base_url,
            "/api/workspaces/plan",
            {
                "path": str(self.root),
                "analysis": opened["setup"]["analysis"],
                "configuration": opened["setup"]["configuration"],
            },
        )
        self.assertEqual((status, planned["plan"]["selected_files"]), (200, 1))
        self.assertIn("quality_readiness", planned["plan"])
        status, applied = _post_json(
            self.base_url,
            "/api/workspaces/apply",
            {"path": str(self.root), "configuration": opened["setup"]["configuration"]},
        )
        self.assertEqual(status, 202)
        handle = applied["workspace"]["id"]
        self.assertIsNotNone(applied["job_id"])
        self.assertTrue((self.root / ".archive-index" / "index.sqlite").is_file())

        for _ in range(50):
            status, jobs = _get_json(self.base_url, f"/api/jobs?workspace={handle}&limit=10")
            self.assertEqual(status, 200)
            if not any(job["status"] in {"pending", "running"} for job in jobs["jobs"]):
                break
            time.sleep(0.1)

        status, summary = _get_json(self.base_url, f"/api/workspace?workspace={handle}")
        self.assertEqual((status, summary["path"], summary["assets"], summary["quality_provider"]), (200, str(self.root), 1, "lar-iqa"))
        status, reopened = _post_json(
            self.base_url,
            "/api/workspaces/open",
            {"path": str(self.root)},
        )
        self.assertEqual((status, reopened["workspace"]["id"], reopened["job_id"]), (200, handle, None))
        status, removed = _post_json(
            self.base_url, "/api/workspaces/remove", {"workspace": handle}
        )
        self.assertEqual((status, removed["removed"]), (200, True))
        self.assertTrue((self.root / ".archive-index" / "index.sqlite").is_file())

    def test_unselected_server_requires_workspace_handle_for_workspace_api(self) -> None:
        status, payload = _get_json(self.base_url, "/api/assets")
        self.assertEqual(status, 400)
        self.assertIn("workspace", payload["error"])

    def test_folder_picker_uses_native_dialog_and_cancel_is_empty(self) -> None:
        with patch("archive_index.api.server._pick_windows_folder", return_value="C:\\Photos\\Arşivi") as pick:
            self.assertEqual(_pick_workspace_path(), "C:\\Photos\\Arşivi")
        pick.assert_called_once_with()
        with patch("archive_index.api.server._pick_windows_folder", return_value=""):
            self.assertEqual(_pick_workspace_path(), "")
        source = (Path(__file__).parents[1] / "src" / "archive_index" / "api" / "server.py").read_text(encoding="utf-8")
        self.assertIn("iid_file_open_dialog", source)
        self.assertNotIn("FolderBrowserDialog", source)
        self.assertIn("_folder_picker_lock", source)
        self.assertIn("blocking=False", source)
        self.assertIn("GetForegroundWindow", source)

    def test_folder_picker_ignores_duplicate_in_flight_requests(self) -> None:
        started = threading.Event()
        release = threading.Event()
        result = []

        def blocking_picker() -> str:
            started.set()
            release.wait(2)
            return "C:\\Photos\\Arşivi"

        with patch("archive_index.api.server._pick_windows_folder", side_effect=blocking_picker) as pick:
            thread = threading.Thread(target=lambda: result.append(_pick_workspace_path()))
            thread.start()
            self.assertTrue(started.wait(2))
            self.assertEqual(_pick_workspace_path(), "")
            release.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["C:\\Photos\\Arşivi"])
        pick.assert_called_once_with()

    def test_unavailable_workspace_can_be_removed_from_registry(self) -> None:
        missing = self.root / "Arşiv yok"
        self.registry_path.write_text(
            json.dumps([{"id": "missing-workspace", "path": str(missing)}], ensure_ascii=False),
            encoding="utf-8",
        )
        status, home = _get_json(self.base_url, "/api/workspaces")
        self.assertEqual((status, home["workspaces"][0]["available"]), (200, False))
        status, info = _post_json(self.base_url, "/api/workspaces/remove-info", {"workspace": "missing-workspace"})
        self.assertEqual((status, info["available"]), (200, False))
        status, removed = _post_json(self.base_url, "/api/workspaces/remove", {"workspace": "missing-workspace"})
        self.assertEqual((status, removed["removed"]), (200, True))
        self.assertFalse(missing.exists())
        self.assertEqual(_get_json(self.base_url, "/api/workspaces")[1]["workspaces"], [])

    def test_home_listing_keeps_healthy_workspace_when_another_read_only_open_fails(self) -> None:
        healthy_root = self.root / "healthy"
        healthy = Workspace.create(healthy_root)
        broken_root = self.root / "broken"
        (broken_root / ".archive-index").mkdir(parents=True)
        (broken_root / ".archive-index" / "index.sqlite").write_bytes(b"not sqlite")
        self.registry_path.write_text(
            json.dumps([
                {"id": workspace_id(healthy), "path": str(healthy_root)},
                {"id": "broken-workspace", "path": str(broken_root)},
            ]),
            encoding="utf-8",
        )

        status, home = _get_json(self.base_url, "/api/workspaces")

        self.assertEqual(status, 200)
        entries = {entry["id"]: entry for entry in home["workspaces"]}
        self.assertTrue(entries[workspace_id(healthy)]["available"])
        self.assertFalse(entries["broken-workspace"]["available"])

    def test_home_thumbnail_prefers_highest_quality_existing_thumbnail(self) -> None:
        handle = workspace_id(self.workspace)
        self.server.registry.add(self.workspace)
        with self.workspace.transaction() as connection:
            rows = connection.execute(
                "SELECT id, relative_path FROM physical_file WHERE media_type = 'image' ORDER BY relative_path"
            ).fetchall()
            for index, row in enumerate(rows):
                name = f"home-{index}.jpg"
                Image.new("RGB", (8, 8), color=(index * 90, 10, 10)).save(self.workspace.index_path(f"thumbnails/{name}"))
                connection.execute("UPDATE physical_file SET quality_score = ? WHERE id = ?", (0.2 + index * 0.7, row["id"]))
                connection.execute(
                    "UPDATE component_state SET output_path = ?, status = 'complete' WHERE physical_file_id = ? AND component = 'thumbnail'",
                    (f"thumbnails/{name}", row["id"]),
                )

        selected = self.server.home_thumbnail(handle)

        self.assertEqual(selected.name, "home-1.jpg")
        status, home = _get_json(self.base_url, "/api/workspaces")
        self.assertEqual(status, 200)
        self.assertIsNotNone(home["workspaces"][0]["thumbnail_url"])
        status, payload = _get_bytes(self.base_url, home["workspaces"][0]["thumbnail_url"])
        self.assertEqual(status, 200)
        pixel = Image.open(BytesIO(payload)).getpixel((0, 0))
        for actual, expected in zip(pixel, (90, 10, 10)):
            self.assertAlmostEqual(actual, expected, delta=3)

    def test_home_thumbnail_falls_back_deterministically_without_quality(self) -> None:
        handle = workspace_id(self.workspace)
        self.server.registry.add(self.workspace)
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE physical_file SET quality_score = NULL")
            rows = connection.execute(
                "SELECT id FROM physical_file WHERE media_type = 'image' ORDER BY relative_path"
            ).fetchall()
            for index, row in enumerate(rows):
                name = f"fallback-{index}.jpg"
                Image.new("RGB", (8, 8), color=(10, index * 90, 10)).save(self.workspace.index_path(f"thumbnails/{name}"))
                connection.execute(
                    "UPDATE component_state SET output_path = ?, status = 'complete' WHERE physical_file_id = ? AND component = 'thumbnail'",
                    (f"thumbnails/{name}", row["id"]),
                )

        self.assertEqual(self.server.home_thumbnail(handle).name, "fallback-0.jpg")

    def test_home_listing_omits_missing_thumbnails_and_unavailable_entries(self) -> None:
        self.server.registry.add(self.workspace)
        with self.workspace.transaction() as connection:
            connection.execute("DELETE FROM component_state WHERE component = 'thumbnail'")
        status, home = _get_json(self.base_url, "/api/workspaces")
        self.assertEqual(status, 200)
        self.assertIsNone(home["workspaces"][0]["thumbnail_url"])

        missing = self.root / "missing-home-workspace"
        self.server.registry.path.write_text(
            json.dumps([{"id": "missing-home-workspace", "path": str(missing)}]),
            encoding="utf-8",
        )
        status, home = _get_json(self.base_url, "/api/workspaces")
        unavailable = next(entry for entry in home["workspaces"] if entry["id"] == "missing-home-workspace")
        self.assertEqual((status, unavailable["available"], unavailable.get("thumbnail_url")), (200, False, None))

    def test_home_registry_inspection_does_not_full_open_workspace(self) -> None:
        registry = self.root / "home-registry.json"
        handle = workspace_id(self.workspace)
        registry.write_text(json.dumps([{"id": handle, "path": str(self.home_root)}]), encoding="utf-8")
        server = WorkspaceHTTPServer(("127.0.0.1", 0), registry_path=registry)
        try:
            with patch("archive_index.api.server.Workspace.open", side_effect=AssertionError("Home listing opened workspace")):
                entries = server.list_workspaces()
            self.assertEqual((len(entries), entries[0]["id"], entries[0]["available"]), (1, handle, True))
        finally:
            server.server_close()

    def test_cached_workspace_with_missing_database_can_be_removed_without_reopening_it(self) -> None:
        stale_root = self.root / "stale"
        stale_root.mkdir()
        stale_workspace = Workspace.create(stale_root)
        handle = self.server._register_workspace(stale_workspace)
        self.server.registry.add(stale_workspace)
        stale_workspace.database_path.unlink()

        status, info = _post_json(self.base_url, "/api/workspaces/remove-info", {"workspace": handle})
        self.assertEqual((status, info["available"]), (200, False))
        status, removed = _post_json(
            self.base_url,
            "/api/workspaces/remove",
            {"workspace": handle, "delete_index": True},
        )
        self.assertEqual((status, removed["removed"]), (200, True))
        self.assertFalse(stale_workspace.database_path.exists())
        self.assertNotIn(handle, [entry["id"] for entry in self.server.registry.entries()])

    def test_workspace_removal_preflight_and_delete_preserve_media(self) -> None:
        source = self.root / "photo.jpg"
        source_bytes = source.read_bytes()
        status, opened = _post_json(self.base_url, "/api/workspaces/open", {"path": str(self.root)})
        self.assertEqual((status, opened["workspace"]), (200, None))
        status, applied = _post_json(
            self.base_url,
            "/api/workspaces/apply",
            {"path": str(self.root), "configuration": opened["setup"]["configuration"]},
        )
        self.assertEqual(status, 202)
        handle = applied["workspace"]["id"]
        for _ in range(300):
            _, jobs = _get_json(self.base_url, f"/api/jobs?workspace={handle}&limit=10")
            if not any(job["status"] in {"pending", "running"} for job in jobs["jobs"]):
                break
            time.sleep(.1)
        status, info = _post_json(self.base_url, "/api/workspaces/remove-info", {"workspace": handle})
        self.assertEqual(status, 200)
        self.assertGreater(info["index_size_bytes"], 0)
        blocked_job = JobStore(self.server._workspaces[handle]).create("test")
        status, _ = _post_json(self.base_url, "/api/workspaces/remove", {"workspace": handle, "delete_index": True})
        self.assertEqual(status, 400)
        JobStore(self.server._workspaces[handle]).cancel(blocked_job)
        status, removed = _post_json(self.base_url, "/api/workspaces/remove", {"workspace": handle, "delete_index": True})
        self.assertEqual((status, removed["removed"]), (200, True))
        self.assertFalse((self.root / ".archive-index").exists())
        self.assertEqual(source.read_bytes(), source_bytes)


def _write_image(path: Path, size: tuple[int, int]) -> None:
    Image.new("RGB", size, color=(100, 140, 200)).save(path, format="JPEG")


def _get_json(base_url: str, path: str):
    status, body = _get_bytes(base_url, path)
    return status, json.loads(body.decode("utf-8"))


def _get_bytes(base_url: str, path: str):
    status, _, body = _get_response(base_url, path)
    return status, body


def _get_response(base_url: str, path: str, headers: dict[str, str] | None = None):
    try:
        request = Request(base_url + path, method="GET", headers=headers or {})
        with urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except HTTPError as error:
        return error.code, error.headers, error.read()


def _post_json(base_url: str, path: str, body: dict[str, object] | None = None):
    try:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(base_url + path, data=data, method="POST")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
