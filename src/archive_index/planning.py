"""Fast, non-destructive filesystem analysis and indexing estimates."""

from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path

from .configuration import media_category, path_in_scope
from .media_types import is_raw_extension, is_rendered_image_extension, media_type_for
from .workspace import INDEX_DIRECTORY

MAX_EXAMPLES = 5
VIDEO_FALLBACK_SAMPLES = 8
FALLBACK_RATES = {
    "scan.discovery": 0.025,
    "scan.hashing": 0.06,
    "metadata.processing": 0.14,
    "thumbnail.image": 0.20,
    "thumbnail.raw": 0.30,
    "thumbnail.video": 1.10,
    "reconciliation": 0.035,
    "quality.image": 0.28,
    "quality.raw": 0.35,
    "quality.video_sample": 0.45,
    "embedding.image": 0.25,
    "embedding.video_sample": 0.30,
    "grouping.image": 0.06,
    "recommendations.asset": 0.015,
}
FIXED_SECONDS = {
    "scan": 1.0,
    "reconciliation": 0.8,
    "embedding_initialization": 8.0,
    "grouping": 0.8,
    "recommendations": 0.3,
}


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


def plan_from_analysis(analysis: dict[str, object], configuration: dict[str, object], workspace=None) -> dict[str, object]:
    totals = {"files": 0, "bytes": 0, "recognized_files": 0, "recognized_bytes": 0, "categories": {}, "extensions": {}}
    folder_rows: list[dict[str, object]] = []
    _accumulate_plan(analysis["root"], configuration, totals, folder_rows)
    counts = _indexed_work(workspace, configuration) if workspace is not None and workspace.database_path.is_file() else None
    for row in folder_rows:
        paths = row.pop("_paths")
        reusable = sum(counts["reusable_by_path"].get(path, False) for path in paths) if counts is not None else 0
        row["reusable_files"] = reusable
        row["pending_files"] = max(0, row["supported_files"] - reusable)
        row["pending_eta_seconds"] = round(row["eta_seconds"] * row["pending_files"] / row["supported_files"], 1) if row["supported_files"] else 0
    pending = _pending_counts(totals, configuration, counts)
    eta = _estimate_seconds(pending, workspace)
    reusable = counts["indexed_reusable_files"] if counts is not None else 0
    plan = {
        "selected_files": totals["files"],
        "selected_supported_files": totals["files"],
        "selected_bytes": totals["bytes"],
        "selected_categories": totals["categories"],
        "selected_extensions": totals["extensions"],
        "quality_image_count": pending["quality_images"] + pending["quality_raw"],
        "quality_rendered_image_count": pending["quality_images"],
        "quality_raw_candidate_count": pending["quality_raw"],
        "quality_video_count": pending["quality_videos"],
        "video_count": totals["categories"].get("video", 0),
        "video_sampling": {
            "target_fps": configuration["video_sampling_fps"],
            "min_frames": configuration["video_sampling_min_frames"],
            "max_frames": configuration["video_sampling_max_frames"],
            "exact_sample_count": pending["video_samples"] if pending["video_duration_known"] else None,
            "note": "Frame count uses each video's duration when it is already indexed; otherwise a conservative estimate is used.",
        },
        "estimated_seconds": round(sum(eta.values()), 1),
        "indexed_reusable_files": reusable,
        "pending_files": max(0, totals["files"] - reusable),
        "eta_seconds_by_feature": {key: round(value, 1) for key, value in eta.items()},
        "folder_rows": folder_rows,
    }
    if counts is not None:
        plan.update({
            "metadata_pending_count": counts["metadata_pending"],
            "thumbnail_pending_count": counts["thumbnail_pending"],
            "quality_pending_count": counts["quality_pending"],
            "quality_rendered_pending_count": counts["quality_images_pending"],
            "quality_raw_pending_count": counts["quality_raw_pending"],
            "quality_video_pending_count": counts["quality_video_pending"],
        })
    else:
        plan.update({
            "metadata_pending_count": totals["files"],
            "thumbnail_pending_count": totals["files"],
            "quality_pending_count": pending["quality_images"] + pending["quality_raw"],
            "quality_rendered_pending_count": pending["quality_images"],
            "quality_raw_pending_count": pending["quality_raw"],
            "quality_video_pending_count": pending["quality_videos"],
        })
    return plan


def _pending_counts(totals, configuration, counts):
    categories = totals["categories"]
    rendered = categories.get("jpeg", 0) + categories.get("other_image", 0)
    raw = categories.get("raw", 0)
    videos = categories.get("video", 0)
    if counts is not None:
        rendered = counts["rendered_images"]
        raw = counts["raw_images"]
        videos = counts["videos"]
    quality_images = counts["quality_images_pending"] if counts is not None else rendered if configuration.get("rendered_quality_provider") == "lar-iqa" else 0
    quality_raw = counts["quality_raw_pending"] if counts is not None else raw if configuration.get("raw_quality_provider") == "lar-iqa" else 0
    quality_videos = counts["quality_video_pending"] if counts is not None else videos if configuration.get("video_quality_enabled", False) else 0
    quality_video_samples = counts["video_samples_pending"] if counts is not None else videos * VIDEO_FALLBACK_SAMPLES
    if not configuration.get("video_quality_enabled", False):
        quality_video_samples = 0
    semantic_images = counts["embedding_images_pending"] if counts is not None else rendered + raw
    semantic_videos = counts["embedding_video_samples_pending"] if counts is not None else videos * VIDEO_FALLBACK_SAMPLES
    if not configuration.get("semantic_search_enabled", False):
        semantic_images = semantic_videos = 0
    video_samples = max(quality_video_samples, semantic_videos)
    return {
        "files": totals["files"] if counts is None else counts["metadata_pending"],
        "metadata": totals["files"] if counts is None else counts["metadata_pending"],
        "thumbnails_image": rendered if counts is None else counts["thumbnail_images_pending"],
        "thumbnails_raw": raw if counts is None else counts["thumbnail_raw_pending"],
        "thumbnails_video": videos if counts is None else counts["thumbnail_video_pending"],
        "quality_images": quality_images,
        "quality_raw": quality_raw,
        "quality_videos": quality_videos,
        "video_samples": video_samples,
        "quality_video_samples": quality_video_samples,
        "semantic_images": semantic_images,
        "semantic_videos": semantic_videos,
        "video_duration_known": counts is not None and counts["video_duration_known"],
        "grouping_images": rendered + raw if counts is None else counts["grouping_images_pending"],
        "recommendation_assets": rendered + raw + videos if counts is None else counts["recommendation_assets_pending"],
    }


def _estimate_seconds(pending, workspace=None):
    rate = lambda name: _historical_rate(workspace, name) if workspace is not None else FALLBACK_RATES[name]
    return {
        "scan": FIXED_SECONDS["scan"] + pending["files"] * rate("scan.discovery"),
        "hashing": pending["files"] * rate("scan.hashing"),
        "metadata": pending["metadata"] * rate("metadata.processing"),
        "thumbnails": pending["thumbnails_image"] * rate("thumbnail.image") + pending["thumbnails_raw"] * rate("thumbnail.raw") + pending["thumbnails_video"] * rate("thumbnail.video"),
        "reconciliation": FIXED_SECONDS["reconciliation"] + pending["files"] * rate("reconciliation"),
        "rendered_quality": pending["quality_images"] * rate("quality.image"),
        "raw_quality": pending["quality_raw"] * rate("quality.raw"),
        "video_quality": pending["quality_videos"] * 0.6 + pending["quality_video_samples"] * rate("quality.video_sample"),
        "embedding_initialization": FIXED_SECONDS["embedding_initialization"] if pending["semantic_images"] + pending["semantic_videos"] else 0,
        "image_embeddings": pending["semantic_images"] * rate("embedding.image"),
        "video_embeddings": pending["semantic_videos"] * rate("embedding.video_sample"),
        "grouping": FIXED_SECONDS["grouping"] + pending["grouping_images"] * rate("grouping.image") if pending["grouping_images"] else 0,
        "recommendations": FIXED_SECONDS["recommendations"] + pending["recommendation_assets"] * rate("recommendations.asset") if pending["recommendation_assets"] else 0,
        "semantic_search": 0,
    }


def _historical_rate(workspace, name: str) -> float:
    connection = workspace.connect()
    try:
        rows = connection.execute("SELECT timing_json FROM job WHERE timing_json IS NOT NULL ORDER BY finished_at DESC LIMIT 8").fetchall()
    finally:
        connection.close()
    seconds = count = 0.0
    for row in rows:
        try:
            summary = json.loads(row["timing_json"])
            seconds += float(summary.get("seconds", {}).get(name, 0))
            count += float(summary.get("counts", {}).get(name, 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return max(seconds / count, FALLBACK_RATES[name] * 0.45) if count > 0 and seconds > 0 else FALLBACK_RATES[name]


def _indexed_work(workspace, configuration):
    connection = workspace.connect()
    try:
        rows = connection.execute(
            """
            SELECT pf.*, metadata.status AS metadata_status, thumbnail.status AS thumbnail_status,
                   quality.status AS quality_status, embedding.status AS embedding_status,
                   vf.physical_file_id AS feature_ready
            FROM physical_file AS pf
            LEFT JOIN component_state metadata ON metadata.physical_file_id = pf.id AND metadata.component = 'metadata'
            LEFT JOIN component_state thumbnail ON thumbnail.physical_file_id = pf.id AND thumbnail.component = 'thumbnail'
            LEFT JOIN component_state quality ON quality.physical_file_id = pf.id AND quality.component = 'quality'
            LEFT JOIN component_state embedding ON embedding.physical_file_id = pf.id AND embedding.component = ?
            LEFT JOIN visual_feature vf ON vf.physical_file_id = pf.id
            WHERE pf.is_online = 1 AND pf.in_scope = 1
            ORDER BY pf.logical_asset_id, pf.relative_path
            """
            , (f"embedding:{configuration['embedding_provider']}",)
        ).fetchall()
    finally:
        connection.close()
    rows = [row for row in rows if path_in_scope(row["relative_path"], configuration)]
    by_asset: dict[str, list] = {}
    for row in rows:
        by_asset.setdefault(row["logical_asset_id"], []).append(row)
    thumbnails = []
    quality = []
    rendered_images = raw_images = raw_only = videos = 0
    for members in by_asset.values():
        rendered = [row for row in members if row["media_type"] == "image" and is_rendered_image_extension(row["extension"])]
        raw = [row for row in members if row["media_type"] == "image" and is_raw_extension(row["extension"])]
        video = [row for row in members if row["media_type"] == "video"]
        if video:
            videos += 1
            thumbnails.append(("video", video[0]))
            quality.append(("video", video[0]))
        elif rendered:
            rendered_images += 1
            thumbnails.append(("image", rendered[0]))
            quality.append(("image", rendered[0]))
        elif raw:
            raw_images += 1
            raw_only += 1
            thumbnails.append(("raw", raw[0]))
            quality.append(("raw", raw[0]))
    video_samples = 0
    video_duration_known = True
    for kind, row in quality:
        if kind != "video":
            continue
        try:
            video_samples += _sample_count(float(row["duration_seconds"]), configuration["video_sampling_fps"], configuration["video_sampling_min_frames"], configuration["video_sampling_max_frames"])
        except (TypeError, ValueError):
            video_samples += VIDEO_FALLBACK_SAMPLES
            video_duration_known = False
    quality_images = [row for kind, row in quality if kind == "image" and configuration.get("rendered_quality_provider") == "lar-iqa"]
    quality_raw = [row for kind, row in quality if kind == "raw" and configuration.get("raw_quality_provider") == "lar-iqa"]
    quality_videos = [row for kind, row in quality if kind == "video" and configuration.get("video_quality_enabled", False)]
    thumbnail_pending = [row for _, row in thumbnails if row["thumbnail_status"] not in {"complete", "not_requested"}]
    return {
        "indexed_reusable_files": sum(row["metadata_status"] == "complete" and row["thumbnail_status"] in {"complete", "not_requested"} for row in rows),
        "reusable_by_path": {
            row["relative_path"]: row["metadata_status"] == "complete" and row["thumbnail_status"] in {"complete", "not_requested"}
            for row in rows
        },
        "metadata_pending": sum(row["metadata_status"] != "complete" for row in rows),
        "thumbnail_pending": len(thumbnail_pending),
        "thumbnail_images_pending": sum(row["thumbnail_status"] not in {"complete", "not_requested"} for kind, row in thumbnails if kind == "image"),
        "thumbnail_raw_pending": sum(row["thumbnail_status"] not in {"complete", "not_requested"} for kind, row in thumbnails if kind == "raw"),
        "thumbnail_video_pending": sum(row["thumbnail_status"] not in {"complete", "not_requested"} for kind, row in thumbnails if kind == "video"),
        "quality_pending": sum(row["quality_status"] != "complete" for row in quality_images + quality_raw + quality_videos),
        "quality_images_pending": sum(row["quality_status"] != "complete" for row in quality_images),
        "quality_raw_pending": sum(row["quality_status"] != "complete" for row in quality_raw),
        "quality_video_pending": sum(row["quality_status"] != "complete" for row in quality_videos),
        "rendered_images": rendered_images,
        "raw_images": raw_images,
        "raw_only_images": raw_only,
        "videos": videos,
        "video_samples_pending": video_samples if configuration.get("video_quality_enabled", False) else 0,
        "video_duration_known": video_duration_known,
        "embedding_images_pending": sum(
            row["embedding_status"] != "complete"
            for kind, row in quality
            if kind in {"image", "raw"}
        ),
        "embedding_video_samples_pending": video_samples
        if configuration.get("semantic_search_enabled", False)
        and configuration.get("include_videos_in_semantic_search", False)
        and any(row["embedding_status"] != "complete" for kind, row in quality if kind == "video")
        else 0,
        "grouping_images_pending": sum(row["feature_ready"] is None for row in rows if row["media_type"] == "image"),
        "recommendation_assets_pending": rendered_images + raw_images + videos,
    }


def _sample_count(duration, fps, minimum, maximum):
    return max(minimum, min(maximum, math.ceil(duration * fps)))


def _folder_node(root: Path, relative: str) -> dict[str, object]:
    direct_files = direct_bytes = recognized_files = recognized_bytes = 0
    direct_extensions: dict[str, int] = {}
    direct_extension_bytes: dict[str, int] = {}
    direct_categories: dict[str, int] = {}
    direct_supported_paths: list[str] = []
    examples: list[str] = []
    children: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        with os.scandir(root) as iterator:
            entries = sorted(iterator, key=lambda item: item.name.casefold())
    except OSError as error:
        return {"name": root.name, "path": relative, "direct_files": 0, "recursive_files": 0, "direct_bytes": 0, "recursive_bytes": 0, "recognized_files": 0, "recognized_bytes": 0, "extensions": {}, "direct_extensions": {}, "direct_extension_bytes": {}, "direct_supported_paths": [], "direct_categories": {}, "categories": {}, "examples": [], "children": [], "error": str(error)}
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
                extension = entry_path.suffix.casefold() or "(none)"
                direct_extensions[extension] = direct_extensions.get(extension, 0) + 1
                direct_extension_bytes[extension] = direct_extension_bytes.get(extension, 0) + size
                category = media_category(extension)
                direct_categories[category] = direct_categories.get(category, 0) + 1
                recognized = media_type_for(entry_path)
                if recognized:
                    recognized_files += 1
                    recognized_bytes += size
                    if category in {"jpeg", "other_image", "raw", "video"}:
                        direct_supported_paths.append(f"{relative}/{entry.name}".strip("/"))
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
    return {"name": root.name, "path": relative, "direct_files": direct_files, "recursive_files": recursive_files, "direct_bytes": direct_bytes, "recursive_bytes": recursive_bytes, "recognized_files": recognized_files, "recognized_bytes": recognized_bytes, "direct_extensions": dict(sorted(direct_extensions.items())), "direct_extension_bytes": dict(sorted(direct_extension_bytes.items())), "direct_supported_paths": direct_supported_paths, "direct_categories": dict(sorted(direct_categories.items())), "extensions": dict(sorted(recursive_extensions.items())), "categories": dict(sorted(recursive_categories.items())), "examples": sorted(examples, key=str.casefold), "children": children, "error": "; ".join(errors) if errors else None}


def _accumulate_plan(node, configuration, totals, folder_rows) -> None:
    path = node["path"]
    selected = _folder_in_scope(path, configuration)
    selected_files = selected_bytes = 0
    selected_categories: dict[str, int] = {}
    selected_extensions: dict[str, int] = {}
    for extension, count in node["direct_extensions"].items():
        category = media_category(extension)
        if extension == "(none)" or category not in {"jpeg", "other_image", "raw", "video"} or not _category_enabled(category, configuration):
            continue
        selected_files += count
        selected_bytes += node["direct_extension_bytes"].get(extension, 0)
        selected_extensions[extension] = count
        selected_categories[category] = count
        if selected:
            totals["files"] += count
            totals["bytes"] += node["direct_extension_bytes"].get(extension, 0)
            totals["extensions"][extension] = totals["extensions"].get(extension, 0) + count
            totals["categories"][category] = totals["categories"].get(category, 0) + count
    paths = [
        candidate for candidate in node["direct_supported_paths"]
        if _category_enabled(media_category(Path(candidate).suffix), configuration)
    ]
    row = {"path": path, "selected": selected, "direct_files": node["direct_files"], "supported_files": selected_files, "supported_bytes": selected_bytes, "categories": selected_categories, "extensions": selected_extensions, "eta_seconds": round(_folder_eta(selected_files, selected_categories, configuration), 1), "_paths": paths}
    folder_rows.append(row)
    for child in node["children"]:
        _accumulate_plan(child, configuration, totals, folder_rows)


def _folder_eta(files, categories, configuration):
    images = categories.get("jpeg", 0) + categories.get("other_image", 0)
    raw = categories.get("raw", 0)
    videos = categories.get("video", 0)
    samples = videos * VIDEO_FALLBACK_SAMPLES
    semantic_video_samples = samples if configuration.get("include_videos_in_semantic_search", False) else 0
    return FIXED_SECONDS["scan"] + files * (FALLBACK_RATES["scan.discovery"] + FALLBACK_RATES["scan.hashing"] + FALLBACK_RATES["metadata.processing"]) + images * (FALLBACK_RATES["thumbnail.image"] + (FALLBACK_RATES["quality.image"] if configuration.get("rendered_quality_provider") == "lar-iqa" else 0)) + raw * (FALLBACK_RATES["thumbnail.raw"] + (FALLBACK_RATES["quality.raw"] if configuration.get("raw_quality_provider") == "lar-iqa" else 0)) + videos * FALLBACK_RATES["thumbnail.video"] + (videos * 0.6 + samples * FALLBACK_RATES["quality.video_sample"] if configuration.get("video_quality_enabled", False) else 0) + ((images + raw) * FALLBACK_RATES["embedding.image"] + semantic_video_samples * FALLBACK_RATES["embedding.video_sample"] if configuration.get("semantic_search_enabled", False) else 0)


def _category_enabled(category: str, configuration) -> bool:
    if category in {"jpeg", "other_image"}:
        return bool(configuration.get("include_rendered_images", configuration.get("include_images", True)))
    if category == "raw":
        return bool(configuration.get("include_raw", configuration.get("include_images", True)))
    if category == "video":
        return bool(configuration.get("include_videos", True))
    return False


def _folder_in_scope(path: str, configuration) -> bool:
    rules = {str(rule["path"]).casefold(): bool(rule["included"]) for rule in configuration.get("folder_rules", [])}
    return rules.get(path.replace("\\", "/").casefold(), True)


def _merge_counts(destination: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        destination[key] = destination.get(key, 0) + value


def _is_reparse(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
