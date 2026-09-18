"""Optional embedded-preview extraction for RAW files."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, ImageOps, UnidentifiedImageError

from .metadata import UnsupportedDecoderError

RAW_PREVIEW_ALGORITHM = "rawpy-embedded-preview"
RAW_PREVIEW_VERSION = "2"


@dataclass(frozen=True)
class RawPreview:
    image: Image.Image
    method: str
    width: int
    height: int
    metadata: dict[str, Any] = field(default_factory=dict)
    capture_time: str | None = None
    capture_time_kind: str | None = None
    source_width: int | None = None
    source_height: int | None = None


def extract_embedded_preview(source: Path) -> RawPreview:
    try:
        import rawpy
    except ImportError as error:
        raise UnsupportedDecoderError(
            "RAW preview extraction requires the optional raw-preview dependency"
        ) from error

    source_width = source_height = None
    raw_exif: dict[str, Any] = {}
    try:
        with rawpy.imread(str(source)) as raw:
            source_width, source_height = _raw_dimensions(raw)
            raw_exif = _raw_camera_metadata(raw)
            thumbnail = raw.extract_thumb()
            thumbnail_format = getattr(thumbnail, "format", None)
            format_name = str(getattr(thumbnail_format, "name", thumbnail_format)).upper()
            payload = thumbnail.data
            exif_values: dict[str, Any] = {}
            gps: dict[str, Any] = {}
            if "JPEG" in format_name or isinstance(payload, (bytes, bytearray)):
                with Image.open(BytesIO(bytes(payload))) as image:
                    exif = image.getexif()
                    exif_values, gps = _preview_exif(exif)
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
    exif = {
        **exif_values,
        **raw_exif,
    }
    values: dict[str, Any] = {
        "format": "RAW",
        "raw_preview": {
            "method": RAW_PREVIEW_ALGORITHM,
            "width": preview.width,
            "height": preview.height,
            "source_width": source_width,
            "source_height": source_height,
        },
        "exif": exif,
    }
    if gps:
        values["gps"] = gps
    from .metadata import _capture_time

    capture_time, capture_time_kind = _capture_time(exif)
    return RawPreview(
        preview,
        RAW_PREVIEW_ALGORITHM,
        preview.width,
        preview.height,
        values,
        capture_time,
        capture_time_kind,
        source_width,
        source_height,
    )


def _preview_exif(exif) -> tuple[dict[str, Any], dict[str, Any]]:
    from .metadata import _curated_ifd_values, _normalized_gps

    values = _curated_ifd_values(exif.items())
    try:
        nested = exif.get_ifd(ExifTags.IFD.Exif)
    except (AttributeError, KeyError, TypeError, ValueError):
        nested = {}
    values.update(_curated_ifd_values(nested.items()))
    return values, _normalized_gps(exif)


def _raw_dimensions(raw) -> tuple[int | None, int | None]:
    sizes = getattr(raw, "sizes", None)
    width = _positive_int(getattr(sizes, "width", None)) or _positive_int(getattr(raw, "raw_width", None))
    height = _positive_int(getattr(sizes, "height", None)) or _positive_int(getattr(raw, "raw_height", None))
    return width, height


def _raw_camera_metadata(raw) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in ("camera_make", "camera_model"):
        value = getattr(raw, name, None)
        if value:
            values[{"camera_make": "Make", "camera_model": "Model"}[name]] = _metadata_text(value)
    lens = getattr(raw, "lens", None)
    if lens:
        value = _metadata_text(lens, ("model", "name", "lens_model"))
        if value:
            values["LensModel"] = value
    timestamp = getattr(raw, "timestamp", None)
    if timestamp:
        values.setdefault("DateTimeOriginal", _metadata_text(timestamp))
    return values


def _metadata_text(value: Any, attributes: tuple[str, ...] = ("value",)) -> str:
    if isinstance(value, (str, int, float)):
        return str(value)
    for attribute in attributes:
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, (str, int, float)) and str(candidate):
            return str(candidate)
    return ""


def _positive_int(value) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
