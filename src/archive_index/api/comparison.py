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
    left_row = _physical(workspace, left_id)
    right_row = _physical(workspace, right_id)
    if left_row["logical_asset_id"] != right_row["logical_asset_id"]:
        raise ResourceNotFound("representations must belong to the same logical asset")
    left = _decode(workspace, left_row)
    right = _decode(workspace, right_row)
    try:
        metrics = {
            "source_bytes": left_row["size_bytes"],
            "comparison_bytes": right_row["size_bytes"],
            "compression_ratio": round(left_row["size_bytes"] / right_row["size_bytes"], 3) if right_row["size_bytes"] else None,
            "comparison_percent": round(right_row["size_bytes"] / left_row["size_bytes"] * 100, 2) if left_row["size_bytes"] else None,
            "mse": None,
            "psnr": None,
        }
        if left.size == right.size:
            mse = _mse(left, right)
            metrics["mse"] = round(mse, 6)
            metrics["psnr"] = None if mse == 0 else round(10 * log10((255 * 255) / mse), 3)
        return {
            "left": _representation(left_row, handle),
            "right": _representation(right_row, handle),
            "metrics": metrics,
            "same_dimensions": left.size == right.size,
            "metrics_note": None if left.size == right.size else "MSE and PSNR are unavailable because dimensions differ.",
        }
    finally:
        left.close()
        right.close()


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
        values = ImageStat.Stat(difference).mean
        return sum(value * value for value in values) / 3
    finally:
        difference.close()
        left_rgb.close()
        right_rgb.close()


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
