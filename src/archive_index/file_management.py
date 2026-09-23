"""Persisted compression profiles and deterministic non-destructive plans."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath

from .workspace import Workspace, WorkspaceError


def list_profiles(workspace: Workspace) -> list[dict[str, object]]:
    connection = workspace.connect()
    try:
        rows = connection.execute(
            "SELECT * FROM compression_profile ORDER BY name, id"
        ).fetchall()
    finally:
        connection.close()
    return [_profile(row) for row in rows]


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
    if codec not in {"jpeg-xl", "avif"}:
        raise ValueError("codec must be jpeg-xl or avif")
    if not container.strip():
        raise ValueError("container is required")
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
    return [{**_ruleset(row), "rules": by_ruleset.get(row["id"], [])} for row in rulesets]


def list_presets(workspace: Workspace) -> list[dict[str, object]]:
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
    return [dict(row) for row in rows]


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


def save_preset(
    workspace: Workspace,
    *,
    name: str,
    ruleset_id: str,
    preset_id: str | None = None,
) -> dict[str, object]:
    if not name.strip():
        raise ValueError("preset name is required")
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
                   pf.media_type, pf.role, pf.size_bytes, pf.in_scope, pf.is_online,
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
    targets: dict[str, str] = {}
    for row in rows:
        rule = next((candidate for candidate in ruleset["rules"] if candidate["enabled"] and _matches(row, candidate["match"])), None)
        if rule is None:
            continue
        action = rule["action"]
        operation = str(action.get("operation", "compress"))
        profile_id = action.get("profile_id")
        profile = profiles.get(profile_id)
        source = row["relative_path"]
        target = _target_path(row, action, profile)
        item_conflicts = []
        if profile is None:
            item_conflicts.append("compression profile is missing")
        elif profile["codec"] == "avif":
            item_conflicts.append("AVIF compression executor is not implemented")
        if operation not in {"compress", "copy", "move"}:
            item_conflicts.append(f"unsupported planned operation: {operation}")
        if target is None:
            item_conflicts.append("target path is missing")
        elif target.casefold() == source.casefold():
            item_conflicts.append("target would overwrite the source")
        elif target.casefold() in targets:
            item_conflicts.append(f"target collides with {targets[target.casefold()]}")
        elif _workspace_file_exists(workspace, target):
            item_conflicts.append("target already exists")
        if target:
            targets[target.casefold()] = source
        operation_row = {
            "physical_file_id": row["id"],
            "logical_asset_id": row["logical_asset_id"],
            "source_relative_path": source,
            "target_relative_path": target,
            "operation": operation,
            "profile_id": profile_id,
            "conflicts": item_conflicts,
            "requires_confirmation": True,
        }
        operations.append(operation_row)
        conflicts.extend({"physical_file_id": row["id"], "reason": reason} for reason in item_conflicts)
    return {
        "available": True,
        "ruleset_id": ruleset_id,
        "operations": operations,
        "conflicts": conflicts,
        "summary": {
            "candidate_count": len(operations),
            "conflict_count": len(conflicts),
            "safe_count": sum(not operation["conflicts"] for operation in operations),
        },
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
    if match.get("role") and row["role"] != match["role"]:
        return False
    if match.get("selection_state") and row["selection_state"] != match["selection_state"]:
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
        return workspace.absolute_path(relative_path).exists()
    except WorkspaceError:
        return True


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
        "summary": {"candidate_count": 0, "conflict_count": 0, "safe_count": 0},
        "empty_reason": reason,
        "executor": {"available": False, "message": "Phase 10A is planning-only; no files will be changed."},
    }


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
