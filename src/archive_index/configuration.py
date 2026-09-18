"""Persisted workspace scope and processing configuration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .media_types import IMAGE_EXTENSIONS, RAW_EXTENSIONS, VIDEO_EXTENSIONS

JPEG_EXTENSIONS = frozenset({".jpeg", ".jpg"})
CONFIGURATION_VERSION = 5
EMBEDDING_PROVIDERS = frozenset({"openclip-b16-datacomp-xl", "siglip2-base-patch16-224"})
VIDEO_SAMPLING_DEFAULT_FPS = 2.0
VIDEO_SAMPLING_DEFAULT_MIN_FRAMES = 2
VIDEO_SAMPLING_DEFAULT_MAX_FRAMES = 32


def default_configuration() -> dict[str, object]:
    return {
        "configuration_version": CONFIGURATION_VERSION,
        "quality_enabled": True,
        "video_processing_enabled": True,
        "include_rendered_images": True,
        "include_raw": True,
        "include_images": True,
        "include_videos": True,
        "image_extensions": sorted(IMAGE_EXTENSIONS),
        "video_extensions": sorted(VIDEO_EXTENSIONS),
        "folder_rules": [],
        "rendered_quality_provider": "lar-iqa",
        "raw_quality_provider": "lar-iqa",
        "video_quality_enabled": True,
        "quality_provider": "lar-iqa",
        "video_sampling_fps": VIDEO_SAMPLING_DEFAULT_FPS,
        "video_sampling_min_frames": VIDEO_SAMPLING_DEFAULT_MIN_FRAMES,
        "video_sampling_max_frames": VIDEO_SAMPLING_DEFAULT_MAX_FRAMES,
        "recommendation_threshold": 0.70,
        "semantic_search_enabled": True,
        "include_videos_in_semantic_search": True,
        "embedding_provider": "openclip-b16-datacomp-xl",
    }


def normalize_configuration(value: Mapping[str, object], root: Path | None = None) -> dict[str, object]:
    defaults = default_configuration()
    if not isinstance(value, Mapping):
        raise ValueError("configuration must be an object")
    version = value.get("configuration_version", CONFIGURATION_VERSION)
    if version not in {1, 2, 3, 4, CONFIGURATION_VERSION}:
        raise ValueError(f"unsupported configuration version: {version}")
    legacy_include_images = _bool(value.get("include_images", defaults["include_images"]), "include_images")
    _bool(
        value.get("include_rendered_images", legacy_include_images),
        "include_rendered_images",
    )
    _bool(value.get("include_raw", legacy_include_images), "include_raw")
    _bool(value.get("include_videos", defaults["include_videos"]), "include_videos")
    include_rendered_images = include_raw = include_videos = True
    legacy_quality_provider = value.get("quality_provider", defaults["quality_provider"])
    for name in ("quality_provider", "rendered_quality_provider", "raw_quality_provider"):
        if name in value and value[name] not in {"off", "lar-iqa"}:
            raise ValueError(f"{name} must be off or lar-iqa")
    quality_enabled_value = value.get("quality_enabled")
    if quality_enabled_value is None and not any(
        name in value for name in ("rendered_quality_provider", "raw_quality_provider", "video_quality_enabled")
    ):
        quality_enabled_value = legacy_quality_provider == "lar-iqa"
    if quality_enabled_value is None:
        quality_enabled_value = defaults["quality_enabled"]
    quality_enabled = _bool(quality_enabled_value, "quality_enabled")
    rendered_quality_provider = value.get(
        "rendered_quality_provider",
        "lar-iqa" if quality_enabled else "off",
    )
    raw_quality_provider = value.get("raw_quality_provider", defaults["raw_quality_provider"])
    video_quality_enabled = _bool(
        value.get("video_quality_enabled", quality_enabled),
        "video_quality_enabled",
    )
    if not quality_enabled:
        rendered_quality_provider = raw_quality_provider = "off"
        video_quality_enabled = False
    video_processing_value = value.get(
        "video_processing_enabled",
        bool(video_quality_enabled or value.get("include_videos_in_semantic_search", defaults["include_videos_in_semantic_search"])),
    )
    video_processing_enabled = _bool(video_processing_value, "video_processing_enabled")
    semantic_search_enabled = _bool(
        value.get("semantic_search_enabled", defaults["semantic_search_enabled"]),
        "semantic_search_enabled",
    )
    result = {
        "configuration_version": CONFIGURATION_VERSION,
        "quality_enabled": quality_enabled,
        "video_processing_enabled": video_processing_enabled,
        "include_rendered_images": include_rendered_images,
        "include_raw": include_raw,
        "include_images": include_rendered_images or include_raw,
        "include_videos": include_videos,
        "image_extensions": sorted(IMAGE_EXTENSIONS),
        "video_extensions": sorted(VIDEO_EXTENSIONS),
        "rendered_quality_provider": rendered_quality_provider,
        "raw_quality_provider": raw_quality_provider,
        "video_quality_enabled": video_quality_enabled,
        "quality_provider": rendered_quality_provider,
        "folder_rules": _folder_rules(value.get("folder_rules", []), root),
        "video_sampling_fps": _positive_float(
            value.get("video_sampling_fps", defaults["video_sampling_fps"]),
            "video_sampling_fps",
            maximum=60.0,
        ),
        "video_sampling_min_frames": _positive_int(
            value.get("video_sampling_min_frames", defaults["video_sampling_min_frames"]),
            "video_sampling_min_frames",
            maximum=256,
        ),
        "video_sampling_max_frames": _positive_int(
            value.get("video_sampling_max_frames", defaults["video_sampling_max_frames"]),
            "video_sampling_max_frames",
            maximum=256,
        ),
        "semantic_search_enabled": semantic_search_enabled,
        "include_videos_in_semantic_search": _bool(
            value.get("include_videos_in_semantic_search", defaults["include_videos_in_semantic_search"])
            and semantic_search_enabled
            and include_videos,
            "include_videos_in_semantic_search",
        ),
        "embedding_provider": value.get("embedding_provider", defaults["embedding_provider"]),
    }
    threshold = value.get("recommendation_threshold", 0.70)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 0 <= threshold <= 1:
        raise ValueError("recommendation_threshold must be between 0 and 1")
    result["recommendation_threshold"] = float(threshold)
    for name in ("rendered_quality_provider", "raw_quality_provider", "quality_provider"):
        if result[name] not in {"off", "lar-iqa"}:
            raise ValueError(f"{name} must be off or lar-iqa")
    if result["video_sampling_min_frames"] > result["video_sampling_max_frames"]:
        raise ValueError("video_sampling_min_frames must not exceed video_sampling_max_frames")
    if result["embedding_provider"] not in EMBEDDING_PROVIDERS:
        raise ValueError("embedding_provider is not supported")
    return result


def configuration_from_connection(connection) -> dict[str, object]:
    row = connection.execute("SELECT * FROM workspace_config WHERE id = 1").fetchone()
    if row is None:
        return default_configuration()
    rules = [
        {"path": item["path"], "included": bool(item["included"])}
        for item in connection.execute("SELECT path, included FROM folder_scope_rule ORDER BY path").fetchall()
    ]
    if row["configuration_version"] < CONFIGURATION_VERSION:
        rules = _materialize_legacy_folder_rules(connection, rules)
    return normalize_configuration({
        "configuration_version": row["configuration_version"],
        "quality_enabled": bool(row["quality_enabled"]),
        "video_processing_enabled": bool(row["video_processing_enabled"]),
        "include_rendered_images": bool(row["include_rendered_images"]),
        "include_raw": bool(row["include_raw"]),
        "include_images": bool(row["include_images"]),
        "include_videos": bool(row["include_videos"]),
        "image_extensions": json.loads(row["image_extensions_json"]),
        "video_extensions": json.loads(row["video_extensions_json"]),
        "rendered_quality_provider": row["rendered_quality_provider"],
        "raw_quality_provider": row["raw_quality_provider"],
        "video_quality_enabled": bool(row["video_quality_enabled"]),
        "folder_rules": rules,
        "video_sampling_fps": row["video_sampling_fps"],
        "video_sampling_min_frames": row["video_sampling_min_frames"],
        "video_sampling_max_frames": row["video_sampling_max_frames"],
        "semantic_search_enabled": bool(row["semantic_search_enabled"]),
        "include_videos_in_semantic_search": bool(row["include_videos_in_semantic_search"]),
        "embedding_provider": row["embedding_provider"],
        "recommendation_threshold": row["recommendation_threshold"],
    })


def save_configuration(connection, value: Mapping[str, object], root: Path | None = None) -> dict[str, object]:
    config = normalize_configuration(value, root)
    now = _timestamp()
    connection.execute(
        """
        INSERT INTO workspace_config(
            id, quality_provider, include_images, include_videos,
            image_extensions_json, video_extensions_json, configuration_version, updated_at,
            video_sampling_fps, video_sampling_min_frames, video_sampling_max_frames,
            include_rendered_images, include_raw, rendered_quality_provider,
            raw_quality_provider, video_quality_enabled,
            semantic_search_enabled, include_videos_in_semantic_search, embedding_provider,
            quality_enabled, video_processing_enabled
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            quality_provider = excluded.quality_provider,
            include_images = excluded.include_images,
            include_videos = excluded.include_videos,
            image_extensions_json = excluded.image_extensions_json,
            video_extensions_json = excluded.video_extensions_json,
            configuration_version = excluded.configuration_version,
            video_sampling_fps = excluded.video_sampling_fps,
            video_sampling_min_frames = excluded.video_sampling_min_frames,
            video_sampling_max_frames = excluded.video_sampling_max_frames,
            include_rendered_images = excluded.include_rendered_images,
            include_raw = excluded.include_raw,
            rendered_quality_provider = excluded.rendered_quality_provider,
            raw_quality_provider = excluded.raw_quality_provider,
            video_quality_enabled = excluded.video_quality_enabled,
            semantic_search_enabled = excluded.semantic_search_enabled,
            include_videos_in_semantic_search = excluded.include_videos_in_semantic_search,
            embedding_provider = excluded.embedding_provider,
            quality_enabled = excluded.quality_enabled,
            video_processing_enabled = excluded.video_processing_enabled,
            updated_at = excluded.updated_at
        """,
        (
            config["rendered_quality_provider"],
            int(config["include_images"]),
            int(config["include_videos"]),
            json.dumps(config["image_extensions"], separators=(",", ":")),
            json.dumps(config["video_extensions"], separators=(",", ":")),
            CONFIGURATION_VERSION,
            now,
            config["video_sampling_fps"],
            config["video_sampling_min_frames"],
            config["video_sampling_max_frames"],
            int(config["include_rendered_images"]),
            int(config["include_raw"]),
            config["rendered_quality_provider"],
            config["raw_quality_provider"],
            int(config["video_quality_enabled"]),
            int(config["semantic_search_enabled"]),
            int(config["include_videos_in_semantic_search"]),
            config["embedding_provider"],
            int(config["quality_enabled"]),
            int(config["video_processing_enabled"]),
        ),
    )
    connection.execute("UPDATE workspace_config SET recommendation_threshold = ? WHERE id = 1", (config["recommendation_threshold"],))
    connection.execute("DELETE FROM folder_scope_rule")
    connection.executemany(
        "INSERT INTO folder_scope_rule(path, included) VALUES (?, ?)",
        ((rule["path"], int(rule["included"])) for rule in config["folder_rules"]),
    )
    return config


def path_in_scope(relative_path: str, config: Mapping[str, object]) -> bool:
    parts = [part for part in str(relative_path).replace("\\", "/").split("/") if part]
    folder_parts = parts[:-1]
    rules = {
        str(rule["path"]).casefold(): bool(rule["included"])
        for rule in config.get("folder_rules", [])
    }
    folder = "/".join(folder_parts).casefold()
    if folder in rules and not rules[folder]:
        return False
    extension = Path(relative_path).suffix.casefold()
    if extension in RAW_EXTENSIONS:
        return bool(config.get("include_raw", config.get("include_images"))) and extension in config.get("image_extensions", ())
    if extension in IMAGE_EXTENSIONS:
        return bool(config.get("include_rendered_images", config.get("include_images"))) and extension in config.get("image_extensions", ())
    if extension in VIDEO_EXTENSIONS:
        return bool(config.get("include_videos")) and extension in config.get("video_extensions", ())
    return False


def media_category(extension: str) -> str:
    extension = extension.casefold()
    if extension in JPEG_EXTENSIONS:
        return "jpeg"
    if extension in RAW_EXTENSIONS:
        return "raw"
    if extension in IMAGE_EXTENSIONS:
        return "other_image"
    if extension in VIDEO_EXTENSIONS:
        return "video"
    return "other"


def _bool(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
    return value


def _positive_float(value: object, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or value > maximum:
        raise ValueError(f"{name} must be greater than 0 and at most {maximum}")
    return float(value)


def _positive_int(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return value


def _folder_rules(value: object, root: Path | None) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("folder_rules must be a list")
    rules: dict[str, tuple[str, bool]] = {}
    for rule in value:
        if not isinstance(rule, Mapping) or not isinstance(rule.get("path"), str) or not isinstance(rule.get("included"), bool):
            raise ValueError("folder rules must contain path and included")
        raw_path = rule["path"]
        path = raw_path.strip("/")
        parts = path.split("/") if path else []
        if (
            "\\" in path
            or raw_path.startswith("/")
            or Path(path).is_absolute()
            or ":" in path
            or any(part in {"", ".", ".."} for part in parts)
            or any(part.casefold() == ".archive-index" for part in parts)
        ):
            raise ValueError("folder rule must be a safe workspace-relative path")
        if root is not None:
            candidate = (root / Path(path)).resolve(strict=False)
            try:
                candidate.relative_to(root.resolve(strict=False))
            except ValueError as error:
                raise ValueError("folder rule is outside the workspace") from error
        rules[path.casefold()] = (path, rule["included"])
    return [
        {"path": path, "included": included}
        for path, included in sorted(rules.values(), key=lambda value: value[0].casefold())
    ]


def _materialize_legacy_folder_rules(connection, rules: list[dict[str, object]]) -> list[dict[str, object]]:
    legacy = {rule["path"].casefold(): bool(rule["included"]) for rule in rules}
    folders = {""}
    for row in connection.execute("SELECT relative_path FROM physical_file").fetchall():
        parts = str(row[0]).replace("\\", "/").split("/")[:-1]
        folders.update("/".join(parts[:index]) for index in range(1, len(parts) + 1))
    materialized = []
    for folder in sorted(folders, key=str.casefold):
        included = True
        parts = folder.split("/") if folder else []
        for index in range(len(parts), -1, -1):
            candidate = "/".join(parts[:index]).casefold()
            if candidate in legacy:
                included = legacy[candidate]
                break
        materialized.append({"path": folder, "included": included})
    return materialized


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
