"""Compact read-only datasets for the workspace visualizations."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from math import isfinite

from ..api.browser import asset_location
from ..indexing.representations import preferred_physical

_ID_CHUNK_SIZE = 500
_CAPTURE_PREFIX = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:[T ](?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?)?"
)


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
        summaries = {item["asset_id"]: item for item in items}
        points = [
            {
                **point,
                "filename": summaries[point["asset_id"]].get("filename"),
                "quality_score": summaries[point["asset_id"]].get("quality_score"),
            }
            for point in points
        ]
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
    summaries = {item["asset_id"]: item for item in items}
    points = []
    for asset_id in asset_ids:
        location = asset_location(by_asset[asset_id])
        if location is not None:
            item = summaries[asset_id]
            points.append({
                "asset_id": asset_id,
                **location,
                "filename": item.get("filename"),
                "media_type": item.get("media_type"),
                "quality_score": item.get("quality_score"),
            })
    return points


def _timeline_points(workspace, items, time_mode: str = "capture") -> list[dict[str, object]]:
    created_by_asset = {}
    if time_mode == "file_created" and items:
        asset_ids = [item["asset_id"] for item in items]
        connection = workspace.connect()
        try:
            for start in range(0, len(asset_ids), _ID_CHUNK_SIZE):
                chunk = asset_ids[start : start + _ID_CHUNK_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT * FROM physical_file
                    WHERE logical_asset_id IN ({placeholders}) AND in_scope = 1
                    ORDER BY logical_asset_id, is_online DESC, relative_path
                    """,
                    chunk,
                ).fetchall()
                for row in rows:
                    created_by_asset.setdefault(row["logical_asset_id"], []).append(row)
        finally:
            connection.close()
    points = []
    for item in items:
        physical = created_by_asset.get(item["asset_id"], [])
        chosen = preferred_physical(physical)
        value = item.get("capture_time") if time_mode == "capture" else (chosen["file_created_time"] if chosen else None)
        kind = item.get("capture_time_kind") if time_mode == "capture" else "file_created"
        coordinate = wall_clock_coordinate(value, kind)
        if coordinate is None:
            continue
        points.append(
            {
                "asset_id": item["asset_id"],
                "time": coordinate,
                "time_kind": time_mode,
                "capture_time": item.get("capture_time"),
                "capture_time_kind": item.get("capture_time_kind"),
                "file_created_time": chosen["file_created_time"] if chosen else None,
                "filename": item.get("filename"),
                "media_type": item.get("media_type"),
                "quality_score": item.get("quality_score"),
            }
        )
    points.sort(key=lambda point: (point["time"], point["asset_id"]))
    return points


def wall_clock_coordinate(value: str | None, kind: str | None = None) -> float | None:
    """Return a timezone-independent coordinate for the displayed wall clock.

    Stored EXIF-local-unknown values are intentionally parsed as date/time
    components, not as UTC instants. Explicit offsets are also positioned by
    their displayed components so changing the machine timezone cannot move a
    photo on the Timeline.
    """

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
        coordinate = date.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    return coordinate if isfinite(coordinate) else None
