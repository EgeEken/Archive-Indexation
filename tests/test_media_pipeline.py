from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from io import BytesIO
from pathlib import Path
from threading import Event
from unittest.mock import patch

from PIL import ExifTags, Image

from archive_index.indexing.media_pipeline import index_workspace, invalidate_component, list_problems
from archive_index.indexing.scanner import scan
from archive_index.jobs.engine import JobStore
from archive_index.workspace import Workspace
from archive_index.api.server import WorkspaceHTTPServer


class MediaPipelineTests(unittest.TestCase):
    def test_metadata_thumbnail_cache_and_invalidation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            source = root / "photo.jpg"
            _write_image(source, size=(640, 480), with_exif=True)
            workspace = Workspace.create(root)
            scan(workspace)

            first = index_workspace(workspace)
            self.assertEqual((first.succeeded, first.errors), (1, 0))
            row, states = _file_and_states(workspace)
            self.assertEqual(states["thumbnail"]["algorithm"], "pillow-reduced-jpeg")
            self.assertEqual(states["thumbnail"]["version"], "pillow-jpeg-v2")
            self.assertEqual(json.loads(states["thumbnail"]["settings_json"])["jpeg_quality"], 50)
            thumbnail_path = workspace.index_path(states["thumbnail"]["output_path"])
            self.assertTrue(thumbnail_path.is_file())
            with Image.open(thumbnail_path) as thumbnail:
                self.assertLessEqual(max(thumbnail.size), 320)
            self.assertEqual((row["width"], row["height"]), (640, 480))
            with closing(workspace.connect()) as connection:
                capture = connection.execute(
                    "SELECT capture_time, capture_time_kind FROM logical_asset WHERE id = ?",
                    (row["logical_asset_id"],),
                ).fetchone()
            self.assertEqual(tuple(capture), ("2024-01-02T03:04:05+03:00", "exif_offset"))

            with patch(
                "archive_index.indexing.media_pipeline.extract_metadata",
                side_effect=AssertionError("metadata recomputed unexpectedly"),
            ) as metadata_mock, patch(
                "archive_index.indexing.media_pipeline.generate_thumbnail",
                side_effect=AssertionError("thumbnail recomputed unexpectedly"),
            ) as thumbnail_mock:
                second = index_workspace(workspace)
            self.assertEqual((second.succeeded, second.errors), (1, 0))
            self.assertEqual(second.skipped, 1)
            metadata_mock.assert_not_called()
            thumbnail_mock.assert_not_called()

            thumbnail_path.unlink()
            rebuilt = index_workspace(workspace, components=("thumbnail",))
            self.assertEqual((rebuilt.succeeded, rebuilt.errors), (1, 0))
            self.assertTrue(thumbnail_path.is_file())

            self.assertEqual(invalidate_component(workspace, "metadata"), 1)
            from archive_index.indexing import media_pipeline

            with patch(
                "archive_index.indexing.media_pipeline.extract_metadata",
                side_effect=media_pipeline.extract_metadata,
            ) as extractor:
                invalidated = index_workspace(workspace, components=("metadata",))
            self.assertEqual((invalidated.succeeded, invalidated.errors), (1, 0))
            extractor.assert_called_once()

    def test_thumbnail_orientation_aspect_transaction_and_source_safety(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            source = root / "oriented.jpg"
            _write_image(source, size=(640, 480), with_exif=True)
            original_bytes = source.read_bytes()
            original_mtime = source.stat().st_mtime_ns
            workspace = Workspace.create(root)
            scan(workspace)
            index_workspace(workspace, components=("thumbnail",))
            _, states = _file_and_states(workspace)
            thumbnail_path = workspace.index_path(states["thumbnail"]["output_path"])

            with Image.open(thumbnail_path) as thumbnail:
                self.assertGreater(thumbnail.height, thumbnail.width)
                self.assertAlmostEqual(thumbnail.width / thumbnail.height, 480 / 640, places=2)
                thumbnail.verify()
            self.assertEqual(source.read_bytes(), original_bytes)
            self.assertEqual(source.stat().st_mtime_ns, original_mtime)

            thumbnail_path.unlink()
            with patch("archive_index.media.thumbnail.os.replace", side_effect=OSError("stop")):
                with self.assertRaises(OSError):
                    from archive_index.media.thumbnail import generate_thumbnail

                    generate_thumbnail(source, thumbnail_path)
            self.assertFalse(thumbnail_path.exists())
            self.assertFalse(thumbnail_path.with_name(f".{thumbnail_path.name}.tmp").exists())

    def test_bad_thumbnail_does_not_abort_other_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            good = root / "good.jpg"
            bad = root / "bad.jpg"
            _write_image(good, size=(200, 100))
            _write_image(bad, size=(100, 200))
            workspace = Workspace.create(root)
            scan(workspace)

            from archive_index.indexing import media_pipeline

            real_generate = media_pipeline.generate_thumbnail

            def fail_one(source, destination, size, prepared_image=None):
                if source.name == "bad.jpg":
                    raise OSError("simulated thumbnail failure")
                return real_generate(source, destination, size, prepared_image)

            with patch(
                "archive_index.indexing.media_pipeline.generate_thumbnail",
                side_effect=fail_one,
            ):
                result = index_workspace(workspace)

            self.assertEqual((result.succeeded, result.errors), (1, 1))
            problems = list_problems(workspace, result.job_id)
            self.assertEqual(len(problems), 1)
            self.assertEqual(problems[0]["relative_path"], "bad.jpg")
            with closing(workspace.connect()) as connection:
                states = connection.execute(
                    """
                    SELECT physical_file.relative_path, component_state.component, component_state.status
                    FROM physical_file
                    JOIN component_state ON component_state.physical_file_id = physical_file.id
                    ORDER BY physical_file.relative_path, component_state.component
                    """
                ).fetchall()
            self.assertEqual(
                [(row[0], row[1], row[2]) for row in states],
                [
                    ("bad.jpg", "metadata", "complete"),
                    ("bad.jpg", "quality", "complete"),
                    ("bad.jpg", "thumbnail", "failed"),
                    ("good.jpg", "metadata", "complete"),
                    ("good.jpg", "quality", "complete"),
                    ("good.jpg", "thumbnail", "complete"),
                ],
            )

    def test_resume_and_cancel_use_same_persistent_job_engine(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            for name in ("a.jpg", "b.jpg"):
                _write_image(root / name, size=(100, 100))
            workspace = Workspace.create(root)
            scan(workspace)

            store = JobStore(workspace)
            job_id = store.create("media_index", 2)
            store.start(job_id)
            with workspace.transaction() as connection:
                file_id = connection.execute(
                    "SELECT id FROM physical_file WHERE relative_path = 'a.jpg'"
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO component_state(
                        physical_file_id, component, status, algorithm, version
                    ) VALUES (?, 'metadata', 'running', 'test', 'test')
                    """,
                    (file_id,),
                )
            self.assertEqual(store.recover_interrupted(), 1)
            self.assertEqual(store.get(job_id)["status"], "interrupted")

            resumed = index_workspace(workspace, job_id=job_id)
            self.assertEqual(resumed.job_id, job_id)
            self.assertFalse(resumed.cancelled)
            self.assertEqual(store.get(job_id)["status"], "complete")

            invalidate_component(workspace, "thumbnail")
            cancel_event = Event()

            def cancel_after_first(progress) -> None:
                if progress.processed == 1:
                    cancel_event.set()

            cancelled = index_workspace(workspace, cancel_event=cancel_event, progress=cancel_after_first)
            self.assertTrue(cancelled.cancelled)
            self.assertEqual(JobStore(workspace).get(cancelled.job_id)["status"], "cancelled")

            from archive_index.indexing import media_pipeline

            with patch(
                "archive_index.indexing.media_pipeline.generate_thumbnail",
                side_effect=media_pipeline.generate_thumbnail,
            ) as generator:
                resumed = index_workspace(
                    workspace,
                    components=("thumbnail",),
                    job_id=cancelled.job_id,
                )
            self.assertFalse(resumed.cancelled)
            self.assertEqual(
                [call.args[0].name for call in generator.call_args_list],
                ["b.jpg"],
            )

    def test_workspace_open_recovers_interrupted_jobs_and_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            _write_image(root / "photo.jpg", size=(100, 100))
            workspace = Workspace.create(root)
            scan(workspace)
            with closing(workspace.connect()) as connection:
                file_id = connection.execute("SELECT id FROM physical_file").fetchone()[0]
            store = JobStore(workspace)
            job_id = store.create("media_index")
            store.start(job_id)
            with workspace.transaction() as connection:
                connection.execute(
                    "INSERT INTO component_state(physical_file_id, component, status, algorithm, version) VALUES (?, 'metadata', 'running', 'old', '1')",
                    (file_id,),
                )
                connection.execute(
                    "INSERT INTO component_state(physical_file_id, component, status, algorithm, version) VALUES (?, 'thumbnail', 'complete', 'old', '1')",
                    (file_id,),
                )
            server = WorkspaceHTTPServer(("127.0.0.1", 0), workspace)
            try:
                self.assertEqual(store.get(job_id)["status"], "interrupted")
                with closing(workspace.connect()) as connection:
                    states = {
                        row[0]: row[1]
                        for row in connection.execute(
                            "SELECT component, status FROM component_state WHERE physical_file_id = ?",
                            (file_id,),
                        ).fetchall()
                    }
                self.assertEqual(states, {"metadata": "pending", "thumbnail": "complete"})
            finally:
                server.server_close()

    def test_video_probe_parser_preserves_basic_stream_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "clip.mp4"
            path.write_bytes(b"placeholder")
            probe_output = {
                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2.5", "tags": {}},
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 1920,
                        "height": 1080,
                    }
                ],
            }
            completed = type("Completed", (), {"stdout": json.dumps(probe_output), "stderr": ""})()
            with patch("archive_index.media.metadata.subprocess.run", return_value=completed):
                from archive_index.media.metadata import extract_metadata

                result = extract_metadata(path, "video")

            self.assertEqual((result.width, result.height, result.duration_seconds, result.codec), (1920, 1080, 2.5, "h264"))

    def test_video_thumbnail_uses_center_frame_and_writes_valid_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "clip.mp4"
            destination = root / "thumbnail.jpg"
            source.write_bytes(b"video")
            frame = BytesIO()
            Image.new("RGB", (640, 360), "red").save(frame, format="JPEG")
            duration = type("Completed", (), {"stdout": "10.0", "stderr": ""})()
            encoded = type("Completed", (), {"stdout": frame.getvalue(), "stderr": b""})()
            with patch("archive_index.media.thumbnail.subprocess.run", side_effect=[duration, encoded]) as runner:
                from archive_index.media.thumbnail import generate_thumbnail

                generate_thumbnail(source, destination, media_type="video")

            self.assertEqual(runner.call_args_list[1].args[0][runner.call_args_list[1].args[0].index("-ss") + 1], "5.0")
            with Image.open(destination) as thumbnail:
                thumbnail.verify()
                self.assertLessEqual(max(thumbnail.size), 320)

    def test_video_indexing_completes_thumbnail_and_reports_quality_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "clip.mp4").write_bytes(b"video")
            workspace = Workspace.create(root)
            scan(workspace)
            from archive_index.media.metadata import MediaMetadata

            metadata = MediaMetadata(
                values={"format": {"format_name": "mp4"}},
                width=1920,
                height=1080,
                duration_seconds=2.0,
                codec="h264",
            )

            def fake_thumbnail(source, destination, size, prepared_image=None, media_type="image"):
                self.assertEqual(media_type, "video")
                Image.new("RGB", (160, 90), "black").save(destination, format="JPEG")

            with patch("archive_index.indexing.media_pipeline.extract_metadata", return_value=metadata), patch(
                "archive_index.indexing.media_pipeline.generate_thumbnail", side_effect=fake_thumbnail
            ):
                result = index_workspace(workspace)

            self.assertEqual((result.succeeded, result.errors), (1, 0))
            _, states = _file_and_states(workspace)
            self.assertEqual(states["thumbnail"]["status"], "complete")
            self.assertEqual(states["quality"]["status"], "not_requested")

            self.assertEqual(states["thumbnail"]["algorithm"], "ffmpeg-center-frame-jpeg")
            self.assertEqual(states["thumbnail"]["version"], "ffmpeg-center-frame-jpeg-v2")
            settings = json.loads(states["thumbnail"]["settings_json"])
            self.assertEqual(settings["selection"], "center_frame")
            self.assertEqual(settings["jpeg_quality"], 50)

    def test_curated_metadata_omits_large_binary_exif(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Workspace.create(Path(temporary_directory) / "archive")
            path = workspace.root / "curated.jpg"
            image = Image.new("RGB", (80, 60), color=(80, 140, 210))
            exif = image.getexif()
            exif[36867] = "2024:01:02 03:04:05"
            exif[36881] = "+03:00"
            exif[37500] = b"maker-note" * 5000
            exif[40092] = b"unknown-binary" * 200
            exif[ExifTags.IFD.Exif] = {
                33434: (1, 4000),
                33437: (56, 10),
                34855: 2500,
                37386: (200, 1),
                37521: "123",
            }
            image.save(path, format="JPEG", exif=exif)

            from archive_index.media.metadata import extract_metadata

            result = extract_metadata(path, "image")
            persisted = json.dumps(result.values, ensure_ascii=False)
            values = result.values["exif"]
            self.assertNotIn("MakerNote", values)
            self.assertNotIn("PrintImageMatching", values)
            self.assertNotIn("maker-note", persisted)
            self.assertLess(len(persisted.encode("utf-8")), 5000)
            self.assertEqual(values["FNumber"], [56, 10])
            self.assertEqual(values["ExposureTime"], [1, 4000])
            self.assertEqual(values["ISOSpeedRatings"], 2500)
            self.assertEqual(values["FocalLength"], [200, 1])
            self.assertEqual(result.capture_time, "2024-01-02T03:04:05.123000+03:00")

            scan(workspace)
            index_workspace(workspace, components=("metadata",))
            with closing(workspace.connect()) as connection:
                persisted = connection.execute(
                    "SELECT metadata_json FROM physical_file WHERE relative_path = 'curated.jpg'"
                ).fetchone()[0]
            self.assertNotIn("MakerNote", persisted)
            self.assertNotIn("maker-note", persisted)
            self.assertLess(len(persisted.encode("utf-8")), 5000)

    def test_metadata_version_invalidates_only_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            source = root / "photo.jpg"
            _write_image(source, size=(640, 480), with_exif=True)
            workspace = Workspace.create(root)
            scan(workspace)
            index_workspace(workspace)
            with workspace.transaction() as connection:
                connection.execute(
                    "UPDATE component_state SET version = 'pillow-curated-exif-v3' WHERE component = 'metadata'"
                )

            from archive_index.indexing import media_pipeline

            with patch(
                "archive_index.indexing.media_pipeline.extract_metadata",
                side_effect=media_pipeline.extract_metadata,
            ) as extractor, patch(
                "archive_index.indexing.media_pipeline.generate_thumbnail",
                side_effect=AssertionError("thumbnail recomputed unexpectedly"),
            ) as thumbnail_mock:
                result = index_workspace(workspace)

            self.assertEqual((result.succeeded, result.errors), (1, 0))
            extractor.assert_called_once()
            thumbnail_mock.assert_not_called()

    def test_exif_local_time_without_offset_is_not_made_utc(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "local.jpg"
            _write_image(path, size=(80, 60), with_exif=True, with_offset=False)
            from archive_index.media.metadata import extract_metadata

            result = extract_metadata(path, "image")

            self.assertEqual(result.capture_time, "2024-01-02T03:04:05")
            self.assertEqual(result.capture_time_kind, "exif_local_unknown")

    def test_nested_exif_shot_settings_are_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "shot.jpg"
            image = Image.new("RGB", (80, 60), color=(80, 140, 210))
            exif = image.getexif()
            exif[ExifTags.IFD.Exif] = {
                33434: (1, 4000),
                33437: (56, 10),
                34855: 2500,
                37386: (200, 1),
            }
            image.save(path, format="JPEG", exif=exif)

            from archive_index.media.metadata import extract_metadata

            values = extract_metadata(path, "image").values["exif"]

            self.assertEqual(values["FNumber"], [56, 10])
            self.assertEqual(values["ExposureTime"], [1, 4000])
            self.assertEqual(values["ISOSpeedRatings"], 2500)
            self.assertEqual(values["FocalLength"], [200, 1])

    def test_exif_subseconds_are_preserved_for_capture_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "subsecond.jpg"
            image = Image.new("RGB", (80, 60), color=(80, 140, 210))
            exif = image.getexif()
            exif[36867] = "2024:01:02 03:04:05"
            exif[36881] = "+03:00"
            exif[ExifTags.IFD.Exif] = {37521: "123"}
            image.save(path, format="JPEG", exif=exif)

            from archive_index.media.metadata import extract_metadata

            result = extract_metadata(path, "image")
            self.assertEqual(result.capture_time, "2024-01-02T03:04:05.123000+03:00")
            self.assertEqual(result.capture_time_kind, "exif_offset")

    def test_discovered_decoder_gap_is_reported_without_aborting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            _write_image(root / "good.jpg", size=(100, 100))
            (root / "camera.arw").write_bytes(b"raw placeholder")
            workspace = Workspace.create(root)
            scan(workspace)

            result = index_workspace(workspace)

            self.assertEqual((result.succeeded, result.errors), (1, 1))
            with closing(workspace.connect()) as connection:
                state = connection.execute(
                    """
                    SELECT component_state.status
                    FROM component_state
                    JOIN physical_file ON physical_file.id = component_state.physical_file_id
                    WHERE physical_file.relative_path = 'camera.arw' AND component_state.component = 'metadata'
                    """
                ).fetchone()[0]
            self.assertEqual(state, "unsupported")


def _write_image(
    path: Path,
    *,
    size: tuple[int, int],
    with_exif: bool = False,
    with_offset: bool = True,
) -> None:
    image = Image.new("RGB", size, color=(80, 140, 210))
    if with_exif:
        exif = image.getexif()
        exif[36867] = "2024:01:02 03:04:05"
        if with_offset:
            exif[36881] = "+03:00"
        exif[274] = 6
        image.save(path, format="JPEG", exif=exif)
    else:
        image.save(path, format="JPEG")


def _file_and_states(workspace: Workspace):
    with closing(workspace.connect()) as connection:
        row = connection.execute("SELECT * FROM physical_file").fetchone()
        states = {
            state["component"]: state
            for state in connection.execute(
                "SELECT * FROM component_state WHERE physical_file_id = ?", (row["id"],)
            ).fetchall()
        }
    return row, states


if __name__ == "__main__":
    unittest.main()
