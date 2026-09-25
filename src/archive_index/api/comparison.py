"""On-demand physical-representation inspection and comparison."""

from __future__ import annotations

import base64
from collections import OrderedDict
from io import BytesIO
from math import log10
from threading import RLock
from time import perf_counter

from PIL import Image, ImageChops, ImageStat

from ..media.image_decode import load_full_image
from ..workspace import Workspace, WorkspaceError
from .errors import ResourceNotFound

MAX_COMPARISON_EDGE = 2048
MAX_COMPARISON_CACHE_ENTRIES = 8
MAX_COMPARISON_CACHE_BYTES = 48 * 1024 * 1024
_comparison_cache: OrderedDict[tuple[object, ...], dict[str, object]] = OrderedDict()
_comparison_cache_bytes = 0
_comparison_cache_lock = RLock()


def comparison_preview(workspace: Workspace, physical_id: str) -> bytes:
    row = _physical(workspace, physical_id)
    image = _decode(workspace, row)
    try:
        image.thumbnail((MAX_COMPARISON_EDGE, MAX_COMPARISON_EDGE), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()
    finally:
        image.close()


def comparison_data(workspace: Workspace, left_id: str, right_id: str, handle: str) -> dict[str, object]:
    started = perf_counter()
    first_row = _physical(workspace, left_id)
    second_row = _physical(workspace, right_id)
    if first_row["logical_asset_id"] != second_row["logical_asset_id"]:
        raise ResourceNotFound("representations must belong to the same logical asset")
    reference_row, compressed_row = _orient(first_row, second_row)
    cache_key = _comparison_cache_key(reference_row, compressed_row)
    with _comparison_cache_lock:
        cached = _comparison_cache.get(cache_key)
        if cached is not None:
            _comparison_cache.move_to_end(cache_key)
            result = dict(cached)
            result["cache_hit"] = True
            result["timings_ms"] = {"total": round((perf_counter() - started) * 1000, 3), "cache_lookup": round((perf_counter() - started) * 1000, 3)}
            return result
    decode_started = perf_counter()
    reference = _decode(workspace, reference_row)
    reference_decode_ms = round((perf_counter() - decode_started) * 1000, 3)
    decode_started = perf_counter()
    compressed = _decode(workspace, compressed_row)
    compressed_decode_ms = round((perf_counter() - decode_started) * 1000, 3)
    try:
        metrics = {
            "reference_bytes": reference_row["size_bytes"],
            "compressed_bytes": compressed_row["size_bytes"],
            "compression_ratio": round(reference_row["size_bytes"] / compressed_row["size_bytes"], 3) if compressed_row["size_bytes"] else None,
            "compressed_percent": round(compressed_row["size_bytes"] / reference_row["size_bytes"] * 100, 2) if reference_row["size_bytes"] else None,
            "mse": None,
            "psnr": None,
            "max_pixel_mse": None,
            "pixel_identical": None,
            "byte_identical": bool(reference_row["sha256"] and compressed_row["sha256"] and reference_row["sha256"] == compressed_row["sha256"]),
        }
        difference_data_url = None
        metrics["source_bytes"] = metrics["reference_bytes"]
        metrics["comparison_bytes"] = metrics["compressed_bytes"]
        metrics["comparison_percent"] = metrics["compressed_percent"]
        metrics_started = perf_counter()
        difference_started = None
        if reference.size == compressed.size:
            difference_started = perf_counter()
            difference, mse, max_pixel_mse = _difference_map(reference, compressed)
            metrics_ms = round((perf_counter() - metrics_started) * 1000, 3)
            try:
                output = BytesIO()
                difference.save(output, format="PNG", optimize=True)
                difference_data_url = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
            finally:
                difference.close()
            difference_encoding_ms = round((perf_counter() - difference_started) * 1000, 3)
            metrics["mse"] = round(mse, 6)
            metrics["max_pixel_mse"] = round(max_pixel_mse, 6)
            metrics["psnr"] = None if mse == 0 else round(10 * log10((255 * 255) / mse), 3)
            metrics["pixel_identical"] = mse == 0
        else:
            metrics_ms = round((perf_counter() - metrics_started) * 1000, 3)
            difference_encoding_ms = 0.0
        reference_data = _representation(workspace, reference_row, handle)
        compressed_data = _representation(workspace, compressed_row, handle)
        result = {
            "reference": reference_data,
            "compressed": compressed_data,
            "left": reference_data,
            "right": compressed_data,
            "metrics": metrics,
            "difference_data_url": difference_data_url,
            "same_dimensions": reference.size == compressed.size,
            "metrics_note": None if reference.size == compressed.size else "MSE and PSNR are unavailable because dimensions differ.",
            "cache_hit": False,
            "timings_ms": {
                "reference_decode": reference_decode_ms,
                "compressed_decode": compressed_decode_ms,
                "metrics": metrics_ms,
                "difference_encoding": difference_encoding_ms,
                "total": round((perf_counter() - started) * 1000, 3),
            },
        }
        _cache_comparison(cache_key, result)
        return result
    finally:
        reference.close()
        compressed.close()


def _physical(workspace: Workspace, physical_id: str):
    connection = workspace.connect()
    try:
        row = connection.execute(
            """
            SELECT pf.*, dp.output_path AS display_preview_output_path,
                   dp.version AS display_preview_version,
                   dp.input_fingerprint AS display_preview_fingerprint
            FROM physical_file AS pf
            LEFT JOIN display_preview AS dp ON dp.physical_file_id = pf.id
            WHERE pf.id = ? AND pf.in_scope = 1 AND pf.is_online = 1
            """,
            (physical_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None or row["media_type"] != "image":
        raise ResourceNotFound("online image representation is unavailable")
    return row


def _decode(workspace: Workspace, row):
    try:
        return load_full_image(workspace.absolute_path(row["relative_path"]))
    except (OSError, RuntimeError, ValueError, WorkspaceError) as error:
        raise ResourceNotFound("representation could not be decoded") from error


def _difference(left: Image.Image, right: Image.Image) -> Image.Image:
    left_rgb = left.convert("RGB")
    right_rgb = right.convert("RGB")
    try:
        return ImageChops.difference(left_rgb, right_rgb)
    finally:
        left_rgb.close()
        right_rgb.close()


def _mse(left: Image.Image, right: Image.Image) -> float:
    difference = _difference(left, right)
    try:
        width, height = difference.size
        return sum(ImageStat.Stat(difference).sum2) / (width * height * 3)
    finally:
        difference.close()


def _difference_map(left: Image.Image, right: Image.Image) -> tuple[Image.Image, float, float]:
    left_rgb = left.convert("RGB")
    right_rgb = right.convert("RGB")
    difference = ImageChops.difference(left_rgb, right_rgb)
    left_rgb.close()
    right_rgb.close()
    try:
        width, height = difference.size
        pixels = difference.load()
        total = 0.0
        maximum = 0.0
        for y in range(height):
            for x in range(width):
                red, green, blue = pixels[x, y]
                value = (red * red + green * green + blue * blue) / 3
                total += value
                maximum = max(maximum, value)
        intensities = bytearray(width * height)
        if maximum:
            index = 0
            for y in range(height):
                for x in range(width):
                    red, green, blue = pixels[x, y]
                    value = (red * red + green * green + blue * blue) / 3
                    intensities[index] = round(value / maximum * 255)
                    index += 1
        intensity = Image.frombytes("L", (width, height), bytes(intensities))
        black = Image.new("L", (width, height), 0)
        result = Image.merge("RGB", (intensity, black, black))
        intensity.close()
        black.close()
        return result, total / (width * height), maximum
    finally:
        difference.close()


def _orient(first, second):
    conventional = {".jpg", ".jpeg", ".png"}
    compressed = {".jxl", ".avif", ".webp"}
    first_ext = first["extension"].casefold()
    second_ext = second["extension"].casefold()
    if first_ext in compressed and second_ext in conventional:
        return second, first
    if second_ext in compressed and first_ext in conventional:
        return first, second
    return (first, second) if (first["relative_path"].casefold(), first["id"]) <= (second["relative_path"].casefold(), second["id"]) else (second, first)


def _representation(workspace: Workspace, row, handle: str) -> dict[str, object]:
    preview_url = f"/api/files/{row['id']}/comparison-preview?workspace={handle}"
    if _valid_display_preview(workspace, row) and row["display_preview_version"] == "imagecodecs-jxl-display-v1":
        preview_url = f"/api/files/{row['id']}/preview?workspace={handle}"
    elif row["extension"].casefold() not in {".jxl", ".avif"}:
        preview_url = f"/api/files/{row['id']}/original?workspace={handle}"
    return {
        "id": row["id"],
        "filename": row["filename"],
        "format": row["extension"].removeprefix(".").upper(),
        "size_bytes": row["size_bytes"],
        "width": row["width"],
        "height": row["height"],
        "preview_url": preview_url,
    }


def _valid_display_preview(workspace: Workspace, row) -> bool:
    try:
        return bool(row["display_preview_output_path"] and workspace.index_path(row["display_preview_output_path"]).is_file())
    except (IndexError, KeyError, OSError, WorkspaceError):
        return False


def _comparison_cache_key(reference, compressed) -> tuple[object, ...]:
    return (
        "comparison-v2",
        reference["id"], reference["relative_path"], reference["size_bytes"], reference["mtime_ns"], reference["sha256"],
        reference["width"], reference["height"], reference["display_preview_output_path"], reference["display_preview_version"], reference["display_preview_fingerprint"],
        compressed["id"], compressed["relative_path"], compressed["size_bytes"], compressed["mtime_ns"], compressed["sha256"],
        compressed["width"], compressed["height"], compressed["display_preview_output_path"], compressed["display_preview_version"], compressed["display_preview_fingerprint"],
    )


def _cache_comparison(key: tuple[object, ...], result: dict[str, object]) -> None:
    global _comparison_cache_bytes
    size = len(result.get("difference_data_url") or "")
    with _comparison_cache_lock:
        previous = _comparison_cache.pop(key, None)
        if previous is not None:
            _comparison_cache_bytes -= len(previous.get("difference_data_url") or "")
        _comparison_cache[key] = result
        _comparison_cache_bytes += size
        while _comparison_cache and (len(_comparison_cache) > MAX_COMPARISON_CACHE_ENTRIES or _comparison_cache_bytes > MAX_COMPARISON_CACHE_BYTES):
            _, evicted = _comparison_cache.popitem(last=False)
            _comparison_cache_bytes -= len(evicted.get("difference_data_url") or "")
