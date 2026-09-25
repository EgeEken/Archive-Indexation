"""Read-only sensor-based RAW inspection previews."""

from __future__ import annotations

from collections import OrderedDict
from io import BytesIO
from math import exp2, isfinite
from threading import RLock

from PIL import Image

from ..media_types import is_raw_extension
from ..workspace import Workspace, WorkspaceError
from .errors import ResourceNotFound

MAX_LONG_EDGE = 2560
MAX_CACHE_ENTRIES = 16
MAX_CACHE_BYTES = 64 * 1024 * 1024
MIN_EXPOSURE_EV = -5.0
MAX_EXPOSURE_EV = 5.0
EXPOSURE_STEP_EV = 0.5
CONTROL_STEP = 5
RAW_EXP_MIN_EV = -2.0
RAW_EXP_MAX_EV = 3.0
_cache: OrderedDict[tuple[str, int, int, float, int, int, int, int], tuple[bytes, str]] = OrderedDict()
_cache_bytes = 0
_cache_lock = RLock()


def raw_development_preview(workspace: Workspace, physical_id: str, exposure_ev: float, white_balance: int = 0, saturation: int = 100, highlights: int = 0, shadows: int = 0) -> tuple[bytes, str]:
    global _cache_bytes
    exposure_ev = _canonical_control(exposure_ev, MIN_EXPOSURE_EV, MAX_EXPOSURE_EV, EXPOSURE_STEP_EV, "exposure_ev")
    white_balance = _canonical_control(white_balance, -100, 100, CONTROL_STEP, "white_balance")
    saturation = _canonical_control(saturation, 50, 150, CONTROL_STEP, "saturation")
    highlights = _canonical_control(highlights, -100, 100, CONTROL_STEP, "highlights")
    shadows = _canonical_control(shadows, -100, 100, CONTROL_STEP, "shadows")
    connection = workspace.connect()
    try:
        row = connection.execute(
            "SELECT id, relative_path, extension, is_online, in_scope FROM physical_file WHERE id = ?",
            (physical_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not row["is_online"] or not row["in_scope"] or not is_raw_extension(row["extension"]):
        raise ResourceNotFound("RAW development is unavailable for this file")
    try:
        source = workspace.absolute_path(row["relative_path"])
        stat = source.stat()
    except (OSError, WorkspaceError) as error:
        raise ResourceNotFound("RAW development is unavailable for this file") from error
    key = (physical_id, stat.st_size, stat.st_mtime_ns, round(exposure_ev, 2), white_balance, saturation, highlights, shadows)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    output = _decode_and_resize(source, exposure_ev, white_balance, saturation, highlights, shadows)
    with _cache_lock:
        previous = _cache.pop(key, None)
        if previous is not None:
            _cache_bytes -= len(previous[0])
        _cache[key] = output
        _cache.move_to_end(key)
        _cache_bytes += len(output[0])
        while _cache and (len(_cache) > MAX_CACHE_ENTRIES or _cache_bytes > MAX_CACHE_BYTES):
            _, evicted = _cache.popitem(last=False)
            _cache_bytes -= len(evicted[0])
    return output


def _canonical_control(value: float, minimum: float, maximum: float, step: float, name: str) -> float | int:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} is outside its supported range") from error
    if not isfinite(numeric) or not minimum <= numeric <= maximum or abs(numeric / step - round(numeric / step)) > 1e-6:
        step_label = f"{step:g}"
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g} in {step_label}-unit steps")
    canonical = round(numeric / step) * step
    return int(canonical) if step >= 1 else round(canonical, 3)


def _decode_and_resize(source, exposure_ev: float, white_balance: int, saturation: int, highlights: int, shadows: int) -> tuple[bytes, str]:
    try:
        import rawpy
    except ImportError as error:
        raise ValueError("RAW development is unavailable for this file") from error
    try:
        raw_exposure = max(RAW_EXP_MIN_EV, min(RAW_EXP_MAX_EV, exposure_ev))
        residual_exposure = exposure_ev - raw_exposure
        with rawpy.imread(str(source)) as raw:
            multipliers = getattr(raw, "camera_whitebalance", None)
            camera_wb_available = isinstance(multipliers, (tuple, list)) and len(multipliers) == 4 and all(isfinite(value) and value > 0 for value in multipliers)
            user_wb = None
            if camera_wb_available and white_balance:
                tint = exp2(white_balance / 200)
                user_wb = [multipliers[0] * tint, multipliers[1], multipliers[2] / tint, multipliers[3]]
            pixels = raw.postprocess(
                use_camera_wb=bool(camera_wb_available and user_wb is None),
                use_auto_wb=not camera_wb_available,
                user_wb=user_wb,
                no_auto_bright=True,
                exp_shift=2.0 ** raw_exposure,
                bright=2.0 ** residual_exposure,
                output_color=rawpy.ColorSpace.sRGB,
                output_bps=8,
                half_size=True,
            )
        with Image.fromarray(pixels, mode="RGB") as image:
            if saturation != 100 or highlights or shadows:
                image = _adjust_preview(image, saturation, highlights, shadows)
            wb_status = "Camera/as-shot WB" if camera_wb_available else "Rawpy auto-WB fallback (camera WB unavailable)"
            image.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            if image is not None and image is not pixels:
                image.close()
            return output.getvalue(), wb_status
    except Exception as error:
        raise ValueError("RAW development is unavailable for this file") from error


def _adjust_preview(image: Image.Image, saturation: int, highlights: int, shadows: int) -> Image.Image:
    import numpy as np
    from PIL import ImageEnhance

    result = ImageEnhance.Color(image).enhance(saturation / 100)
    if highlights or shadows:
        values = np.asarray(result, dtype=np.float32) / 255
        luminance = values.max(axis=2)
        highlight_t = np.clip((luminance - 0.45) / 0.55, 0, 1)
        highlight_weight = highlight_t * highlight_t * (3 - 2 * highlight_t)
        shadow_t = np.clip(luminance / 0.6, 0, 1)
        shadow_weight = (1 - shadow_t * shadow_t * (3 - 2 * shadow_t))
        gain = 1 + highlights / 100 * 0.75 * highlight_weight + shadows / 100 * 0.75 * shadow_weight
        adjusted = np.clip(values * gain[:, :, None], 0, 1)
        result.close()
        result = Image.fromarray(np.round(adjusted * 255).astype(np.uint8), mode="RGB")
    if result is not image:
        image.close()
    return result


def clear_raw_development_cache() -> None:
    global _cache_bytes
    with _cache_lock:
        _cache.clear()
        _cache_bytes = 0
