"""Cached local media capability probes."""

from __future__ import annotations

import re
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def ffmpeg_capabilities() -> dict[str, object]:
    try:
        version = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.splitlines()[0]
        encoders = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "ffmpeg_available": False,
            "version": None,
            "av1_encoders": [],
            "av1_available": False,
            "error": str(error),
        }
    names = sorted(
        {
            match.group(1)
            for match in re.finditer(r"^\s*V\S*\s+(\S+).*\bAV1", encoders, re.MULTILINE | re.IGNORECASE)
        }
    )
    return {
        "ffmpeg_available": True,
        "version": version,
        "av1_encoders": names,
        "av1_available": bool(names),
        "error": None,
    }


@lru_cache(maxsize=1)
def av1_capability() -> dict[str, object]:
    capability = ffmpeg_capabilities()
    if "libsvtav1" in capability["av1_encoders"]:
        try:
            probe = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "color=c=black:s=64x64:r=1,format=yuv420p", "-frames:v", "1",
                    "-c:v", "libsvtav1", "-preset", "10", "-crf", "30", "-f", "null", "-",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return {
                "available": True,
                "production_ready": False,
                "encoders": capability["av1_encoders"],
                "encoder": "libsvtav1",
                "message": f"libsvtav1 was detected but the production probe failed: {error}",
            }
        if probe.returncode:
            return {
                "available": True,
                "production_ready": False,
                "encoders": capability["av1_encoders"],
                "encoder": "libsvtav1",
                "message": f"libsvtav1 was detected but the production probe failed: {(probe.stderr or '').strip()[-400:]}",
            }
        return {
            "available": True,
            "production_ready": True,
            "encoders": capability["av1_encoders"],
            "encoder": "libsvtav1",
            "message": "Production AV1 is available for the validated single-video 8/10-bit subset with explicit timing and stream preservation checks.",
        }
    return {
        "available": bool(capability["ffmpeg_available"]),
        "production_ready": False,
        "encoders": capability["av1_encoders"],
        "encoder": None,
        "message": "Production AV1 requires libsvtav1 in the current FFmpeg runtime.",
    }


@lru_cache(maxsize=1)
def jpegxl_tool_capabilities() -> dict[str, object]:
    explicit = os.environ.get("ARCHIVE_INDEX_CODEC_DIR")
    search_directories = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        search_directories.append(Path(local_app_data) / "Archive Indexation" / "codecs")
    package_root = Path(__file__).resolve().parent
    search_directories.extend([
        package_root / "codecs",
        Path(sys.executable).resolve().parent / "codecs",
    ])
    if explicit:
        search_directories.append(Path(explicit))
    project_tools = Path(__file__).resolve().parents[3] / ".tools"
    if project_tools.is_dir():
        search_directories.extend(path.parent for path in project_tools.rglob("cjxl.exe"))
    tools: dict[str, str | None] = {}
    for name in ("cjxl", "djxl", "jxlinfo"):
        candidate = None
        for directory in search_directories:
            for suffix in (".exe", ""):
                path = directory / f"{name}{suffix}"
                if path.is_file():
                    candidate = str(path)
                    break
            if candidate:
                break
        tools[name] = candidate or shutil.which(name)
    version = None
    help_text = ""
    if tools["cjxl"]:
        try:
            result = subprocess.run([tools["cjxl"], "--version"], capture_output=True, text=True, timeout=10)
            version = (result.stdout or result.stderr).strip().splitlines()[0] if (result.stdout or result.stderr).strip() else None
            help_result = subprocess.run([tools["cjxl"], "--help", "-v", "-v"], capture_output=True, text=True, timeout=10)
            help_text = f"{help_result.stdout}\n{help_result.stderr}"
        except (OSError, subprocess.SubprocessError):
            pass
    metadata_flags = {
        flag: flag in help_text
        for flag in ("icc_pathname", "icc_in", "metadata", "keys 'exif'", "keys 'xmp'")
    }
    available = bool(tools["cjxl"] and tools["djxl"])
    metadata_flags["keys 'xmp'"] = "'xmp'" in help_text
    metadata_ready = available and metadata_flags["icc_pathname"] and metadata_flags["keys 'exif'"] and metadata_flags["keys 'xmp'"] and "--lossless_jpeg" in help_text
    return {
        "available": available,
        "metadata_ready": metadata_ready,
        "tools": tools,
        "version": version,
        "metadata_flags": metadata_flags,
        "message": "cjxl/djxl metadata runtime is available." if metadata_ready else "Metadata-preserving cjxl/djxl tools are not available in the configured application runtime.",
    }
