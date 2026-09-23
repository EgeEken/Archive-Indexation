"""Persisted compression profiles and deterministic non-destructive plans."""

from __future__ import annotations

import json
import hashlib
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .workspace import Workspace, WorkspaceError

BUILTIN_BALANCED_PROFILE_ID = "builtin-jxl-balanced"
BUILTIN_ARCHIVE_CLEANUP_ID = "builtin-archive-cleanup"
BUILTIN_KEEP_SELECTED_ID = "builtin-keep-selected-only"
BUILTIN_AV1_PROFILE_ID = "builtin-av1-archival"


def list_profiles(workspace: Workspace) -> list[dict[str, object]]:
    _ensure_builtins(workspace)
    connection = workspace.connect()
    try:
        rows = connection.execute(
            "SELECT * FROM compression_profile ORDER BY name, id"
        ).fetchall()
    finally:
        connection.close()
    return [{**_profile(row), "is_builtin": row["id"] == BUILTIN_BALANCED_PROFILE_ID} for row in rows]


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
    if profile_id == BUILTIN_BALANCED_PROFILE_ID:
        raise ValueError("built-in compression profiles are immutable")
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
            (profile_id, name.strip(), codec, container.strip(), json.dumps(settings or {}, sort_keys=True), now, now),
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
            "SELECT * FROM file_management_rule ORDER BY ruleset_id, position, id"
        ).fetchall()
    finally:
        connection.close()
    by_ruleset: dict[str, list[dict[str, object]]] = {}
    for row in rules:
        by_ruleset.setdefault(row["ruleset_id"], []).append(_rule(row))
    return [{**_ruleset(row), "rules": by_ruleset.get(row["id"], []), "is_builtin": row["id"] in {BUILTIN_ARCHIVE_CLEANUP_ID, BUILTIN_KEEP_SELECTED_ID}} for row in rulesets]


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
    return [{**dict(row), "is_builtin": row["id"] in {BUILTIN_ARCHIVE_CLEANUP_ID, BUILTIN_KEEP_SELECTED_ID}} for row in rows]


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
    if ruleset_id in {BUILTIN_ARCHIVE_CLEANUP_ID, BUILTIN_KEEP_SELECTED_ID}:
        raise ValueError("built-in rulesets are immutable")
    now = _timestamp()
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
        for position, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise ValueError("each rule must be an object")
            match = rule.get("match") or {}
            action = rule.get("action") or {}
            if not isinstance(match, dict) or not isinstance(action, dict):
                raise ValueError("rule match and action must be objects")
            connection.execute(
                """
                INSERT INTO file_management_rule(
                    id, ruleset_id, position, enabled, match_json, action_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(rule.get("id") or uuid.uuid4()), ruleset_id, position,
                    int(bool(rule.get("enabled", True))), json.dumps(match, sort_keys=True),
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
    if preset_id in {BUILTIN_ARCHIVE_CLEANUP_ID, BUILTIN_KEEP_SELECTED_ID}:
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
            if operation == "compress":
                if profile is None:
                    item_conflicts.append("compression profile is missing")
                elif profile["codec"] in {"avif", "av1"}:
                    item_conflicts.append(f"{profile['codec'].upper()} compression encoder is not installed")
            if operation not in {"compress", "copy", "move", "delete"}:
                item_conflicts.append(f"unsupported planned operation: {operation}")
            if operation in {"compress", "copy", "move"} and target is None:
                item_conflicts.append("target path is missing")
            compiled.append({"rule": rule, "action": action, "operation": operation, "profile": profile, "profile_id": profile_id, "target": target, "conflicts": item_conflicts})

        _validate_action_combination(compiled)
        for item in compiled:
            item_conflicts = list(item["conflicts"])
            operation = item["operation"]
            target = item["target"]
            if target is not None:
                key = target.casefold()
                if key == source.casefold():
                    item_conflicts.append("target would overwrite the source")
                elif key in targets and targets[key][0] != row["id"]:
                    item_conflicts.append(f"target collides with {targets[key][1]}")
                else:
                    existing = _workspace_target_status(workspace, target, row["sha256"])
                    if existing == "conflict":
                        item_conflicts.append("destination exists with different content")
                if key not in targets:
                    targets[key] = (row["id"], source)
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
                "source_disposition": item["action"].get("source_disposition", "keep"),
                "destination_status": _workspace_target_status(workspace, target, row["sha256"]) if target else None,
                "conflicts": item_conflicts,
                "requires_confirmation": True,
            }
            operations.append(operation_row)
            conflicts.extend({"physical_file_id": row["id"], "reason": reason} for reason in item_conflicts)
    summary = _plan_summary(workspace, rows, operations, conflicts)
    return {
        "available": True,
        "ruleset_id": ruleset_id,
        "operations": operations,
        "conflicts": conflicts,
        "summary": summary,
        "executor": {"available": False, "message": "Phase 10A is planning-only; no files will be changed."},
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
    template = action.get("target_template")
    if not template:
        if profile is None:
            return None
        template = "{relative_dir}/{stem}.{container}"
    source = PurePosixPath(row["relative_path"])
    values = {
        "filename": row["filename"],
        "stem": source.stem,
        "ext": source.suffix.removeprefix("."),
        "relative_dir": str(source.parent) if str(source.parent) != "." else "",
        "container": profile["container"] if profile else "",
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
        path = workspace.absolute_path(relative_path)
    except WorkspaceError:
        return "conflict"
    if not path.is_file():
        return None
    if not source_sha256:
        return "conflict"
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return "already_satisfied" if digest.hexdigest() == source_sha256 else "conflict"


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
                item["conflicts"].append("a source representation cannot be copied and deleted in the same plan")


def _plan_summary(workspace, rows, operations, conflicts):
    groups = {
        "delete": {"file_count": 0, "bytes": 0},
        "copy": {"file_count": 0, "bytes_added": 0},
        "move": {"file_count": 0, "bytes_moved": 0},
        "compress": {"file_count": 0, "source_bytes": 0, "estimated_output_bytes": 0, "estimated_bytes_saved": 0},
    }
    for operation in operations:
        group = groups[operation["operation"]]
        size = int(operation["bytes"] or 0)
        group["file_count"] += 1
        if operation["operation"] == "delete":
            group["bytes"] += size
        elif operation["operation"] == "copy":
            group["bytes_added"] += size
        elif operation["operation"] == "move":
            group["bytes_moved"] += size
        else:
            group["source_bytes"] += size
            estimated = max(1, int(size * 0.35)) if operation["profile_id"] and str(operation["profile_id"]).startswith("builtin-jxl") else max(1, int(size * 0.5))
            group["estimated_output_bytes"] += estimated
            group["estimated_bytes_saved"] += max(0, size - estimated)
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
    net_freed = groups["delete"]["bytes"] + groups["compress"]["estimated_bytes_saved"] - groups["copy"]["bytes_added"]
    return {
        "candidate_count": len(operations),
        "conflict_count": len(conflicts),
        "safe_count": sum(not operation["conflicts"] for operation in operations),
        "delete": groups["delete"],
        "copy": groups["copy"],
        "move": groups["move"],
        "compress": groups["compress"],
        "estimated_net_bytes_freed": net_freed,
        "peak_temporary_bytes": max((operation["bytes"] for operation in operations if operation["operation"] == "compress"), default=0),
        "available_space_bytes": _available_space(workspace),
        "assets_with_no_surviving_representation": max(0, len(assets) - surviving),
    }


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
    return "raw" if row["extension"].casefold() in {".arw", ".cr2", ".cr3", ".dng", ".nef", ".raf", ".rw2"} else "rendered-image"


def _representation_origin(row) -> str:
    return "managed" if str(row["role"] or "").casefold().startswith(("managed", "derived")) else "external"


def _profile(row):
    return {**dict(row), "settings": _json(row["settings_json"])}


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
        "summary": {
            "candidate_count": 0,
            "conflict_count": 0,
            "safe_count": 0,
            "delete": {"file_count": 0, "bytes": 0},
            "copy": {"file_count": 0, "bytes_added": 0},
            "move": {"file_count": 0, "bytes_moved": 0},
            "compress": {"file_count": 0, "source_bytes": 0, "estimated_output_bytes": 0, "estimated_bytes_saved": 0},
            "estimated_net_bytes_freed": 0,
            "peak_temporary_bytes": 0,
            "available_space_bytes": None,
            "assets_with_no_surviving_representation": 0,
        },
        "empty_reason": reason,
        "executor": {"available": False, "message": "Phase 10A is planning-only; no files will be changed."},
    }


def _ensure_builtins(workspace: Workspace) -> None:
    now = _timestamp()
    profile_settings = {"distance": 1.5, "effort": 7}
    cleanup_rules = [
        {"enabled": True, "match": {"selection_state": "undecided", "representation_class": "raw"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "rejected", "representation_class": "raw"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "selected", "representation_class": "raw"}, "action": {"operation": "copy", "target_template": "raws/{filename}"}},
        {"enabled": True, "match": {"selection_state": "selected", "formats": ["jpeg", "png"]}, "action": {"operation": "copy", "target_template": "jpgs/{filename}"}},
        {"enabled": True, "match": {"selection_state": "undecided", "representation_class": "rendered-image"}, "action": {"operation": "compress", "profile_id": BUILTIN_BALANCED_PROFILE_ID, "source_disposition": "replace"}},
        {"enabled": True, "match": {"selection_state": "rejected", "representation_class": "rendered-image"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "rejected", "representation_class": "video"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"representation_class": "video", "selection_state_not": "rejected"}, "action": {"operation": "compress", "profile_id": BUILTIN_AV1_PROFILE_ID, "source_disposition": "replace"}},
    ]
    selected_rules = [
        {"enabled": True, "match": {"selection_state": "rejected"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "undecided"}, "action": {"operation": "delete"}},
    ]
    with workspace.transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO compression_profile(id, name, codec, container, settings_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (BUILTIN_BALANCED_PROFILE_ID, "JXL Balanced", "jpeg-xl", "jxl", json.dumps(profile_settings, sort_keys=True), now, now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO compression_profile(id, name, codec, container, settings_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (BUILTIN_AV1_PROFILE_ID, "AV1 Archival (pending)", "av1", "mp4", json.dumps({"status": "pending"}, sort_keys=True), now, now),
        )
        for ruleset_id, name, description, rules in (
            (BUILTIN_ARCHIVE_CLEANUP_ID, "Archive cleanup", "Conservative archive cleanup planning", cleanup_rules),
            (BUILTIN_KEEP_SELECTED_ID, "Keep selected only", "Keep selected representations and plan removal of the rest", selected_rules),
        ):
            connection.execute(
                "INSERT OR IGNORE INTO file_management_ruleset(id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (ruleset_id, name, description, now, now),
            )
            if connection.execute("SELECT COUNT(*) FROM file_management_rule WHERE ruleset_id = ?", (ruleset_id,)).fetchone()[0] == 0:
                for position, rule in enumerate(rules):
                    connection.execute(
                        "INSERT INTO file_management_rule(id, ruleset_id, position, enabled, match_json, action_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (f"{ruleset_id}-{position + 1}", ruleset_id, position, int(rule["enabled"]), json.dumps(rule["match"], sort_keys=True), json.dumps(rule["action"], sort_keys=True), now, now),
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
