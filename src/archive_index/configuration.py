"""Persisted workspace scope and processing configuration."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .media_types import IMAGE_EXTENSIONS, RAW_EXTENSIONS, VIDEO_EXTENSIONS

JPEG_EXTENSIONS = frozenset({".jpeg", ".jpg"})
CONFIGURATION_VERSION = 1


def default_configuration() -> dict[str, object]:
    return {
        "configuration_version": CONFIGURATION_VERSION,
        "include_images": True,
        "include_videos": True,
        "image_extensions": sorted(IMAGE_EXTENSIONS),
        "video_extensions": sorted(VIDEO_EXTENSIONS),
        "folder_rules": [],
        "quality_provider": "lar-iqa",
    }


def normalize_configuration(value: Mapping[str, object], root: Path | None = None) -> dict[str, object]:
    defaults = default_configuration()
    if not isinstance(value, Mapping):
        raise ValueError("configuration must be an object")
    version = value.get("configuration_version", CONFIGURATION_VERSION)
    if version != CONFIGURATION_VERSION:
        raise ValueError(f"unsupported configuration version: {version}")
    result = {
        "configuration_version": CONFIGURATION_VERSION,
        "include_images": _bool(value.get("include_images", defaults["include_images"]), "include_images"),
        "include_videos": _bool(value.get("include_videos", defaults["include_videos"]), "include_videos"),
        "image_extensions": _extensions(value.get("image_extensions", defaults["image_extensions"]), IMAGE_EXTENSIONS, "image_extensions"),
        "video_extensions": _extensions(value.get("video_extensions", defaults["video_extensions"]), VIDEO_EXTENSIONS, "video_extensions"),
        "quality_provider": value.get("quality_provider", defaults["quality_provider"]),
        "folder_rules": _folder_rules(value.get("folder_rules", []), root),
    }
    if result["quality_provider"] not in {"off", "lar-iqa"}:
        raise ValueError("quality_provider must be off or lar-iqa")
    return result


def configuration_from_connection(connection) -> dict[str, object]:
    row = connection.execute("SELECT * FROM workspace_config WHERE id = 1").fetchone()
    if row is None:
        return default_configuration()
    if row["configuration_version"] != CONFIGURATION_VERSION:
        raise ValueError(f"unsupported configuration version: {row['configuration_version']}")
    rules = [
        {"path": item["path"], "included": bool(item["included"])}
        for item in connection.execute("SELECT path, included FROM folder_scope_rule ORDER BY path").fetchall()
    ]
    return normalize_configuration({
        "configuration_version": row["configuration_version"],
        "include_images": bool(row["include_images"]),
        "include_videos": bool(row["include_videos"]),
        "image_extensions": json.loads(row["image_extensions_json"]),
        "video_extensions": json.loads(row["video_extensions_json"]),
        "quality_provider": row["quality_provider"],
        "folder_rules": rules,
    })


def save_configuration(connection, value: Mapping[str, object], root: Path | None = None) -> dict[str, object]:
    config = normalize_configuration(value, root)
    now = _timestamp()
    connection.execute(
        """
        INSERT INTO workspace_config(
            id, quality_provider, include_images, include_videos,
            image_extensions_json, video_extensions_json, configuration_version, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            quality_provider = excluded.quality_provider,
            include_images = excluded.include_images,
            include_videos = excluded.include_videos,
            image_extensions_json = excluded.image_extensions_json,
            video_extensions_json = excluded.video_extensions_json,
            configuration_version = excluded.configuration_version,
            updated_at = excluded.updated_at
        """,
        (
            config["quality_provider"],
            int(config["include_images"]),
            int(config["include_videos"]),
            json.dumps(config["image_extensions"], separators=(",", ":")),
            json.dumps(config["video_extensions"], separators=(",", ":")),
            CONFIGURATION_VERSION,
            now,
        ),
    )
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
    nearest = True
    for index in range(len(folder_parts), -1, -1):
        candidate = "/".join(folder_parts[:index]).casefold()
        if candidate in rules:
            nearest = rules[candidate]
            break
    if not nearest:
        return False
    extension = Path(relative_path).suffix.casefold()
    if extension in IMAGE_EXTENSIONS:
        return bool(config.get("include_images")) and extension in config.get("image_extensions", ())
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


def _extensions(value: object, allowed: frozenset[str], name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list")
    extensions = sorted({item.casefold() for item in value})
    if any(item not in allowed for item in extensions):
        raise ValueError(f"{name} contains an unsupported extension")
    return extensions


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


def _timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")
