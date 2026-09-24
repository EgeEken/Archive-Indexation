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

import imagecodecs
import numpy as np
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
from archive_index.indexing.reconciliation import reconcile_workspace
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
        cls.raw_offline_root = root / "raw-offline-workspace"
        cls.raw_online_root = root / "raw-online-workspace"
        cls._create_main_fixtures(cls.main_root)
        cls._create_setup_fixtures(cls.setup_root)
        cls._create_image(cls.offline_root / "initial.jpg", (120, 80), (70, 140, 220))
        cls._create_image(cls.raw_offline_root / "initial.jpg", (120, 80), (70, 140, 220))
        (cls.raw_offline_root / "initial.arw").write_bytes(b"offline raw fixture")
        cls._create_image(cls.raw_online_root / "initial.jpg", (120, 80), (70, 140, 220))
        (cls.raw_online_root / "initial.arw").write_bytes(b"online raw fixture")
        cls.main_workspace = cls._build_workspace(cls.main_root, semantic=True)
        cls.offline_workspace = cls._build_workspace(cls.offline_root, semantic=False)
        cls.raw_offline_workspace = cls._build_workspace(cls.raw_offline_root, semantic=False)
        cls.raw_online_workspace = cls._build_workspace(cls.raw_online_root, semantic=False)
        with cls.raw_online_workspace.transaction() as connection:
            connection.execute("UPDATE physical_file SET is_online = 1 WHERE relative_path = 'initial.arw'")
        cls.main_handle = workspace_id(cls.main_workspace)
        cls.offline_handle = workspace_id(cls.offline_workspace)
        cls.raw_offline_handle = workspace_id(cls.raw_offline_workspace)
        cls.raw_online_handle = workspace_id(cls.raw_online_workspace)
        cls.registry_path = root / "registry.json"
        registry = WorkspaceRegistry(cls.registry_path)
        registry.add(cls.main_workspace)
        registry.add(cls.offline_workspace)
        registry.add(cls.raw_offline_workspace)
        registry.add(cls.raw_online_workspace)

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
        with Image.open(root / "alpha.jpg") as image:
            (root / "alpha.jxl").write_bytes(imagecodecs.jpegxl_encode(np.asarray(image.convert("RGB")), lossless=True))
        (root / "alpha.arw").write_bytes(b"test-only raw fixture")
        cls._create_image(root / "nested" / "portrait.jpg", (80, 140), (70, 150, 220))
        with Image.open(root / "nested" / "portrait.jpg") as image:
            (root / "nested" / "portrait.jxl").write_bytes(imagecodecs.jpegxl_encode(np.asarray(image.convert("RGB")), lossless=True))
        cls._create_image(root / "nested" / "child" / "child.jpg", (140, 80), (80, 190, 110))
        cls._create_image(root / "group" / "group-a.jpg", (120, 80), (220, 180, 60))
        cls._create_image(root / "group" / "group-b.jpg", (120, 80), (220, 179, 60))

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
                "include_raw": False,
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
        with workspace.transaction() as connection:
            connection.execute("UPDATE physical_file SET in_scope = 0 WHERE extension = '.arw'")
        index_workspace(workspace, components=("metadata", "thumbnail"), quality_provider=OffQualityProvider())
        reconcile_workspace(workspace)
        if (root / "initial.arw").exists():
            with workspace.transaction() as connection:
                jpeg = connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'initial.jpg'").fetchone()
                raw = connection.execute("SELECT logical_asset_id FROM physical_file WHERE relative_path = 'initial.arw'").fetchone()
                if jpeg and raw:
                    if jpeg["logical_asset_id"] != raw["logical_asset_id"]:
                        connection.execute("UPDATE physical_file SET logical_asset_id = ? WHERE relative_path = 'initial.arw'", (jpeg["logical_asset_id"],))
                    connection.execute("UPDATE physical_file SET in_scope = 1, is_online = 0 WHERE relative_path = 'initial.arw'")
                    if jpeg["logical_asset_id"] != raw["logical_asset_id"]:
                        connection.execute("DELETE FROM logical_asset WHERE id = ?", (raw["logical_asset_id"],))
        with workspace.transaction() as connection:
            rows = connection.execute(
                "SELECT logical_asset_id, relative_path FROM physical_file ORDER BY relative_path"
            ).fetchall()
            gps = {
                "alpha.jpg": (48.792146, 2.369163),
                "portrait.jpg": (41.008238, 28.978359),
                "child.jpg": (41.008238, 28.978359),
            }
            capture_times = [
                "2026-01-10T10:00:00",
                "2026-01-25T10:00:00",
                "2026-01-25T10:00:00",
                "2026-02-05T12:00:00",
                "2026-03-01T09:30:00",
            ]
            capture_by_stem = {}
            for index, row in enumerate(rows):
                stem = Path(row["relative_path"]).stem.casefold()
                if stem not in capture_by_stem:
                    capture_by_stem[stem] = capture_times[len(capture_by_stem) % len(capture_times)]
                capture = capture_by_stem[stem]
                connection.execute(
                    "UPDATE logical_asset SET capture_time = ?, capture_time_kind = 'exif_local_unknown' WHERE id = ?",
                    (capture, row["logical_asset_id"]),
                )
                connection.execute(
                    "UPDATE physical_file SET file_created_time = ? WHERE logical_asset_id = ?",
                    (f"2025-12-01T08:00:{index:02d}", row["logical_asset_id"]),
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
            seen_assets = set()
            for row in rows:
                if row["logical_asset_id"] in seen_assets:
                    continue
                seen_assets.add(row["logical_asset_id"])
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

    def _wait_index_idle(self, known_job_ids: set[str]) -> None:
        deadline = time.monotonic() + 90
        observed_run = False
        idle_since = None
        while time.monotonic() < deadline:
            response = self.page.request.get(f"{self.base_url}/api/jobs?workspace={self.offline_handle}&limit=10")
            self.assertEqual(response.status, 200)
            jobs = response.json()["jobs"]
            observed_run |= any(job["id"] not in known_job_ids for job in jobs)
            active = any(job["status"] in {"pending", "running"} for job in jobs)
            if active:
                idle_since = None
            elif observed_run:
                idle_since = idle_since or time.monotonic()
                if time.monotonic() - idle_since >= 2:
                    break
            self.page.wait_for_timeout(250)
        self.assertTrue(observed_run, "indexing job was not created")
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

    def test_fullscreen_camera_reaches_all_edges_for_landscape_and_portrait(self) -> None:
        self._open_main()
        for filename in ("alpha.jpg", "portrait.jpg"):
            card = self.page.locator(".photo-card", has_text=filename).first
            card.locator(".thumb").click()
            self.page.locator("#viewer[open]").wait_for()
            pane = self.page.locator("#viewer-media-pane")
            image = self.page.locator("#viewer-media img.viewer-media")
            image.wait_for()
            self.page.wait_for_function("document.querySelector('#viewer-media img.viewer-media')?.complete")
            fit = self.page.evaluate("""() => {
              const frame=document.querySelector('#viewer-media-pane').getBoundingClientRect();
              const image=document.querySelector('#viewer-media img.viewer-media').getBoundingClientRect();
              return {frame:{left:frame.left,right:frame.right,top:frame.top,bottom:frame.bottom},image:{left:image.left,right:image.right,top:image.top,bottom:image.bottom}};
            }""")
            self.assertGreaterEqual(fit["image"]["left"], fit["frame"]["left"] - 1)
            self.assertLessEqual(fit["image"]["right"], fit["frame"]["right"] + 1)
            self.assertGreaterEqual(fit["image"]["top"], fit["frame"]["top"] - 1)
            self.assertLessEqual(fit["image"]["bottom"], fit["frame"]["bottom"] + 1)
            box = pane.bounding_box()
            self.page.evaluate("""({x,y}) => {
              const pane=document.querySelector('#viewer-media-pane');
              for(let i=0;i<22;i++) pane.dispatchEvent(new WheelEvent('wheel',{deltaY:-120,clientX:x,clientY:y,bubbles:true,cancelable:true}));
            }""", {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2})
            self.assertGreater(self.page.locator("#viewer-media img.viewer-media").evaluate("node => new DOMMatrix(getComputedStyle(node).transform).a"), 1)
            image_box = image.bounding_box()
            async_check = """() => {
              const frame=document.querySelector('#viewer-media-pane').getBoundingClientRect();
              const image=document.querySelector('#viewer-media img.viewer-media').getBoundingClientRect();
              return {frame:{left:frame.left,right:frame.right,top:frame.top,bottom:frame.bottom},image:{left:image.left,right:image.right,top:image.top,bottom:image.bottom}};
            }"""
            center_x = image_box["x"] + image_box["width"] / 2
            center_y = image_box["y"] + image_box["height"] / 2
            for dx, dy, edge, boundary in ((10000, 0, "left", "left"), (-10000, 0, "right", "right"), (0, 10000, "top", "top"), (0, -10000, "bottom", "bottom")):
                self.page.mouse.move(center_x, center_y)
                self.page.mouse.down()
                self.page.mouse.move(center_x + dx, center_y + dy, steps=2)
                self.page.mouse.up()
                bounds = self.page.evaluate(async_check)
                camera = self.page.evaluate("() => {const i=document.querySelector('#viewer-media img.viewer-media'); return {transform:i.style.transform,frame:document.querySelector('#viewer-media-pane').getBoundingClientRect().toJSON(),image:i.getBoundingClientRect().toJSON()}}")
                self.assertAlmostEqual(bounds["image"][edge], bounds["frame"][boundary], delta=2, msg=f"{filename}: {edge}; {bounds}; {camera}")
            self.page.locator("#viewer-close").click()
            self.page.locator("#viewer").wait_for(state="hidden")

    def test_portrait_representation_comparison_geometry_and_difference_mode(self) -> None:
        self._open_main()
        self.page.locator(".photo-card", has_text="portrait.jpg").first.locator(".info-button").click()
        self.page.locator("#details[open]").wait_for()
        row = self.page.locator("#details .representation-row", has_text="portrait.jxl").first
        row.locator("[data-representation-view]").click()
        self.page.locator(".comparison-slider").wait_for()
        self.page.wait_for_function("[...document.querySelectorAll('.comparison-slider [data-comparison-image]')].every(image => image.complete && image.naturalWidth > 0)")
        fit = self.page.evaluate("""() => {
          const frame=document.querySelector('[data-comparison-frame]').getBoundingClientRect();
          const images=[...document.querySelectorAll('.comparison-slider [data-comparison-image]')].map(image=>image.getBoundingClientRect());
          return {frame:{left:frame.left,right:frame.right,top:frame.top,bottom:frame.bottom},images:images.map(image=>({left:image.left,right:image.right,top:image.top,bottom:image.bottom}))};
        }""")
        for first, second in zip(fit["images"][0].values(), fit["images"][1].values()):
            self.assertAlmostEqual(first, second, delta=1)
        self.assertEqual(self.page.locator('[data-compare-mode="difference"]').count(), 1)
        self.page.locator('[data-compare-mode="difference"]').click()
        difference = self.page.locator(".comparison-difference-image")
        difference.wait_for()
        self.page.wait_for_function("document.querySelector('.comparison-difference-image')?.complete")
        self.assertTrue(self.page.evaluate("""() => {
          const image=document.querySelector('.comparison-difference-image');
          const canvas=document.createElement('canvas'); canvas.width=image.naturalWidth; canvas.height=image.naturalHeight;
          const context=canvas.getContext('2d'); context.drawImage(image,0,0);
          return context.getImageData(0,0,canvas.width,canvas.height).data.every((value,index)=>index%4===3 || value===0);
        }"""))
        self.page.locator('[data-compare-mode="slider"]').click()
        self.page.locator(".comparison-slider").wait_for()
        self.page.locator("[data-comparison-close]").click()

    def test_file_management_and_representation_comparison_workflow(self) -> None:
        self._open_main()
        self.page.locator("#file-management-button").click()
        self.page.locator("#file-management-dialog[open]").wait_for()
        self.assertIn("Archive cleanup", self.page.locator("#file-management-ruleset-select").inner_text())
        self.assertGreater(self.page.locator(".file-rule-card").count(), 0)
        self.page.locator('[data-file-management-tab="profiles"]').click()
        self.page.locator('[data-file-management-section="profiles"]:not(.hidden)').wait_for()
        self.assertEqual(self.page.locator("[data-profile-preview]").count(), 3)
        self.assertEqual(self.page.locator('.profile-card', has_text="AV1 Archival").locator("[data-profile-preview]").count(), 0)
        self.page.locator('.profile-card', has_text="JXL Balanced").locator("[data-profile-preview]").click()
        self.page.locator("#representation-comparison[open]").wait_for()
        self.assertIn("Compression preset preview", self.page.locator("#representation-comparison").inner_text())
        self.assertIn("JXL Balanced", self.page.locator("#representation-comparison").inner_text())
        self.page.locator('[data-preview-mode="side"]').click()
        self.page.locator(".comparison-stage").wait_for()
        self.page.locator('[data-comparison-close]').click()
        self.page.locator('[data-file-management-tab="rules"]').click()
        self.page.locator('[data-file-management-section="rules"]:not(.hidden)').wait_for()
        dialog_text = self.page.locator("#file-management-dialog").inner_text()
        self.assertNotIn("Plan changes to the files in this archive.", dialog_text)
        self.assertEqual(self.page.locator('[data-rule-field="enabled"]').count(), 0)
        self.assertTrue(self.page.locator(".rule-number").first.inner_text().startswith("Rule "))
        self.assertTrue(self.page.locator(".rule-line").first.inner_text().lstrip().startswith("For"), repr(self.page.locator(".rule-line").first.inner_text()))
        copy_rule = self.page.locator(".file-rule-card").nth(2)
        self.assertEqual(copy_rule.locator(".rule-options-copy [data-rule-field=\"destination\"]").count(), 1)
        self.assertGreater(
            copy_rule.locator(".rule-help").bounding_box()["y"],
            copy_rule.locator("[data-rule-field=\"preserve\"]").bounding_box()["y"],
        )
        self.assertTrue(copy_rule.locator("[data-rule-field=\"preserve\"]").is_checked())
        second_copy_rule = self.page.locator(".file-rule-card").nth(3)
        self.assertTrue(second_copy_rule.locator("[data-rule-field=\"preserve\"]").is_checked())
        self.assertTrue(second_copy_rule.locator("[data-rule-field=\"renameOnConflict\"]").is_checked())
        compression_rule = self.page.locator(".file-rule-card").nth(4)
        self.assertEqual(compression_rule.locator(".rule-options-compress [data-rule-field=\"profileId\"]").count(), 1)
        self.assertEqual(compression_rule.locator(".rule-options-compress [data-rule-field=\"disposition\"]").count(), 1)
        self.assertEqual(compression_rule.locator(".rule-options-compress [data-rule-field=\"inPlace\"]").count(), 1)
        self.assertEqual(compression_rule.locator(".rule-options-compress [data-rule-field=\"renameOnConflict\"]").count(), 1)
        self.assertGreater(
            compression_rule.locator(".rule-options-compress [data-rule-field=\"destination\"]").bounding_box()["y"],
            compression_rule.locator(".rule-options-compress [data-rule-field=\"renameOnConflict\"]").bounding_box()["y"],
        )
        first_rule = self.page.locator(".file-rule-card").first
        first_rule.locator('[data-rule-field="operation"]').select_option("copy")
        self.assertEqual(first_rule.locator(".rule-options-copy [data-rule-field=\"destination\"]").count(), 1)
        self.assertEqual(first_rule.locator(".rule-options-copy [data-rule-field=\"preserve\"]").count(), 1)
        destination = first_rule.locator(".rule-options-copy [data-rule-field=\"destination\"]")
        destination.fill("archive/raw")
        self.assertIn("archive/raw/photos/day1/file.jpg", first_rule.locator("[data-rule-help]").inner_text())
        self.assertTrue(destination.evaluate("node => document.activeElement === node"))
        first_rule.locator('[data-rule-field="preserve"]').uncheck()
        self.assertIn("archive/raw/file.jpg", first_rule.locator("[data-rule-help]").inner_text())
        self.page.locator("#file-management-ruleset-kind").wait_for()
        self.assertEqual(self.page.locator("#file-management-ruleset-kind").inner_text(), "Custom Ruleset")
        self.page.get_by_role("button", name="Analyze plan").click()
        self.page.locator('[data-file-management-section="plan"]:not(.hidden)').wait_for()
        self.page.locator("#file-management-plan-summary").wait_for()
        dialog_text = self.page.locator("#file-management-dialog").inner_text()
        self.assertNotIn("Settings JSON", dialog_text)
        self.assertNotIn("Rules JSON", dialog_text)
        self.assertNotIn("candidate_count", dialog_text)
        self.page.locator("#file-management-close").click()

        self.page.locator(".photo-card", has_text="alpha.jpg").first.locator(".info-button").click()
        self.page.locator("#details[open]").wait_for()
        self.assertGreaterEqual(self.page.locator("#details .representation-row").count(), 2)
        jxl_row = self.page.locator("#details .representation-row", has_text="alpha.jxl").first
        jxl_row.locator('[data-representation-view]').click()
        self.page.locator("#representation-comparison[open]").wait_for()
        self.assertIn("Lineage unknown", self.page.locator("#representation-comparison").inner_text())
        self.page.locator(".comparison-slider").wait_for()
        self.assertEqual(self.page.locator("[data-comparison-target]").count(), 0)
        self.assertEqual(self.page.locator("[data-representation-open-viewer]").count(), 0)
        self.assertEqual(self.page.locator("[data-comparison-metrics] .comparison-metric-mse").count(), 1)
        self.page.get_by_role("button", name="Side by side").click()
        self.page.locator(".comparison-stage").wait_for()
        self.page.get_by_role("button", name="Slider").click()
        self.page.locator(".comparison-slider").wait_for()
        self.page.locator("[data-comparison-close]").click()
        self.page.locator("#details-close").click()

    def test_offline_raw_representation_short_circuits_viewer(self) -> None:
        self.page.goto(f"{self.base_url}/?workspace={self.raw_offline_handle}", wait_until="domcontentloaded")
        self.page.locator(".photo-card", has_text="initial.jpg").first.wait_for()
        requests: list[str] = []
        self.page.on("request", lambda request: requests.append(request.url))
        self.page.locator(".photo-card", has_text="initial.jpg").first.locator(".info-button").click()
        self.page.locator("#details[open]").wait_for()
        raw_row = self.page.locator("#details .representation-row", has_text="initial.arw").first
        self.assertIn("Offline", raw_row.inner_text())
        raw_row.locator('[data-representation-view]').click()
        self.page.locator("#representation-comparison[open]").wait_for()
        self.assertIn("This representation is offline.", self.page.locator("#representation-comparison").inner_text())
        self.assertEqual(self.page.locator("[data-raw-image]").count(), 0)
        self.assertEqual(self.page.locator("[data-raw-exposure]").count(), 0)
        self.assertEqual(self.page.locator("[data-compare-mode]").count(), 0)
        self.assertFalse(any("raw-development-preview" in url for url in requests))

    def test_raw_loading_indicator_stays_attached_across_latest_request(self) -> None:
        self.page.add_init_script(
            """
            (() => {
              const originalFetch = window.fetch.bind(window);
              window.__rawResolvers = [];
              window.__rawUrls = [];
              window.fetch = (input, init) => {
                if (!String(input).includes("raw-development-preview")) return originalFetch(input, init);
                window.__rawUrls.push(String(input));
                return new Promise(resolve => window.__rawResolvers.push(resolve));
              };
              window.__resolveRaw = index => window.__rawResolvers[index](new Response(new Blob([], {type: "image/jpeg"}), {status: 200, headers:{"X-RAW-White-Balance":"Camera/as-shot WB"}}));
            })();
            """
        )
        self.page.goto(f"{self.base_url}/?workspace={self.raw_online_handle}", wait_until="domcontentloaded")
        self.page.locator(".photo-card", has_text="initial.jpg").first.wait_for()
        self.page.locator(".photo-card", has_text="initial.jpg").first.locator(".info-button").click()
        self.page.locator("#details[open]").wait_for()
        self.page.locator("#details .representation-row", has_text="initial.arw").first.locator("[data-representation-view]").click()
        self.page.locator("#representation-comparison[open]").wait_for()
        loading = self.page.locator("[data-raw-loading]")
        self.assertEqual(loading.count(), 1)
        self.assertEqual(loading.evaluate("node => node.parentElement.contains(node)"), True)
        self.assertTrue(loading.is_visible())

        exposure = self.page.locator("[data-raw-exposure]")
        exposure.evaluate("node => { node.value = '1'; node.dispatchEvent(new Event('input', {bubbles: true})); }")
        self.page.wait_for_function("window.__rawResolvers.length === 2")
        second_params = self.page.evaluate("new URL(window.__rawUrls[1], location.origin).searchParams.toString()")
        for name, value in (("exposure_ev", "1"), ("white_balance", "0"), ("saturation", "100"), ("highlights", "0"), ("shadows", "0")):
            self.assertIn(f"{name}={value}", second_params)
        self.page.evaluate("window.__resolveRaw(0)")
        self.assertTrue(loading.is_visible())
        self.page.evaluate("window.__resolveRaw(1)")
        self.page.wait_for_function("document.querySelector('[data-raw-loading]').classList.contains('hidden')")
        self.assertFalse(loading.is_visible())
        self.assertIn("+1.00 EV", self.page.locator("[data-raw-exposure-value]").inner_text())
        self.page.locator("[data-raw-white-balance]").evaluate("node => { node.value = '35'; node.dispatchEvent(new Event('input', {bubbles: true})); }")
        self.page.wait_for_function("window.__rawResolvers.length === 3")
        self.assertIn("white_balance=35", self.page.evaluate("new URL(window.__rawUrls[2], location.origin).searchParams.toString()"))
        self.page.evaluate("window.__resolveRaw(2)")
        self.page.wait_for_function("document.querySelector('[data-raw-wb-status]').textContent === 'Camera/as-shot WB'")
        self.page.locator("[data-raw-reset]").click()
        self.page.wait_for_function("window.__rawResolvers.length === 4")
        reset_params = self.page.evaluate("new URL(window.__rawUrls[3], location.origin).searchParams.toString()")
        self.assertEqual(self.page.locator("[data-raw-saturation-value]").inner_text(), "100%")
        self.assertIn("white_balance=0", reset_params)

    def test_similar_weaker_results_navigation_and_close_variants(self) -> None:
        self._open_main()
        self.page.locator(".photo-card", has_text="alpha.jpg").first.click(position={"x": 30, "y": 30})
        self.page.locator("#viewer[open]").wait_for()
        self.page.locator("#viewer-similar").click()
        self.page.locator("#similar-gallery").wait_for(state="visible")
        self.assertTrue(self.page.locator("#viewer-open-normal").evaluate("node => node.classList.contains('hidden')"))
        self.page.get_by_text("No strongly similar assets found").wait_for()
        self.page.locator("#similar-more").wait_for()
        self.page.locator("#similar-more").click()
        self.page.locator(".similar-result").first.wait_for()
        self.assertEqual(self.page.locator(".similar-result").count(), 4)
        self.page.locator(".similar-result").first.click()
        self.page.locator("#viewer-open-normal").wait_for(state="visible")
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
        self.assertIn("Group 1", group.inner_text())
        group.locator(".group-photo").first.click()
        self.page.locator("#viewer[open]").wait_for()
        self.page.locator("#viewer-grouping").wait_for(state="visible")
        self.assertIn("group-", self.page.locator("#viewer-title").inner_text())
        self.page.locator("#viewer-close").click()
        self.page.locator("#groups-view").wait_for(state="visible")

    def test_show_image_group_uses_direct_locator_and_does_not_restore_gallery_late(self) -> None:
        self._open_main()
        self.page.evaluate("document.body.style.minHeight = '1800px'")
        self.page.evaluate("state.scrollPositions.gallery = 180")
        self.page.evaluate("window.scrollTo(0, 180)")
        self.page.locator(".photo-card", has_text="group-a.jpg").click(position={"x": 30, "y": 30})
        self.page.locator("#viewer-grouping").wait_for(state="visible")
        requests = []
        self.page.on("request", lambda request: requests.append(request.url) if "/api/groups/locate" in request.url else None)
        self.page.evaluate("""() => {
          const fetchOriginal = window.fetch.bind(window);
          window.__releaseGroupLocate = null;
          window.fetch = (...args) => String(args[0]).includes('/api/groups/locate?')
            ? new Promise(resolve => { window.__releaseGroupLocate = () => fetchOriginal(...args).then(resolve); })
            : fetchOriginal(...args);
        }""")
        self.page.locator("#viewer-grouping").click()
        self.page.locator("#viewer").wait_for(state="hidden", timeout=250)
        self.page.evaluate("window.__releaseGroupLocate?.()")
        self.page.locator("#groups-view").wait_for(state="visible")
        self.page.locator(".group-row.focused-group").wait_for()
        self.assertTrue(any("/api/groups/locate" in url for url in requests))
        self.page.wait_for_timeout(650)
        self.assertTrue(self.page.locator("#groups-view").is_visible())
        self.page.locator("#gallery-view-toggle").click()
        self.page.locator("#gallery").wait_for(state="visible")
        self.assertEqual(self.page.evaluate("new URLSearchParams(location.search).get('view')"), "gallery")

    def test_viewer_closes_before_delayed_gallery_return_and_stale_result_is_ignored(self) -> None:
        self._open_main()
        self.page.locator(".photo-card").first.click(position={"x": 30, "y": 30})
        self.page.locator("#viewer[open]").wait_for()
        self.page.evaluate("""() => {
          const fetchOriginal = window.fetch.bind(window);
          window.__releaseBrowserReturn = null;
          window.fetch = (...args) => String(args[0]).includes('/api/browser?')
            ? new Promise(resolve => { window.__releaseBrowserReturn = () => fetchOriginal(...args).then(resolve); })
            : fetchOriginal(...args);
          state.windowStart = 600;
        }""")
        self.page.locator("#viewer-close").click()
        self.page.locator("#viewer").wait_for(state="hidden", timeout=250)
        self.page.locator("#groups-view-toggle").click()
        self.page.locator("#groups-view").wait_for(state="visible")
        self.page.evaluate("window.__releaseBrowserReturn?.()")
        self.page.wait_for_timeout(250)
        self.assertTrue(self.page.locator("#groups-view").is_visible())

    def test_large_gallery_keeps_loading_runway_bounded_and_loads_forward_windows(self) -> None:
        total = 2400
        synthetic = None

        def browser_response(route):
            nonlocal synthetic
            response = route.fetch()
            data = response.json()
            if synthetic is None:
                synthetic = []
                while len(synthetic) < total:
                    for item in data["items"]:
                        clone = dict(item)
                        index = len(synthetic)
                        clone.update(asset_id=f"synthetic-{index}", filename=f"synthetic-{index:04}.jpg")
                        synthetic.append(clone)
                        if len(synthetic) == total:
                            break
            query = dict(part.split("=", 1) for part in route.request.url.split("?", 1)[1].split("&") if "=" in part)
            offset, limit = int(query.get("offset", 0)), int(query.get("limit", 60))
            data.update(items=synthetic[offset:offset + limit], total=total, media_shown=total, media_total=total,
                        workspace_total=total, has_next=offset + limit < total)
            route.fulfill(response=response, json=data)

        self.page.route("**/api/browser?*", browser_response)
        self._open_main()
        self.page.wait_for_function("() => document.querySelectorAll('#gallery .photo-card').length > 0")
        last_seen = -1
        for _ in range(4):
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            self.page.wait_for_function("""previous => {
              const cards = [...document.querySelectorAll('#gallery .photo-card')];
              const index = Number(cards.at(-1)?.querySelector('.filename')?.textContent.match(/synthetic-(\\d+)/)?.[1] ?? -1);
              return index > previous;
            }""", arg=last_seen, timeout=8000)
            filename = self.page.locator("#gallery .photo-card .filename").last.inner_text()
            last_seen = int(re.search(r"synthetic-(\d+)", filename).group(1))
            self.page.wait_for_function("""() => {
              const gallery = document.querySelector('#gallery');
              const spacers = [...gallery.querySelectorAll('.window-spacer')];
              const bottom = spacers.at(-1);
              return bottom && parseFloat(bottom.style.height) <= 2 * Number(getComputedStyle(gallery).getPropertyValue('--card-height').replace('px','')) + 24;
            }""", timeout=8000)
        self.assertLessEqual(self.page.locator("#gallery .photo-card").count(), 180)
        self.assertTrue(self.page.locator("#gallery .loading-state").is_visible())

    def test_unavailable_visualizations_are_hidden_and_url_falls_back(self) -> None:
        self.page.goto(f"{self.base_url}/?workspace={self.offline_handle}", wait_until="domcontentloaded")
        self.page.locator("#workspace-view").wait_for(state="visible")
        self.page.locator("#timeline-view-toggle").wait_for(state="visible")
        self.page.locator("#geo-view-toggle").wait_for(state="hidden")
        self.page.locator("#vector-view-toggle").wait_for(state="hidden")
        self.page.goto(f"{self.base_url}/?workspace={self.offline_handle}&view=geo", wait_until="domcontentloaded")
        self.page.locator("#workspace-view").wait_for(state="visible")
        self.page.locator("#gallery").wait_for(state="visible")
        self.page.locator("#geo-view-toggle").wait_for(state="hidden")

    def test_geo_map_renders_count_and_asset_preview(self) -> None:
        self._open_main()
        self.page.get_by_role("tab", name="Geo Map").click()
        self.page.locator("#geo-view").wait_for(state="visible")
        self.assertEqual(self.page.locator("#filters").evaluate("node => getComputedStyle(node).left"), "0px")
        self.assertEqual(self.page.locator("main").evaluate("node => getComputedStyle(node).marginLeft"), "0px")
        self.page.locator("#geo-canvas").wait_for()
        self.page.wait_for_function("() => Number(document.querySelector('#geo-canvas').dataset.pointCount) === 3")
        self.page.wait_for_function("() => document.querySelector('#geo-canvas').dataset.firstTargetX !== undefined")
        self.page.wait_for_function("() => Number(document.querySelector('#geo-canvas').getBoundingClientRect().height) > 380")
        self.assertIn("3 geotagged assets · 5 filtered assets", self.page.locator("#geo-status").inner_text())
        self.assertTrue(self.page.evaluate("() => document.documentElement.scrollHeight <= window.innerHeight + 2"))
        canvas = self.page.locator("#geo-canvas")
        canvas.click(position={
            "x": float(canvas.get_attribute("data-first-target-x")),
            "y": float(canvas.get_attribute("data-first-target-y")),
        })
        self.page.locator("#viewer[open]").wait_for(timeout=15000)
        self.assertIn(".jpg", self.page.locator("#viewer-title").inner_text())
        self.assertIn(self.page.locator("#viewer-count").inner_text(), {"1 of 1", "1 of 2", "2 of 2"})
        if self.page.locator("#viewer-count").inner_text() == "1 of 2":
            self.page.locator("#viewer-next").click()
            self.page.wait_for_function("() => document.querySelector('#viewer-count').textContent === '2 of 2'")
            self.page.locator("#viewer-previous").click()
        elif self.page.locator("#viewer-count").inner_text() == "2 of 2":
            self.page.locator("#viewer-previous").click()
            self.page.wait_for_function("() => document.querySelector('#viewer-count').textContent === '1 of 2'")
            self.page.locator("#viewer-next").click()
        self.assertEqual(self.page.locator("#visualization-selection-panel").count(), 0)
        self.page.locator("#viewer-info").click()
        self.page.locator("#viewer-details").wait_for(state="visible")
        self.page.locator("#viewer-close").click()
        self.page.locator("#viewer").wait_for(state="hidden")
        self.assertEqual(self.page.locator("#geo-canvas").get_attribute("data-badge-count"), "2")
        canvas.click(position={
            "x": float(canvas.get_attribute("data-badge-x")),
            "y": float(canvas.get_attribute("data-badge-y")),
        })
        self.page.locator("#viewer[open]").wait_for(timeout=15000)
        self.assertIn(self.page.locator("#viewer-count").inner_text(), {"1 of 2", "2 of 2"})
        if self.page.locator("#viewer-count").inner_text() == "1 of 2":
            self.page.locator("#viewer-next").click()
            self.page.wait_for_function("() => document.querySelector('#viewer-count').textContent === '2 of 2'")
        else:
            self.page.locator("#viewer-previous").click()
            self.page.wait_for_function("() => document.querySelector('#viewer-count').textContent === '1 of 2'")
        self.page.locator("#viewer-close").click()

    def test_timeline_renders_and_zoom_changes_range(self) -> None:
        self._open_main()
        self.page.get_by_role("tab", name="Timeline").click()
        self.page.locator("#timeline-view").wait_for(state="visible")
        self.page.wait_for_function("() => Number(document.querySelector('#timeline-canvas').dataset.pointCount) === 5")
        self.page.wait_for_function("() => document.querySelector('#timeline-canvas').dataset.firstTargetX !== undefined")
        self.page.wait_for_function("() => Number(document.querySelector('#timeline-canvas').dataset.badgeCount) === 2")
        self.page.wait_for_function("() => Number(document.querySelector('#timeline-canvas').getBoundingClientRect().height) > 500")
        self.assertIn("5 timed assets · 5 filtered assets", self.page.locator("#timeline-status").inner_text())
        canvas = self.page.locator("#timeline-canvas")
        canvas_box = canvas.bounding_box()
        before_span = float(canvas.get_attribute("data-visible-span"))
        self.page.mouse.move(canvas_box["x"] + canvas_box["width"] * .72, canvas_box["y"] + canvas_box["height"] * .88)
        self.page.mouse.wheel(0, -420)
        self.page.wait_for_function("before => Number(document.querySelector('#timeline-canvas').dataset.visibleSpan) < before", arg=before_span)
        before_center = float(canvas.get_attribute("data-center-time"))
        self.page.mouse.move(canvas_box["x"] + canvas_box["width"] * .66, canvas_box["y"] + canvas_box["height"] * .88)
        self.page.mouse.down()
        self.page.mouse.move(canvas_box["x"] + canvas_box["width"] * .56, canvas_box["y"] + canvas_box["height"] * .88)
        self.page.mouse.up()
        self.page.wait_for_function("before => Math.abs(Number(document.querySelector('#timeline-canvas').dataset.centerTime) - before) > 0.01", arg=before_center)
        badge_x = float(canvas.get_attribute("data-badge-x"))
        badge_y = float(canvas.get_attribute("data-badge-y"))
        grouped_before = float(canvas.get_attribute("data-view-scale"))
        canvas.click(position={"x": badge_x, "y": badge_y})
        self.page.wait_for_function("before => Number(document.querySelector('#timeline-canvas').dataset.viewScale) < before", arg=grouped_before)
        self.page.wait_for_timeout(900)
        canvas.click(position={
            "x": float(canvas.get_attribute("data-first-target-x")),
            "y": float(canvas.get_attribute("data-first-target-y")),
        })
        self.page.locator("#viewer[open]").wait_for(timeout=15000)
        self.assertEqual(self.page.locator("#viewer-count").inner_text(), "1 of 2")
        self.page.locator("#viewer-next").click()
        self.page.wait_for_function("() => document.querySelector('#viewer-count').textContent === '2 of 2'")
        self.page.locator("#viewer-close").click()
        self.page.locator("#viewer").wait_for(state="hidden")
        before = float(canvas.get_attribute("data-view-scale"))
        self.page.locator("#timeline-zoom-in").click()
        self.page.wait_for_function("before => Number(document.querySelector('#timeline-canvas').dataset.viewScale) < before", arg=before)
        self.assertTrue(self.page.evaluate("() => document.documentElement.scrollHeight <= window.innerHeight + 2"))
        self.page.locator("#timeline-mode-file-created").click()
        self.page.wait_for_function("() => document.querySelector('#timeline-canvas').dataset.timeMode === 'file_created'")
        self.page.wait_for_function("() => document.querySelector('#timeline-canvas').dataset.firstTargetX !== undefined")
        canvas.click(position={
            "x": float(canvas.get_attribute("data-first-target-x")),
            "y": float(canvas.get_attribute("data-first-target-y")),
        })
        self.page.locator("#viewer[open]").wait_for(timeout=15000)
        self.assertIn(".jpg", self.page.locator("#viewer-title").inner_text())
        self.assertEqual(self.page.locator("#visualization-selection-panel").count(), 0)
        self.page.locator("#viewer-info").click()
        self.page.locator("#viewer-details").wait_for(state="visible")
        self.page.locator("#viewer-close").click()

    def test_vector_cloud_uses_stored_projection_and_selects_asset(self) -> None:
        self.page.goto(f"{self.base_url}/?workspace={self.main_handle}&view=vector", wait_until="domcontentloaded")
        self.page.locator("#workspace-view").wait_for(state="visible")
        self.page.locator("#vector-view").wait_for(state="visible")
        self.page.wait_for_function("() => Number(document.querySelector('#vector-canvas').dataset.pointCount) === 5")
        self.page.wait_for_function("() => document.querySelector('#vector-canvas').dataset.firstTargetX !== undefined")
        self.page.wait_for_function("() => Number(document.querySelector('#vector-canvas').dataset.badgeCount) > 1")
        self.page.wait_for_function("() => Number(document.querySelector('#vector-canvas').getBoundingClientRect().height) > 380")
        self.assertIn("5 projected assets · 5 filtered assets", self.page.locator("#vector-status").inner_text())
        self.assertTrue(self.page.evaluate("() => document.documentElement.scrollHeight <= window.innerHeight + 2"))
        self.assertFalse(any("openclip" in entry.lower() or "model" in entry.lower() for entry in self.browser_log))
        canvas = self.page.locator("#vector-canvas")
        canvas.click(position={
            "x": float(canvas.get_attribute("data-first-target-x")),
            "y": float(canvas.get_attribute("data-first-target-y")),
        })
        self.page.locator("#viewer[open]").wait_for(timeout=15000)
        self.assertIn(".jpg", self.page.locator("#viewer-title").inner_text())
        self.assertEqual(self.page.locator("#visualization-selection-panel").count(), 0)
        self.page.locator("#viewer-info").click()
        self.page.locator("#viewer-details").wait_for(state="visible")
        self.page.locator("#viewer-close").click()

    def test_reindex_add_remove_offline_cleanup_preserves_fixture_boundary(self) -> None:
        self.page.goto(f"{self.base_url}/?workspace={self.offline_handle}", wait_until="domcontentloaded")
        self.page.locator(".photo-card", has_text="initial.jpg").first.wait_for()
        added = self.offline_root / "added.jpg"
        self._create_image(added, (90, 90), (180, 70, 160))
        known_job_ids = {job["id"] for job in self.page.request.get(f"{self.base_url}/api/jobs?workspace={self.offline_handle}&limit=10").json()["jobs"]}
        self.page.locator("#index").click()
        self._wait_index_idle(known_job_ids)
        self.page.locator(".photo-card", has_text="added.jpg").first.wait_for()

        initial = self.offline_root / "initial.jpg"
        initial.unlink()
        known_job_ids = {job["id"] for job in self.page.request.get(f"{self.base_url}/api/jobs?workspace={self.offline_handle}&limit=10").json()["jobs"]}
        self.page.locator("#index").click()
        self._wait_index_idle(known_job_ids)
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
