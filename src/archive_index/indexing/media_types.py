"""Compatibility import for indexing modules."""

from ..media_types import (
    IMAGE_EXTENSIONS,
    RAW_EXTENSIONS,
    RENDERED_IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    is_raw_extension,
    is_rendered_image_extension,
    media_type_for,
)

__all__ = [
    "IMAGE_EXTENSIONS", "RAW_EXTENSIONS", "RENDERED_IMAGE_EXTENSIONS", "VIDEO_EXTENSIONS",
    "is_raw_extension", "is_rendered_image_extension", "media_type_for",
]
