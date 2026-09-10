"""Supported media extension registry."""

from __future__ import annotations

from pathlib import Path

IMAGE_EXTENSIONS = frozenset(
    {
        ".arw",
        ".avif",
        ".cr2",
        ".cr3",
        ".dng",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".jxl",
        ".nef",
        ".png",
        ".raf",
        ".rw2",
        ".webp",
    }
)
VIDEO_EXTENSIONS = frozenset({".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"})


def media_type_for(path: Path) -> str | None:
    extension = path.suffix.casefold()
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return None
