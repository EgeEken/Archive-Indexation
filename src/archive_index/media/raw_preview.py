"""Optional embedded-preview extraction for RAW files."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .metadata import UnsupportedDecoderError

RAW_PREVIEW_ALGORITHM = "rawpy-embedded-preview"
RAW_PREVIEW_VERSION = "2"


@dataclass(frozen=True)
class RawPreview:
    image: Image.Image
    method: str
    width: int
    height: int


def extract_embedded_preview(source: Path) -> RawPreview:
    try:
        import rawpy
    except ImportError as error:
        raise UnsupportedDecoderError(
            "RAW preview extraction requires the optional raw-preview dependency"
        ) from error

    try:
        with rawpy.imread(str(source)) as raw:
            thumbnail = raw.extract_thumb()
            thumbnail_format = getattr(thumbnail, "format", None)
            format_name = str(getattr(thumbnail_format, "name", thumbnail_format)).upper()
            payload = thumbnail.data
            if "JPEG" in format_name or isinstance(payload, (bytes, bytearray)):
                with Image.open(BytesIO(bytes(payload))) as image:
                    oriented = ImageOps.exif_transpose(image).convert("RGB")
                    preview = oriented.copy()
            else:
                preview = ImageOps.exif_transpose(Image.fromarray(payload)).convert("RGB").copy()
    except UnsupportedDecoderError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, TypeError, RuntimeError) as error:
        raise UnsupportedDecoderError(
            f"an adequate embedded RAW preview is unavailable for {source.name}"
        ) from error
    except Exception as error:
        raise UnsupportedDecoderError(
            f"an adequate embedded RAW preview is unavailable for {source.name}"
        ) from error
    return RawPreview(preview, RAW_PREVIEW_ALGORITHM, preview.width, preview.height)
