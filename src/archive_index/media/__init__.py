"""Media metadata and derived-file helpers."""

from .metadata import (
    MediaMetadata,
    MetadataExtractionError,
    UnsupportedDecoderError,
    extract_metadata,
)
from .thumbnail import THUMBNAIL_SIZE, generate_thumbnail
from .quality import QualityResult, measure_quality, score_from_raw
from .quality_provider import (
    LAR_IQA_ALGORITHM,
    LAR_IQA_VERSION,
    QualityProviderError,
    QualityProviderUnavailable,
    create_quality_provider,
)

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
    "LAR_IQA_ALGORITHM",
    "LAR_IQA_VERSION",
    "QualityProviderError",
    "QualityProviderUnavailable",
    "create_quality_provider",
]
