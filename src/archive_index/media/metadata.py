"""Image metadata extraction and preliminary ffprobe support."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from numbers import Real
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image, UnidentifiedImageError

DECODER_GAP_EXTENSIONS = frozenset(
    {".arw", ".cr2", ".cr3", ".dng", ".heic", ".heif", ".jxl", ".nef", ".raf", ".rw2"}
)
CURATED_EXIF_FIELDS = frozenset(
    {
        "DateTimeOriginal",
        "DateTimeDigitized",
        "DateTime",
        "SubSecTimeOriginal",
        "SubSecTimeDigitized",
        "SubSecTime",
        "SubsecTimeOriginal",
        "SubsecTimeDigitized",
        "SubsecTime",
        "OffsetTimeOriginal",
        "OffsetTimeDigitized",
        "OffsetTime",
        "Make",
        "Model",
        "LensMake",
        "LensModel",
        "LensSpecification",
        "FNumber",
        "ApertureValue",
        "ExposureTime",
        "ShutterSpeedValue",
        "ISOSpeedRatings",
        "PhotographicSensitivity",
        "FocalLength",
        "FocalLengthIn35mmFilm",
        "Software",
        "ExposureBiasValue",
        "Flash",
        "WhiteBalance",
        "Orientation",
    }
)


class MetadataExtractionError(RuntimeError):
    """Raised when media metadata cannot be decoded."""


class UnsupportedDecoderError(MetadataExtractionError):
    """Raised when the file type is discovered but no decoder is available."""


@dataclass(frozen=True)
class MediaMetadata:
    values: dict[str, Any]
    capture_time: str | None = None
    capture_time_kind: str | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    codec: str | None = None


def extract_metadata(path: Path, media_type: str) -> MediaMetadata:
    if media_type == "image":
        return _extract_image_metadata(path)
    if media_type == "video":
        return _probe_video_metadata(path)
    raise MetadataExtractionError(f"unsupported media type: {media_type}")


def _extract_image_metadata(path: Path) -> MediaMetadata:
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            exif_values = _curated_ifd_values(exif.items())
            try:
                nested_exif = exif.get_ifd(ExifTags.IFD.Exif)
            except (AttributeError, KeyError, TypeError, ValueError):
                nested_exif = {}
            exif_values.update(_curated_ifd_values(nested_exif.items()))
            capture_time, capture_time_kind = _capture_time(exif_values)
            values = {
                "format": image.format,
                "mode": image.mode,
                "exif": exif_values,
            }
            gps = _normalized_gps(exif)
            if gps:
                values["gps"] = gps
            return MediaMetadata(
                values=values,
                capture_time=capture_time,
                capture_time_kind=capture_time_kind,
                width=image.width,
                height=image.height,
            )
    except UnidentifiedImageError as error:
        if path.suffix.casefold() in DECODER_GAP_EXTENSIONS:
            raise UnsupportedDecoderError(
                f"no image decoder is configured for {path.suffix.casefold()}"
            ) from error
        raise MetadataExtractionError(f"could not decode image: {path}") from error
    except (OSError, ValueError, SyntaxError) as error:
        raise MetadataExtractionError(f"could not read image metadata: {path}") from error


def _probe_video_metadata(path: Path) -> MediaMetadata:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise UnsupportedDecoderError("ffprobe is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise MetadataExtractionError(f"ffprobe timed out: {path}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        raise MetadataExtractionError(detail or f"ffprobe failed: {path}") from error

    try:
        probe = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise MetadataExtractionError("ffprobe returned invalid JSON") from error

    streams = probe.get("streams", [])
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
    format_data = probe.get("format", {}) or {}
    format_tags = format_data.get("tags") or {}
    duration = _float_or_none(video_stream.get("duration") or format_data.get("duration"))
    values = {
        "format": {
            "filename": format_data.get("filename"),
            "format_name": format_data.get("format_name"),
            "format_long_name": format_data.get("format_long_name"),
            "tags": format_tags,
        },
        "video_stream": {
            "width": video_stream.get("width"),
            "height": video_stream.get("height"),
            "codec_name": video_stream.get("codec_name"),
            "pix_fmt": video_stream.get("pix_fmt"),
            "r_frame_rate": video_stream.get("r_frame_rate"),
        },
    }
    return MediaMetadata(
        values=values,
        capture_time=_container_capture_time(format_tags),
        capture_time_kind="container_metadata" if format_tags.get("creation_time") else None,
        width=_int_or_none(video_stream.get("width")),
        height=_int_or_none(video_stream.get("height")),
        duration_seconds=duration,
        codec=video_stream.get("codec_name"),
    )


def _capture_time(exif_values: dict[str, Any]) -> tuple[str | None, str | None]:
    for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
        value = exif_values.get(key)
        if value is None:
            continue
        suffix = key.removeprefix("DateTime")
        offset = exif_values.get(f"OffsetTime{suffix}") or exif_values.get("OffsetTimeOriginal")
        subsecond = exif_values.get(f"SubSecTime{suffix}") or exif_values.get(f"SubsecTime{suffix}")
        parsed = _parse_exif_datetime(str(value), offset, subsecond)
        if parsed is not None:
            return parsed
    return None, None


def _parse_exif_datetime(value: str, offset: Any, subsecond: Any = None) -> tuple[str, str]:
    try:
        parsed = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return value, "exif_local_unknown"
    if subsecond is not None:
        digits = "".join(character for character in str(subsecond) if character.isdigit())[:6]
        if digits:
            parsed = parsed.replace(microsecond=int(digits.ljust(6, "0")))
    timezone_offset = _parse_offset(offset)
    if timezone_offset is None:
        precision = "microseconds" if parsed.microsecond else "seconds"
        return parsed.isoformat(timespec=precision), "exif_local_unknown"
    precision = "microseconds" if parsed.microsecond else "seconds"
    return parsed.replace(tzinfo=timezone_offset).isoformat(timespec=precision), "exif_offset"


def _parse_offset(value: Any) -> timezone | None:
    if not value:
        return None
    text = str(value)
    if text == "Z":
        return timezone.utc
    if len(text) == 6 and text[0] in "+-" and text[3] == ":":
        try:
            hours = int(text[1:3])
            minutes = int(text[4:6])
        except ValueError:
            return None
        if minutes > 59:
            return None
        sign = 1 if text[0] == "+" else -1
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    return None


def _container_capture_time(tags: dict[str, Any]) -> str | None:
    value = tags.get("creation_time")
    if not value:
        return None
    return str(value)


def _curated_ifd_values(items) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for tag, value in items:
        name = ExifTags.TAGS.get(tag, str(tag))
        if name not in CURATED_EXIF_FIELDS:
            continue
        normalized = _json_value(value)
        if normalized is not None:
            values[name] = normalized
    return values


def _normalized_gps(exif) -> dict[str, Any]:
    try:
        gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}
    values = {ExifTags.GPSTAGS.get(tag, str(tag)): value for tag, value in gps_ifd.items()}
    result: dict[str, Any] = {}
    latitude = _gps_coordinate(values.get("GPSLatitude"), values.get("GPSLatitudeRef"))
    longitude = _gps_coordinate(values.get("GPSLongitude"), values.get("GPSLongitudeRef"))
    altitude = _gps_number(values.get("GPSAltitude"))
    direction = _gps_number(values.get("GPSImgDirection"))
    if latitude is not None:
        result["latitude"] = latitude
    if longitude is not None:
        result["longitude"] = longitude
    if altitude is not None:
        result["altitude_m"] = altitude * (-1 if _gps_number(values.get("GPSAltitudeRef")) == 1 else 1)
    if direction is not None:
        result["image_direction_degrees"] = direction
    date = _json_value(values.get("GPSDateStamp"))
    time_values = values.get("GPSTimeStamp")
    if date is not None and isinstance(time_values, (list, tuple)) and len(time_values) >= 3:
        numbers = [_gps_number(value) for value in time_values[:3]]
        if all(number is not None for number in numbers):
            result["timestamp"] = f"{date}T{int(numbers[0]):02d}:{int(numbers[1]):02d}:{numbers[2]:06.3f}".rstrip("0").rstrip(".")
    return result


def _gps_coordinate(value: Any, reference: Any) -> float | None:
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    parts = [_gps_number(item) for item in value[:3]]
    if any(part is None for part in parts):
        return None
    coordinate = parts[0] + parts[1] / 60 + parts[2] / 3600
    if str(reference).upper() in {"S", "W"}:
        coordinate = -coordinate
    return coordinate


def _gps_number(value: Any) -> float | None:
    normalized = _json_value(value)
    if isinstance(normalized, (list, tuple)) and len(normalized) == 2:
        try:
            denominator = float(normalized[1])
            return float(normalized[0]) / denominator if denominator else None
        except (TypeError, ValueError):
            return None
    try:
        return float(normalized) if normalized is not None else None
    except (TypeError, ValueError):
        return None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Real):
        return float(value)
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        try:
            return [int(value.numerator), int(value.denominator)]
        except (TypeError, ValueError, ZeroDivisionError):
            return None
    if isinstance(value, (list, tuple)):
        normalized = [_json_value(item) for item in value]
        return normalized if all(item is not None for item in normalized) else None
    if isinstance(value, bytes):
        return None
    return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
