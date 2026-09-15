"""Fast, non-destructive filesystem analysis for workspace setup."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .configuration import media_category, path_in_scope
from .media_types import is_raw_extension, is_rendered_image_extension, media_type_for
from .workspace import INDEX_DIRECTORY

MAX_EXAMPLES = 5


def analyze_folder(root: str | Path) -> dict[str, object]:
    workspace_root = Path(root).expanduser().resolve(strict=False)
    if not workspace_root.is_dir():
        raise ValueError(f"folder is not available: {workspace_root}")
    if workspace_root.name.casefold() == INDEX_DIRECTORY.casefold():
        raise ValueError("the application index cannot be used as a workspace")
    tree = _folder_node(workspace_root, "")
    totals = {
        "files": tree["recursive_files"],
        "bytes": tree["recursive_bytes"],
        "recognized_files": tree["recognized_files"],
        "recognized_bytes": tree["recognized_bytes"],
        "categories": tree["categories"],
        "extensions": tree["extensions"],
    }
    return {"root": tree, "totals": totals, "path": str(workspace_root)}


def plan_from_analysis(
    analysis: dict[str, object], configuration: dict[str, object], workspace=None
) -> dict[str, object]:
    totals = {"files": 0, "bytes": 0, "recognized_files": 0, "recognized_bytes": 0, "categories": {}, "extensions": {}}
    _accumulate_plan(analysis["root"], configuration, totals, "")
    rendered_image_count = sum(totals["categories"].get(category, 0) for category in ("jpeg", "other_image"))
    raw_count = totals["categories"].get("raw", 0)
    raw_quality_count = raw_count if configuration.get("raw_quality_provider") == "lar-iqa" else 0
    quality_image_count = (
        rendered_image_count if configuration.get("rendered_quality_provider", configuration.get("quality_provider")) == "lar-iqa" else 0
    ) + raw_quality_count
    video_count = totals["categories"].get("video", 0)
    estimated_seconds = round(quality_image_count * 0.12 + video_count * 0.08, 1)
    plan = {
        "selected_files": totals["files"],
        "selected_bytes": totals["bytes"],
        "selected_categories": totals["categories"],
        "selected_extensions": totals["extensions"],
        "quality_image_count": quality_image_count,
        "quality_rendered_image_count": rendered_image_count if configuration.get("rendered_quality_provider", configuration.get("quality_provider")) == "lar-iqa" else 0,
        "quality_raw_candidate_count": raw_quality_count,
        "quality_video_count": video_count if configuration.get("video_quality_enabled", configuration.get("quality_provider") == "lar-iqa") else 0,
        "video_count": video_count,
        "video_sampling": {
            "target_fps": configuration["video_sampling_fps"],
            "min_frames": configuration["video_sampling_min_frames"],
            "max_frames": configuration["video_sampling_max_frames"],
            "exact_sample_count": None,
            "note": "Exact frame count is determined from each video's probed duration during indexing.",
        },
        "estimated_seconds": estimated_seconds,
        "estimate_note": "Rough estimate; actual time depends on the local machine, media decoders, and model readiness.",
    }
    if workspace is not None and workspace.database_path.is_file():
        counts = _indexed_quality_counts(workspace, configuration)
        if counts is not None:
            plan.update(counts)
            plan["estimated_seconds"] = round(
                counts["quality_image_count"] * 0.12
                + counts["quality_video_count"] * 0.08,
                1,
            )
            plan["estimate_note"] = (
                "Existing logical-asset relationships are used when available; new or unindexed files remain approximate."
            )
    return plan


def _indexed_quality_counts(workspace, configuration: dict[str, object]) -> dict[str, int] | None:
    connection = workspace.connect()
    try:
        rows = connection.execute(
            "SELECT logical_asset_id, media_type, extension, relative_path FROM physical_file WHERE is_online = 1"
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return None
    scoped = [row for row in rows if path_in_scope(row["relative_path"], configuration)]
    rendered_assets = {
        row["logical_asset_id"]
        for row in scoped
        if row["media_type"] == "image" and is_rendered_image_extension(row["extension"])
    }
    raw_assets = {
        row["logical_asset_id"]
        for row in scoped
        if row["media_type"] == "image" and is_raw_extension(row["extension"])
    }
    rendered_quality = (
        len(rendered_assets)
        if configuration.get("rendered_quality_provider", configuration.get("quality_provider")) == "lar-iqa"
        else 0
    )
    raw_quality = (
        len(raw_assets - rendered_assets)
        if configuration.get("raw_quality_provider") == "lar-iqa"
        else 0
    )
    video_count = sum(row["media_type"] == "video" for row in scoped)
    return {
        "quality_image_count": rendered_quality + raw_quality,
        "quality_rendered_image_count": rendered_quality,
        "quality_raw_candidate_count": raw_quality,
        "quality_video_count": video_count if configuration.get("video_quality_enabled", False) else 0,
    }


def _folder_node(root: Path, relative: str) -> dict[str, object]:
    direct_files = direct_bytes = recognized_files = recognized_bytes = 0
    direct_extensions: dict[str, int] = {}
    direct_extension_bytes: dict[str, int] = {}
    direct_categories: dict[str, int] = {}
    examples: list[str] = []
    children: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        with os.scandir(root) as iterator:
            entries = sorted(iterator, key=lambda item: item.name.casefold())
    except OSError as error:
        return {
            "name": root.name,
            "path": relative,
            "direct_files": 0,
            "recursive_files": 0,
            "direct_bytes": 0,
            "recursive_bytes": 0,
            "recognized_files": 0,
            "recognized_bytes": 0,
            "extensions": {},
            "direct_extensions": {},
            "direct_extension_bytes": {},
            "direct_categories": {},
            "categories": {},
            "examples": [],
            "children": [],
            "error": str(error),
        }
    for entry in entries:
        if entry.name.casefold() == INDEX_DIRECTORY.casefold():
            continue
        entry_path = Path(entry.path)
        try:
            if _is_reparse(entry):
                continue
            if entry.is_dir(follow_symlinks=False):
                child_relative = f"{relative}/{entry.name}".strip("/")
                children.append(_folder_node(entry_path, child_relative))
            elif entry.is_file(follow_symlinks=False):
                size = entry.stat(follow_symlinks=False).st_size
                direct_files += 1
                direct_bytes += size
                extension = entry_path.suffix.casefold()
                extension = extension or "(none)"
                direct_extensions[extension] = direct_extensions.get(extension, 0) + 1
                direct_extension_bytes[extension] = direct_extension_bytes.get(extension, 0) + size
                category = media_category(extension)
                direct_categories[category] = direct_categories.get(category, 0) + 1
                recognized = media_type_for(entry_path)
                if recognized:
                    recognized_files += 1
                    recognized_bytes += size
                if len(examples) < MAX_EXAMPLES and recognized:
                    examples.append(entry.name)
        except OSError as error:
            errors.append(f"{entry.name}: {error}")
    recursive_files = direct_files
    recursive_bytes = direct_bytes
    recursive_extensions = dict(direct_extensions)
    recursive_categories = dict(direct_categories)
    for child in children:
        recursive_files += child["recursive_files"]
        recursive_bytes += child["recursive_bytes"]
        recognized_files += child["recognized_files"]
        recognized_bytes += child["recognized_bytes"]
        _merge_counts(recursive_extensions, child["extensions"])
        _merge_counts(recursive_categories, child["categories"])
    return {
        "name": root.name,
        "path": relative,
        "direct_files": direct_files,
        "recursive_files": recursive_files,
        "direct_bytes": direct_bytes,
        "recursive_bytes": recursive_bytes,
        "recognized_files": recognized_files,
        "recognized_bytes": recognized_bytes,
        "direct_extensions": dict(sorted(direct_extensions.items())),
        "direct_extension_bytes": dict(sorted(direct_extension_bytes.items())),
        "direct_categories": dict(sorted(direct_categories.items())),
        "extensions": dict(sorted(recursive_extensions.items())),
        "categories": dict(sorted(recursive_categories.items())),
        "examples": sorted(examples, key=str.casefold),
        "children": children,
        "error": "; ".join(errors) if errors else None,
    }


def _accumulate_plan(node, configuration, totals, parent_path: str) -> None:
    path = node["path"]
    folder = path or parent_path
    in_scope = _folder_in_scope(path, configuration)
    if in_scope:
        for extension, count in node["direct_extensions"].items():
            if extension == "(none)":
                continue
            category = media_category(extension)
            selected = (
                category in {"jpeg", "other_image"}
                and configuration.get("include_rendered_images", configuration["include_images"])
                and extension in configuration["image_extensions"]
            ) or (
                category == "raw"
                and configuration.get("include_raw", configuration["include_images"])
                and extension in configuration["image_extensions"]
            ) or (
                category == "video"
                and configuration["include_videos"]
                and extension in configuration["video_extensions"]
            )
            if selected:
                totals["files"] += count
                totals["bytes"] += node["direct_extension_bytes"].get(extension, 0)
                totals["extensions"][extension] = totals["extensions"].get(extension, 0) + count
                totals["categories"][category] = totals["categories"].get(category, 0) + count
    for child in node["children"]:
        _accumulate_plan(child, configuration, totals, folder)


def _folder_in_scope(path: str, configuration) -> bool:
    rules = {str(rule["path"]).casefold(): bool(rule["included"]) for rule in configuration.get("folder_rules", [])}
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    for index in range(len(parts), -1, -1):
        candidate = "/".join(parts[:index]).casefold()
        if candidate in rules:
            return bool(rules[candidate])
    return True


def _merge_counts(destination: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        destination[key] = destination.get(key, 0) + value


def _is_reparse(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
