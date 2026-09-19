"""Browser-facing asset and physical-representation services."""

from __future__ import annotations

import json
from math import isfinite
from urllib.parse import urlencode

from ..indexing.representations import preferred_physical
from ..indexing.video_quality import video_quality_details
from ..media_types import is_raw_extension
from ..workspace import Workspace, WorkspaceError
from .errors import ResourceNotFound


def asset_summary(
    workspace: Workspace,
    asset,
    handle: str,
    physical=None,
    is_representative: bool = False,
    is_recommended: bool = False,
    recommendation_run_id: str | None = None,
    current_group_id: str | None = None,
) -> dict[str, object]:
    physical = physical_rows(workspace, asset["id"]) if physical is None else physical
    active_physical = [row for row in physical if row["in_scope"]] or physical
    representative = preferred_physical(active_physical)
    online = preferred_physical([row for row in active_physical if row["is_online"]])
    thumbnail = preferred_physical(
        [
            row for row in active_physical
            if row["thumbnail_status"] == "complete"
            and row["thumbnail_output_path"]
            and _valid_index_file(workspace, row["thumbnail_output_path"])
        ],
        component="thumbnail",
    )
    if representative is None:
        raise ResourceNotFound("asset has no physical representation")
    width, height = effective_dimensions(active_physical)
    rendered_quality = [
        row for row in active_physical
        if row["media_type"] != "image" or not is_raw_extension(row["extension"])
    ]
    quality_rows = rendered_quality or active_physical
    quality_score = next(
        (row["quality_score"] for row in quality_rows if row["quality_score"] is not None),
        None,
    )
    display = preferred_physical(
        [row for row in active_physical if row["is_online"] and (row["media_type"] != "image" or not is_raw_extension(row["extension"]))]
    ) or online
    display_url = None
    if display is not None:
        display_url = _url(
            f"/api/files/{display['id']}/{('preview' if display['media_type'] == 'image' and is_raw_extension(display['extension']) else 'original')}",
            handle,
        )
    return {
        "asset_id": asset["id"],
        "media_type": asset["media_type"],
        "codec": (online or representative)["codec"],
        "capture_time": asset["capture_time"],
        "capture_time_kind": asset["capture_time_kind"],
        "filename": representative["filename"],
        "relative_path": representative["relative_path"],
        "is_online": any(row["is_online"] for row in physical),
        "physical_count": len(physical),
        "online_count": sum(bool(row["is_online"]) for row in physical),
        "thumbnail_url": _url(f"/api/assets/{asset['id']}/thumbnail", handle) if thumbnail else None,
        "original_url": _url(f"/api/files/{online['id']}/original", handle) if online else None,
        "display_url": display_url,
        "quality_score": quality_score,
        "quality_source": quality_source(next((row for row in quality_rows if row["quality_score"] is not None), None)),
        "issues": asset_issues(physical),
        "is_representative": is_representative,
        "current_group_id": current_group_id,
        "auto_recommended": is_recommended,
        "user_decision": asset["selection_state"],
        "user_decision_updated_at": asset["selection_updated_at"],
        "recommendation_run_id": recommendation_run_id,
        "preferred_physical_id": representative["id"],
        "width": width,
        "height": height,
    }


def effective_dimensions(physical):
    active = [row for row in physical if row["in_scope"]] or list(physical)
    preferred = preferred_physical(active)
    candidates = ([preferred] if preferred is not None else []) + [
        row for row in active if row is not preferred
    ]
    for row in candidates:
        if row["width"] and row["height"]:
            return row["width"], row["height"]
    return None, None


def asset_issues(physical) -> list[str]:
    issues: list[str] = []
    active = [row for row in physical if row["in_scope"]] or list(physical)
    relevant = [
        row for row in active
        if row["media_type"] != "image" or not is_raw_extension(row["extension"])
    ] or active
    if not any(row["is_online"] for row in active):
        issues.append("offline")
    statuses = [row[key] for row in relevant for key in ("metadata_status", "thumbnail_status", "quality_component_status")]
    if any(status in {"pending", "running"} for status in statuses):
        issues.append("processing")
    if any(status == "unsupported" for status in statuses):
        issues.append("unsupported")
    if any(status == "failed" for status in statuses):
        issues.append("failed")
    return issues


def safe_absolute_path(workspace: Workspace, relative_path: str) -> str | None:
    try:
        return str(workspace.absolute_path(relative_path))
    except WorkspaceError:
        return None


def asset_detail(
    workspace: Workspace,
    asset_id: str,
    handle: str,
    current_recommendations,
    current_group_ids,
) -> dict[str, object]:
    connection = workspace.connect()
    try:
        asset = connection.execute("SELECT * FROM logical_asset WHERE id = ?", (asset_id,)).fetchone()
    finally:
        connection.close()
    if asset is None:
        raise ResourceNotFound("asset not found")
    physical = physical_rows(workspace, asset_id)
    preferred = preferred_physical(physical)
    ordered_physical = ([preferred] if preferred is not None else []) + [
        row for row in physical if preferred is None or row["id"] != preferred["id"]
    ]
    relationships = current_relationships(workspace, [row["id"] for row in physical])
    recommendation_ids, recommendation_run_id = current_recommendations(workspace)
    current_group_id = current_group_ids(workspace, [asset_id]).get(asset_id)
    location = asset_location(physical)
    return {
        "asset_id": asset["id"],
        "media_type": asset["media_type"],
        "capture_time": asset["capture_time"],
        "capture_time_kind": asset["capture_time_kind"],
        "auto_recommended": asset_id in recommendation_ids,
        "user_decision": asset["selection_state"],
        "user_decision_updated_at": asset["selection_updated_at"],
        "recommendation_run_id": recommendation_run_id,
        "current_group_id": current_group_id,
        "location": location,
        "physical_files": [
            {
                "id": row["id"],
                "relative_path": row["relative_path"],
                "absolute_path": safe_absolute_path(workspace, row["relative_path"]),
                "filename": row["filename"],
                "extension": row["extension"],
                "media_type": row["media_type"],
                "role": row["role"],
                "is_preferred": preferred is not None and row["id"] == preferred["id"],
                "relationships": relationships.get(row["id"], []),
                "representation_label": representation_label(row, relationships.get(row["id"], [])),
                "size_bytes": row["size_bytes"],
                "file_created_time": row["file_created_time"],
                "is_online": bool(row["is_online"]),
                "width": row["width"],
                "height": row["height"],
                "duration_seconds": row["duration_seconds"],
                "codec": row["codec"],
                "metadata": _json_or_none(row["metadata_json"]),
                "quality_score": row["quality_score"],
                "quality_source": quality_source(row),
                "quality_raw": _json_or_none(row["quality_raw_json"]),
                "quality_components": _json_or_none(row["quality_components_json"]),
                "video_quality": video_quality_details(workspace, row["id"])
                if row["media_type"] == "video" else None,
                "original_url": _url(f"/api/files/{row['id']}/original", handle) if row["is_online"] and row["in_scope"] else None,
                "thumbnail_url": _url(f"/api/files/{row['id']}/thumbnail", handle) if row["in_scope"] and row["thumbnail_status"] == "complete" and row["thumbnail_output_path"] and _valid_index_file(workspace, row["thumbnail_output_path"]) else None,
                "in_scope": bool(row["in_scope"]),
                "components": {
                    "metadata": component_info(row, "metadata"),
                    "thumbnail": component_info(row, "thumbnail"),
                    "quality": component_info(row, "quality"),
                },
            }
            for row in ordered_physical
        ],
    }


def asset_location(physical) -> dict[str, float] | None:
    active = [row for row in physical if row["in_scope"]] or list(physical)
    preferred = preferred_physical(active)
    candidates = ([preferred] if preferred is not None else []) + [
        row for row in active if preferred is None or row["id"] != preferred["id"]
    ]
    for row in candidates:
        metadata = _json_or_none(row["metadata_json"]) or {}
        gps = metadata.get("gps") or {}
        try:
            latitude = float(gps["latitude"])
            longitude = float(gps["longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        if isfinite(latitude) and isfinite(longitude) and -90 <= latitude <= 90 and -180 <= longitude <= 180:
            return {"latitude": latitude, "longitude": longitude}
    return None


def physical_rows(workspace: Workspace, asset_id: str):
    connection = workspace.connect()
    try:
        return connection.execute(
            """
            SELECT pf.*,
                   metadata.status AS metadata_status, metadata.algorithm AS metadata_algorithm,
                   metadata.version AS metadata_version, metadata.error_message AS metadata_error,
                   thumbnail.status AS thumbnail_status, thumbnail.algorithm AS thumbnail_algorithm,
                   thumbnail.version AS thumbnail_version, thumbnail.error_message AS thumbnail_error,
                   thumbnail.output_path AS thumbnail_output_path,
                   quality.status AS quality_component_status, quality.algorithm AS quality_component_algorithm,
                   quality.version AS quality_component_version, quality.error_message AS quality_component_error
            FROM physical_file AS pf
            LEFT JOIN component_state AS metadata ON metadata.physical_file_id = pf.id AND metadata.component = 'metadata'
            LEFT JOIN component_state AS thumbnail ON thumbnail.physical_file_id = pf.id AND thumbnail.component = 'thumbnail'
            LEFT JOIN component_state AS quality ON quality.physical_file_id = pf.id AND quality.component = 'quality'
            WHERE pf.logical_asset_id = ?
            ORDER BY pf.is_online DESC, pf.relative_path
            """,
            (asset_id,),
        ).fetchall()
    finally:
        connection.close()


def physical_rows_for_assets(workspace: Workspace, asset_ids: list[str]) -> dict[str, list]:
    if not asset_ids:
        return {}
    placeholders = ",".join("?" for _ in asset_ids)
    connection = workspace.connect()
    try:
        rows = connection.execute(
            f"""
            SELECT pf.*,
                   metadata.status AS metadata_status, metadata.algorithm AS metadata_algorithm,
                   metadata.version AS metadata_version, metadata.error_message AS metadata_error,
                   thumbnail.status AS thumbnail_status, thumbnail.algorithm AS thumbnail_algorithm,
                   thumbnail.version AS thumbnail_version, thumbnail.error_message AS thumbnail_error,
                   thumbnail.output_path AS thumbnail_output_path,
                   quality.status AS quality_component_status, quality.algorithm AS quality_component_algorithm,
                   quality.version AS quality_component_version, quality.error_message AS quality_component_error
            FROM physical_file AS pf
            LEFT JOIN component_state AS metadata ON metadata.physical_file_id = pf.id AND metadata.component = 'metadata'
            LEFT JOIN component_state AS thumbnail ON thumbnail.physical_file_id = pf.id AND thumbnail.component = 'thumbnail'
            LEFT JOIN component_state AS quality ON quality.physical_file_id = pf.id AND quality.component = 'quality'
            WHERE pf.logical_asset_id IN ({placeholders}) AND pf.in_scope = 1
            ORDER BY pf.logical_asset_id, pf.is_online DESC, pf.relative_path
            """,
            asset_ids,
        ).fetchall()
    finally:
        connection.close()
    grouped: dict[str, list] = {asset_id: [] for asset_id in asset_ids}
    for row in rows:
        grouped.setdefault(row["logical_asset_id"], []).append(row)
    return grouped


def current_relationships(workspace: Workspace, physical_ids: list[str]) -> dict[str, list[str]]:
    if not physical_ids:
        return {}
    placeholders = ",".join("?" for _ in physical_ids)
    connection = workspace.connect()
    try:
        rows = connection.execute(
            f"""
            SELECT pr.source_physical_file_id, pr.target_physical_file_id, pr.relationship_type
            FROM physical_relationship AS pr
            JOIN workspace_reconciliation AS wr ON wr.active_run_id = pr.run_id AND wr.id = 1
            WHERE pr.source_physical_file_id IN ({placeholders})
               OR pr.target_physical_file_id IN ({placeholders})
            ORDER BY pr.relationship_type, pr.source_physical_file_id, pr.target_physical_file_id
            """,
            [*physical_ids, *physical_ids],
        ).fetchall()
    finally:
        connection.close()
    relationships: dict[str, list[str]] = {}
    for row in rows:
        label = "Exact duplicate" if row["relationship_type"] == "exact_duplicate" else "RAW/JPEG pair"
        relationships.setdefault(row["source_physical_file_id"], []).append(label)
        relationships.setdefault(row["target_physical_file_id"], []).append(label)
    return {file_id: sorted(set(values)) for file_id, values in relationships.items()}


def representation_label(row, relationships: list[str]) -> str:
    if row["role"] == "camera_raw":
        return "RAW source"
    if row["role"] == "camera_jpeg":
        return "Camera JPEG"
    if relationships:
        return relationships[0]
    return "Physical file"


def component_info(row, component: str) -> dict[str, object]:
    prefix = "quality_component_" if component == "quality" else f"{component}_"
    return {"status": row[f"{prefix}status"], "algorithm": row[f"{prefix}algorithm"], "version": row[f"{prefix}version"], "error": row[f"{prefix}error"]}


def quality_source(row) -> str | None:
    if row is None or row["quality_score"] is None:
        return None
    raw = _json_or_none(row["quality_raw_json"])
    if isinstance(raw, dict) and raw.get("quality_source") == "raw_embedded_preview":
        return "RAW embedded preview"
    if row["media_type"] == "video":
        return "sampled video frames"
    return "rendered image"


def _url(path: str, handle: str) -> str:
    return f"{path}?{urlencode({'workspace': handle})}"


def _valid_index_file(workspace: Workspace, path: str) -> bool:
    try:
        return workspace.index_path(path).is_file()
    except WorkspaceError:
        return False


def _json_or_none(value: str | None):
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None
