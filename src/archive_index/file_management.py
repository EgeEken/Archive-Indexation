"""Persisted compression profiles and deterministic non-destructive plans."""

from __future__ import annotations

import json
import hashlib
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .workspace import Workspace, WorkspaceError
from .media.capabilities import av1_capability

BUILTIN_HIGH_QUALITY_PROFILE_ID = "builtin-jxl-high-quality"
BUILTIN_BALANCED_PROFILE_ID = "builtin-jxl-balanced"
BUILTIN_HIGH_COMPRESSION_PROFILE_ID = "builtin-jxl-high-compression"
BUILTIN_ARCHIVE_CLEANUP_ID = "builtin-archive-cleanup"
BUILTIN_KEEP_SELECTED_ID = "builtin-keep-selected-only"
BUILTIN_AV1_PROFILE_ID = "builtin-av1-archival"
BUILTIN_PROFILE_IDS = {
    BUILTIN_HIGH_QUALITY_PROFILE_ID,
    BUILTIN_BALANCED_PROFILE_ID,
    BUILTIN_HIGH_COMPRESSION_PROFILE_ID,
    BUILTIN_AV1_PROFILE_ID,
}
BUILTIN_RULESET_IDS = {BUILTIN_ARCHIVE_CLEANUP_ID, BUILTIN_KEEP_SELECTED_ID}


def quality_to_distance(quality: float) -> float:
    """Map the user-facing quality scale to the native JPEG XL distance scale."""
    value = max(0.0, min(100.0, float(quality)))
    return round((100.0 - value) * 1.5 / 40.0, 4)


def distance_to_quality(distance: float) -> float:
    """Read legacy/custom native-distance settings without changing their meaning."""
    return round(max(0.0, min(100.0, 100.0 - float(distance) * 40.0 / 1.5)), 2)


def list_profiles(workspace: Workspace) -> list[dict[str, object]]:
    _ensure_builtins(workspace)
    connection = workspace.connect()
    try:
        rows = connection.execute(
            "SELECT * FROM compression_profile ORDER BY name, id"
        ).fetchall()
    finally:
        connection.close()
    return [{**_profile(row), "is_builtin": row["id"] in BUILTIN_PROFILE_IDS} for row in rows]


def save_profile(
    workspace: Workspace,
    *,
    name: str,
    codec: str,
    container: str,
    settings: dict[str, object] | None = None,
    profile_id: str | None = None,
) -> dict[str, object]:
    if not name.strip():
        raise ValueError("profile name is required")
    if codec not in {"jpeg-xl", "avif", "av1"}:
        raise ValueError("codec must be jpeg-xl, avif, or av1")
    if not container.strip():
        raise ValueError("container is required")
    if profile_id in BUILTIN_PROFILE_IDS:
        raise ValueError("built-in compression profiles are immutable")
    normalized_settings = dict(settings or {})
    if codec == "jpeg-xl":
        if "quality" in normalized_settings:
            normalized_settings["quality"] = max(0.0, min(100.0, float(normalized_settings["quality"])))
            normalized_settings["distance"] = quality_to_distance(normalized_settings["quality"])
        elif "distance" in normalized_settings:
            normalized_settings["quality"] = distance_to_quality(normalized_settings["distance"])
    now = _timestamp()
    profile_id = profile_id or str(uuid.uuid4())
    with workspace.transaction() as connection:
        connection.execute(
            """
            INSERT INTO compression_profile(id, name, codec, container, settings_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, codec = excluded.codec,
              container = excluded.container, settings_json = excluded.settings_json,
              updated_at = excluded.updated_at
            """,
            (profile_id, name.strip(), codec, container.strip(), json.dumps(normalized_settings, sort_keys=True), now, now),
        )
    return next(profile for profile in list_profiles(workspace) if profile["id"] == profile_id)


def list_rulesets(workspace: Workspace) -> list[dict[str, object]]:
    _ensure_builtins(workspace)
    connection = workspace.connect()
    try:
        rulesets = connection.execute(
            "SELECT * FROM file_management_ruleset ORDER BY name, id"
        ).fetchall()
        rules = connection.execute(
            "SELECT * FROM file_management_rule WHERE enabled = 1 ORDER BY ruleset_id, position, id"
        ).fetchall()
    finally:
        connection.close()
    by_ruleset: dict[str, list[dict[str, object]]] = {}
    for row in rules:
        by_ruleset.setdefault(row["ruleset_id"], []).append(_rule(row))
    return [{**_ruleset(row), "rules": by_ruleset.get(row["id"], []), "is_builtin": row["id"] in BUILTIN_RULESET_IDS} for row in rulesets]


def list_presets(workspace: Workspace) -> list[dict[str, object]]:
    _ensure_builtins(workspace)
    connection = workspace.connect()
    try:
        rows = connection.execute(
            """
            SELECT p.*, r.name AS ruleset_name
            FROM file_management_preset AS p
            JOIN file_management_ruleset AS r ON r.id = p.ruleset_id
            ORDER BY p.name, p.id
            """
        ).fetchall()
    finally:
        connection.close()
    return [{**dict(row), "is_builtin": row["id"] in BUILTIN_RULESET_IDS} for row in rows]


def save_ruleset(
    workspace: Workspace,
    *,
    name: str,
    rules: list[dict[str, object]],
    description: str = "",
    ruleset_id: str | None = None,
) -> dict[str, object]:
    if not name.strip():
        raise ValueError("ruleset name is required")
    if not isinstance(rules, list):
        raise ValueError("rules must be a list")
    if ruleset_id in BUILTIN_RULESET_IDS:
        raise ValueError("built-in rulesets are immutable")
    now = _timestamp()
    new_ruleset = ruleset_id is None
    ruleset_id = ruleset_id or str(uuid.uuid4())
    with workspace.transaction() as connection:
        connection.execute(
            """
            INSERT INTO file_management_ruleset(id, name, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, description = excluded.description,
              updated_at = excluded.updated_at
            """,
            (ruleset_id, name.strip(), description.strip(), now, now),
        )
        connection.execute("DELETE FROM file_management_rule WHERE ruleset_id = ?", (ruleset_id,))
        rule_ids = set()
        for position, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise ValueError("each rule must be an object")
            match = rule.get("match") or {}
            action = rule.get("action") or {}
            if not isinstance(match, dict) or not isinstance(action, dict):
                raise ValueError("rule match and action must be objects")
            rule_id = str(rule.get("id") or uuid.uuid4())
            if new_ruleset or rule_id in rule_ids:
                rule_id = str(uuid.uuid4())
            rule_ids.add(rule_id)
            connection.execute(
                """
                INSERT INTO file_management_rule(
                    id, ruleset_id, position, enabled, match_json, action_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id, ruleset_id, position,
                    1, json.dumps(match, sort_keys=True),
                    json.dumps(action, sort_keys=True), now, now,
                ),
            )
    return next(item for item in list_rulesets(workspace) if item["id"] == ruleset_id)


def delete_ruleset(workspace: Workspace, ruleset_id: str) -> None:
    if ruleset_id in {BUILTIN_ARCHIVE_CLEANUP_ID, BUILTIN_KEEP_SELECTED_ID}:
        raise ValueError("built-in rulesets are immutable")
    with workspace.transaction() as connection:
        connection.execute("DELETE FROM file_management_ruleset WHERE id = ?", (ruleset_id,))
        connection.execute("UPDATE workspace_file_management SET active_ruleset_id = NULL, updated_at = ? WHERE id = 1 AND active_ruleset_id = ?", (_timestamp(), ruleset_id))


def save_preset(
    workspace: Workspace,
    *,
    name: str,
    ruleset_id: str,
    preset_id: str | None = None,
) -> dict[str, object]:
    if not name.strip():
        raise ValueError("preset name is required")
    if preset_id in BUILTIN_RULESET_IDS:
        raise ValueError("built-in presets are immutable")
    if not any(item["id"] == ruleset_id for item in list_rulesets(workspace)):
        raise ValueError("ruleset not found")
    now = _timestamp()
    preset_id = preset_id or str(uuid.uuid4())
    with workspace.transaction() as connection:
        connection.execute(
            """
            INSERT INTO file_management_preset(id, name, ruleset_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name = excluded.name, ruleset_id = excluded.ruleset_id,
              updated_at = excluded.updated_at
            """,
            (preset_id, name.strip(), ruleset_id, now, now),
        )
    return next(item for item in list_presets(workspace) if item["id"] == preset_id)


def set_active_ruleset(workspace: Workspace, ruleset_id: str | None) -> None:
    if ruleset_id is not None and not any(item["id"] == ruleset_id for item in list_rulesets(workspace)):
        raise ValueError("ruleset not found")
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE workspace_file_management SET active_ruleset_id = ?, updated_at = ? WHERE id = 1",
            (ruleset_id, _timestamp()),
        )


def build_dry_run_plan(workspace: Workspace, ruleset_id: str | None = None) -> dict[str, object]:
    _ensure_builtins(workspace)
    rulesets = list_rulesets(workspace)
    if ruleset_id is None:
        connection = workspace.connect()
        try:
            row = connection.execute(
                "SELECT active_ruleset_id FROM workspace_file_management WHERE id = 1"
            ).fetchone()
        finally:
            connection.close()
        ruleset_id = row["active_ruleset_id"] if row else None
    ruleset = next((item for item in rulesets if item["id"] == ruleset_id), None)
    if ruleset is None:
        return _empty_plan("No file-management ruleset is active.")
    profiles = {profile["id"]: profile for profile in list_profiles(workspace)}
    connection = workspace.connect()
    try:
        rows = connection.execute(
            """
            SELECT pf.id, pf.logical_asset_id, pf.relative_path, pf.filename, pf.extension,
                       pf.media_type, pf.role, pf.size_bytes, pf.sha256, pf.in_scope, pf.is_online,
                   la.selection_state
            FROM physical_file AS pf
            JOIN logical_asset AS la ON la.id = pf.logical_asset_id
            WHERE pf.in_scope = 1 AND pf.is_online = 1
            ORDER BY pf.relative_path, pf.id
            """
        ).fetchall()
    finally:
        connection.close()
    operations = []
    conflicts = []
    blockers: dict[str, dict[str, object]] = {}
    targets: dict[str, tuple[str, str]] = {}
    for row in rows:
        matching_rules = [candidate for candidate in ruleset["rules"] if candidate["enabled"] and _matches(row, candidate["match"])]
        if not matching_rules:
            continue
        source = row["relative_path"]
        compiled = []
        for rule in matching_rules:
            action = rule["action"]
            operation = str(action.get("operation", "compress"))
            profile_id = action.get("profile_id")
            profile = profiles.get(profile_id)
            target = _target_path(row, action, profile) if operation in {"compress", "copy", "move"} else None
            item_conflicts = []
            item_blockers = []
            if operation == "compress":
                if profile is None:
                    item_blockers.append("compression profile is missing")
                elif profile["codec"] == "av1":
                    capability = av1_capability()
                    if not capability["available"]:
                        item_blockers.append(capability["message"])
                    else:
                        item_blockers.append("AV1 Archival profile is pending; production encoding is not enabled.")
                elif profile["codec"] == "avif":
                    item_blockers.append("AVIF encoding is not available in the planning runtime.")
            if operation not in {"compress", "copy", "move", "delete"}:
                item_conflicts.append(f"unsupported planned operation: {operation}")
            if operation in {"compress", "copy", "move"} and target is None:
                item_conflicts.append("target path is missing")
            compiled.append({"rule": rule, "action": action, "operation": operation, "profile": profile, "profile_id": profile_id, "target": target, "conflicts": item_conflicts, "blockers": item_blockers})

        _validate_action_combination(compiled)
        for item in compiled:
            item_conflicts = list(item["conflicts"])
            item_blockers = list(item["blockers"])
            operation = item["operation"]
            target = item["target"]
            source_replacement = operation == "compress" and item["action"].get("source_disposition") == "replace" and item["action"].get("compress_in_place", True)
            rename_on_conflict = item["action"].get("rename_on_conflict", True) is not False
            renamed_from = None
            replaces_source_in_place = False
            if target is not None:
                target, destination_status, renamed_from, replaces_source_in_place, target_conflict = _resolve_target(
                    workspace,
                    target,
                    source,
                    row["sha256"],
                    operation,
                    source_replacement,
                    rename_on_conflict,
                    targets,
                )
                if target_conflict:
                    item_conflicts.append(target_conflict)
                targets.setdefault(target.casefold(), (row["id"], source))
            else:
                destination_status = None
            for blocker in item_blockers:
                key = f"{item['profile_id']}:{blocker}"
                entry = blockers.setdefault(key, {"profile_id": item["profile_id"], "profile_name": item["profile"]["name"] if item["profile"] else None, "reason": blocker, "affected_count": 0})
                entry["affected_count"] += 1
            operation_row = {
                "physical_file_id": row["id"],
                "logical_asset_id": row["logical_asset_id"],
                "filename": row["filename"],
                "source_relative_path": source,
                "target_relative_path": target,
                "operation": operation,
                "profile_id": item["profile_id"],
                "profile_name": item["profile"]["name"] if item["profile"] else None,
                "rule_id": item["rule"]["id"],
                "bytes": row["size_bytes"] or 0,
                "estimated_output_bytes": _estimated_output_bytes(row["size_bytes"] or 0, item["profile"]),
                "estimated_storage_delta_bytes": _storage_delta(row["size_bytes"] or 0, item["profile"], operation, item["action"]),
                "source_disposition": item["action"].get("source_disposition", "keep"),
                "destination_status": destination_status,
                "renamed_from": renamed_from,
                "renamed_to_avoid_conflict": renamed_from is not None,
                "replaces_source_in_place": replaces_source_in_place,
                "conflicts": item_conflicts,
                "blockers": item_blockers,
                "requires_confirmation": True,
            }
            operations.append(operation_row)
            conflicts.extend({
                "physical_file_id": row["id"],
                "filename": row["filename"],
                "source_relative_path": source,
                "target_relative_path": target,
                "rule_id": item["rule"]["id"],
                "operation": operation,
                "reason": reason,
            } for reason in item_conflicts)
    summary = _plan_summary(workspace, rows, operations, conflicts)
    summary["blocker_count"] = sum(int(item["affected_count"]) for item in blockers.values())
    summary["capability_blockers"] = list(blockers.values())
    return {
        "available": True,
        "ruleset_id": ruleset_id,
        "operations": operations,
        "conflicts": conflicts,
        "blockers": list(blockers.values()),
        "summary": summary,
        "executor": {"available": False, "message": "Execution is unavailable until the safety executor is enabled."},
    }


def _matches(row, match: dict[str, object]) -> bool:
    if match.get("media_type") and row["media_type"] != match["media_type"]:
        return False
    extensions = match.get("extensions") or []
    if extensions and row["extension"].casefold() not in {str(value).casefold() for value in extensions}:
        return False
    if match.get("folder_prefix") and not (
        row["relative_path"] == match["folder_prefix"]
        or row["relative_path"].startswith(f"{str(match['folder_prefix']).rstrip('/')}/")
    ):
        return False
    if match.get("formats") and _format_name(row["extension"]) not in {str(value).casefold() for value in match["formats"]}:
        return False
    if match.get("format") and _format_name(row["extension"]) != str(match["format"]).casefold():
        return False
    if match.get("representation_class") and _representation_class(row) != match["representation_class"]:
        return False
    if match.get("origin") and _representation_origin(row) != match["origin"]:
        return False
    if match.get("role") and row["role"] != match["role"]:
        return False
    if match.get("selection_state") and row["selection_state"] != match["selection_state"]:
        return False
    if match.get("selection_state_not") and row["selection_state"] == match["selection_state_not"]:
        return False
    if match.get("selection_states") and row["selection_state"] not in match["selection_states"]:
        return False
    if match.get("min_size_bytes") is not None and (row["size_bytes"] or 0) < int(match["min_size_bytes"]):
        return False
    if match.get("max_size_bytes") is not None and (row["size_bytes"] or 0) > int(match["max_size_bytes"]):
        return False
    return True


def _target_path(row, action, profile):
    source = PurePosixPath(row["relative_path"])
    container = profile["container"] if profile else ""
    if action.get("destination_dir") is not None:
        destination = str(action.get("destination_dir") or "").strip().strip("/")
        if action.get("operation") == "compress" and action.get("compress_in_place", True):
            destination = "/".join(part for part in (destination, str(source.parent) if str(source.parent) != "." else "") if part)
        elif action.get("operation") in {"copy", "move"} and action.get("preserve_relative_structure"):
            destination = "/".join(part for part in (destination, str(source.parent) if str(source.parent) != "." else "") if part)
        filename = f"{source.stem}.{container}" if action.get("operation") == "compress" else row["filename"]
        return "/".join(part for part in (destination, filename) if part) or None
    template = action.get("target_template")
    if not template:
        if profile is None:
            return None
        template = "{relative_dir}/{stem}.{container}"
    values = {
        "filename": row["filename"],
        "stem": source.stem,
        "ext": source.suffix.removeprefix("."),
        "relative_dir": str(source.parent) if str(source.parent) != "." else "",
        "container": container,
    }
    try:
        target = str(template).format(**values).replace("\\", "/")
    except (KeyError, ValueError):
        return None
    return target.lstrip("/") or None


def _workspace_file_exists(workspace: Workspace, relative_path: str) -> bool:
    try:
        return workspace.absolute_path(relative_path).is_file()
    except WorkspaceError:
        return True


def _workspace_target_status(workspace: Workspace, relative_path: str | None, source_sha256: str | None) -> str | None:
    if not relative_path:
        return None
    try:
        path = _case_insensitive_path(workspace, relative_path)
    except WorkspaceError:
        return "conflict"
    if path is None or not path.is_file():
        return None
    if not source_sha256:
        return "conflict"
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "already_satisfied" if digest.hexdigest() == source_sha256 else "conflict"


def _case_insensitive_path(workspace: Workspace, relative_path: str) -> Path | None:
    current = workspace.root
    for part in PurePosixPath(relative_path).parts:
        if part in {"", "."}:
            continue
        if not current.is_dir():
            return None
        match = next((child for child in current.iterdir() if child.name.casefold() == part.casefold()), None)
        current = match if match is not None else current / part
    return current if current.exists() else None


def _resolve_target(
    workspace: Workspace,
    target: str,
    source: str,
    source_sha256: str | None,
    operation: str,
    source_replacement: bool,
    rename_on_conflict: bool,
    planned_targets: dict[str, tuple[str, str]],
) -> tuple[str, str | None, str | None, bool, str | None]:
    source_key = source.casefold()
    target_key = target.casefold()
    if target_key == source_key:
        if source_replacement:
            return target, "replace_source", None, True, None
        if operation == "move":
            return target, "already_satisfied", None, False, None
        if operation == "copy":
            return target, "already_satisfied", None, False, None
        if rename_on_conflict:
            renamed = _next_target(target, workspace, planned_targets)
            return renamed, "renamed", target, False, None
        return target, "conflict", None, False, "A file already exists at this destination."

    existing = _workspace_target_status(workspace, target, source_sha256)
    planned = target_key in planned_targets
    if existing == "already_satisfied" and not planned:
        return target, existing, None, False, None
    if not planned and existing is None:
        return target, None, None, False, None
    if rename_on_conflict:
        renamed = _next_target(target, workspace, planned_targets)
        return renamed, "renamed", target, False, None
    reason = "Another planned output already uses this destination." if planned else "A file already exists at this destination."
    return target, "conflict", None, False, reason


def _next_target(target: str, workspace: Workspace, planned_targets: dict[str, tuple[str, str]]) -> str:
    path = PurePosixPath(target)
    suffix = path.suffix
    stem = path.name[:-len(suffix)] if suffix else path.name
    number = 1
    while True:
        candidate = str(path.with_name(f"{stem} ({number}){suffix}")).replace("\\", "/")
        key = candidate.casefold()
        if key not in planned_targets and _workspace_target_status(workspace, candidate, None) is None:
            return candidate
        number += 1


def _validate_action_combination(compiled) -> None:
    operations = [item["operation"] for item in compiled]
    if operations.count("move") > 1:
        for item in compiled:
            if item["operation"] == "move":
                item["conflicts"].append("multiple Move actions match the same representation")
    terminal = {"delete", "move"}
    replace = [item for item in compiled if item["operation"] == "compress" and item["action"].get("source_disposition") == "replace"]
    if any(operation in terminal for operation in operations) and (len([operation for operation in operations if operation in terminal]) > 1 or replace):
        for item in compiled:
            if item["operation"] in terminal or item in replace:
                item["conflicts"].append("terminal actions conflict; use one of Delete, Move, or Replace")
    if "delete" in operations and "copy" in operations:
        for item in compiled:
            if item["operation"] in {"delete", "copy"}:
                item["conflicts"].append("A source representation cannot be copied and deleted in the same plan.")


def _plan_summary(workspace, rows, operations, conflicts):
    groups = {
        "delete": {"file_count": 0, "bytes": 0, "source_bytes": 0, "estimated_storage_delta_bytes": 0},
        "copy": {"file_count": 0, "bytes_added": 0, "estimated_storage_delta_bytes": 0},
        "move": {"file_count": 0, "bytes_moved": 0, "estimated_storage_delta_bytes": 0},
        "compress": {"file_count": 0, "source_bytes": 0, "estimated_output_bytes": 0, "estimated_bytes_saved": 0, "estimated_storage_delta_bytes": 0},
    }
    for operation in operations:
        group = groups[operation["operation"]]
        size = int(operation["bytes"] or 0)
        group["file_count"] += 1
        group["estimated_storage_delta_bytes"] += int(operation.get("estimated_storage_delta_bytes") or 0)
        if operation["operation"] == "delete":
            group["bytes"] += size
            group["source_bytes"] += size
        elif operation["operation"] == "copy":
            group["bytes_added"] += size
        elif operation["operation"] == "move":
            group["bytes_moved"] += size
        else:
            group["source_bytes"] += size
            estimated = int(operation.get("estimated_output_bytes") or _estimated_output_bytes(size, None))
            group["estimated_output_bytes"] += estimated
            group["estimated_bytes_saved"] += max(0, size - estimated) if operation.get("source_disposition") == "replace" else 0
    surviving = 0
    connection = workspace.connect()
    try:
        assets = connection.execute("SELECT id FROM logical_asset").fetchall()
        files = connection.execute("SELECT id, logical_asset_id FROM physical_file WHERE in_scope = 1 AND is_online = 1").fetchall()
    finally:
        connection.close()
    for asset in assets:
        members = [row for row in files if row["logical_asset_id"] == asset["id"]]
        if not members:
            continue
        member_operations = {
            row["id"]: [operation for operation in operations if operation["physical_file_id"] == row["id"]]
            for row in members
        }
        if any(
            not any(operation["operation"] == "delete" for operation in member_operations[row["id"]])
            or any(operation["operation"] in {"copy", "move", "compress"} for operation in member_operations[row["id"]])
            for row in members
        ):
            surviving += 1
    storage_delta = sum(int(group["estimated_storage_delta_bytes"]) for group in groups.values())
    return {
        "candidate_count": len(operations),
        "conflict_count": len(conflicts),
        "safe_count": sum(not operation["conflicts"] and not operation.get("blockers") for operation in operations),
        "blocked_count": sum(bool(operation.get("blockers")) for operation in operations),
        "delete": groups["delete"],
        "copy": groups["copy"],
        "move": groups["move"],
        "compress": groups["compress"],
        "estimated_storage_delta_bytes": storage_delta,
        "estimated_net_bytes_freed": max(0, -storage_delta),
        "estimated_net_bytes_added": max(0, storage_delta),
        "peak_temporary_bytes": sum(int(operation.get("estimated_output_bytes") or 0) for operation in operations if operation["operation"] == "compress"),
        "available_space_bytes": _available_space(workspace),
        "assets_with_no_surviving_representation": max(0, len(assets) - surviving),
    }


def _estimated_output_bytes(size: int, profile: dict[str, object] | None) -> int:
    if not size:
        return 0
    settings = (profile or {}).get("settings") or {}
    if "estimated_output_ratio" in settings:
        ratio = max(0.01, min(1.0, float(settings["estimated_output_ratio"])))
    elif profile and profile.get("codec") == "jpeg-xl":
        quality = float(settings.get("quality", distance_to_quality(settings.get("distance", 1.5))))
        ratio = {80: 0.5, 60: 0.35, 40: 0.25}.get(round(quality), 0.35)
    else:
        ratio = 0.5
    return max(1, int(size * ratio))


def _storage_delta(size: int, profile: dict[str, object] | None, operation: str, action: dict[str, object]) -> int:
    if operation == "delete":
        return -size
    if operation == "copy":
        return size
    if operation == "move":
        return 0
    output = _estimated_output_bytes(size, profile)
    return output - size if action.get("source_disposition", "keep") == "replace" else output


def _available_space(workspace):
    try:
        return shutil.disk_usage(workspace.root).free
    except OSError:
        return None


def _format_name(extension: str) -> str:
    return {".jpg": "jpeg", ".jpeg": "jpeg"}.get(extension.casefold(), extension.casefold().removeprefix("."))


def _representation_class(row) -> str:
    if row["media_type"] == "video":
        return "video"
    extension = row["extension"].casefold()
    if extension in {".arw", ".cr2", ".cr3", ".dng", ".nef", ".raf", ".rw2"}:
        return "raw"
    if extension in {".jpg", ".jpeg", ".png"}:
        return "conventional-image"
    if extension in {".jxl", ".avif", ".webp"}:
        return "compressed-image"
    return "rendered-image"


def _representation_origin(row) -> str:
    return "managed" if str(row["role"] or "").casefold().startswith(("managed", "derived")) else "external"


def _profile(row):
    profile = {**dict(row), "settings": _json(row["settings_json"])}
    if profile["codec"] == "av1":
        profile["capability"] = av1_capability()
    return profile


def _ruleset(row):
    return dict(row)


def _rule(row):
    return {
        "id": row["id"],
        "position": row["position"],
        "enabled": bool(row["enabled"]),
        "match": _json(row["match_json"]) or {},
        "action": _json(row["action_json"]) or {},
    }


def _empty_plan(reason: str):
    return {
        "available": True,
        "ruleset_id": None,
        "operations": [],
        "conflicts": [],
        "blockers": [],
        "summary": {
            "candidate_count": 0,
            "conflict_count": 0,
            "safe_count": 0,
            "blocked_count": 0,
            "capability_blockers": [],
            "delete": {"file_count": 0, "bytes": 0, "source_bytes": 0, "estimated_storage_delta_bytes": 0},
            "copy": {"file_count": 0, "bytes_added": 0, "estimated_storage_delta_bytes": 0},
            "move": {"file_count": 0, "bytes_moved": 0, "estimated_storage_delta_bytes": 0},
            "compress": {"file_count": 0, "source_bytes": 0, "estimated_output_bytes": 0, "estimated_bytes_saved": 0, "estimated_storage_delta_bytes": 0},
            "estimated_storage_delta_bytes": 0,
            "estimated_net_bytes_freed": 0,
            "estimated_net_bytes_added": 0,
            "peak_temporary_bytes": 0,
            "available_space_bytes": None,
            "assets_with_no_surviving_representation": 0,
        },
        "empty_reason": reason,
        "executor": {"available": False, "message": "Execution is unavailable until the safety executor is enabled."},
    }


def _ensure_builtins(workspace: Workspace) -> None:
    now = _timestamp()
    profiles = [
        (BUILTIN_HIGH_QUALITY_PROFILE_ID, "JXL High Quality", {"quality": 80, "distance": quality_to_distance(80), "effort": 7}),
        (BUILTIN_BALANCED_PROFILE_ID, "JXL Balanced", {"quality": 60, "distance": quality_to_distance(60), "effort": 7}),
        (BUILTIN_HIGH_COMPRESSION_PROFILE_ID, "JXL High Compression", {"quality": 40, "distance": quality_to_distance(40), "effort": 7}),
        (BUILTIN_AV1_PROFILE_ID, "AV1 Archival (pending)", {"status": "pending"}),
    ]
    cleanup_rules = [
        {"enabled": True, "match": {"selection_state": "undecided", "representation_class": "raw"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "rejected", "representation_class": "raw"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "selected", "representation_class": "raw"}, "action": {"operation": "copy", "destination_dir": "raws", "preserve_relative_structure": True, "rename_on_conflict": True}},
        {"enabled": True, "match": {"selection_state": "selected", "formats": ["jpeg", "png"]}, "action": {"operation": "copy", "destination_dir": "jpgs", "preserve_relative_structure": True, "rename_on_conflict": True}},
        {"enabled": True, "match": {"selection_state": "undecided", "formats": ["jpeg", "png"]}, "action": {"operation": "compress", "profile_id": BUILTIN_BALANCED_PROFILE_ID, "source_disposition": "replace", "compress_in_place": True, "rename_on_conflict": True}},
        {"enabled": True, "match": {"selection_state": "rejected", "formats": ["jpeg", "png"]}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "rejected", "representation_class": "video"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"representation_class": "video", "selection_state_not": "rejected"}, "action": {"operation": "compress", "profile_id": BUILTIN_AV1_PROFILE_ID, "source_disposition": "replace", "compress_in_place": True, "rename_on_conflict": True}},
    ]
    selected_rules = [
        {"enabled": True, "match": {"selection_state": "rejected"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "undecided"}, "action": {"operation": "delete"}},
    ]
    with workspace.transaction() as connection:
        for profile_id, name, settings in profiles:
            connection.execute(
                "INSERT INTO compression_profile(id, name, codec, container, settings_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, codec=excluded.codec, container=excluded.container, settings_json=excluded.settings_json, updated_at=excluded.updated_at",
                (profile_id, name, "jpeg-xl" if profile_id != BUILTIN_AV1_PROFILE_ID else "av1", "jxl" if profile_id != BUILTIN_AV1_PROFILE_ID else "mp4", json.dumps(settings, sort_keys=True), now, now),
            )
        for ruleset_id, name, description, rules in (
            (BUILTIN_ARCHIVE_CLEANUP_ID, "Archive cleanup", "Conservative archive cleanup planning", cleanup_rules),
            (BUILTIN_KEEP_SELECTED_ID, "Keep selected only", "Keep selected representations and plan removal of the rest", selected_rules),
        ):
            connection.execute(
                "INSERT OR IGNORE INTO file_management_ruleset(id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (ruleset_id, name, description, now, now),
            )
            connection.execute("DELETE FROM file_management_rule WHERE ruleset_id = ?", (ruleset_id,))
            for position, rule in enumerate(rules):
                connection.execute(
                    "INSERT INTO file_management_rule(id, ruleset_id, position, enabled, match_json, action_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"{ruleset_id}-{position + 1}", ruleset_id, position, 1, json.dumps(rule["match"], sort_keys=True), json.dumps(rule["action"], sort_keys=True), now, now),
                )
            connection.execute(
                "INSERT OR IGNORE INTO file_management_preset(id, name, ruleset_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (ruleset_id, name, ruleset_id, now, now),
            )


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
