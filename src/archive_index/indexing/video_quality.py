"""Uniform video-frame sampling and aggregate technical quality scoring."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from threading import Event
from datetime import datetime, timezone
from collections.abc import Callable
from tempfile import TemporaryDirectory
from time import monotonic, perf_counter

from PIL import Image, ImageOps, UnidentifiedImageError

from ..jobs.engine import report_substage, JobProgress, JobRunResult, JobStore, run_items
from ..media.metadata import MetadataExtractionError, UnsupportedDecoderError
from ..media.quality_provider import (
    LAR_IQA_CHECKPOINT_SHA256,
    QualityProvider,
    QualityProviderUnavailable,
    create_quality_provider,
)
from ..workspace import Workspace

VIDEO_SAMPLER_ALGORITHM = "ffmpeg-uniform-bin-center"
VIDEO_SAMPLER_VERSION = "3"
VIDEO_FRAME_MAX_DECODE_DIMENSION = 1920
VIDEO_EXTRACTION_TIMEOUT_SECONDS = 300
VIDEO_SINGLE_PROCESS_MAX_DURATION_SECONDS = 30.0
VIDEO_AGGREGATE_ALGORITHM = "lar-iqa-video-top-quartile-mean"
VIDEO_AGGREGATE_VERSION = "1"
VIDEO_QUALITY_ALGORITHM = VIDEO_AGGREGATE_ALGORITHM
VIDEO_QUALITY_VERSION = VIDEO_AGGREGATE_VERSION


class VideoQualityError(RuntimeError):
    """A video cannot produce a usable aggregate quality result."""


class VideoExtractionCancelled(VideoQualityError):
    """Video frame extraction was stopped before the sample set completed."""


@dataclass(frozen=True)
class ExtractedVideoFrame:
    actual_timestamp: float | None
    image: Image.Image


def sample_count(duration_seconds: float, target_fps: float, minimum: int, maximum: int) -> int:
    if duration_seconds <= 0 or not math.isfinite(duration_seconds):
        raise VideoQualityError("video duration is unknown or invalid")
    if target_fps <= 0 or minimum < 1 or maximum < minimum:
        raise ValueError("invalid video sampling settings")
    return max(minimum, min(maximum, math.ceil(duration_seconds * target_fps)))


def sample_timestamps(duration_seconds: float, count: int) -> list[float]:
    if duration_seconds <= 0 or count < 1:
        raise ValueError("duration and sample count must be positive")
    return [duration_seconds * (index + 0.5) / count for index in range(count)]


def aggregate_scores(scores: list[float], requested_count: int) -> tuple[float | None, int, bool]:
    successful = len(scores)
    required = math.ceil(requested_count * 0.75)
    if successful < required:
        return None, successful, False
    top_count = max(1, math.ceil(successful * 0.25))
    return sum(sorted(scores, reverse=True)[:top_count]) / top_count, successful, successful < requested_count


def video_quality_provenance(provider: QualityProvider, configuration: dict[str, object]) -> tuple[str, str, dict[str, object]]:
    provider_settings = dict(getattr(provider, "settings", {}))
    if provider.algorithm == "lar-iqa":
        provider_settings["checkpoint_sha256"] = (
            getattr(provider, "checkpoint_sha256", None) or LAR_IQA_CHECKPOINT_SHA256
        )
    return (
        VIDEO_QUALITY_ALGORITHM,
        VIDEO_QUALITY_VERSION,
        {
            "sampler_algorithm": VIDEO_SAMPLER_ALGORITHM,
            "sampler_version": VIDEO_SAMPLER_VERSION,
            "target_fps": configuration["video_sampling_fps"],
            "min_frames": configuration["video_sampling_min_frames"],
            "max_frames": configuration["video_sampling_max_frames"],
            "max_decode_dimension": VIDEO_FRAME_MAX_DECODE_DIMENSION,
            "single_process_max_duration_seconds": VIDEO_SINGLE_PROCESS_MAX_DURATION_SECONDS,
            "long_video_strategy": "per-frame-input-seek",
            "sample_position": "duration * (index + 0.5) / sample_count",
            "aggregate_algorithm": VIDEO_AGGREGATE_ALGORITHM,
            "aggregate_version": VIDEO_AGGREGATE_VERSION,
            "provider_algorithm": provider.algorithm,
            "provider_version": provider.version,
            "provider_settings": provider_settings,
        },
    )


def extract_video_frames(
    source: Path,
    timestamps: list[float],
    cancel_event: Event | None = None,
    timings: dict[str, float] | None = None,
    seek_per_frame: bool = False,
    progress=None,
) -> list[ExtractedVideoFrame]:
    if not timestamps:
        return []
    if seek_per_frame:
        frames = []
        try:
            for timestamp in timestamps:
                frames.append(_extract_video_frame_seek(source, timestamp, cancel_event, timings))
                if progress:
                    progress(len(frames), len(timestamps))
            return frames
        except Exception:
            for frame in frames:
                frame.image.close()
            raise
    with TemporaryDirectory(prefix="archive-index-video-") as temporary_directory:
        output_pattern = str(Path(temporary_directory) / "sample-%05d.png")
        command = [
            "ffmpeg",
            "-y",
            "-v", "info",
            "-nostats",
            "-i",
            str(source),
            "-map", "0:v:0",
            "-an",
            "-vf",
            _video_selection_filter(timestamps),
            "-fps_mode", "vfr",
            "-frames:v",
            str(len(timestamps)),
            "-f",
            "image2",
            "-vcodec",
            "png",
            output_pattern,
        ]
        process = None
        try:
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError as error:
            raise UnsupportedDecoderError("ffmpeg is not installed") from error
        assert process.stderr is not None
        try:
            extraction_start = monotonic()
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    _stop_process(process)
                    raise VideoExtractionCancelled("video frame extraction cancelled")
                try:
                    _, stderr = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if progress:
                        progress(max(0, len(list(Path(temporary_directory).glob("sample-*.png"))) - 1), len(timestamps))
                    if monotonic() - extraction_start >= VIDEO_EXTRACTION_TIMEOUT_SECONDS:
                        _stop_process(process)
                        raise MetadataExtractionError(f"video frame extraction timed out: {source}")
            if timings is not None:
                timings["ffmpeg"] = timings.get("ffmpeg", 0.0) + monotonic() - extraction_start
        finally:
            if process.poll() is None:
                _stop_process(process)
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise MetadataExtractionError(detail or f"video frame extraction failed: {source}")
        actual_timestamps = _reported_timestamps(stderr)
        paths = sorted(Path(temporary_directory).glob("sample-*.png"))
        frames = []
        decode_start = monotonic()
        for index, path in enumerate(paths[:len(timestamps)]):
            try:
                with Image.open(path) as image:
                    frames.append(
                        ExtractedVideoFrame(
                            actual_timestamps[index] if index < len(actual_timestamps) else None,
                            ImageOps.exif_transpose(image).convert("RGB").copy(),
                        )
                    )
            except (UnidentifiedImageError, OSError, ValueError) as error:
                for frame in frames:
                    frame.image.close()
                raise MetadataExtractionError(f"ffmpeg returned an invalid video frame: {source}") from error
        if timings is not None:
            timings["png_decode"] = timings.get("png_decode", 0.0) + monotonic() - decode_start
        if progress:
            progress(len(frames), len(timestamps))
        return frames


def extract_video_frame(source: Path, timestamp: float) -> Image.Image:
    frames = extract_video_frames(source, [timestamp])
    if not frames:
        raise MetadataExtractionError(f"ffmpeg returned no video frame: {source}")
    frame = frames[0].image
    for extra in frames[1:]:
        extra.image.close()
    return frame


def _extract_video_frame_seek(
    source: Path,
    timestamp: float,
    cancel_event: Event | None,
    timings: dict[str, float] | None,
) -> ExtractedVideoFrame:
    command = [
        "ffmpeg", "-y", "-v", "info", "-nostats", "-ss", f"{timestamp:.9f}", "-i", str(source),
        "-map", "0:v:0", "-an", "-frames:v", "1", "-vf",
        f"scale={VIDEO_FRAME_MAX_DECODE_DIMENSION}:{VIDEO_FRAME_MAX_DECODE_DIMENSION}:force_original_aspect_ratio=decrease,showinfo",
        "-f", "image2pipe", "-vcodec", "png", "pipe:1",
    ]
    started = monotonic()
    process = None
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as error:
        raise UnsupportedDecoderError("ffmpeg is not installed") from error
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                _stop_process(process)
                raise VideoExtractionCancelled("video frame extraction cancelled")
            try:
                payload, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if monotonic() - started >= VIDEO_EXTRACTION_TIMEOUT_SECONDS:
                    _stop_process(process)
                    raise MetadataExtractionError(f"video frame extraction timed out: {source}")
        if timings is not None:
            timings["ffmpeg"] = timings.get("ffmpeg", 0.0) + monotonic() - started
    finally:
        if process.poll() is None:
            _stop_process(process)
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise MetadataExtractionError(detail or f"video frame extraction failed: {source}")
    decode_started = monotonic()
    try:
        with Image.open(BytesIO(payload)) as image:
            frame = ImageOps.exif_transpose(image).convert("RGB").copy()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise MetadataExtractionError(f"ffmpeg returned an invalid video frame: {source}") from error
    if timings is not None:
        timings["png_decode"] = timings.get("png_decode", 0.0) + monotonic() - decode_started
    return ExtractedVideoFrame(None, frame)


def _video_selection_filter(timestamps: list[float]) -> str:
    selectors = [
        f"gte(t\\,{timestamp:.9f})*(isnan(prev_selected_t)+lt(prev_selected_t\\,{timestamp:.9f}))"
        for timestamp in timestamps
    ]
    return (
        f"select='{'+'.join(selectors)}',"
        f"scale={VIDEO_FRAME_MAX_DECODE_DIMENSION}:{VIDEO_FRAME_MAX_DECODE_DIMENSION}:force_original_aspect_ratio=decrease,showinfo"
    )


def _reported_timestamps(stderr: bytes) -> list[float]:
    return [float(match) for match in re.findall(r"pts_time:([+-]?(?:\d+(?:\.\d*)?|\.\d+))", stderr.decode(errors="replace"))]


def _stop_process(process) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def index_video_quality(
    workspace: Workspace,
    *,
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[JobProgress], None] | None = None,
    quality_provider: str | QualityProvider | None = None,
    quality_batch_size: int | None = None,
    timings: dict[str, float] | None = None,
) -> JobRunResult:
    configuration = workspace.configuration()
    provider = create_quality_provider(
        (
            workspace.quality_provider()
            if configuration["video_quality_enabled"] and quality_provider is None
            else "off"
            if quality_provider is None
            else quality_provider
        ),
        batch_size=quality_batch_size,
    )
    connection = workspace.connect()
    try:
        rows = connection.execute(
            "SELECT * FROM physical_file WHERE media_type = 'video' AND is_online = 1 AND in_scope = 1 ORDER BY relative_path"
        ).fetchall()
    finally:
        connection.close()
    algorithm, version, settings = video_quality_provenance(provider, configuration)
    _ensure_quality_states(workspace, rows, algorithm, version, settings, provider.enabled)
    if not provider.enabled:
        return run_items(
            workspace,
            "video_quality",
            rows,
            lambda row: "skipped",
            job_id=job_id,
            cancel_event=cancel_event,
            progress=progress,
            physical_file_id=lambda row: row["id"],
            relative_path=lambda row: row["relative_path"],
            stage="video quality disabled",
        )

    pending = [
        row for row in rows
        if not _video_state_ready(workspace, row, algorithm, version, settings)
    ]
    preflight_error = None
    if pending:
        try:
            provider.preflight()
        except Exception as error:
            preflight_error = error

    def worker(row):
        if _video_state_ready(workspace, row, algorithm, version, settings):
            return "skipped"
        if preflight_error is not None:
            _mark_failed(workspace, row, algorithm, version, settings, preflight_error)
            raise preflight_error
        try:
            return _process_video(
                workspace,
                row,
                provider,
                algorithm,
                version,
                settings,
                cancel_event,
                timings,
                (lambda stage, current, total: report_substage(job_id, stage, current, total, row["filename"])) if job_id else None,
            )
        except Exception as error:
            _mark_failed(workspace, row, algorithm, version, settings, error)
            raise

    return run_items(
        workspace,
        "video_quality",
        rows,
        worker,
        job_id=job_id,
        cancel_event=cancel_event,
        progress=progress,
        physical_file_id=lambda row: row["id"],
        relative_path=lambda row: row["relative_path"],
        stage="video quality",
    )


def video_quality_details(workspace: Workspace, physical_file_id: str) -> dict[str, object] | None:
    connection = workspace.connect()
    try:
        row = connection.execute(
            """
            SELECT vsr.*
            FROM workspace_video_sample AS wvs
            JOIN video_sample_run AS vsr ON vsr.id = wvs.active_run_id
            WHERE wvs.physical_file_id = ?
            """,
            (physical_file_id,),
        ).fetchone()
        samples = connection.execute(
            "SELECT sample_index, requested_timestamp, actual_timestamp, timestamp_error_seconds, extraction_status, quality_status, quality_score, error_message FROM video_sample WHERE run_id = (SELECT active_run_id FROM workspace_video_sample WHERE physical_file_id = ?) ORDER BY sample_index",
            (physical_file_id,),
        ).fetchall() if row is not None else []
    finally:
        connection.close()
    if row is None:
        return None
    return {
        "run_id": row["id"],
        "sampler_algorithm": row["sampler_algorithm"],
        "sampler_version": row["sampler_version"],
        "settings": json.loads(row["settings_json"]),
        "duration_seconds": row["duration_seconds"],
        "requested_count": row["requested_count"],
        "successful_count": row["successful_count"],
        "status": row["status"],
        "aggregate_algorithm": row["aggregate_algorithm"],
        "aggregate_version": row["aggregate_version"],
        "samples": [dict(sample) for sample in samples],
    }


def _process_video(workspace, row, provider, algorithm, version, settings, cancel_event, timings=None, substage=None):
    duration = row["duration_seconds"]
    count = sample_count(
        float(duration) if duration is not None else 0.0,
        float(settings["target_fps"]),
        int(settings["min_frames"]),
        int(settings["max_frames"]),
    )
    timestamps = sample_timestamps(float(duration), count)
    input_fingerprint = _video_input_fingerprint(row, duration)
    settings_json = json.dumps(settings, ensure_ascii=False, sort_keys=True)
    run = _get_or_create_run(
        workspace,
        row,
        algorithm,
        version,
        settings_json,
        input_fingerprint,
        float(duration),
        count,
    )
    _mark_running(workspace, row, run["id"], algorithm, version, settings_json, input_fingerprint)
    sample_rows = _sample_rows(workspace, run["id"])
    batch_size = max(1, int(getattr(provider, "batch_size", 1)))
    pending_samples = [
        sample
        for sample in sample_rows
        if sample["quality_status"] != "complete" or sample["quality_score"] is None
    ]
    frames: list[Image.Image] = []
    frame_rows = []
    try:
        if cancel_event is not None and cancel_event.is_set():
            _mark_cancelled(workspace, row["id"], run["id"])
            return "cancelled"
        try:
            extraction_start = perf_counter()
            extract_args = (
                workspace.absolute_path(row["relative_path"]),
                [sample["requested_timestamp"] for sample in pending_samples],
                cancel_event,
            )
            extract_kwargs = {}
            if substage:
                substage("Extracting frames", 0, len(pending_samples))
                extract_kwargs["progress"] = lambda current, total: substage("Extracting frames", current, total)
            if float(duration) > VIDEO_SINGLE_PROCESS_MAX_DURATION_SECONDS:
                extract_kwargs["seek_per_frame"] = True
            if timings is not None:
                extract_kwargs["timings"] = timings
            extracted = extract_video_frames(*extract_args, **extract_kwargs)
            if timings is not None:
                timings["extraction_total"] = timings.get("extraction_total", 0.0) + perf_counter() - extraction_start
        except VideoExtractionCancelled:
            _mark_cancelled(workspace, row["id"], run["id"])
            return "cancelled"
        except Exception as error:
            extraction_error = error
            extracted = []
        else:
            extraction_error = None
        assessed = 0
        if substage:
            substage("Assessing frames", 0, len(extracted))
        for sample, extracted_frame in zip(pending_samples, extracted):
            actual_timestamp = extracted_frame.actual_timestamp
            timestamp_error = (
                abs(actual_timestamp - sample["requested_timestamp"])
                if actual_timestamp is not None
                else None
            )
            _mark_sample_extracted(
                workspace,
                run["id"],
                sample["sample_index"],
                actual_timestamp,
                timestamp_error,
            )
            frames.append(extracted_frame.image)
            frame_rows.append(sample)
            if len(frames) >= batch_size:
                _score_batch(workspace, run["id"], frame_rows, frames, provider, timings)
                assessed += len(frames)
                if substage:
                    substage("Assessing frames", assessed, len(extracted))
                frames.clear()
                frame_rows.clear()
        if len(extracted) < len(pending_samples):
            for sample in pending_samples[len(extracted):]:
                _mark_sample_failed(
                    workspace,
                    run["id"],
                    sample["sample_index"],
                    "extraction",
                    extraction_error or VideoQualityError("ffmpeg did not return the requested video sample"),
                )
        if frames:
            _score_batch(workspace, run["id"], frame_rows, frames, provider, timings)
            assessed += len(frames)
            if substage:
                substage("Assessing frames", assessed, len(extracted))
        if cancel_event is not None and cancel_event.is_set():
            _mark_cancelled(workspace, row["id"], run["id"])
            return "cancelled"
    finally:
        for frame in frames:
            frame.close()

    connection = workspace.connect()
    try:
        scores = [
            float(sample[0])
            for sample in connection.execute(
                "SELECT quality_score FROM video_sample WHERE run_id = ? AND quality_status = 'complete' AND quality_score IS NOT NULL ORDER BY sample_index",
                (run["id"],),
            ).fetchall()
        ]
    finally:
        connection.close()
    score, successful, partial = aggregate_scores(scores, count)
    if score is None:
        error = VideoQualityError(
            f"video quality needs at least {math.ceil(count * 0.75)} successful samples; got {successful}"
        )
        _mark_failed(workspace, row, algorithm, version, settings, error, run_id=run["id"], successful=successful)
        raise error
    _publish_result(workspace, row, run["id"], algorithm, version, settings_json, input_fingerprint, score, successful, partial)
    return None


def _score_batch(workspace, run_id, samples, frames, provider, timings=None) -> None:
    score_start = perf_counter()
    score_function = getattr(provider, "score_prepared_images", provider.score_images)
    provider_timings_before = dict(getattr(provider, "last_timings", {}))
    try:
        results = score_function(frames)
        if len(results) != len(samples):
            raise VideoQualityError("quality provider returned an unexpected frame count")
    except Exception:
        results = []
        for frame in frames:
            try:
                results.append(score_function([frame])[0])
            except Exception as error:
                results.append(error)
    if timings is not None:
        timings["quality_score"] = timings.get("quality_score", 0.0) + perf_counter() - score_start
        for name, total in getattr(provider, "last_timings", {}).items():
            timings[name] = timings.get(name, 0.0) + total - provider_timings_before.get(name, 0.0)
    persist_start = perf_counter()
    for sample, result in zip(samples, results):
        if isinstance(result, BaseException):
            _mark_sample_failed(workspace, run_id, sample["sample_index"], "quality", result)
        else:
            raw = dict(result.raw)
            raw["provider_algorithm"] = provider.algorithm
            raw["provider_version"] = provider.version
            _mark_sample_quality(workspace, run_id, sample["sample_index"], raw, result.score)
    for frame in frames:
        frame.close()
    if timings is not None:
        timings["sample_persistence"] = timings.get("sample_persistence", 0.0) + perf_counter() - persist_start


def _get_or_create_run(workspace, row, algorithm, version, settings_json, input_fingerprint, duration, count):
    connection = workspace.connect()
    try:
        run = connection.execute(
            """
            SELECT * FROM video_sample_run
            WHERE physical_file_id = ? AND sampler_algorithm = ? AND sampler_version = ?
              AND input_fingerprint = ? AND requested_count = ?
              AND status IN ('running', 'cancelled', 'complete', 'partial', 'sampling_only')
            ORDER BY CASE WHEN settings_json = ? THEN 0 ELSE 1 END, created_at DESC LIMIT 1
            """,
            (row["id"], VIDEO_SAMPLER_ALGORITHM, VIDEO_SAMPLER_VERSION, input_fingerprint, count, settings_json),
        ).fetchone()
    finally:
        connection.close()
    if run is not None and run["requested_count"] == count:
        return run
    run_id = str(uuid.uuid4())
    now = _timestamp()
    with workspace.transaction() as connection:
        connection.execute(
            """
            INSERT INTO video_sample_run(
                id, physical_file_id, sampler_algorithm, sampler_version, settings_json,
                input_fingerprint, duration_seconds, requested_count, status,
                aggregate_algorithm, aggregate_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (
                run_id, row["id"], VIDEO_SAMPLER_ALGORITHM, VIDEO_SAMPLER_VERSION,
                settings_json, input_fingerprint, duration, count,
                VIDEO_AGGREGATE_ALGORITHM, VIDEO_AGGREGATE_VERSION, now,
            ),
        )
        connection.executemany(
            "INSERT INTO video_sample(run_id, physical_file_id, sample_index, requested_timestamp) VALUES (?, ?, ?, ?)",
            ((run_id, row["id"], index, timestamp) for index, timestamp in enumerate(sample_timestamps(duration, count))),
        )
    return _sample_run(workspace, run_id)


def _sample_run(workspace, run_id):
    connection = workspace.connect()
    try:
        return connection.execute("SELECT * FROM video_sample_run WHERE id = ?", (run_id,)).fetchone()
    finally:
        connection.close()


def _sample_rows(workspace, run_id):
    connection = workspace.connect()
    try:
        return connection.execute(
            "SELECT * FROM video_sample WHERE run_id = ? ORDER BY sample_index", (run_id,)
        ).fetchall()
    finally:
        connection.close()


def _video_state_ready(workspace, row, algorithm, version, settings) -> bool:
    connection = workspace.connect()
    try:
        state = connection.execute(
            "SELECT * FROM component_state WHERE physical_file_id = ? AND component = 'quality'",
            (row["id"],),
        ).fetchone()
        active = connection.execute(
            """
            SELECT vsr.* FROM workspace_video_sample AS wvs
            JOIN video_sample_run AS vsr ON vsr.id = wvs.active_run_id
            WHERE wvs.physical_file_id = ?
            """,
            (row["id"],),
        ).fetchone()
    finally:
        connection.close()
    if state is None:
        return False
    if provider_is_disabled(algorithm):
        return state["status"] in {"not_requested", "unsupported", "complete"}
    if state["status"] == "unsupported":
        return True
    return bool(
        state["status"] == "complete"
        and state["algorithm"] == algorithm
        and state["version"] == version
        and state["input_fingerprint"] == _video_input_fingerprint(row, row["duration_seconds"])
        and row["quality_score"] is not None
        and active is not None
        and active["status"] in {"complete", "partial"}
        and active["settings_json"] == json.dumps(settings, ensure_ascii=False, sort_keys=True)
        and active["input_fingerprint"] == _video_input_fingerprint(row, row["duration_seconds"])
    )


def _ensure_quality_states(workspace, rows, algorithm, version, settings, enabled=True):
    settings_json = json.dumps(settings, ensure_ascii=False, sort_keys=True)
    with workspace.transaction() as connection:
        for row in rows:
            connection.execute(
                """
                INSERT INTO component_state(
                    physical_file_id, component, status, algorithm, version, settings_json
                ) VALUES (?, 'quality', ?, ?, ?, ?)
                ON CONFLICT(physical_file_id, component) DO NOTHING
                """,
                (row["id"], "pending" if enabled else "not_requested", algorithm, version, settings_json),
            )
            if not enabled:
                connection.execute(
                    "UPDATE component_state SET status = CASE WHEN status = 'complete' THEN status ELSE 'not_requested' END, algorithm = ?, version = ?, settings_json = ?, started_at = NULL, error_message = NULL WHERE physical_file_id = ? AND component = 'quality'",
                    (algorithm, version, settings_json, row["id"]),
                )


def provider_is_disabled(algorithm: str) -> bool:
    return algorithm == "quality-off"


def _video_input_fingerprint(row, duration) -> str:
    value = f"{row['sha256'] or f'stat:{row['size_bytes']}:{row['mtime_ns']}'}|duration:{duration!r}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mark_running(workspace, row, run_id, algorithm, version, settings_json, input_fingerprint):
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE video_sample_run SET status = 'running', completed_at = NULL, error_message = NULL WHERE id = ?",
            (run_id,),
        )
        connection.execute(
            """
            UPDATE component_state
            SET status = 'running', algorithm = ?, version = ?, settings_json = ?,
                input_fingerprint = ?, started_at = ?, completed_at = NULL, error_message = NULL
            WHERE physical_file_id = ? AND component = 'quality'
            """,
            (algorithm, version, settings_json, input_fingerprint, _timestamp(), row["id"]),
        )


def _mark_sample_extracted(workspace, run_id, sample_index, actual_timestamp, timestamp_error):
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE video_sample SET extraction_status = 'complete', actual_timestamp = ?, timestamp_error_seconds = ?, error_message = NULL WHERE run_id = ? AND sample_index = ?",
            (actual_timestamp, timestamp_error, run_id, sample_index),
        )


def _mark_sample_quality(workspace, run_id, sample_index, raw, score):
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE video_sample SET quality_status = 'complete', quality_raw_json = ?, quality_score = ?, error_message = NULL WHERE run_id = ? AND sample_index = ?",
            (json.dumps(raw, ensure_ascii=False, sort_keys=True), score, run_id, sample_index),
        )


def _mark_sample_failed(workspace, run_id, sample_index, stage, error):
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE video_sample SET extraction_status = CASE WHEN ? = 'extraction' THEN 'failed' ELSE extraction_status END, quality_status = CASE WHEN ? = 'quality' THEN 'failed' ELSE quality_status END, error_message = ? WHERE run_id = ? AND sample_index = ?",
            (stage, stage, str(error), run_id, sample_index),
        )


def _mark_cancelled(workspace, file_id, run_id):
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE video_sample_run SET status = 'cancelled', completed_at = ?, error_message = ? WHERE id = ?",
            (_timestamp(), "cancelled", run_id),
        )
        connection.execute(
            "UPDATE component_state SET status = 'pending', started_at = NULL, error_message = NULL WHERE physical_file_id = ? AND component = 'quality'",
            (file_id,),
        )


def _mark_failed(workspace, row, algorithm, version, settings, error, run_id=None, successful=0):
    now = _timestamp()
    with workspace.transaction() as connection:
        if run_id is not None:
            connection.execute(
                "UPDATE video_sample_run SET status = 'failed', successful_count = ?, completed_at = ?, error_message = ? WHERE id = ?",
                (successful, now, str(error), run_id),
            )
        connection.execute(
            "UPDATE physical_file SET quality_raw_json = NULL, quality_components_json = NULL, quality_score = NULL, quality_algorithm = NULL, quality_version = NULL WHERE id = ?",
            (row["id"],),
        )
        connection.execute(
            """
            UPDATE component_state
            SET status = ?, algorithm = ?, version = ?, settings_json = ?, input_fingerprint = ?,
                started_at = NULL, completed_at = NULL, error_message = ?
            WHERE physical_file_id = ? AND component = 'quality'
            """,
            (
                "unsupported" if isinstance(error, UnsupportedDecoderError) else "failed",
                algorithm, version, json.dumps(settings, ensure_ascii=False, sort_keys=True),
                _video_input_fingerprint(row, row["duration_seconds"]), str(error), row["id"],
            ),
        )


def _publish_result(workspace, row, run_id, algorithm, version, settings_json, input_fingerprint, score, successful, partial):
    now = _timestamp()
    raw = {
        "provider": "lar-iqa",
        "aggregate_algorithm": VIDEO_AGGREGATE_ALGORITHM,
        "aggregate_version": VIDEO_AGGREGATE_VERSION,
        "requested_samples": _sample_count_for_run(workspace, run_id),
        "successful_samples": successful,
        "partial": partial,
    }
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE video_sample_run SET status = ?, successful_count = ?, completed_at = ?, error_message = ? WHERE id = ?",
            ("partial" if partial else "complete", successful, now, "some samples failed" if partial else None, run_id),
        )
        connection.execute(
            """
            UPDATE physical_file
            SET quality_raw_json = ?, quality_components_json = ?, quality_score = ?,
                quality_algorithm = ?, quality_version = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(raw, sort_keys=True), json.dumps({"successful_samples": successful}), score, algorithm, version, now, row["id"]),
        )
        connection.execute(
            """
            UPDATE component_state
            SET status = 'complete', algorithm = ?, version = ?, settings_json = ?,
                input_fingerprint = ?, started_at = NULL, completed_at = ?, error_message = NULL
            WHERE physical_file_id = ? AND component = 'quality'
            """,
            (algorithm, version, settings_json, input_fingerprint, now, row["id"]),
        )
        connection.execute(
            """
            INSERT INTO workspace_video_sample(physical_file_id, active_run_id) VALUES (?, ?)
            ON CONFLICT(physical_file_id) DO UPDATE SET active_run_id = excluded.active_run_id
            """,
            (row["id"], run_id),
        )


def _sample_count_for_run(workspace, run_id):
    connection = workspace.connect()
    try:
        return connection.execute("SELECT requested_count FROM video_sample_run WHERE id = ?", (run_id,)).fetchone()[0]
    finally:
        connection.close()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
