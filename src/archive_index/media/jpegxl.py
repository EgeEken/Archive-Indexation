"""Shared JPEG XL production and preview settings/validation."""

from __future__ import annotations

import hashlib
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageOps, ImageStat

JXL_ALGORITHM = "imagecodecs-jpegxl-archival"
JXL_ALGORITHM_VERSION = "1"
JXL_SUPPORTED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png"})
JXL_SOURCE_REPLACEMENT_BLOCKER = (
    "Source replacement is disabled because the current JPEG XL encoder cannot preserve required source metadata."
)
JXL_ICC_BLOCKER = (
    "JPEG XL compression is unavailable for this image because its embedded ICC color profile cannot currently be preserved safely."
)


def quality_to_distance(quality: float) -> float:
    value = max(0.0, min(100.0, float(quality)))
    return round((100.0 - value) * 1.5 / 40.0, 4)


def distance_to_quality(distance: float) -> float:
    return round(max(0.0, min(100.0, 100.0 - float(distance) * 40.0 / 1.5)), 2)


def profile_settings(profile: dict[str, object] | None) -> dict[str, object]:
    settings = dict((profile or {}).get("settings") or {})
    quality = float(settings.get("quality", distance_to_quality(settings.get("distance", 1.5))))
    effort = int(settings.get("effort", 7))
    if not 0 <= quality <= 100:
        raise ValueError("JPEG XL quality must be between 0 and 100.")
    if not 1 <= effort <= 9:
        raise ValueError("JPEG XL effort must be between 1 and 9.")
    return {
        "quality": round(quality, 2),
        "distance": quality_to_distance(quality),
        "effort": effort,
        "algorithm": JXL_ALGORITHM,
        "algorithm_version": JXL_ALGORITHM_VERSION,
    }


@lru_cache(maxsize=1)
def production_capability() -> dict[str, object]:
    try:
        import imagecodecs
    except ImportError as error:
        return {
            "decoder_available": False,
            "production_encoder_available": False,
            "source_replacement_available": False,
            "icc_preservation_available": False,
            "decoder_version": None,
            "encoder_version": None,
            "message": "JPEG XL encoder is unavailable because imagecodecs is not installed.",
            "error": str(error),
        }
    encoder = getattr(imagecodecs, "jpegxl_encode", None)
    decoder = getattr(imagecodecs, "jpegxl_decode", None)
    if encoder is None or decoder is None:
        return {
            "decoder_available": decoder is not None,
            "production_encoder_available": False,
            "source_replacement_available": False,
            "icc_preservation_available": False,
            "decoder_version": None,
            "encoder_version": None,
            "message": "JPEG XL encoder is unavailable in the current imagecodecs runtime.",
            "error": None,
        }
    try:
        encoder(np.zeros((2, 2, 3), dtype=np.uint8), distance=1.5, effort=7)
    except Exception as error:
        return {
            "decoder_available": True,
            "production_encoder_available": False,
            "source_replacement_available": False,
            "icc_preservation_available": False,
            "decoder_version": str(imagecodecs.jpegxl_version()),
            "encoder_version": str(imagecodecs.jpegxl_version()),
            "message": "JPEG XL encoder probe failed in the current runtime.",
            "error": str(error),
        }
    return {
        "decoder_available": True,
        "production_encoder_available": True,
        "source_replacement_available": False,
        "icc_preservation_available": False,
        "decoder_version": str(imagecodecs.jpegxl_version()),
        "encoder_version": str(imagecodecs.jpegxl_version()),
        "message": JXL_SOURCE_REPLACEMENT_BLOCKER,
        "error": None,
    }


def load_source(source: Path) -> tuple[Image.Image, dict[str, object]]:
    with Image.open(source) as original:
        mode = original.mode
        if mode not in {"RGB", "RGBA"}:
            raise ValueError("Production JPEG XL supports 8-bit RGB and RGBA JPEG/PNG sources only.")
        metadata = _metadata_contract(original)
        image = ImageOps.exif_transpose(original).copy()
    return image, metadata


def source_blocker(source: Path) -> str | None:
    try:
        with Image.open(source) as image:
            if image.mode not in {"RGB", "RGBA"}:
                return "Production JPEG XL supports 8-bit RGB and RGBA JPEG/PNG sources only."
            if image.info.get("icc_profile"):
                return JXL_ICC_BLOCKER
    except Exception as error:
        return f"Source cannot be read by production JPEG XL: {error}"
    return None


def encode(image: Image.Image, profile: dict[str, object] | None) -> bytes:
    import imagecodecs

    if image.mode not in {"RGB", "RGBA"}:
        raise ValueError("Production JPEG XL supports 8-bit RGB and RGBA images only.")
    settings = profile_settings(profile)
    return imagecodecs.jpegxl_encode(
        np.asarray(image),
        distance=settings["distance"],
        effort=settings["effort"],
    )


def decode(encoded: bytes) -> Image.Image:
    import imagecodecs

    value = np.asarray(imagecodecs.jpegxl_decode(encoded))
    if value.ndim not in {2, 3} or value.size == 0:
        raise ValueError("JPEG XL decoder returned an invalid image.")
    if value.dtype.kind == "f":
        value = np.clip(value, 0, 1 if value.max(initial=0) <= 1 else 255)
        if value.max(initial=0) <= 1:
            value = value * 255
        value = value.astype("uint8")
    elif value.dtype != np.uint8:
        value = np.clip(value, 0, 255).astype("uint8")
    if value.ndim == 3 and value.shape[2] not in {1, 3, 4}:
        raise ValueError("JPEG XL decoder returned unsupported channels.")
    image = Image.fromarray(value)
    return image.convert("RGB") if image.mode == "L" else image


def validate(encoded: bytes, expected: Image.Image) -> dict[str, object]:
    if not encoded:
        raise ValueError("JPEG XL encoder returned an empty output.")
    decoded = decode(encoded)
    try:
        expected_channels = len(expected.getbands())
        decoded_channels = len(decoded.getbands())
        if decoded.size != expected.size:
            raise ValueError("JPEG XL output dimensions did not match the source.")
        if expected_channels != decoded_channels:
            raise ValueError("JPEG XL output channel/alpha semantics did not match the source.")
        alpha_exact = None
        if "A" in expected.getbands():
            alpha_exact = _alpha_equal(expected, decoded)
            if not alpha_exact:
                raise ValueError("JPEG XL output alpha did not match the source exactly.")
        mse = _mse(expected, decoded)
        if _catastrophic(expected, decoded):
            raise ValueError("JPEG XL output failed catastrophic visual validation.")
        return {
            "size_bytes": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "width": decoded.width,
            "height": decoded.height,
            "channels": decoded_channels,
            "mse": round(mse, 6) if math.isfinite(mse) else None,
            "alpha_exact": alpha_exact,
            "metadata_contract": "source-retained; encoder does not embed required EXIF/ICC/XMP metadata",
        }
    finally:
        decoded.close()


def _metadata_contract(image: Image.Image) -> dict[str, object]:
    exif = image.getexif()
    return {
        "orientation": int(exif.get(274, 1) or 1),
        "has_exif": bool(exif),
        "has_gps": 34853 in exif,
        "has_icc": bool(image.info.get("icc_profile")),
        "has_xmp": bool(image.info.get("xmp")),
        "alpha": "A" in image.getbands(),
        "mode": image.mode,
        "metadata_policy": "source-retained",
    }


def _alpha_equal(left: Image.Image, right: Image.Image) -> bool:
    difference = ImageChops.difference(left.getchannel("A"), right.getchannel("A"))
    try:
        return difference.getbbox() is None
    finally:
        difference.close()


def _mse(left: Image.Image, right: Image.Image) -> float:
    left_rgb = left.convert("RGB")
    right_rgb = right.convert("RGB")
    difference = ImageChops.difference(left_rgb, right_rgb)
    try:
        width, height = difference.size
        return sum(ImageStat.Stat(difference).sum2) / (width * height * 3)
    finally:
        difference.close()
        left_rgb.close()
        right_rgb.close()


def _catastrophic(expected: Image.Image, actual: Image.Image) -> bool:
    expected_small = expected.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
    actual_small = actual.convert("RGB").resize((32, 32), Image.Resampling.BILINEAR)
    try:
        expected_mean = sum(ImageStat.Stat(expected_small).mean) / 3
        actual_mean = sum(ImageStat.Stat(actual_small).mean) / 3
        expected_extrema = expected_small.getextrema()
        actual_extrema = actual_small.getextrema()
        expected_range = max(high for _, high in expected_extrema) - min(low for low, _ in expected_extrema)
        actual_range = max(high for _, high in actual_extrema) - min(low for low, _ in actual_extrema)
        return (expected_mean > 12 and actual_mean < expected_mean * 0.03) or (
            expected_range > 48 and actual_range < 3
        )
    finally:
        expected_small.close()
        actual_small.close()
