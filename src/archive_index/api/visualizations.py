"""Compact read-only datasets for the workspace visualizations."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from math import isfinite

from ..api.browser import asset_location
from ..indexing.representations import preferred_physical
from ..media.metadata import _parse_exif_datetime

_ID_CHUNK_SIZE = 500
_CAPTURE_PREFIX = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:[T ](?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?)?"
)


def visualization_capabilities(workspace) -> dict[str, bool]:
    from ..indexing.projection import current_projection

    return {
        "geo": _workspace_has_gps(workspace),
        "timeline": True,
        "vector": current_projection(workspace)[0] is not None,
    }


def _workspace_has_gps(workspace) -> bool:
    connection = workspace.connect()
    try:
        last_rowid = 0
        while True:
            rows = connection.execute(
                """
                SELECT rowid, metadata_json
                FROM physical_file
                WHERE rowid > ? AND in_scope = 1 AND metadata_json LIKE '%gps%'
                ORDER BY rowid
                LIMIT ?
                """,
                (last_rowid, _ID_CHUNK_SIZE),
            ).fetchall()
            if not rows:
                return False
            for row in rows:
                try:
                    gps = (json.loads(row["metadata_json"]) or {}).get("gps") or {}
                    latitude = float(gps["latitude"])
                    longitude = float(gps["longitude"])
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
                if isfinite(latitude) and isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    return True
            last_rowid = rows[-1]["rowid"]
    finally:
        connection.close()


def visualization_data(workspace, query, handle, *, filter_assets, kind: str) -> dict[str, object]:
    items, search, revision, workspace_total = filter_assets(workspace, query, handle)
    base = {
        "view": kind,
        "filtered_asset_count": len(items),
        "represented_point_count": 0,
        "browser_revision": revision,
        "workspace_total": workspace_total,
        "search": search,
        "available": True,
        "empty_reason": None,
    }
    if kind == "geo":
        points = _geo_points(workspace, items)
        base.update(points=points, represented_point_count=len(points))
        if not points:
            base["empty_reason"] = (
                "No assets match these filters."
                if not items
                else "No filtered assets have valid GPS coordinates."
            )
        return base
    if kind == "timeline":
        time_mode = (query.get("time_mode") or ["capture"])[0]
        if time_mode not in {"capture", "file_created"}:
            time_mode = "capture"
        points = _timeline_points(workspace, items, time_mode)
        base["time_mode"] = time_mode
        base.update(points=points, represented_point_count=len(points))
        if not points:
            base["empty_reason"] = (
                "No assets match these filters."
                if not items
                else "No filtered assets have a usable capture time."
                if time_mode == "capture"
                else "No filtered assets have a usable file-created time."
            )
        return base
    if kind == "vector":
        from ..indexing.projection import current_projection, projection_points

        projection, reason = current_projection(workspace)
        if projection is None:
            base.update(available=False, empty_reason=reason)
            return base | {"points": []}
        points = projection_points(workspace, projection["id"], [item["asset_id"] for item in items])
        quality_by_asset = {item["asset_id"]: item.get("quality_score") for item in items}
        points = [{**point, "quality_score": quality_by_asset.get(point["asset_id"])} for point in points]
        base.update(
            points=points,
            represented_point_count=len(points),
            projection_run_id=projection["id"],
            algorithm=projection["algorithm"],
            version=projection["version"],
        )
        if not points:
            base["empty_reason"] = "No filtered assets have a projected semantic vector."
        return base
    raise ValueError(f"unknown visualization: {kind}")


def _geo_points(workspace, items: list[dict[str, object]]) -> list[dict[str, object]]:
    asset_ids = [item["asset_id"] for item in items]
    quality_by_asset = {item["asset_id"]: item.get("quality_score") for item in items}
    if not asset_ids:
        return []
    by_asset: dict[str, list] = {asset_id: [] for asset_id in asset_ids}
    connection = workspace.connect()
    try:
        for start in range(0, len(asset_ids), _ID_CHUNK_SIZE):
            chunk = asset_ids[start : start + _ID_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT id, logical_asset_id, relative_path, filename, extension,
                       media_type, role, is_online, in_scope, metadata_json
                FROM physical_file
                WHERE logical_asset_id IN ({placeholders}) AND in_scope = 1
                ORDER BY logical_asset_id, is_online DESC, relative_path
                """,
                chunk,
            ).fetchall()
            for row in rows:
                by_asset[row["logical_asset_id"]].append(row)
    finally:
        connection.close()
    points = []
    for asset_id in asset_ids:
        location = asset_location(by_asset[asset_id])
        if location is not None:
            points.append({"asset_id": asset_id, "quality_score": quality_by_asset.get(asset_id), **location})
    return points


def _timeline_points(workspace, items, time_mode: str = "capture") -> list[dict[str, object]]:
    physical_by_asset = {}
    if items:
        asset_ids = [item["asset_id"] for item in items]
        connection = workspace.connect()
        try:
            for start in range(0, len(asset_ids), _ID_CHUNK_SIZE):
                chunk = asset_ids[start : start + _ID_CHUNK_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT id, logical_asset_id, relative_path, filename, extension,
                           media_type, role, is_online, in_scope, metadata_json,
                           file_created_time
                    FROM physical_file
                    WHERE logical_asset_id IN ({placeholders}) AND in_scope = 1
                    ORDER BY logical_asset_id, is_online DESC, relative_path
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    physical_by_asset.setdefault(row["logical_asset_id"], []).append(row)
        finally:
            connection.close()
    points = []
    for item in items:
        physical = physical_by_asset.get(item["asset_id"], [])
        chosen = preferred_physical(physical)
        value = item.get("capture_time") if time_mode == "capture" else (chosen["file_created_time"] if chosen else None)
        kind = item.get("capture_time_kind") if time_mode == "capture" else "file_created"
        precision_us = timestamp_precision_us(value)
        if time_mode == "capture":
            metadata_value, metadata_kind, metadata_precision = _metadata_capture_time(physical)
            if metadata_value is not None and metadata_precision < precision_us:
                value, kind, precision_us = metadata_value, metadata_kind, metadata_precision
        coordinate = wall_clock_coordinate(value, kind)
        coordinate_us = wall_clock_coordinate_us(value, kind)
        if coordinate is None or coordinate_us is None:
            continue
        points.append(
            {
                "asset_id": item["asset_id"],
                "time": coordinate,
                "time_us": coordinate_us,
                "time_precision_us": precision_us,
                "time_kind": time_mode,
                "capture_time": item.get("capture_time"),
                "capture_time_kind": item.get("capture_time_kind"),
                "file_created_time": chosen["file_created_time"] if chosen else None,
                "media_type": item.get("media_type"),
                "quality_score": item.get("quality_score"),
            }
        )
    points.sort(key=lambda point: (point["time_us"], point["asset_id"]))
    return points


def _metadata_capture_time(physical) -> tuple[str | None, str | None, int]:
    best: tuple[str, str, int] | None = None
    for row in physical:
        metadata = _json_or_none(row["metadata_json"]) or {}
        exif = metadata.get("exif") or {}
        for key in ("DateTimeOriginal", "DateTimeDigitized", "DateTime"):
            value = exif.get(key)
            if value is None:
                continue
            suffix = key.removeprefix("DateTime")
            offset = exif.get(f"OffsetTime{suffix}") or exif.get("OffsetTimeOriginal")
            subsecond = exif.get(f"SubSecTime{suffix}") or exif.get(f"SubsecTime{suffix}")
            parsed, kind = _parse_exif_datetime(str(value), offset, subsecond)
            precision = subsecond_precision_us(subsecond)
            candidate = (parsed, kind, precision)
            if best is None or precision < best[2]:
                best = candidate
            break
    return best if best is not None else (None, None, 1_000_000)


def timestamp_precision_us(value: str | None) -> int:
    if not value:
        return 1_000_000
    match = _CAPTURE_PREFIX.match(str(value))
    fraction = match.group("fraction") if match else None
    digits = min(6, len(fraction or ""))
    return 1_000_000 // 10**digits


def subsecond_precision_us(value) -> int:
    digits = min(6, len("".join(character for character in str(value) if character.isdigit()))) if value is not None else 0
    return 1_000_000 // 10**digits if digits else 1_000_000


def wall_clock_coordinate(value: str | None, kind: str | None = None) -> float | None:
    """Return a timezone-independent coordinate for the displayed wall clock.

    Stored EXIF-local-unknown values are intentionally parsed as date/time
    components, not as UTC instants. Explicit offsets are also positioned by
    their displayed components so changing the machine timezone cannot move a
    photo on the Timeline.
    """

    coordinate_us = wall_clock_coordinate_us(value, kind)
    return coordinate_us / 1_000_000 if coordinate_us is not None else None


def wall_clock_coordinate_us(value: str | None, kind: str | None = None) -> int | None:
    if not value:
        return None
    match = _CAPTURE_PREFIX.match(str(value))
    if match is None or match.group("hour") is None:
        return None
    try:
        fraction = (match.group("fraction") or "")[:6].ljust(6, "0")
        date = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            int(fraction or 0),
            tzinfo=timezone.utc,
        )
        coordinate = int(date.timestamp()) * 1_000_000 + date.microsecond
    except (TypeError, ValueError, OverflowError):
        return None
    return coordinate if isfinite(coordinate) else None


def _json_or_none(value):
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
