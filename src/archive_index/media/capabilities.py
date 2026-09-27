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


def av1_capability() -> dict[str, object]:
    capability = ffmpeg_capabilities()
    if capability["av1_available"]:
        return {
            "available": True,
            "production_ready": False,
            "encoders": capability["av1_encoders"],
            "message": "AV1 candidates are available, but production execution remains blocked pending stream, color, packaging, and recovery validation.",
        }
    return {
        "available": False,
        "production_ready": False,
        "encoders": [],
        "message": "AV1 encoder is unavailable in the current FFmpeg runtime.",
    }


@lru_cache(maxsize=1)
def jpegxl_tool_capabilities() -> dict[str, object]:
    explicit = os.environ.get("ARCHIVE_INDEX_CODEC_DIR")
    search_directories = []
    if explicit:
        search_directories.append(Path(explicit))
    package_root = Path(__file__).resolve().parent
    search_directories.extend([package_root / "codecs", Path(sys.executable).resolve().parent / "codecs"])
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
            help_result = subprocess.run([tools["cjxl"], "--help"], capture_output=True, text=True, timeout=10)
            help_text = f"{help_result.stdout}\n{help_result.stderr}"
        except (OSError, subprocess.SubprocessError):
            pass
    metadata_flags = {
        flag: flag in help_text
        for flag in ("--icc_pathname", "--icc_in", "--metadata", "--exif", "--xmp")
    }
    return {
        "available": bool(tools["cjxl"] and tools["djxl"]),
        "tools": tools,
        "version": version,
        "metadata_flags": metadata_flags,
        "message": "cjxl/djxl runtime is available for further metadata-contract validation." if tools["cjxl"] and tools["djxl"] else "Metadata-preserving cjxl/djxl tools are not available in the configured application runtime.",
    }
