"""On-demand physical-representation inspection and comparison."""

from __future__ import annotations

from collections import OrderedDict
from io import BytesIO
from math import log10
from threading import RLock
from time import perf_counter

import numpy as np
from PIL import Image, ImageChops, ImageStat

from ..media.image_decode import load_full_image
from ..workspace import Workspace, WorkspaceError
from .errors import ResourceNotFound

MAX_COMPARISON_EDGE = 2048
MAX_COMPARISON_CACHE_ENTRIES = 8
MAX_COMPARISON_CACHE_BYTES = 48 * 1024 * 1024
MAX_DIFFERENCE_CACHE_ENTRIES = 4
MAX_DIFFERENCE_CACHE_BYTES = 32 * 1024 * 1024
DIFFERENCE_ALGORITHM_VERSION = "difference-log-v1"
_comparison_cache: OrderedDict[tuple[object, ...], dict[str, object]] = OrderedDict()
_comparison_cache_bytes = 0
_difference_cache: OrderedDict[tuple[object, ...], dict[str, object]] = OrderedDict()
_difference_cache_bytes = 0
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
    workspace_identity = _workspace_identity(workspace)
    cache_key = _comparison_cache_key(workspace_identity, reference_row, compressed_row)
    cache_lookup_started = perf_counter()
    with _comparison_cache_lock:
        cached = _comparison_cache.get(cache_key)
        if cached is not None:
            _comparison_cache.move_to_end(cache_key)
            result = dict(cached)
            result["cache_hit"] = True
            result["timings_ms"] = {"cache_lookup": round((perf_counter() - cache_lookup_started) * 1000, 3), "total": round((perf_counter() - started) * 1000, 3)}
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
            "global_mse": None,
            "psnr": None,
            "max_pixel_mse": None,
            "pixel_identical": None,
            "byte_identical": bool(reference_row["sha256"] and compressed_row["sha256"] and reference_row["sha256"] == compressed_row["sha256"]),
        }
        metrics["source_bytes"] = metrics["reference_bytes"]
        metrics["comparison_bytes"] = metrics["compressed_bytes"]
        metrics["comparison_percent"] = metrics["compressed_percent"]
        metrics_started = perf_counter()
        if reference.size == compressed.size:
            mse, max_pixel_mse = _comparison_metrics(reference, compressed)
            metrics_ms = round((perf_counter() - metrics_started) * 1000, 3)
            metrics["mse"] = round(mse, 6)
            metrics["global_mse"] = round(mse, 6)
            metrics["max_pixel_mse"] = round(max_pixel_mse, 6)
            metrics["psnr"] = None if mse == 0 else round(10 * log10((255 * 255) / mse), 3)
            metrics["pixel_identical"] = mse == 0
        else:
            metrics_ms = round((perf_counter() - metrics_started) * 1000, 3)
        reference_data = _representation(workspace, reference_row, handle)
        compressed_data = _representation(workspace, compressed_row, handle)
        result = {
            "reference": reference_data,
            "compressed": compressed_data,
            "left": reference_data,
            "right": compressed_data,
            "metrics": metrics,
            "difference_data_url": None,
            "same_dimensions": reference.size == compressed.size,
            "metrics_note": None if reference.size == compressed.size else "MSE and PSNR are unavailable because dimensions differ.",
            "cache_hit": False,
            "timings_ms": {
                "reference_decode": reference_decode_ms,
                "compressed_decode": compressed_decode_ms,
                "global_metrics": metrics_ms,
                "difference_compute": None,
                "difference_encode": None,
                "total": round((perf_counter() - started) * 1000, 3),
            },
        }
        _cache_comparison(cache_key, result)
        return result
    finally:
        reference.close()
        compressed.close()


def comparison_difference(workspace: Workspace, left_id: str, right_id: str) -> tuple[bytes, dict[str, object]]:
    started = perf_counter()
    first_row = _physical(workspace, left_id)
    second_row = _physical(workspace, right_id)
    if first_row["logical_asset_id"] != second_row["logical_asset_id"]:
        raise ResourceNotFound("representations must belong to the same logical asset")
    reference_row, compressed_row = _orient(first_row, second_row)
    if reference_row["width"] != compressed_row["width"] or reference_row["height"] != compressed_row["height"]:
        raise ValueError("Difference is unavailable because representation dimensions differ.")
    workspace_identity = _workspace_identity(workspace)
    cache_key = _difference_cache_key(workspace_identity, reference_row, compressed_row)
    cache_lookup_started = perf_counter()
    with _comparison_cache_lock:
        cached = _difference_cache.get(cache_key)
        if cached is not None:
            _difference_cache.move_to_end(cache_key)
            cached_timings = cached["timings_ms"]
            timings = {
                "cache_hit": True,
                "cache_lookup": round((perf_counter() - cache_lookup_started) * 1000, 3),
                "reference_decode": 0.0,
                "compressed_decode": 0.0,
                "difference_compute": 0.0,
                "difference_encode": 0.0,
                "global_mse": cached_timings["global_mse"],
                "max_pixel_mse": cached_timings["max_pixel_mse"],
                "total": round((perf_counter() - started) * 1000, 3),
            }
            return cached["body"], timings
    decode_started = perf_counter()
    reference = _decode(workspace, reference_row)
    reference_decode_ms = round((perf_counter() - decode_started) * 1000, 3)
    decode_started = perf_counter()
    compressed = _decode(workspace, compressed_row)
    compressed_decode_ms = round((perf_counter() - decode_started) * 1000, 3)
    try:
        compute_started = perf_counter()
        difference, mse, max_pixel_mse = _difference_map(reference, compressed)
        difference_compute_ms = round((perf_counter() - compute_started) * 1000, 3)
        try:
            encode_started = perf_counter()
            output = BytesIO()
            difference.save(output, format="PNG", optimize=True)
            body = output.getvalue()
            difference_encode_ms = round((perf_counter() - encode_started) * 1000, 3)
        finally:
            difference.close()
        timings = {
            "cache_hit": False,
            "reference_decode": reference_decode_ms,
            "compressed_decode": compressed_decode_ms,
            "difference_compute": difference_compute_ms,
            "difference_encode": difference_encode_ms,
            "global_mse": round(mse, 6),
            "max_pixel_mse": round(max_pixel_mse, 6),
            "total": round((perf_counter() - started) * 1000, 3),
        }
        _cache_difference(cache_key, body, timings)
        return body, timings
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


def _comparison_metrics(left: Image.Image, right: Image.Image) -> tuple[float, float]:
    difference = _difference(left, right)
    try:
        width, height = difference.size
        mse = sum(ImageStat.Stat(difference).sum2) / (width * height * 3)
        values = np.asarray(difference, dtype=np.uint16)
        max_pixel_mse = float(np.max(np.sum(values * values, axis=2, dtype=np.uint32)) / 3.0)
        return mse, max_pixel_mse
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
        values = np.asarray(difference, dtype=np.uint16)
        pixel_mse = np.sum(values * values, axis=2, dtype=np.uint32) / 3.0
        total = float(np.mean(pixel_mse))
        maximum = float(np.max(pixel_mse))
        if maximum:
            intensity = np.rint(np.log1p(pixel_mse) / np.log1p(maximum) * 255).clip(0, 255).astype(np.uint8)
        else:
            intensity = np.zeros((height, width), dtype=np.uint8)
        intensity_image = Image.fromarray(intensity, mode="L")
        black = Image.new("L", (width, height), 0)
        result = Image.merge("RGB", (intensity_image, black, black))
        intensity_image.close()
        black.close()
        return result, total, maximum
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


def _workspace_identity(workspace: Workspace) -> str:
    connection = workspace.connect()
    try:
        row = connection.execute("SELECT workspace_id FROM workspace_info WHERE id = 1").fetchone()
    finally:
        connection.close()
    return str(row["workspace_id"]) if row else str(workspace.root.resolve())


def _pair_cache_fingerprint(reference, compressed) -> tuple[object, ...]:
    return (
        reference["id"], reference["relative_path"], reference["size_bytes"], reference["mtime_ns"], reference["sha256"],
        reference["width"], reference["height"], reference["display_preview_output_path"], reference["display_preview_version"], reference["display_preview_fingerprint"],
        compressed["id"], compressed["relative_path"], compressed["size_bytes"], compressed["mtime_ns"], compressed["sha256"],
        compressed["width"], compressed["height"], compressed["display_preview_output_path"], compressed["display_preview_version"], compressed["display_preview_fingerprint"],
    )


def _comparison_cache_key(workspace_identity: str, reference, compressed) -> tuple[object, ...]:
    return ("comparison-metrics-v3", workspace_identity, *_pair_cache_fingerprint(reference, compressed))


def _difference_cache_key(workspace_identity: str, reference, compressed) -> tuple[object, ...]:
    return (DIFFERENCE_ALGORITHM_VERSION, workspace_identity, *_pair_cache_fingerprint(reference, compressed))


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


def _cache_difference(key: tuple[object, ...], body: bytes, timings: dict[str, object]) -> None:
    global _difference_cache_bytes
    with _comparison_cache_lock:
        previous = _difference_cache.pop(key, None)
        if previous is not None:
            _difference_cache_bytes -= len(previous["body"])
        _difference_cache[key] = {"body": body, "timings_ms": timings}
        _difference_cache_bytes += len(body)
        while _difference_cache and (len(_difference_cache) > MAX_DIFFERENCE_CACHE_ENTRIES or _difference_cache_bytes > MAX_DIFFERENCE_CACHE_BYTES):
            _, evicted = _difference_cache.popitem(last=False)
            _difference_cache_bytes -= len(evicted["body"])
