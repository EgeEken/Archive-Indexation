from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from PIL import Image

from archive_index.api.server import WorkspaceHTTPServer, _pick_workspace_path
from archive_index.indexing.media_pipeline import index_workspace as run_index_workspace
from archive_index.indexing.grouping import build_groups, extract_visual_features
from archive_index.indexing.recommendation import build_recommendations
from archive_index.indexing.scanner import scan
from archive_index.jobs.engine import JobStore
from archive_index.workspace import Workspace
from archive_index.media.quality_provider import LegacyPillowProvider, OffQualityProvider


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

    def test_home_summary_filters_and_pagination(self) -> None:
        status, home = _get_json(self.base_url, "/api/workspace")
        self.assertEqual(status, 200)
        self.assertEqual((home["assets"], home["online_files"]), (3, 3))

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
        self.assertIn(b"page-size", html)
        self.assertIn(b"Choose folder", html)
        status, css = _get_bytes(self.base_url, "/app.css")
        self.assertEqual(status, 200)
        self.assertIn(b"aspect-ratio: 1 / 1", css)
        status, js = _get_bytes(self.base_url, "/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b">Groups<", html)
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
        self.assertIn("aria-label=\"Previous page\"".encode(), html)
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
        for label in (b"All", b"Representatives", b"Recommended", b"Selected", b"Rejected", b"Undecided"):
            self.assertIn(b"data-selection-filter=\"" + label.lower() + b"\"", html)
        self.assertIn(b"review-filter-buttons", html)
        self.assertIn(b"function formatCapture", js)
        self.assertIn(b"slice(1, 3)", js)
        self.assertIn(b'data-detail-thumbnail', js)
        self.assertIn(b'renderDetails(state.viewerDetail', js)
        self.assertIn(b'params.set("selection", state.selectionFilter)', js)

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
        self.assertEqual(physical["components"]["metadata"]["status"], "complete")
        self.assertEqual(physical["components"]["thumbnail"]["status"], "complete")
        self.assertEqual(physical["components"]["quality"]["status"], "complete")
        self.assertIsNotNone(physical["quality_score"])
        self.assertIsNotNone(physical["original_url"])
        self.assertIsNotNone(physical["thumbnail_url"])

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

        status, filtered = _get_json(self.base_url, "/api/groups?q=root.jpg")
        self.assertEqual((status, filtered["total"], len(filtered["groups"][0]["members"])), (200, 1, 1))
        status, video_groups = _get_json(self.base_url, "/api/groups?media_type=video")
        self.assertEqual((status, video_groups["total"], video_groups["empty_reason"]), (200, 0, "Video grouping is not supported yet."))
        group_id = groups["groups"][0]["group_id"]
        status, location = _get_json(self.base_url, f"/api/groups/locate?group_id={group_id}")
        self.assertEqual((status, location["found"], location["page"]), (200, True, 1))

        status, representatives = _get_json(self.base_url, "/api/assets?representatives=1")
        self.assertEqual(status, 200)
        self.assertEqual(representatives["total"], 2)
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
        self.assertEqual(status, 200)
        handle = opened["workspace"]["id"]
        self.assertIsNotNone(opened["job_id"])
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

    def test_folder_picker_returns_helper_output_and_cancel_is_empty(self) -> None:
        completed = type("Completed", (), {"returncode": 0, "stdout": "C:\\Photos\\Archive\n", "stderr": ""})()
        with patch("archive_index.api.server.subprocess.run", return_value=completed):
            self.assertEqual(_pick_workspace_path(), "C:\\Photos\\Archive")
        completed.stdout = "\n"
        with patch("archive_index.api.server.subprocess.run", return_value=completed):
            self.assertEqual(_pick_workspace_path(), "")

    def test_workspace_removal_preflight_and_delete_preserve_media(self) -> None:
        source = self.root / "photo.jpg"
        source_bytes = source.read_bytes()
        status, opened = _post_json(self.base_url, "/api/workspaces/open", {"path": str(self.root)})
        self.assertEqual(status, 200)
        handle = opened["workspace"]["id"]
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
