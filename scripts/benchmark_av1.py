"""Benchmark disposable FFmpeg AV1 candidates without touching a workspace."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

PROFILES = {
    "AV1 1080p60 Fast": ((1920, 1080), 60, 8),
    "AV1 1080p60 Very Fast": ((1920, 1080), 60, 10),
    "AV1 4K120 Fast": ((3840, 2160), 120, 8),
    "AV1 4K120 Very Fast": ((3840, 2160), 120, 10),
}
ENCODERS = ("libsvtav1", "libaom-av1", "librav1e", "av1_nvenc", "av1_qsv", "av1_amf")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=3.0)
    parser.add_argument("--size", default="640x360")
    parser.add_argument("--rate", type=int, default=30)
    parser.add_argument("--encoders", help="Comma-separated encoder candidates to benchmark")
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()
    ffmpeg_version = _run(["ffmpeg", "-version"]).stdout.splitlines()[0]
    encoders_output = _run(["ffmpeg", "-hide_banner", "-encoders"]).stdout
    requested = tuple(name.strip() for name in args.encoders.split(",")) if args.encoders else ENCODERS
    available = [name for name in requested if any(name in line.split() for line in encoders_output.splitlines())]
    with tempfile.TemporaryDirectory(prefix="archive-index-av1-") as directory:
        root = Path(directory)
        source = root / "source.mp4"
        _run([
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={args.size}:rate={args.rate}",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", str(args.duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
        ])
        results = []
        for encoder in available:
            for name, (cap, fps_cap, preset) in PROFILES.items():
                output = root / f"{encoder}-{name.lower().replace(' ', '-')}.mp4"
                fps_filter = f",fps={fps_cap}" if args.rate > fps_cap else ""
                command = [
                    "ffmpeg", "-y", "-v", "error", "-i", str(source), "-map", "0:v:0", "-map", "0:a?",
                    "-vf", f"scale=w='min(iw,{cap[0]})':h='min(ih,{cap[1]})':force_original_aspect_ratio=decrease:force_divisible_by=2{fps_filter}",
                    "-c:v", encoder, *_encoder_settings(encoder, preset), "-crf", "32", "-c:a", "copy",
                    "-map_metadata", "0", "-map_chapters", "0", "-movflags", "+use_metadata_tags", str(output),
                ]
                started = time.perf_counter()
                completed = subprocess.run(command, capture_output=True, text=True)
                elapsed = time.perf_counter() - started
                result = {
                    "profile": name,
                    "encoder": encoder,
                    "command": command,
                    "source": _probe(source),
                    "output": _probe(output) if completed.returncode == 0 and output.is_file() else None,
                    "encode_seconds": round(elapsed, 4),
                    "output_bytes": output.stat().st_size if output.is_file() else None,
                    "status": "complete" if completed.returncode == 0 else "failed",
                    "stderr": completed.stderr[-1000:] if completed.returncode else None,
                    "quality": _quality_metrics(source, output) if completed.returncode == 0 and output.is_file() else {},
                    "stream_contract": "video transcode, compatible audio stream copy, source metadata/chapter mapping; HDR/VFR validation is not established",
                }
                if result["output_bytes"] and source.stat().st_size:
                    result["compression_ratio"] = round(source.stat().st_size / result["output_bytes"], 4)
                results.append(result)
    report = {
        "ffmpeg_version": ffmpeg_version,
        "source": {"size": args.size, "fps": args.rate, "duration": args.duration, "fixture": "synthetic testsrc2 + sine"},
        "encoders_detected": available,
        "profiles": PROFILES,
        "results": results,
        "production_enabled": False,
        "production_blocker": "AV1 stream, HDR/color, VFR, packaging, and recovery validation remains incomplete.",
    }
    output = json.dumps(report, indent=2)
    if args.output_json:
        args.output_json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=True)


def _encoder_settings(encoder: str, preset: int) -> list[str]:
    if encoder == "libsvtav1":
        return ["-preset", str(preset)]
    if encoder == "libaom-av1":
        return ["-cpu-used", str(preset)]
    if encoder == "librav1e":
        return ["-speed", str(preset)]
    return ["-preset", str(preset)]


def _probe(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,pix_fmt:stream_tags=rotate", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(completed.stdout)


def _quality_metrics(source: Path, output: Path) -> dict[str, object]:
    if output is None:
        return {}
    metrics = {}
    for name in ("psnr", "ssim"):
        completed = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(source), "-i", str(output), "-lavfi", f"[0:v][1:v]{name}=stats_file=-", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        if completed.returncode == 0:
            lines = (completed.stdout + "\n" + completed.stderr).splitlines()
            if name == "psnr":
                match = next((re.search(r"psnr_avg:([^\s]+)", line) for line in reversed(lines) if "psnr_avg:" in line), None)
            else:
                match = next((re.search(r"All:([^\s]+)", line) for line in reversed(lines) if "All:" in line), None)
            metrics[name] = match.group(1) if match else None
    return metrics


if __name__ == "__main__":
    raise SystemExit(main())
