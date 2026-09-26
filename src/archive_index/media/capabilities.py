"""Cached local media capability probes."""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache


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
