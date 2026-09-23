"""On-demand physical-representation inspection and comparison."""

from __future__ import annotations

from io import BytesIO
from math import log10

from PIL import Image, ImageChops, ImageStat

from ..media.image_decode import load_full_image
from ..workspace import Workspace, WorkspaceError
from .errors import ResourceNotFound

MAX_COMPARISON_EDGE = 2048


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
    first_row = _physical(workspace, left_id)
    second_row = _physical(workspace, right_id)
    if first_row["logical_asset_id"] != second_row["logical_asset_id"]:
        raise ResourceNotFound("representations must belong to the same logical asset")
    reference_row, compressed_row = _orient(first_row, second_row)
    reference = _decode(workspace, reference_row)
    compressed = _decode(workspace, compressed_row)
    try:
        metrics = {
            "reference_bytes": reference_row["size_bytes"],
            "compressed_bytes": compressed_row["size_bytes"],
            "compression_ratio": round(reference_row["size_bytes"] / compressed_row["size_bytes"], 3) if compressed_row["size_bytes"] else None,
            "compressed_percent": round(compressed_row["size_bytes"] / reference_row["size_bytes"] * 100, 2) if reference_row["size_bytes"] else None,
            "mse": None,
            "psnr": None,
            "pixel_identical": None,
        }
        metrics["source_bytes"] = metrics["reference_bytes"]
        metrics["comparison_bytes"] = metrics["compressed_bytes"]
        metrics["comparison_percent"] = metrics["compressed_percent"]
        if reference.size == compressed.size:
            mse = _mse(reference, compressed)
            metrics["mse"] = round(mse, 6)
            metrics["psnr"] = None if mse == 0 else round(10 * log10((255 * 255) / mse), 3)
            metrics["pixel_identical"] = mse == 0
        reference_data = _representation(reference_row, handle)
        compressed_data = _representation(compressed_row, handle)
        return {
            "reference": reference_data,
            "compressed": compressed_data,
            "left": reference_data,
            "right": compressed_data,
            "metrics": metrics,
            "same_dimensions": reference.size == compressed.size,
            "metrics_note": None if reference.size == compressed.size else "MSE and PSNR are unavailable because dimensions differ.",
        }
    finally:
        reference.close()
        compressed.close()


def _physical(workspace: Workspace, physical_id: str):
    connection = workspace.connect()
    try:
        row = connection.execute(
            "SELECT * FROM physical_file WHERE id = ? AND in_scope = 1 AND is_online = 1",
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


def _representation(row, handle: str) -> dict[str, object]:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "format": row["extension"].removeprefix(".").upper(),
        "size_bytes": row["size_bytes"],
        "width": row["width"],
        "height": row["height"],
        "preview_url": f"/api/files/{row['id']}/comparison-preview?workspace={handle}",
    }
