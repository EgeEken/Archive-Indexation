"""Read-only sensor-based RAW inspection previews."""

from __future__ import annotations

from collections import OrderedDict
from io import BytesIO
from math import isfinite
from threading import RLock

from PIL import Image

from ..media_types import is_raw_extension
from ..workspace import Workspace, WorkspaceError
from .errors import ResourceNotFound

MAX_LONG_EDGE = 2560
MAX_CACHE_ENTRIES = 16
MIN_EXPOSURE_EV = -5.0
MAX_EXPOSURE_EV = 5.0
RAW_EXP_MIN_EV = -2.0
RAW_EXP_MAX_EV = 3.0
_cache: OrderedDict[tuple[str, int, int, float], bytes] = OrderedDict()
_cache_lock = RLock()


def raw_development_preview(workspace: Workspace, physical_id: str, exposure_ev: float) -> bytes:
    if not isfinite(exposure_ev) or not MIN_EXPOSURE_EV <= exposure_ev <= MAX_EXPOSURE_EV or abs(exposure_ev * 4 - round(exposure_ev * 4)) > 1e-6:
        raise ValueError("exposure_ev must be between -5 and 5 in 0.25 EV steps")
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
    key = (physical_id, stat.st_size, stat.st_mtime_ns, round(exposure_ev, 2))
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    output = _decode_and_resize(source, exposure_ev)
    with _cache_lock:
        _cache[key] = output
        _cache.move_to_end(key)
        while len(_cache) > MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
    return output


def _decode_and_resize(source, exposure_ev: float) -> bytes:
    try:
        import rawpy
    except ImportError as error:
        raise ValueError("RAW development is unavailable for this file") from error
    try:
        raw_exposure = max(RAW_EXP_MIN_EV, min(RAW_EXP_MAX_EV, exposure_ev))
        residual_exposure = exposure_ev - raw_exposure
        with rawpy.imread(str(source)) as raw:
            pixels = raw.postprocess(
                use_camera_wb=True,
                no_auto_bright=True,
                exp_shift=2.0 ** raw_exposure,
                bright=2.0 ** residual_exposure,
                output_color=rawpy.ColorSpace.sRGB,
                output_bps=8,
                half_size=True,
            )
        with Image.fromarray(pixels, mode="RGB") as image:
            image.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="PNG", optimize=True)
            return output.getvalue()
    except Exception as error:
        raise ValueError("RAW development is unavailable for this file") from error


def clear_raw_development_cache() -> None:
    with _cache_lock:
        _cache.clear()
