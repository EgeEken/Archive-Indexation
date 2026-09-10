"""Media metadata and derived-file helpers."""

from .metadata import (
    MediaMetadata,
    MetadataExtractionError,
    UnsupportedDecoderError,
    extract_metadata,
)
from .thumbnail import THUMBNAIL_SIZE, generate_thumbnail
from .quality import QualityResult, measure_quality, score_from_raw

__all__ = [
    "MediaMetadata",
    "MetadataExtractionError",
    "THUMBNAIL_SIZE",
    "UnsupportedDecoderError",
    "extract_metadata",
    "generate_thumbnail",
    "QualityResult",
    "measure_quality",
    "score_from_raw",
]
