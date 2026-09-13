"""Deterministic selection of a physical representation for an asset."""

from __future__ import annotations

from collections.abc import Iterable

from ..media_types import is_raw_extension


def row_value(row, key: str, default=None):
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def preferred_physical(rows: Iterable, *, component: str | None = None):
    candidates = list(rows)
    if not candidates:
        return None

    def key(row):
        status = None
        if component:
            status = row_value(row, f"{component}_status")
            if status is None and component == "quality":
                status = row_value(row, "quality_component_status")
        complete = status == "complete"
        rendered = row_value(row, "media_type") != "image" or not is_raw_extension(
            row_value(row, "extension", "")
        )
        return (
            not bool(row_value(row, "in_scope", 1)),
            not bool(row_value(row, "is_online", 0)),
            not complete if component else False,
            not rendered,
            row_value(row, "role", "") != "camera_jpeg",
            row_value(row, "relative_path", "").casefold(),
            row_value(row, "relative_path", ""),
            row_value(row, "id", ""),
        )

    return min(candidates, key=key)
