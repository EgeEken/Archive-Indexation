from __future__ import annotations

import math
import json
import re
import struct
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from archive_index.api.server import WorkspaceHTTPServer
from archive_index.app_state import WorkspaceRegistry, workspace_id
from archive_index.indexing.grouping import build_groups, extract_visual_features
from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.projection import build_semantic_projection
from archive_index.indexing.scanner import scan
from archive_index.media.quality_provider import OffQualityProvider
from archive_index.workspace import Workspace


@unittest.skipUnless(sync_playwright is not None, "Playwright is not installed")
class BrowserE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory(prefix="archive-index-e2e-")
        root = Path(cls.temporary_directory.name)
        cls.main_root = root / "indexed-workspace"
        cls.setup_root = root / "setup-workspace"
        cls.offline_root = root / "offline-workspace"
        cls._create_main_fixtures(cls.main_root)
        cls._create_setup_fixtures(cls.setup_root)
        cls._create_image(cls.offline_root / "initial.jpg", (120, 80), (70, 140, 220))
        cls.main_workspace = cls._build_workspace(cls.main_root, semantic=True)
        cls.offline_workspace = cls._build_workspace(cls.offline_root, semantic=False)
        cls.main_handle = workspace_id(cls.main_workspace)
        cls.offline_handle = workspace_id(cls.offline_workspace)
        cls.registry_path = root / "registry.json"
        registry = WorkspaceRegistry(cls.registry_path)
        registry.add(cls.main_workspace)
        registry.add(cls.offline_workspace)

        cls.server = WorkspaceHTTPServer(("127.0.0.1", 0), registry_path=cls.registry_path)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage"],
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
        self.page = self.context.new_page()
        self.browser_log: list[str] = []
        self.page.on("console", lambda message: self.browser_log.append(f"console {message.type}: {message.text}"))
        self.page.on("pageerror", lambda error: self.browser_log.append(f"pageerror: {error}"))
        self.page.on("requestfailed", lambda request: self.browser_log.append(f"requestfailed {request.url}: {request.failure}"))

    def tearDown(self) -> None:
        result = self._outcome.result
        failed = any(test is self for test, _ in result.failures + result.errors)
        if failed:
            output = Path(__file__).parents[2] / "test-results" / "e2e"
            output.mkdir(parents=True, exist_ok=True)
            name = self.id().rsplit(".", 1)[-1]
            self.page.screenshot(path=str(output / f"{name}.png"), full_page=True)
            (output / f"{name}.log").write_text("\n".join(self.browser_log), encoding="utf-8")
            self.context.tracing.stop(path=str(output / f"{name}.zip"))
        else:
            self.context.tracing.stop()
        self.context.close()

    @staticmethod
    def _create_image(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", size, color).save(path, format="JPEG", quality=92)

    @classmethod
    def _create_main_fixtures(cls, root: Path) -> None:
        cls._create_image(root / "alpha.jpg", (160, 100), (220, 80, 80))
        cls._create_image(root / "nested" / "portrait.jpg", (80, 140), (70, 150, 220))
        cls._create_image(root / "nested" / "child" / "child.jpg", (140, 80), (80, 190, 110))
        cls._create_image(root / "group" / "group-a.jpg", (120, 80), (220, 180, 60))
        cls._create_image(root / "group" / "group-b.jpg", (120, 80), (220, 180, 60))

    @classmethod
    def _create_setup_fixtures(cls, root: Path) -> None:
        cls._create_image(root / "setup-root.jpg", (100, 70), (100, 100, 180))
        cls._create_image(root / "nested" / "child.jpg", (70, 100), (180, 100, 100))
        root.mkdir(parents=True, exist_ok=True)
        (root / "clip.mp4").write_bytes(b"test-only non-media fixture")

    @classmethod
    def _build_workspace(cls, root: Path, *, semantic: bool) -> Workspace:
        workspace = Workspace.create(root)
        configuration = workspace.configuration()
        configuration.update(
            {
                "quality_enabled": False,
                "rendered_quality_provider": "off",
                "raw_quality_provider": "off",
                "video_quality_enabled": False,
                "video_processing_enabled": False,
                "semantic_search_enabled": semantic,
                "include_videos_in_semantic_search": False,
            }
        )
        workspace.apply_configuration(configuration)
        scan(workspace)
        index_workspace(workspace, components=("metadata", "thumbnail"), quality_provider=OffQualityProvider())
        with workspace.transaction() as connection:
            rows = connection.execute(
                "SELECT logical_asset_id, relative_path FROM physical_file ORDER BY relative_path"
            ).fetchall()
            gps = {
                "alpha.jpg": (48.792146, 2.369163),
                "portrait.jpg": (41.008238, 28.978359),
                "child.jpg": (40.712776, -74.005974),
            }
            for index, row in enumerate(rows):
                capture = f"2026-01-01T12:00:{index:02d}"
                connection.execute(
                    "UPDATE logical_asset SET capture_time = ?, capture_time_kind = 'exif_local_unknown' WHERE id = ?",
                    (capture, row["logical_asset_id"]),
                )
                filename = Path(row["relative_path"]).name
                if filename in gps:
                    latitude, longitude = gps[filename]
                    connection.execute(
                        "UPDATE physical_file SET metadata_json = ? WHERE logical_asset_id = ?",
                        (json.dumps({"gps": {"latitude": latitude, "longitude": longitude}}), row["logical_asset_id"]),
                    )
        if semantic:
            extract_visual_features(workspace, workers=1)
            build_groups(workspace)
            cls._seed_embeddings(workspace)
            build_semantic_projection(workspace)
        return workspace

    @staticmethod
    def _seed_embeddings(workspace: Workspace) -> None:
        provider = "openclip-b16-datacomp-xl"
        model_version = "e2e-controlled"
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with workspace.transaction() as connection:
            connection.execute(
                "INSERT INTO embedding_run(id, provider, model_id, model_version, embedding_dimension, settings_json, status, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, 'complete', ?, ?)",
                (run_id, provider, "e2e-controlled", model_version, 2, "{}", now, now),
            )
            rows = connection.execute(
                "SELECT pf.id, pf.logical_asset_id, pf.filename FROM physical_file AS pf WHERE pf.in_scope = 1 AND pf.media_type = 'image' ORDER BY pf.relative_path"
            ).fetchall()
            for row in rows:
                values = (1.0, 0.0) if row["filename"] == "alpha.jpg" else (0.6, 0.8) if row["filename"] == "child.jpg" else (0.0, 1.0)
                blob = _float16_blob(values)
                fingerprint = "e2e-controlled"
                connection.execute(
                    """
                    INSERT INTO component_state(physical_file_id, component, status, algorithm, version, settings_json, input_fingerprint, completed_at)
                    VALUES (?, ?, 'complete', 'semantic-embedding', ?, '{}', ?, ?)
                    ON CONFLICT(physical_file_id, component) DO UPDATE SET status = 'complete', algorithm = excluded.algorithm, version = excluded.version, settings_json = excluded.settings_json, input_fingerprint = excluded.input_fingerprint, completed_at = excluded.completed_at, error_message = NULL
                    """,
                    (row["id"], f"embedding:{provider}", model_version, fingerprint, now),
                )
                connection.execute(
                    """
                    INSERT INTO logical_asset_embedding(run_id, logical_asset_id, source_physical_file_id, source_kind, input_fingerprint, embedding, embedding_dimension, created_at, updated_at)
                    VALUES (?, ?, ?, 'image', ?, ?, 2, ?, ?)
                    """,
                    (run_id, row["logical_asset_id"], row["id"], fingerprint, blob, now, now),
                )
            connection.execute(
                "UPDATE workspace_embedding SET active_provider = ?, active_run_id = ?, updated_at = ? WHERE id = 1",
                (provider, run_id, now),
            )

    def _open_home(self) -> None:
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.page.locator("#home-view").wait_for(state="visible")
        self.page.locator("article.recent-card").first.wait_for()

    def _open_main(self) -> None:
        self.page.goto(f"{self.base_url}/?workspace={self.main_handle}", wait_until="domcontentloaded")
        self.page.locator("#workspace-view").wait_for(state="visible")
        self.page.locator(".photo-card").first.wait_for(timeout=15000)

    def _wait_index_idle(self) -> None:
        deadline = time.monotonic() + 90
        observed_active = False
        idle_since = None
        while time.monotonic() < deadline:
            response = self.page.request.get(f"{self.base_url}/api/jobs?workspace={self.offline_handle}&limit=10")
            self.assertEqual(response.status, 200)
            jobs = response.json()["jobs"]
            active = any(job["status"] in {"pending", "running"} for job in jobs)
            observed_active |= active
            if active:
                idle_since = None
            elif observed_active:
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= 2:
                    break
            self.page.wait_for_timeout(250)
        self.assertTrue(observed_active, "indexing job did not become active")
        self.assertFalse(active, "indexing job did not become idle")
        self.page.locator(".photo-card").first.wait_for(timeout=15000)

    def test_home_cold_load_header_card_thumbnail_and_open(self) -> None:
        self._open_home()
        self.assertTrue(self.page.locator("#workspace-crumb").get_attribute("class").find("hidden") >= 0)
        card = self.page.locator("article.recent-card").filter(has_text="indexed-workspace")
        self.assertEqual(card.count(), 1)
        thumbnail = card.locator("img.recent-thumb")
        thumbnail.wait_for(state="visible")
        self.assertGreater(thumbnail.evaluate("image => image.naturalWidth"), 0)
        card.get_by_role("button", name="Open").click()
        self.page.wait_for_url(re.compile(r"workspace=" + re.escape(self.main_handle)))
        self.page.locator("#workspace-view").wait_for(state="visible")
        self.page.locator(".photo-card").first.wait_for(timeout=15000)
        self.assertEqual(self.page.locator(".photo-card").count(), 5)

    def test_configure_folder_semantics_controls_and_planner(self) -> None:
        self._open_home()
        self.page.locator("#workspace-path").fill(str(self.setup_root))
        self.page.get_by_role("button", name="Open workspace").click()
        self.page.locator("#setup-view").wait_for(state="visible")
        self.page.locator("#setup-index-summary").wait_for()
        self.page.locator("#setup-video-section").wait_for(state="visible", timeout=15000)
        self.assertTrue(self.page.locator("#setup-quality").is_checked())
        self.assertTrue(self.page.locator("#setup-semantic-search").is_checked())
        self.assertFalse(self.page.locator("#setup-video-section").evaluate("node => node.classList.contains('hidden')"))
        before = self.page.locator("#setup-index-summary").inner_text()

        root_input = self.page.locator('input[data-folder-path=""]')
        child_input = self.page.locator('input[data-folder-path="nested"]')
        root_input.uncheck()
        self.page.wait_for_function(
            "expected => document.querySelector('#setup-index-summary').textContent !== expected",
            arg=before,
        )
        self.assertFalse(root_input.is_checked())
        self.assertTrue(child_input.is_checked())

        self.page.locator("#setup-quality").uncheck()
        self.page.locator("#setup-quality-details").wait_for(state="hidden")
        self.assertFalse(self.page.locator("#setup-embedding-status").evaluate("node => node.classList.contains('hidden')"))
        self.page.locator("#setup-semantic-search").uncheck()
        self.page.locator("#setup-embedding-status").wait_for(state="hidden")
        self.page.locator("#setup-video-section").wait_for(state="hidden")
        self.page.locator("#setup-semantic-search").check()
        self.page.locator("#setup-video-section").wait_for(state="visible")
        self.page.locator("#setup-video-participation").uncheck()
        self.page.locator("#setup-video-sampling").wait_for(state="hidden")
        self.page.locator("#setup-cancel").click()
        self.page.locator("#home-view").wait_for(state="visible")

    def test_gallery_startup_renders_media_without_blank_top_spacer(self) -> None:
        self._open_main()
        first_card = self.page.locator(".photo-card").first
        self.assertTrue(first_card.is_visible())
        top_spacer = self.page.locator("#gallery .window-spacer").first
        self.assertEqual(top_spacer.evaluate("node => Math.round(parseFloat(getComputedStyle(node).height))"), 0)
        first_image = first_card.locator("img.thumb")
        first_image.wait_for(state="visible")
        self.assertGreater(first_image.evaluate("image => image.naturalWidth"), 0)

    def test_browser_folder_filename_layout_filters_and_sorting(self) -> None:
        self._open_main()
        self.page.locator('[data-sort="filename"]').click()
        self.page.wait_for_function(
            "expected => [...document.querySelectorAll('.photo-card .filename')].map(node => node.textContent.trim()).join('|') === expected",
            arg="portrait.jpg|group-b.jpg|group-a.jpg|child.jpg|alpha.jpg",
            timeout=15000,
        )
        descending = self.page.locator(".photo-card .filename").all_text_contents()
        self.page.locator("#direction-button").click()
        self.page.wait_for_function(
            "expected => [...document.querySelectorAll('.photo-card .filename')].map(node => node.textContent.trim()).join('|') === expected",
            arg="alpha.jpg|child.jpg|group-a.jpg|group-b.jpg|portrait.jpg",
            timeout=15000,
        )
        ascending = self.page.locator(".photo-card .filename").all_text_contents()
        self.assertEqual(ascending, sorted(ascending, key=str.casefold))
        self.assertEqual(descending, list(reversed(ascending)))

        self.page.locator("#clear-filters").click()
        self.page.locator("#folder-open").click()
        self.page.get_by_role("button", name="Deselect all").click()
        nested = self.page.locator("label.folder-check").filter(has_text=re.compile(r"^nested"))
        nested.locator("input").check()
        nested_child = self.page.locator("label.folder-check").filter(has_text=re.compile(r"^child"))
        nested_child.locator("input").check()
        root = self.page.locator("label.folder-check").filter(has_text=re.compile(r"^Workspace root"))
        self.assertFalse(root.locator("input").is_checked())
        self.assertTrue(nested_child.locator("input").is_checked())
        self.page.get_by_role("button", name="Close folders").click()
        self.page.locator(".photo-card", has_text="portrait.jpg").first.wait_for()
        self.assertEqual(self.page.locator(".photo-card", has_text="portrait.jpg").count(), 1)
        self.assertEqual(self.page.locator(".photo-card", has_text="child.jpg").count(), 1)
        self.page.wait_for_function(
            "() => ![...document.querySelectorAll('.photo-card .filename')].some(node => node.textContent.trim() === 'alpha.jpg')",
            timeout=15000,
        )
        self.assertEqual(self.page.locator(".photo-card", has_text="alpha.jpg").count(), 0)

    def test_manual_review_survives_refresh(self) -> None:
        self._open_main()
        card = self.page.locator(".photo-card", has_text="alpha.jpg").first
        card.locator('[data-decision="selected"]').click()
        selected = self.page.locator(".photo-card", has_text="alpha.jpg").first.locator(".select").filter(has_text="Selected")
        selected.wait_for(timeout=15000)
        self.page.reload(wait_until="domcontentloaded")
        self.page.locator(".photo-card", has_text="alpha.jpg").first.wait_for()
        refreshed = self.page.locator(".photo-card", has_text="alpha.jpg").first
        self.assertIn("Selected", refreshed.locator(".select").inner_text())

    def test_details_viewer_navigation_and_main_close(self) -> None:
        self._open_main()
        card = self.page.locator(".photo-card").first
        filename = card.locator(".filename").inner_text()
        card.locator(".info-button").click()
        self.page.locator("#details[open]").wait_for()
        self.assertIn(filename, self.page.locator("#details-title").inner_text())
        self.page.locator("#details [data-detail-thumbnail]").click()
        self.page.locator("#viewer[open]").wait_for()
        self.assertIn(filename, self.page.locator("#viewer-title").inner_text())
        self.page.locator("#viewer-next").click()
        self.assertNotIn(filename, self.page.locator("#viewer-title").inner_text())
        self.page.locator("#viewer-previous").click()
        self.assertIn(filename, self.page.locator("#viewer-title").inner_text())
        self.page.locator("#viewer-close").click()
        self.page.locator("#viewer").wait_for(state="hidden")
        self.assertTrue(self.page.locator("#gallery").is_visible())
        self.assertEqual(self.page.locator("dialog[open]").count(), 0)

    def test_similar_weaker_results_navigation_and_close_variants(self) -> None:
        self._open_main()
        self.page.locator(".photo-card", has_text="alpha.jpg").first.click(position={"x": 30, "y": 30})
        self.page.locator("#viewer[open]").wait_for()
        self.page.locator("#viewer-similar").click()
        self.page.locator("#similar-gallery").wait_for(state="visible")
        self.page.get_by_text("No strongly similar assets found").wait_for()
        self.page.locator("#similar-more").wait_for()
        self.page.locator("#similar-more").click()
        self.page.locator(".similar-result").first.wait_for()
        self.assertEqual(self.page.locator(".similar-result").count(), 4)
        self.page.locator(".similar-result").first.click()
        self.assertIn("child.jpg", self.page.locator("#viewer-title").inner_text())
        self.assertEqual(self.page.locator("#viewer-count").inner_text(), "2 of 5")
        self.page.locator("#viewer-previous").click()
        self.assertIn("alpha.jpg", self.page.locator("#viewer-title").inner_text())
        self.page.locator("#viewer-next").click()
        self.assertIn("child.jpg", self.page.locator("#viewer-title").inner_text())
        self.page.locator("#viewer-close").click()
        self.page.locator("#viewer").wait_for(state="hidden")
        self.assertTrue(self.page.locator("#similar-gallery").evaluate("node => node.classList.contains('hidden')"))

        self.page.locator(".photo-card", has_text="alpha.jpg").first.click(position={"x": 30, "y": 30})
        self.page.locator("#viewer-similar").click()
        self.page.locator("#similar-more").click()
        self.page.locator(".similar-result").first.click()
        self.page.locator("#similar-close").click()
        self.assertTrue(self.page.locator("#viewer[open]").is_visible())
        self.assertIn("alpha.jpg", self.page.locator("#viewer-title").inner_text())

    def test_groupings_render_member_and_viewer(self) -> None:
        self._open_main()
        self.page.locator("#groups-view-toggle").click()
        self.page.locator(".group-row").first.wait_for()
        group = self.page.locator(".group-row").filter(has_text="2 members").first
        self.assertEqual(group.count(), 1)
        group.locator(".group-photo").first.click()
        self.page.locator("#viewer[open]").wait_for()
        self.assertIn("group-", self.page.locator("#viewer-title").inner_text())
        self.page.locator("#viewer-close").click()
        self.page.locator("#groups-view").wait_for(state="visible")

    def test_geo_map_renders_count_and_asset_preview(self) -> None:
        self._open_main()
        self.page.get_by_role("tab", name="Geo Map").click()
        self.page.locator("#geo-view").wait_for(state="visible")
        self.page.locator("#geo-canvas").wait_for()
        self.page.wait_for_function("() => Number(document.querySelector('#geo-canvas').dataset.pointCount) === 3")
        self.page.wait_for_function("() => document.querySelector('#geo-canvas').dataset.firstTargetX !== undefined")
        self.assertIn("3 geotagged assets · 5 filtered assets", self.page.locator("#geo-status").inner_text())
        canvas = self.page.locator("#geo-canvas")
        canvas.click(position={
            "x": float(canvas.get_attribute("data-first-target-x")),
            "y": float(canvas.get_attribute("data-first-target-y")),
        })
        self.page.locator("#viewer[open]").wait_for(timeout=15000)
        self.assertIn(".jpg", self.page.locator("#viewer-title").inner_text())
        self.page.locator("#viewer-close").click()
        self.page.locator("#viewer").wait_for(state="hidden")

    def test_timeline_renders_and_zoom_changes_range(self) -> None:
        self._open_main()
        self.page.get_by_role("tab", name="Timeline").click()
        self.page.locator("#timeline-view").wait_for(state="visible")
        self.page.wait_for_function("() => Number(document.querySelector('#timeline-canvas').dataset.pointCount) === 5")
        self.page.wait_for_function("() => document.querySelector('#timeline-canvas').dataset.firstTargetX !== undefined")
        self.assertIn("5 timed assets · 5 filtered assets", self.page.locator("#timeline-status").inner_text())
        canvas = self.page.locator("#timeline-canvas")
        before = float(canvas.get_attribute("data-view-scale"))
        self.page.locator("#timeline-zoom-in").click()
        self.page.wait_for_function("before => Number(document.querySelector('#timeline-canvas').dataset.viewScale) > before", arg=before)
        self.page.locator("#timeline-time-mode").select_option("file_created")
        self.page.wait_for_function("() => document.querySelector('#timeline-canvas').dataset.timeMode === 'file_created'")
        self.page.wait_for_function("() => document.querySelector('#timeline-canvas').dataset.firstTargetX !== undefined")
        canvas.click(position={
            "x": float(canvas.get_attribute("data-first-target-x")),
            "y": float(canvas.get_attribute("data-first-target-y")),
        })
        self.page.locator("#viewer[open]").wait_for(timeout=15000)
        self.assertIn(".jpg", self.page.locator("#viewer-title").inner_text())

    def test_vector_cloud_uses_stored_projection_and_selects_asset(self) -> None:
        self.page.goto(f"{self.base_url}/?workspace={self.main_handle}&view=vector", wait_until="domcontentloaded")
        self.page.locator("#workspace-view").wait_for(state="visible")
        self.page.locator("#vector-view").wait_for(state="visible")
        self.page.wait_for_function("() => Number(document.querySelector('#vector-canvas').dataset.pointCount) === 5")
        self.page.wait_for_function("() => document.querySelector('#vector-canvas').dataset.firstTargetX !== undefined")
        self.assertIn("5 projected assets · 5 filtered assets", self.page.locator("#vector-status").inner_text())
        self.assertFalse(any("openclip" in entry.lower() or "model" in entry.lower() for entry in self.browser_log))
        canvas = self.page.locator("#vector-canvas")
        canvas.click(position={
            "x": float(canvas.get_attribute("data-first-target-x")),
            "y": float(canvas.get_attribute("data-first-target-y")),
        })
        self.page.locator("#viewer[open]").wait_for(timeout=15000)
        self.assertIn(".jpg", self.page.locator("#viewer-title").inner_text())

    def test_reindex_add_remove_offline_cleanup_preserves_fixture_boundary(self) -> None:
        self.page.goto(f"{self.base_url}/?workspace={self.offline_handle}", wait_until="domcontentloaded")
        self.page.locator(".photo-card", has_text="initial.jpg").first.wait_for()
        added = self.offline_root / "added.jpg"
        self._create_image(added, (90, 90), (180, 70, 160))
        self.page.locator("#index").click()
        self._wait_index_idle()
        self.page.locator(".photo-card", has_text="added.jpg").first.wait_for()

        initial = self.offline_root / "initial.jpg"
        initial.unlink()
        self.page.locator("#index").click()
        self._wait_index_idle()
        offline_card = self.page.locator(".photo-card", has_text="initial.jpg").first
        offline_card.get_by_text("Offline", exact=True).wait_for(timeout=15000)
        self.assertFalse(initial.exists())

        self.page.locator("#configure-workspace").click()
        self.page.locator("#setup-view").wait_for(state="visible")
        self.page.locator("#forget-offline-button").click()
        self.page.locator("#offline-dialog[open]").wait_for()
        self.assertIn("This does not delete files from disk.", self.page.locator("#offline-dialog").inner_text())
        self.page.locator("#offline-forget").click()
        self.page.get_by_text("1 offline media entry forgotten.").wait_for()
        self.assertFalse(initial.exists())
        response = self.page.request.get(f"{self.base_url}/api/browser?workspace={self.offline_handle}&limit=60")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["total"], 1)


def _float16_blob(values: tuple[float, ...]) -> bytes:
    norm = math.sqrt(sum(value * value for value in values))
    return struct.pack("<" + "e" * len(values), *(value / norm for value in values))


if __name__ == "__main__":
    unittest.main()
