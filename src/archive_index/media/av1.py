"""Safe-subset production AV1 encoding through the installed FFmpeg/SVT-AV1."""

from __future__ import annotations

import json
import math
import subprocess
import time
from fractions import Fraction
from pathlib import Path

from PIL import Image, ImageStat

AV1_ALGORITHM = "ffmpeg-libsvtav1-archival"
AV1_ALGORITHM_VERSION = "2"
AV1_SUPPORTED_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".mkv"})
AV1_AUDIO_CODECS = frozenset({"aac", "alac", "mp3", "ac3", "eac3"})
AV1_TEXT_SUBTITLE_CODECS = frozenset({"mov_text", "subrip", "srt", "ass", "ssa", "webvtt"})
AV1_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67", "bt2020-10", "bt2020-12"})
PLAN_METADATA_TIMEOUT_SECONDS = 2.5
EXECUTION_PREFLIGHT_TIMEOUT_SECONDS = 15.0


class ProbeTimeoutError(RuntimeError):
    pass


def profile_settings(profile: dict[str, object] | None) -> dict[str, object]:
    raw = dict((profile or {}).get("settings") or {})
    cap = raw.get("resolution_cap") or [3840, 2160]
    if len(cap) != 2:
        raise ValueError("AV1 resolution cap must contain width and height.")
    speed = str(raw.get("speed_class", "very_fast"))
    if speed not in {"fast", "very_fast"}:
        raise ValueError("AV1 speed class is invalid.")
    return {
        "resolution_cap": [int(cap[0]), int(cap[1])],
        "fps_cap": float(raw.get("fps_cap", 120)),
        "speed_class": speed,
        "preset": 8 if speed == "fast" else 10,
        "crf": int(raw.get("crf", 30)),
        "pix_fmt": "yuv420p",
        "encoder": "libsvtav1",
        "algorithm": AV1_ALGORITHM,
        "algorithm_version": AV1_ALGORITHM_VERSION,
        "contract_version": "av1-svt-v2",
    }


def analyze_source(
    source: Path,
    cancelled=None,
    progress=None,
    *,
    deep_timing: bool = True,
    metadata_timeout: float = PLAN_METADATA_TIMEOUT_SECONDS,
    timing_timeout: float = EXECUTION_PREFLIGHT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    if progress:
        progress("Inspecting video streams")
    result = _run_ffprobe(
        [
            "ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
            "-show_chapters", str(source),
        ],
        cancelled,
        timeout=metadata_timeout,
    )
    if result.returncode:
        raise ValueError("FFprobe could not read the video source.")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("FFprobe returned invalid media metadata.") from error
    streams = payload.get("streams") or []
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    if len(video) != 1:
        raise ValueError("AV1 archival supports exactly one video stream.")
    video_stream = video[0]
    rotation = _rotation(video_stream)
    fps = _ratio(video_stream.get("avg_frame_rate")) or _ratio(video_stream.get("r_frame_rate"))
    duration = _number(video_stream.get("duration")) or _number((payload.get("format") or {}).get("duration"))
    timing_cfr = None
    timing = {"status": "preflight_required"}
    if deep_timing:
        if progress:
            progress("Checking video frame timing")
        timing_cfr, timing = _timing_is_cfr(
            source, video_stream, duration, fps, cancelled=cancelled,
            timeout=timing_timeout, return_details=True,
        )
    format_tags = dict((payload.get("format") or {}).get("tags") or {})
    return {
        "streams": streams,
        "video": video_stream,
        "audio": [stream for stream in streams if stream.get("codec_type") == "audio"],
        "ancillary": _ancillary_summary(streams),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "display_width": int(video_stream.get("height") or 0) if rotation in {90, 270} else int(video_stream.get("width") or 0),
        "display_height": int(video_stream.get("width") or 0) if rotation in {90, 270} else int(video_stream.get("height") or 0),
        "fps": fps,
        "duration": duration,
        "rotation": rotation,
        "codec": video_stream.get("codec_name"),
        "pix_fmt": video_stream.get("pix_fmt"),
        "color_space": video_stream.get("color_space"),
        "color_transfer": video_stream.get("color_transfer"),
        "color_primaries": video_stream.get("color_primaries"),
        "color_range": video_stream.get("color_range"),
        "bits_per_raw_sample": int(video_stream.get("bits_per_raw_sample") or 8),
        "format_tags": format_tags,
        "video_tags": dict(video_stream.get("tags") or {}),
        "chapters": payload.get("chapters") or [],
        "timing_cfr": timing_cfr,
        "timing": timing,
        "preflight_required": not deep_timing,
    }


def source_blocker(source: Path, profile: dict[str, object] | None) -> str | None:
    if source.suffix.casefold() not in AV1_SUPPORTED_EXTENSIONS:
        return "This video container is not supported by production AV1 compression."
    try:
        info = analyze_source(source)
    except ProbeTimeoutError as error:
        return str(error)
    except ValueError as error:
        return str(error)
    return source_blocker_from_info(info, profile)


def source_blocker_from_info(info: dict[str, object], profile: dict[str, object] | None, *, require_timing: bool = True) -> str | None:
    if not info.get("width") or not info.get("height") or not info.get("duration") or not info.get("fps"):
        return "Video dimensions, duration, and frame rate must be known for AV1 execution."
    unsupported = [stream for stream in info.get("ancillary") or [] if stream.get("codec_type") == "subtitle" and not _ancillary_supported(stream)]
    if unsupported:
        descriptions = ", ".join(_ancillary_description(stream) for stream in unsupported[:3])
        suffix = f"; {len(unsupported) - 3} more" if len(unsupported) > 3 else ""
        return f"{descriptions}{suffix} cannot be preserved in the MP4 output."
    if _source_bit_depth(info) > 10:
        return "Video bit depth above 10-bit is not supported by the production AV1 contract."
    if not _supported_video_pixel_format(info.get("pix_fmt")):
        return f"Video pixel format {info.get('pix_fmt') or 'unknown'} cannot be converted safely to AV1 4:2:0."
    if require_timing and info.get("timing_cfr") is None:
        return "Video frame timing could not be determined safely."
    settings = profile_settings(profile)
    if float(info.get("fps") or 0) <= 0 or float(settings["fps_cap"]) <= 0:
        return "Video frame rate is invalid for AV1 execution."
    return None


def capped_dimensions(info: dict[str, object], profile: dict[str, object] | None) -> tuple[int, int]:
    settings = profile_settings(profile)
    max_width, max_height = settings["resolution_cap"]
    source_width = int(info["width"])
    source_height = int(info["height"])
    rotation = int(info.get("rotation") or 0) % 360
    display_width = source_height if rotation in {90, 270} else source_width
    display_height = source_width if rotation in {90, 270} else source_height
    if display_width < display_height:
        max_width, max_height = max_height, max_width
    scale = min(1.0, max_width / display_width, max_height / display_height)
    display_target_width = max(2, int(math.floor(display_width * scale / 2) * 2))
    display_target_height = max(2, int(math.floor(display_height * scale / 2) * 2))
    if rotation in {90, 270}:
        width, height = display_target_height, display_target_width
    else:
        width, height = display_target_width, display_target_height
    return width, height


def build_command(source: Path, output: Path, info: dict[str, object], profile: dict[str, object] | None) -> list[str]:
    settings = profile_settings(profile)
    width, height = capped_dimensions(info, profile)
    pixel_format = output_pixel_format(info)
    filters = []
    if (width, height) != (int(info["width"]), int(info["height"])):
        filters.append(f"scale={width}:{height}:flags=lanczos")
    if float(info["fps"]) > float(settings["fps_cap"]) + 0.001:
        filters.append(f"fps={settings['fps_cap']:g}")
    arguments = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-noautorotate", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-map_chapters", "0",
        "-c:v", "libsvtav1", "-preset", str(settings["preset"]), "-crf", str(settings["crf"]),
        "-pix_fmt", pixel_format, "-fps_mode", "passthrough",
    ]
    audio_streams = info.get("audio") or []
    for index, stream in enumerate(audio_streams):
        if stream.get("codec_name") in AV1_AUDIO_CODECS:
            arguments.extend([f"-c:a:{index}", "copy"])
        else:
            arguments.extend([f"-c:a:{index}", "aac", f"-b:a:{index}", "192k"])
    subtitle_streams = [stream for stream in info.get("ancillary") or [] if stream.get("codec_type") == "subtitle"]
    if subtitle_streams:
        arguments.extend(["-map", "0:s?", "-c:s", "copy"])
        for index, stream in enumerate(subtitle_streams):
            codec = str(stream.get("codec_name") or stream.get("codec_tag_string") or "").casefold()
            arguments.extend([f"-c:s:{index}", "copy" if codec == "mov_text" else "mov_text"])
    if filters:
        arguments.extend(["-vf", ",".join(filters)])
    for key, option in (("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"), ("color_space", "-colorspace"), ("color_range", "-color_range")):
        if info.get(key):
            arguments.extend([option, str(info[key])])
    if int(info.get("rotation") or 0):
        arguments.extend(["-metadata:s:v:0", f"rotate={int(info['rotation'])}"])
    arguments.extend(["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-f", "mp4", str(output)])
    return arguments


def encode(source: Path, output: Path, info: dict[str, object], profile: dict[str, object], cancel, progress=None) -> dict[str, object]:
    process = subprocess.Popen(build_command(source, output, info, profile), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if cancel.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise InterruptedError("Execution cancelled during AV1 encoding.")
            if line.startswith("out_time_ms=") and progress:
                value = _number(line.split("=", 1)[1])
                if value is not None and info.get("duration"):
                    progress(min(1.0, value / 1_000_000 / float(info["duration"])))
        stderr = process.stderr.read() if process.stderr else ""
        returncode = process.wait()
        if returncode:
            raise RuntimeError(stderr.strip() or "FFmpeg AV1 encoding failed.")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    return {"size_bytes": output.stat().st_size, "sha256": _sha256(output)}


def validate(source: Path, output: Path, source_info: dict[str, object], profile: dict[str, object]) -> dict[str, object]:
    if not output.is_file() or output.stat().st_size <= 0:
        raise ValueError("AV1 encoder returned an empty output.")
    output_info = analyze_source(output, deep_timing=True, metadata_timeout=EXECUTION_PREFLIGHT_TIMEOUT_SECONDS, timing_timeout=EXECUTION_PREFLIGHT_TIMEOUT_SECONDS)
    _validate_output_contract(output_info, source_info, profile)
    _validate_samples(source, output, source_info, output_info)
    report = preservation_report(source_info, output_info, profile)
    return {
        "size_bytes": output.stat().st_size,
        "sha256": _sha256(output),
        "source": metadata_summary(source_info),
        "output": metadata_summary(output_info),
        "settings": {**profile_settings(profile), "pix_fmt": output_pixel_format(source_info)},
        "preservation_report": report,
    }


def validate_final(output: Path, source_info: dict[str, object], profile: dict[str, object], expected_sha256: str | None = None) -> dict[str, object]:
    if not output.is_file() or output.stat().st_size <= 0:
        raise ValueError("AV1 final output is unavailable.")
    output_info = analyze_source(output, deep_timing=True, metadata_timeout=EXECUTION_PREFLIGHT_TIMEOUT_SECONDS, timing_timeout=EXECUTION_PREFLIGHT_TIMEOUT_SECONDS)
    _validate_output_contract(output_info, source_info, profile)
    sha256 = _sha256(output)
    if expected_sha256 and sha256 != expected_sha256:
        raise ValueError("AV1 final output hash did not match the validated temporary output.")
    return {
        "size_bytes": output.stat().st_size,
        "sha256": sha256,
        "source": metadata_summary(source_info),
        "output": metadata_summary(output_info),
        "settings": {**profile_settings(profile), "pix_fmt": output_pixel_format(source_info)},
        "preservation_report": preservation_report(source_info, output_info, profile),
    }


def _validate_output_contract(output_info: dict[str, object], source_info: dict[str, object], profile: dict[str, object]) -> None:
    if output_info["codec"] != "av1":
        raise ValueError("AV1 output did not contain an AV1 video stream.")
    expected_width, expected_height = capped_dimensions(source_info, profile)
    if (output_info["width"], output_info["height"]) != (expected_width, expected_height):
        raise ValueError("AV1 output dimensions did not match the profile cap.")
    if float(output_info["fps"]) > min(float(source_info["fps"]), float(profile_settings(profile)["fps_cap"])) + 0.01:
        raise ValueError("AV1 output frame rate exceeded the source or profile cap.")
    if len(output_info["audio"]) != len(source_info["audio"]):
        raise ValueError("AV1 output did not preserve all supported audio tracks.")
    if _ancillary_contract(output_info.get("ancillary")) != _ancillary_contract(source_info.get("ancillary")):
        raise ValueError("AV1 output did not preserve supported ancillary streams.")
    if output_info["rotation"] != source_info["rotation"]:
        raise ValueError("AV1 output rotation metadata did not match the source.")
    cap_applied = float(source_info["fps"]) > float(profile_settings(profile)["fps_cap"]) + 0.01
    if source_info.get("timing_cfr") is False and output_info.get("timing_cfr") is True and not cap_applied:
        raise ValueError("AV1 output changed variable source timing to constant frame rate unexpectedly.")
    expected_display_width = expected_width if output_info["rotation"] not in {90, 270} else expected_height
    expected_display_height = expected_height if output_info["rotation"] not in {90, 270} else expected_width
    if (output_info["display_width"], output_info["display_height"]) != (expected_display_width, expected_display_height):
        raise ValueError("AV1 output display dimensions did not match the profile cap.")
    source_duration = float(source_info["duration"] or 0)
    output_duration = float(output_info["duration"] or 0)
    if not output_duration or abs(output_duration - source_duration) > max(0.25, source_duration * 0.02):
        raise ValueError("AV1 output duration did not match the source within tolerance.")
    expected_pixel_format = output_pixel_format(source_info)
    if output_info["pix_fmt"] not in {expected_pixel_format, "yuv420p", "yuv420p10le"} or output_info["bits_per_raw_sample"] > 10:
        raise ValueError("AV1 output bit-depth or pixel format did not match the selected preservation contract.")


def metadata_summary(info: dict[str, object]) -> dict[str, object]:
    value = _summary(info)
    fps = value.get("fps")
    if isinstance(fps, Fraction):
        value["fps"] = f"{fps.numerator}/{fps.denominator}"
    return value


def preservation_report(source_info: dict[str, object], output_info: dict[str, object], profile: dict[str, object] | None) -> dict[str, object]:
    settings = profile_settings(profile)
    report = {"preserved": [], "changed": [], "lost": [], "hard_blockers": []}
    source_display = f"{source_info.get('display_width')}×{source_info.get('display_height')}"
    output_display = f"{output_info.get('display_width')}×{output_info.get('display_height')}"
    if source_display == output_display:
        report["preserved"].append({"kind": "resolution", "severity": "info", "source": source_display, "output": output_display, "message": "Display resolution is preserved."})
    else:
        report["changed"].append({"kind": "resolution", "severity": "info", "source": source_display, "output": output_display, "message": "Resolution is reduced by the selected profile cap."})
    source_fps = source_info.get("fps")
    output_fps = output_info.get("fps")
    if source_fps == output_fps or (source_fps and output_fps and float(source_fps) <= float(settings["fps_cap"]) and abs(float(source_fps) - float(output_fps)) < 0.02):
        report["preserved"].append({"kind": "framerate", "severity": "info", "source": _fps_text(source_fps), "output": _fps_text(output_fps), "message": "Source timing is preserved."})
    else:
        report["changed"].append({"kind": "framerate", "severity": "info", "source": _fps_text(source_fps), "output": _fps_text(output_fps), "message": "Frame rate is reduced to the selected profile cap."})
    source_pix = str(source_info.get("pix_fmt") or "unknown")
    output_pix = str(output_info.get("pix_fmt") or output_pixel_format(source_info))
    if "422" in source_pix or "444" in source_pix or source_pix != output_pix:
        report["changed"].append({"kind": "chroma_subsampling", "severity": "info", "source": source_pix, "output": output_pix, "message": "Chroma/pixel format is converted for AV1."})
    else:
        report["preserved"].append({"kind": "pixel_format", "severity": "info", "source": source_pix, "output": output_pix, "message": "Pixel format is preserved within the AV1 contract."})
    source_audio = source_info.get("audio") or []
    output_audio = output_info.get("audio") or []
    if len(source_audio) == len(output_audio):
        report["preserved"].append({"kind": "audio_tracks", "severity": "info", "source": str(len(source_audio)), "output": str(len(output_audio)), "message": "Audio tracks remain separate."})
    for left, right in zip(source_audio, output_audio):
        if left.get("codec_name") != right.get("codec_name"):
            report["changed"].append({"kind": "audio_codec", "severity": "info", "source": left.get("codec_name") or "unknown", "output": right.get("codec_name") or "unknown", "message": "An incompatible audio track was transcoded instead of dropped."})
    source_subtitles = [stream for stream in source_info.get("ancillary") or [] if stream.get("codec_type") == "subtitle"]
    output_subtitles = [stream for stream in output_info.get("ancillary") or [] if stream.get("codec_type") == "subtitle"]
    if len(source_subtitles) == len(output_subtitles):
        report["preserved"].append({"kind": "subtitle_tracks", "severity": "info", "source": str(len(source_subtitles)), "output": str(len(output_subtitles)), "message": "Subtitle tracks remain represented."})
    for stream in source_info.get("ancillary") or []:
        if stream.get("codec_type") in {"data", "attachment"}:
            report["lost"].append({"kind": f"{stream.get('codec_type')}_stream", "severity": "warning", "message": f"{_ancillary_description(stream)} is not carried into the MP4 output."})
    for key, label in (("color_transfer", "transfer"), ("color_primaries", "primaries"), ("color_space", "matrix"), ("color_range", "range")):
        source_value = source_info.get(key)
        output_value = output_info.get(key)
        if source_value and source_value == output_value:
            report["preserved"].append({"kind": f"color_{label}", "severity": "info", "source": source_value, "output": output_value, "message": f"Color {label} is preserved."})
        elif source_value:
            report["lost"].append({"kind": f"color_{label}", "severity": "warning", "source": source_value, "output": output_value, "message": f"Source color {label} changed in the output."})
    return report


def output_pixel_format(info: dict[str, object]) -> str:
    return "yuv420p10le" if _source_bit_depth(info) > 8 or info.get("color_transfer") in AV1_HDR_TRANSFERS else "yuv420p"


def _fps_text(value) -> str:
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    return str(value or "unknown")


def _validate_samples(source: Path, output: Path, source_info: dict[str, object], output_info: dict[str, object]) -> None:
    duration = float(source_info["duration"])
    for timestamp in (0.1, max(0.1, duration / 2), max(0.1, duration - 0.1)):
        left = _sample(source, min(timestamp, max(0.0, duration - 0.01)))
        right = _sample(output, min(timestamp, max(0.0, float(output_info["duration"] or duration) - 0.01)))
        try:
            left_mean = sum(ImageStat.Stat(left).mean) / 3
            right_mean = sum(ImageStat.Stat(right).mean) / 3
            left_range = max(high for _, high in left.getextrema()) - min(low for low, _ in left.getextrema())
            right_range = max(high for _, high in right.getextrema()) - min(low for low, _ in right.getextrema())
            if left_mean > 12 and right_mean < left_mean * 0.03:
                raise ValueError("AV1 output failed catastrophic visual validation.")
            if left_range > 48 and right_range < 3:
                raise ValueError("AV1 output failed catastrophic visual validation.")
        finally:
            left.close()
            right.close()


def _sample(path: Path, timestamp: float) -> Image.Image:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}", "-i", str(path), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode or not result.stdout:
        raise ValueError("AV1 sample frame could not be decoded.")
    from io import BytesIO

    with Image.open(BytesIO(result.stdout)) as image:
        return image.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)


def _summary(info: dict[str, object]) -> dict[str, object]:
    summary = {
        key: info.get(key)
        for key in (
            "width", "height", "display_width", "display_height", "fps", "duration", "rotation", "codec",
            "pix_fmt", "color_space", "color_transfer", "color_primaries", "color_range",
            "bits_per_raw_sample", "format_tags", "video_tags", "chapters", "timing_cfr",
        )
    }
    summary["audio"] = [
        {
            "codec_name": stream.get("codec_name"),
            "tags": _semantic_tags(stream.get("tags")),
            "disposition": _stream_disposition(stream),
        }
        for stream in (info.get("audio") or [])
    ]
    summary["ancillary"] = info.get("ancillary") or []
    return summary


def _is_cfr(stream: dict[str, object]) -> bool:
    average = _ratio(stream.get("avg_frame_rate"))
    real = _ratio(stream.get("r_frame_rate"))
    return average is not None and real is not None and abs(float(average) - float(real)) < 0.01


def _timing_is_cfr(source: Path, stream: dict[str, object], duration: float | None, fps: Fraction | None, *, cancelled=None, timeout: float = EXECUTION_PREFLIGHT_TIMEOUT_SECONDS, return_details: bool = False):
    def pack(value, details):
        return (value, details) if return_details else value
    if duration is None or fps is None or float(fps) <= 0:
        return pack(None, {})
    nominal = 1.0 / float(fps)
    starts = [0.0, max(0.0, duration / 2.0), max(0.0, duration - 2.0)]
    intervals = []
    for start in starts:
        value = f"{start:.6f}%+#80"
        if value not in intervals:
            intervals.append(value)
    probe = _run_ffprobe(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-read_intervals", ",".join(intervals),
            "-show_entries", "frame=best_effort_timestamp_time,pkt_duration_time", "-of", "json", str(source),
        ],
        cancelled,
        timeout=timeout,
    )
    if probe.returncode:
        return pack(None, {})
    try:
        frames = json.loads(probe.stdout).get("frames") or []
    except json.JSONDecodeError:
        return pack(None, {})
    timestamps = [_number(frame.get("best_effort_timestamp_time")) for frame in frames]
    timestamps = [value for value in timestamps if value is not None]
    boundary = max(0.25, nominal * 8.0)
    deltas = [right - left for left, right in zip(timestamps, timestamps[1:]) if 0 < right - left <= boundary]
    durations = [_number(frame.get("pkt_duration_time")) for frame in frames]
    durations = [value for value in durations if value is not None and value > 0]
    observed = deltas + durations
    if len(observed) < 3:
        return pack(None, {"sample_count": len(observed)})
    tolerance = max(0.0015, nominal * 0.02)
    valid = all(abs(value - nominal) <= tolerance for value in observed)
    return pack(valid, {
        "sample_count": len(observed),
        "nominal_frame_duration": nominal,
        "min_observed_duration": min(observed),
        "max_observed_duration": max(observed),
        "interval_count": len(intervals),
    })


def _run_ffprobe(arguments: list[str], cancelled=None, *, timeout: float = PLAN_METADATA_TIMEOUT_SECONDS):
    if cancelled is None:
        try:
            return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as error:
            raise ProbeTimeoutError(f"FFprobe exceeded the {timeout:g}-second safety deadline.") from error
    process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancelled.is_set():
                _stop_process(process)
                raise InterruptedError("Plan analysis was cancelled.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _stop_process(process)
                raise ProbeTimeoutError(f"FFprobe exceeded the {timeout:g}-second safety deadline.")
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                return subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            _stop_process(process)


def _stop_process(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=0.5)


def _ancillary_summary(streams: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "index": int(stream.get("index") or 0),
            "codec_type": stream.get("codec_type"),
            "codec_name": stream.get("codec_name"),
            "codec_tag_string": stream.get("codec_tag_string"),
            "codec_tag": stream.get("codec_tag"),
            "tags": _semantic_tags(stream.get("tags")),
            "disposition": _stream_disposition(stream),
        }
        for stream in streams
        if stream.get("codec_type") not in {"video", "audio"}
    ]


def _ancillary_supported(stream: dict[str, object]) -> bool:
    codec = str(stream.get("codec_name") or stream.get("codec_tag_string") or "").casefold()
    if stream.get("codec_type") == "subtitle":
        return codec in AV1_TEXT_SUBTITLE_CODECS
    return False


def _supported_video_pixel_format(value: object) -> bool:
    pixel_format = str(value or "").casefold()
    return pixel_format.startswith(("yuv420", "yuv422", "yuv444", "nv12", "p010"))


def _source_bit_depth(info: dict[str, object]) -> int:
    value = int(info.get("bits_per_raw_sample") or 0)
    pixel_format = str(info.get("pix_fmt") or "").casefold()
    if "10" in pixel_format:
        value = max(value, 10)
    if "12" in pixel_format:
        value = max(value, 12)
    return value or 8


def _ancillary_description(stream: dict[str, object]) -> str:
    kind = str(stream.get("codec_type") or "unknown")
    codec = str(stream.get("codec_name") or stream.get("codec_tag_string") or "unknown")
    return f"Unsupported {kind} stream: {codec}"


def _ancillary_contract(streams: list[dict[str, object]] | None) -> list[dict[str, object]]:
    return [
        {
            "codec_type": "subtitle",
            "codec_name": "mov_text",
            "tags": _semantic_tags(stream.get("tags")),
        }
        for stream in streams or []
        if stream.get("codec_type") == "subtitle" and _ancillary_supported(stream)
    ]


def _metadata_blocker(info: dict[str, object]) -> str | None:
    format_tags = {str(key).casefold(): value for key, value in (info.get("format_tags") or {}).items()}
    supported_format = {
        "creation_time", "title", "comment", "description", "location", "com.apple.quicktime.location.iso6709",
        "make", "model", "artist", "album", "genre", "date", "synopsis", "show", "season_number", "episode_id",
    }
    ignored_format = {"major_brand", "minor_version", "compatible_brands", "encoder", "software", "handler_name", "vendor_id"}
    unknown = sorted(key for key, value in format_tags.items() if value not in {None, ""} and key not in supported_format | ignored_format)
    if unknown:
        return f"Video contains metadata that the current AV1 archival contract cannot safely preserve: {unknown[0]}."
    streams = [info.get("video")] + list(info.get("audio") or [])
    for stream in (stream for stream in streams if stream):
        tags = {str(key).casefold(): value for key, value in (stream.get("tags") or {}).items()}
        unknown = sorted(key for key, value in tags.items() if value not in {None, ""} and key not in {
            "creation_time", "title", "comment", "description", "location", "com.apple.quicktime.location.iso6709",
            "make", "model", "language", "handler_name", "encoder", "vendor_id",
        })
        if unknown:
            return f"Video contains metadata that the current AV1 archival contract cannot safely preserve: {unknown[0]}."
    for chapter in info.get("chapters") or []:
        unknown = sorted(
            str(key).casefold()
            for key, value in (chapter.get("tags") or {}).items()
            if value not in {None, ""} and str(key).casefold() not in {"title", "language"}
        )
        if unknown:
            return f"Video contains metadata that the current AV1 archival contract cannot safely preserve: chapter {unknown[0]}."
    return None


def _semantic_tags(tags: dict[str, object] | None) -> dict[str, str]:
    allowed = {
        "creation_time", "title", "comment", "description", "location", "com.apple.quicktime.location.iso6709",
        "make", "model", "artist", "album", "genre", "date", "synopsis", "show", "season_number", "episode_id",
        "language",
    }
    return {str(key).casefold(): str(value) for key, value in (tags or {}).items() if str(key).casefold() in allowed and value not in {None, ""}}


def _stream_disposition(stream: dict[str, object]) -> dict[str, int]:
    keys = {"default", "forced", "hearing_impaired", "visual_impaired", "attached_pic", "timed_thumbnails"}
    return {key: int(value or 0) for key, value in (stream.get("disposition") or {}).items() if key in keys}


def _chapter_summary(chapters: list[dict[str, object]] | None) -> list[dict[str, object]]:
    return [
        {
            "start_time": round(float(chapter.get("start_time") or 0), 3),
            "end_time": round(float(chapter.get("end_time") or 0), 3),
            "title": str((chapter.get("tags") or {}).get("title") or ""),
            "language": str((chapter.get("tags") or {}).get("language") or ""),
        }
        for chapter in (chapters or [])
    ]


def _rotation(stream: dict[str, object]) -> int:
    value = (stream.get("tags") or {}).get("rotate")
    if value is None:
        for item in stream.get("side_data_list") or []:
            if item.get("rotation") is not None:
                value = item["rotation"]
                break
    try:
        return int(float(value)) % 360 if value is not None else 0
    except (TypeError, ValueError):
        return 0


def _ratio(value) -> Fraction | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None


def _number(value) -> float | None:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
