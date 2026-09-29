from __future__ import annotations

import subprocess
import tempfile
import threading
import time
import json
import io
import unittest
from fractions import Fraction
from unittest.mock import patch
from pathlib import Path

from archive_index.file_management import build_dry_run_plan, list_profiles, save_ruleset
from archive_index.file_management_executor import cancel_execution, get_execution, prepare_execution, start_execution
from archive_index.indexing.scanner import scan
from archive_index.media.av1 import ProbeTimeoutError, _run_ffprobe, _timing_is_cfr, analyze_source, build_command, capped_dimensions, preservation_report, source_blocker, source_blocker_from_info
from archive_index.workspace import Workspace


class Phase10CAV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000", "-t", "3", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.root / "clip.mp4")],
            check=True,
        )
        self.workspace = Workspace.create(self.root)
        scan(self.workspace)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _wait(self, execution_id: str):
        for _ in range(3000):
            result = get_execution(self.workspace, execution_id)
            if result["status"] not in {"running", "cancelling"}:
                return result
            time.sleep(0.01)
        self.fail("AV1 execution did not finish")

    def test_safe_subset_replaces_source_in_place_and_preserves_audio(self):
        profile = next(item for item in list_profiles(self.workspace) if item["name"] == "AV1 1080p60 Very Fast")
        ruleset = save_ruleset(
            self.workspace,
            name="av1-safe-subset",
            rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        self.assertFalse(plan["operations"][0]["blockers"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        start_execution(self.workspace, execution["id"])
        result = self._wait(execution["id"])
        self.assertEqual(result["status"], "completed")
        self.assertTrue((self.root / "clip.mp4").is_file())
        self.assertEqual(analyze_source(self.root / "clip.mp4")["codec"], "av1")
        self.assertEqual(len(analyze_source(self.root / "clip.mp4")["audio"]), 1)
        self.assertEqual(result["operations"][0]["profile_snapshot"]["settings"]["preset"], 10)
        self.assertFalse(list(self.root.glob(".*.archive-index-*.backup")))

    def test_caps_never_upscale(self):
        info = analyze_source(self.root / "clip.mp4")
        self.assertEqual(capped_dimensions(info, {"settings": {"resolution_cap": [3840, 2160], "fps_cap": 120, "speed_class": "fast"}}), (640, 360))
        self.assertIsNone(source_blocker(self.root / "clip.mp4", {"settings": {"resolution_cap": [1920, 1080], "fps_cap": 60, "speed_class": "fast"}}))

    def test_resolution_and_fps_caps_preserve_portrait_and_rational_rates(self):
        profile_1080 = {"settings": {"resolution_cap": [1920, 1080], "fps_cap": 60, "speed_class": "fast"}}
        profile_4k = {"settings": {"resolution_cap": [3840, 2160], "fps_cap": 120, "speed_class": "very_fast"}}
        low = {"width": 1280, "height": 720, "rotation": 0, "fps": Fraction(24, 1)}
        medium = {"width": 1920, "height": 1080, "rotation": 0, "fps": Fraction(30, 1)}
        high = {"width": 3840, "height": 2160, "rotation": 0, "fps": Fraction(60, 1)}
        ultra = {"width": 3840, "height": 2160, "rotation": 0, "fps": Fraction(120, 1)}
        portrait = {"width": 1920, "height": 1080, "rotation": 90, "fps": Fraction(59_940, 1_001)}
        physical_portrait = {"width": 1080, "height": 1920, "rotation": 0, "fps": Fraction(30, 1)}
        portrait_4k = {"width": 3840, "height": 2160, "rotation": 90, "fps": Fraction(60, 1)}
        self.assertEqual(capped_dimensions(low, profile_4k), (1280, 720))
        self.assertEqual(capped_dimensions(medium, profile_4k), (1920, 1080))
        self.assertEqual(capped_dimensions(high, profile_4k), (3840, 2160))
        self.assertEqual(capped_dimensions(ultra, profile_1080), (1920, 1080))
        self.assertEqual(capped_dimensions(portrait, profile_1080), (1920, 1080))
        self.assertEqual(capped_dimensions(physical_portrait, profile_1080), (1080, 1920))
        self.assertEqual(capped_dimensions(portrait_4k, profile_1080), (1920, 1080))
        command = build_command(self.root / "clip.mp4", self.root / "output.tmp", ultra, profile_1080)
        self.assertTrue(any("fps=60" in item for item in command))
        self.assertIn("-noautorotate", command)

    def test_timing_probe_rejects_misleading_variable_frame_rate(self):
        frames = [{"best_effort_timestamp_time": str(index / 30), "pkt_duration_time": str(duration)} for index, duration in enumerate([1 / 30, 1 / 30, 0.05, 1 / 30, 1 / 30])]
        result = subprocess.CompletedProcess([], 0, json.dumps({"frames": frames}), "")
        with patch("archive_index.media.av1.subprocess.run", return_value=result):
            self.assertFalse(_timing_is_cfr(self.root / "clip.mp4", {}, 3.0, Fraction(30, 1)))

    def test_timing_probe_uses_one_ffprobe_process(self):
        frames = [{"best_effort_timestamp_time": str(index / 30), "pkt_duration_time": str(1 / 30)} for index in range(20)]
        result = subprocess.CompletedProcess([], 0, json.dumps({"frames": frames}), "")
        with patch("archive_index.media.av1.subprocess.run", return_value=result) as probe:
            self.assertTrue(_timing_is_cfr(self.root / "clip.mp4", {}, 3.0, Fraction(30, 1)))
        self.assertEqual(probe.call_count, 1)

    def test_cancellable_ffprobe_has_deadline_and_stops_hung_child(self):
        class HungProcess:
            stdout = io.StringIO()
            stderr = io.StringIO()

            def __init__(self):
                self.returncode = None
                self.terminated = False
                self.killed = False

            def poll(self):
                return -9 if self.killed else None

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired("ffprobe", timeout)
                self.returncode = -9
                return self.returncode

            def communicate(self, timeout=None):
                raise AssertionError("hung probe should be stopped before communicate")

        process = HungProcess()
        with patch("archive_index.media.av1.subprocess.Popen", return_value=process):
            with self.assertRaises(ProbeTimeoutError):
                _run_ffprobe(["ffprobe"], threading.Event(), timeout=0)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)

    def test_plan_does_not_run_deep_av1_timing_for_uncached_source(self):
        profile = next(item for item in list_profiles(self.workspace) if item["codec"] == "av1")
        ruleset = save_ruleset(
            self.workspace,
            name="av1-plan-preflight",
            rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        with patch("archive_index.compression_analysis.analyze_av1", side_effect=AssertionError("Plan ran AV1 deep analysis")):
            plan = build_dry_run_plan(self.workspace, ruleset["id"])
        self.assertTrue(plan["operations"][0]["preflight_required"])
        self.assertEqual(plan["summary"]["preflight_required_count"], 1)

    def test_preflight_timeout_is_not_persisted(self):
        from archive_index.compression_analysis import preflight_av1

        connection = self.workspace.connect()
        try:
            row = connection.execute("SELECT id, relative_path, sha256, size_bytes, mtime_ns FROM physical_file WHERE extension = '.mp4'").fetchone()
        finally:
            connection.close()
        with patch("archive_index.compression_analysis.analyze_av1", side_effect=ProbeTimeoutError("timed out")) as analyzer:
            first = preflight_av1(self.workspace, row, timeout=0.01)
            second = preflight_av1(self.workspace, row, timeout=0.01)
        self.assertEqual(first.status, "timeout")
        self.assertEqual(second.status, "timeout")
        self.assertEqual(analyzer.call_count, 2)
        connection = self.workspace.connect()
        try:
            self.assertIsNone(connection.execute("SELECT analysis_json FROM compression_source_analysis WHERE physical_file_id = ? AND codec = 'av1'", (row["id"],)).fetchone())
        finally:
            connection.close()

    def test_unknown_extra_stream_is_reported_and_blocks_lossy_output(self):
        info = {
            "width": 640, "height": 360, "duration": 3.0, "fps": Fraction(24, 1),
            "ancillary": [{"codec_type": "data", "codec_name": "gpmd", "codec_tag_string": "gpmd"}],
            "audio": [], "color_transfer": "bt709", "pix_fmt": "yuv420p", "bits_per_raw_sample": 8,
            "timing_cfr": True, "format_tags": {}, "video_tags": {}, "video": {"tags": {}}, "chapters": [],
        }
        profile = {"settings": {"resolution_cap": [1920, 1080], "fps_cap": 60, "speed_class": "fast"}}
        self.assertIn("Unsupported data stream: gpmd", source_blocker_from_info(info, profile))
        report = preservation_report(info, {**info, "width": 640, "height": 360, "pix_fmt": "yuv420p", "ancillary": []}, profile)
        self.assertTrue(any(item["kind"] == "data_stream" and "gpmd" in item["message"] for item in report["lost"]))

    def test_quicktime_timecode_is_reported_and_blocks_lossy_output(self):
        info = {
            "width": 640, "height": 360, "duration": 3.0, "fps": Fraction(24, 1),
            "ancillary": [{"codec_type": "data", "codec_name": None, "codec_tag_string": "tmcd"}],
            "audio": [], "color_transfer": "bt709", "pix_fmt": "yuv420p", "bits_per_raw_sample": 8,
            "timing_cfr": True, "format_tags": {}, "video_tags": {}, "video": {"tags": {}}, "chapters": [],
        }
        profile = {"settings": {"resolution_cap": [1920, 1080], "fps_cap": 60, "speed_class": "fast"}}
        self.assertIn("Unsupported data stream: tmcd", source_blocker_from_info(info, profile))
        report = preservation_report(info, {**info, "width": 640, "height": 360, "pix_fmt": "yuv420p", "ancillary": []}, profile)
        self.assertTrue(any(item["kind"] == "data_stream" and "tmcd" in item["message"] for item in report["lost"]))

    def test_real_irregular_timestamps_are_preserved_as_vfr(self):
        source = self.root / "vfr.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                "-i", "testsrc2=size=320x180:rate=30", "-t", "2", "-vf",
                "setpts=PTS+if(eq(N\\,20)\\,0.1/TB\\,0)", "-fps_mode", "vfr",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ],
            check=True,
        )
        info = analyze_source(source)
        self.assertFalse(info["timing_cfr"])
        self.assertIsNone(source_blocker(source, {"settings": {"resolution_cap": [1920, 1080], "fps_cap": 60, "speed_class": "fast"}}))

    def test_cancelled_encode_keeps_source_and_removes_owned_temp(self):
        profile = next(item for item in list_profiles(self.workspace) if item["name"] == "AV1 1080p60 Very Fast")
        ruleset = save_ruleset(
            self.workspace,
            name="av1-cancel",
            rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])

        def cancel_encode(source, output, info, profile, cancel, progress):
            cancel.set()
            raise InterruptedError("cancelled by test")

        with patch("archive_index.file_management_executor.encode_av1", side_effect=cancel_encode):
            start_execution(self.workspace, execution["id"])
            result = self._wait(execution["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertTrue((self.root / "clip.mp4").is_file())
        self.assertFalse(list(self.root.rglob(".*.archive-index-*.tmp")))

    def test_cancel_after_validation_does_not_start_source_swap(self):
        profile = next(item for item in list_profiles(self.workspace) if item["name"] == "AV1 1080p60 Very Fast")
        ruleset = save_ruleset(
            self.workspace,
            name="av1-cancel-before-swap",
            rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        source = self.root / "clip.mp4"
        original = source.read_bytes()
        from archive_index.media.av1 import validate as validate_av1_real

        def validate_then_cancel(source_path, output_path, source_info, profile_snapshot):
            result = validate_av1_real(source_path, output_path, source_info, profile_snapshot)
            cancel_execution(self.workspace, execution["id"])
            return result

        with patch("archive_index.file_management_executor.validate_av1", side_effect=validate_then_cancel):
            start_execution(self.workspace, execution["id"])
            result = self._wait(execution["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(list(self.root.rglob(".*.archive-index-*.backup")))

    def test_finalized_in_place_output_recovers_after_process_loss(self):
        profile = next(item for item in list_profiles(self.workspace) if item["name"] == "AV1 1080p60 Very Fast")
        ruleset = save_ruleset(
            self.workspace,
            name="av1-recovery",
            rules=[{"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": profile["id"], "source_disposition": "replace"}}],
        )
        plan = build_dry_run_plan(self.workspace, ruleset["id"])
        execution = prepare_execution(self.workspace, ruleset["id"], plan["plan_digest"])
        source = self.root / "clip.mp4"
        info = analyze_source(source)
        output = self.root / "recovered-output.mp4"
        from archive_index.media.av1 import encode as encode_av1

        encoded = encode_av1(source, output, info, plan["operations"][0]["profile_snapshot"], threading.Event())
        source.write_bytes(output.read_bytes())
        output.unlink()
        connection = self.workspace.connect()
        try:
            operation_id = connection.execute("SELECT id FROM file_management_execution_operation WHERE execution_id = ?", (execution["id"],)).fetchone()[0]
        finally:
            connection.close()
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE file_management_execution SET status = 'running' WHERE id = ?", (execution["id"],))
            connection.execute(
                "UPDATE file_management_execution_operation SET status = 'running', stage = 'finalizing', actual_output_size_bytes = ?, actual_output_sha256 = ? WHERE id = ?",
                (encoded["size_bytes"], encoded["sha256"], operation_id),
            )
        recovered = get_execution(self.workspace, execution["id"])
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["operations"][0]["status"], "completed")
        self.assertEqual(analyze_source(source)["codec"], "av1")
