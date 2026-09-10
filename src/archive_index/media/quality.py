"""Fixed, technical image-quality measurements and score calibration."""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageChops, ImageFilter, ImageStat

QUALITY_ANALYSIS_SIZE = (320, 320)
QUALITY_MEASUREMENT_VERSION = "1"
QUALITY_SCORE_VERSION = "1"
QUALITY_ALGORITHM = "pillow-technical-quality"


@dataclass(frozen=True)
class QualityResult:
    raw: dict[str, float | int | str]
    components: dict[str, float]
    score: float


def measure_quality(image: Image.Image) -> QualityResult:
    working = image.copy()
    working.thumbnail(QUALITY_ANALYSIS_SIZE, Image.Resampling.LANCZOS)
    gray = working.convert("L")
    width, height = gray.size
    pixels = width * height
    histogram = gray.histogram()
    underexposed = sum(histogram[:9]) / pixels
    overexposed = sum(histogram[248:]) / pixels
    contrast = ImageStat.Stat(gray).stddev[0]
    focus = _mean_gradient(gray)
    median = gray.filter(ImageFilter.MedianFilter(3))
    noise = ImageStat.Stat(ImageChops.difference(gray, median)).mean[0]
    raw = {
        "measurement_version": QUALITY_MEASUREMENT_VERSION,
        "analysis_width": width,
        "analysis_height": height,
        "focus_gradient": round(focus, 6),
        "underexposed_fraction": round(underexposed, 6),
        "overexposed_fraction": round(overexposed, 6),
        "contrast_stddev": round(contrast, 6),
        "noise_mad": round(noise, 6),
    }
    return score_from_raw(raw)


def score_from_raw(raw: dict[str, float | int | str]) -> QualityResult:
    focus = _unit_interval((float(raw["focus_gradient"]) - 2.0) / 26.0)
    clipping = float(raw["underexposed_fraction"]) + float(raw["overexposed_fraction"])
    exposure = 1.0 - _unit_interval(clipping / 0.20)
    contrast = _unit_interval((float(raw["contrast_stddev"]) - 8.0) / 56.0)
    noise = 1.0 - _unit_interval(float(raw["noise_mad"]) / 18.0)
    components = {
        "focus": round(focus, 6),
        "exposure": round(exposure, 6),
        "contrast": round(contrast, 6),
        "noise": round(noise, 6),
    }
    score = round(
        0.35 * components["focus"]
        + 0.30 * components["exposure"]
        + 0.20 * components["contrast"]
        + 0.15 * components["noise"],
        6,
    )
    return QualityResult(raw, components, score)


def raw_is_compatible(raw_json: str | None, input_fingerprint: str) -> bool:
    if not raw_json:
        return False
    try:
        import json

        raw = json.loads(raw_json)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        raw.get("measurement_version") == QUALITY_MEASUREMENT_VERSION
        and raw.get("input_fingerprint") == input_fingerprint
    )


def _mean_gradient(gray: Image.Image) -> float:
    width, height = gray.size
    if width < 2 or height < 2:
        return 0.0
    horizontal = ImageChops.difference(
        gray.crop((1, 0, width, height)), gray.crop((0, 0, width - 1, height))
    )
    vertical = ImageChops.difference(
        gray.crop((0, 1, width, height)), gray.crop((0, 0, width, height - 1))
    )
    return (ImageStat.Stat(horizontal).mean[0] + ImageStat.Stat(vertical).mean[0]) / 2.0


def _unit_interval(value: float) -> float:
    return max(0.0, min(1.0, value))
