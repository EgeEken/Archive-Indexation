"""Persisted compression profiles and deterministic non-destructive plans."""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .workspace import Workspace, WorkspaceError, _is_reparse_point
from .media.capabilities import av1_capability
from .media.jpegxl import (
    JXL_SOURCE_REPLACEMENT_BLOCKER,
    JXL_SUPPORTED_EXTENSIONS,
    distance_to_quality,
    production_capability,
    profile_settings,
    quality_to_distance,
    source_blocker,
)

BUILTIN_HIGH_QUALITY_PROFILE_ID = "builtin-jxl-high-quality"
BUILTIN_BALANCED_PROFILE_ID = "builtin-jxl-balanced"
BUILTIN_HIGH_COMPRESSION_PROFILE_ID = "builtin-jxl-high-compression"
BUILTIN_ARCHIVE_CLEANUP_ID = "builtin-archive-cleanup"
BUILTIN_KEEP_SELECTED_ID = "builtin-keep-selected-only"
BUILTIN_AV1_PROFILE_ID = "builtin-av1-4k120-very-fast"
LEGACY_BUILTIN_AV1_PROFILE_ID = "builtin-av1-archival"
BUILTIN_AV1_PROFILE_IDS = {
    "builtin-av1-1080p60-fast",
    "builtin-av1-1080p60-very-fast",
    "builtin-av1-4k120-fast",
    BUILTIN_AV1_PROFILE_ID,
    LEGACY_BUILTIN_AV1_PROFILE_ID,
}
BUILTIN_PROFILE_IDS = {
    BUILTIN_HIGH_QUALITY_PROFILE_ID,
    BUILTIN_BALANCED_PROFILE_ID,
    BUILTIN_HIGH_COMPRESSION_PROFILE_ID,
    *BUILTIN_AV1_PROFILE_IDS,
}
BUILTIN_RULESET_IDS = {BUILTIN_ARCHIVE_CLEANUP_ID, BUILTIN_KEEP_SELECTED_ID}
AV1_EXECUTION_BLOCKER = "AV1 execution remains blocked until benchmark, stream, color, and recovery validation is complete."
AVIF_EXECUTION_BLOCKER = "AVIF archival execution is not enabled; AVIF remains preview-only."
CONFLICT_POLICIES = {"rename", "skip", "overwrite"}
_plan_runs = {}
_plan_runs_lock = threading.Lock()


def list_profiles(workspace: Workspace, *, seed: bool = True) -> list[dict[str, object]]:
    if seed:
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
    _ensure_no_active_execution(workspace)
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


def list_rulesets(workspace: Workspace, *, seed: bool = True) -> list[dict[str, object]]:
    if seed:
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
    _ensure_no_active_execution(workspace)
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
                    json.dumps(_normalize_action(action), sort_keys=True), now, now,
                ),
            )
    return next(item for item in list_rulesets(workspace) if item["id"] == ruleset_id)


def delete_ruleset(workspace: Workspace, ruleset_id: str) -> None:
    _ensure_no_active_execution(workspace)
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
    _ensure_no_active_execution(workspace)
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
    _ensure_no_active_execution(workspace)
    if ruleset_id is not None and not any(item["id"] == ruleset_id for item in list_rulesets(workspace)):
        raise ValueError("ruleset not found")
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE workspace_file_management SET active_ruleset_id = ?, updated_at = ? WHERE id = 1",
            (ruleset_id, _timestamp()),
        )


def build_dry_run_plan(workspace: Workspace, ruleset_id: str | None = None, *, progress=None, cancelled=None) -> dict[str, object]:
    def report(phase, completed=0, total=0):
        if progress:
            progress(phase, completed, total)
        if cancelled and cancelled.is_set():
            raise InterruptedError("Plan analysis was cancelled.")

    report("Loading indexed files")
    _ensure_builtins(workspace)
    rulesets = list_rulesets(workspace, seed=False)
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
    profiles = {profile["id"]: profile for profile in list_profiles(workspace, seed=False)}
    connection = workspace.connect()
    try:
        rows = connection.execute(
            """
            SELECT pf.id, pf.logical_asset_id, pf.relative_path, pf.filename, pf.extension,
                       pf.media_type, pf.role, pf.size_bytes, pf.mtime_ns, pf.sha256, pf.in_scope, pf.is_online,
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
    targets: dict[str, tuple[str, str, str | None]] = {}
    directory_entries: dict[Path, dict[str, Path]] = {}
    target_statuses: dict[tuple[str, str | None], str | None] = {}
    target_hashes: dict[str, str] = {}
    total_rows = len(rows)
    for row_index, row in enumerate(rows):
        report("Matching rules", row_index, total_rows)
        matching_rules = [candidate for candidate in ruleset["rules"] if candidate["enabled"] and _matches(row, candidate["match"])]
        if not matching_rules:
            report("Resolving targets", row_index + 1, total_rows)
            continue
        source = row["relative_path"]
        compiled = []
        for rule in matching_rules:
            action = rule["action"]
            operation = str(action.get("operation", "compress"))
            profile_id = action.get("profile_id")
            profile = profiles.get(profile_id)
            target_validation_error = None
            if operation in {"compress", "copy", "move"}:
                try:
                    target = _target_path(workspace, row, action, profile)
                except WorkspaceError as error:
                    target = None
                    target_validation_error = str(error)
            else:
                target = None
            item_conflicts = []
            item_blockers = []
            if target_validation_error:
                item_conflicts.append(target_validation_error)
            if operation == "compress":
                if profile is None:
                    item_blockers.append("compression profile is missing; supported Phase 10C compression is required")
                elif profile.get("codec") == "jpeg-xl":
                    capability = production_capability()
                    if not capability.get("production_encoder_available"):
                        item_blockers.append(str(capability.get("message") or "JPEG XL encoder is unavailable."))
                    if row["extension"].casefold() not in JXL_SUPPORTED_EXTENSIONS:
                        item_blockers.append("This source format is not supported by production JPEG XL compression.")
                    else:
                        source_issue = source_blocker(workspace.absolute_path(source))
                        if source_issue:
                            item_blockers.append(source_issue)
                    if action.get("source_disposition", "keep") == "replace" and not capability.get("source_replacement_available"):
                        item_blockers.append(JXL_SOURCE_REPLACEMENT_BLOCKER)
                elif profile.get("codec") == "av1":
                    item_blockers.append(AV1_EXECUTION_BLOCKER)
                elif profile.get("codec") == "avif":
                    item_blockers.append(AVIF_EXECUTION_BLOCKER)
                else:
                    item_blockers.append("This compression codec is not supported for production execution.")
            if operation not in {"compress", "copy", "move", "delete"}:
                item_conflicts.append(f"unsupported planned operation: {operation}")
            if operation in {"compress", "copy", "move"} and target is None and target_validation_error is None:
                item_conflicts.append("target path is missing")
            compiled.append({"rule": rule, "action": action, "operation": operation, "profile": profile, "profile_id": profile_id, "target": target, "conflicts": item_conflicts, "blockers": item_blockers})

        _validate_action_combination(compiled)
        report("Checking destination conflicts", row_index, total_rows)
        for item in compiled:
            item_conflicts = list(item["conflicts"])
            item_blockers = list(item["blockers"])
            operation = item["operation"]
            target = item["target"]
            source_replacement = operation == "compress" and item["action"].get("source_disposition") == "replace" and item["action"].get("compress_in_place", True)
            conflict_policy = _conflict_policy(item["action"])
            renamed_from = None
            replaces_source_in_place = False
            target_snapshot = None
            if target is not None:
                target, destination_status, renamed_from, replaces_source_in_place, target_conflict, coalesced, target_snapshot = _resolve_target(
                    workspace,
                    target,
                    source,
                    row["sha256"],
                    operation,
                    source_replacement,
                    conflict_policy,
                    targets,
                    directory_entries,
                    target_statuses,
                    target_hashes,
                )
                if target_conflict:
                    item_conflicts.append(target_conflict)
                targets.setdefault(target.casefold(), (row["id"], source, row["sha256"]))
            else:
                destination_status = None
                coalesced = False
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
                "source_size_bytes": row["size_bytes"] or 0,
                "source_mtime_ns": row["mtime_ns"],
                "source_sha256": row["sha256"],
                "estimated_output_bytes": _estimated_output_bytes(row["size_bytes"] or 0, item["profile"]),
                "estimated_storage_delta_bytes": _storage_delta(row["size_bytes"] or 0, item["profile"], operation, item["action"]),
                "source_disposition": item["action"].get("source_disposition", "keep"),
                "destination_status": destination_status,
                "conflict_policy": conflict_policy,
                "target_snapshot": target_snapshot,
                "renamed_from": renamed_from,
                "renamed_to_avoid_conflict": renamed_from is not None,
                "replaces_source_in_place": replaces_source_in_place,
                "coalesced_by_planned_target": coalesced,
                "conflicts": item_conflicts,
                "blockers": item_blockers,
                "rule_snapshot": item["rule"],
                "profile_snapshot": _profile_snapshot(item["profile"]),
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
        report("Resolving targets", row_index + 1, total_rows)
    report("Summarizing plan", total_rows, total_rows)
    summary = _plan_summary(workspace, rows, operations, conflicts)
    summary["blocker_count"] = sum(int(item["affected_count"]) for item in blockers.values())
    summary["capability_blockers"] = list(blockers.values())
    return {
        "available": True,
        "ruleset_id": ruleset_id,
        "plan_digest": _plan_digest(operations, ruleset),
        "operations": operations,
        "conflicts": conflicts,
        "blockers": list(blockers.values()),
        "summary": summary,
        "executor": {"available": True, "supported_operations": ["copy", "compress", "move", "delete"], "message": "Phase 10C JPEG XL keep-source execution is available. Source replacement, AV1, and AVIF remain blocked."},
    }


def start_plan_analysis(workspace: Workspace, ruleset_id: str | None = None) -> str:
    session_id = str(uuid.uuid4())
    cancellation = threading.Event()
    session = {
        "workspace": str(workspace.root),
        "status": "running",
        "phase": "Loading indexed files",
        "completed": 0,
        "total": 0,
        "started": time.monotonic(),
        "cancellation": cancellation,
        "result": None,
        "error": None,
    }
    with _plan_runs_lock:
        for existing in _plan_runs.values():
            if existing["workspace"] == session["workspace"] and existing["status"] == "running":
                existing["cancellation"].set()
        _plan_runs[session_id] = session
        finished = [key for key, value in _plan_runs.items() if value["status"] != "running"]
        while len(_plan_runs) > 8 and finished:
            del _plan_runs[finished.pop(0)]

    def run():
        def update(phase, completed, total):
            session.update(phase=phase, completed=completed, total=total)
        try:
            result = build_dry_run_plan(workspace, ruleset_id, progress=update, cancelled=cancellation)
            session.update(status="complete", result=result)
        except InterruptedError:
            session.update(status="cancelled")
        except Exception as error:
            session.update(status="failed", error=str(error))
        finally:
            session["elapsed_seconds"] = time.monotonic() - session["started"]

    threading.Thread(target=run, daemon=True, name="file-plan-analysis").start()
    return session_id


def plan_analysis_status(workspace: Workspace, session_id: str) -> dict[str, object]:
    with _plan_runs_lock:
        session = _plan_runs.get(session_id)
        if session is None or session["workspace"] != str(workspace.root):
            raise ValueError("Plan analysis session was not found.")
        return {key: value for key, value in session.items() if key not in {"workspace", "started", "cancellation"}} | {
            "elapsed_seconds": session.get("elapsed_seconds", time.monotonic() - session["started"]),
        }


def cancel_plan_analysis(workspace: Workspace, session_id: str) -> bool:
    with _plan_runs_lock:
        session = _plan_runs.get(session_id)
        if session is None or session["workspace"] != str(workspace.root) or session["status"] != "running":
            return False
        session["cancellation"].set()
        return True


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


def _target_path(workspace: Workspace, row, action, profile):
    source = PurePosixPath(row["relative_path"])
    container = profile["container"] if profile else ""
    if action.get("destination_dir") is not None:
        destination = str(action.get("destination_dir") or "").replace("\\", "/")
        if destination.endswith("/"):
            destination = destination.rstrip("/") or "/"
        if action.get("operation") == "compress" and action.get("compress_in_place", True):
            destination = "/".join(part for part in (destination, str(source.parent) if str(source.parent) != "." else "") if part)
        elif action.get("operation") in {"copy", "move"} and action.get("preserve_relative_structure"):
            destination = "/".join(part for part in (destination, str(source.parent) if str(source.parent) != "." else "") if part)
        filename = f"{source.stem}.{container}" if action.get("operation") == "compress" else row["filename"]
        target = "/".join(part for part in (destination, filename) if part) or None
        return _normalize_target_path(workspace, target)
    template = action.get("target_template")
    if not template:
        if profile is None:
            return None
        relative_dir = str(source.parent) if str(source.parent) != "." else ""
        template = f"{relative_dir + '/' if relative_dir else ''}{{stem}}.{{container}}"
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
    return _normalize_target_path(workspace, target)


def _normalize_target_path(workspace: Workspace, target: str | None) -> str | None:
    if target is None:
        return None
    raw = str(target)
    if not raw or raw.strip() != raw or "\x00" in raw:
        raise WorkspaceError("Destination is malformed or empty.")
    normalized = raw.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise WorkspaceError("Destination must be workspace-relative; absolute, drive, UNC, and root-relative paths are not allowed.")
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        raise WorkspaceError("Destination escapes the workspace.")
    if any(part in {"", "."} for part in parts):
        raise WorkspaceError("Destination is malformed.")
    for part in parts:
        _validate_windows_segment(part)
    if any(part.casefold() == ".archive-index" for part in parts):
        raise WorkspaceError("Destination may not use the application state directory.")
    normalized = "/".join(parts)
    candidate = (workspace.root / Path(normalized)).resolve(strict=False)
    workspace_root = workspace.root.resolve(strict=False)
    try:
        candidate.relative_to(workspace_root)
    except ValueError as error:
        raise WorkspaceError("Destination escapes the workspace.") from error
    try:
        candidate.relative_to(workspace.index_directory.resolve(strict=False))
    except ValueError:
        return normalized
    raise WorkspaceError("Destination may not use the application state directory.")


def _validate_windows_segment(segment: str) -> None:
    if any(ord(character) < 32 or character in '<>:"|?*' for character in segment):
        raise WorkspaceError("Destination contains characters that are invalid on Windows.")
    if segment.endswith((" ", ".")):
        raise WorkspaceError("Destination segments may not end with a space or period.")
    device = segment.split(".", 1)[0].casefold()
    if device in {"con", "prn", "aux", "nul"} or re.fullmatch(r"(?:com|lpt)[1-9]", device):
        raise WorkspaceError("Destination uses a reserved Windows device name.")


def _workspace_file_exists(workspace: Workspace, relative_path: str) -> bool:
    try:
        return workspace.absolute_path(relative_path).is_file()
    except WorkspaceError:
        return True


def _workspace_target_status(workspace: Workspace, relative_path: str | None, source_sha256: str | None, directory_entries=None, target_statuses=None, target_hashes=None) -> str | None:
    if not relative_path:
        return None
    cache_key = (relative_path.casefold(), source_sha256)
    if target_statuses is not None and cache_key in target_statuses:
        return target_statuses[cache_key]
    try:
        path = _case_insensitive_path(workspace, relative_path, directory_entries)
    except WorkspaceError:
        return "conflict"
    if path is None:
        status = None
    elif _is_reparse_point(path) or not path.is_file():
        status = "conflict"
    elif not source_sha256:
        status = "conflict"
    else:
        target_key = str(path).casefold()
        digest = target_hashes.get(target_key) if target_hashes is not None else None
        if digest is None:
            hasher = hashlib.sha256()
            with path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            if target_hashes is not None:
                target_hashes[target_key] = digest
        status = "already_satisfied" if digest == source_sha256 else "conflict"
    if target_statuses is not None:
        target_statuses[cache_key] = status
    return status


def _target_snapshot(workspace: Workspace, relative_path: str | None) -> dict[str, object] | None:
    if not relative_path:
        return None
    try:
        path = _case_insensitive_path(workspace, relative_path)
        if path is None or not path.is_file() or _is_reparse_point(path):
            return None
        stat_result = path.stat()
        return {
            "relative_path": relative_path,
            "size_bytes": stat_result.st_size,
            "mtime_ns": stat_result.st_mtime_ns,
            "sha256": _hash_path(path),
            "file_type": "regular",
        }
    except (OSError, WorkspaceError):
        return None


def _hash_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _case_insensitive_path(workspace: Workspace, relative_path: str, directory_entries=None) -> Path | None:
    current = workspace.root
    for part in PurePosixPath(relative_path).parts:
        if part in {"", "."}:
            continue
        if not current.is_dir():
            return None
        entries = None
        if directory_entries is not None:
            entries = directory_entries.get(current)
            if entries is None:
                entries = {child.name.casefold(): child for child in current.iterdir()}
                directory_entries[current] = entries
        match = entries.get(part.casefold()) if entries is not None else next((child for child in current.iterdir() if child.name.casefold() == part.casefold()), None)
        current = match if match is not None else current / part
    return current if current.exists() else None


def _resolve_target(
    workspace: Workspace,
    target: str,
    source: str,
    source_sha256: str | None,
    operation: str,
    source_replacement: bool,
    conflict_policy: str,
    planned_targets: dict[str, tuple[str, str, str | None]],
    directory_entries=None,
    target_statuses=None,
    target_hashes=None,
 ) -> tuple[str, str | None, str | None, bool, str | None, bool, dict[str, object] | None]:
    source_key = source.casefold()
    target_key = target.casefold()
    planned = planned_targets.get(target_key)
    if planned is not None and planned[2] and source_sha256 and planned[2] == source_sha256:
        return target, "already_satisfied", None, False, None, True, None
    if target_key == source_key:
        if source_replacement:
            return target, "replace_source", None, True, None, False, _target_snapshot(workspace, target)
        if operation == "move":
            return target, "already_satisfied", None, False, None, False, None
        if operation == "copy":
            return target, "already_satisfied", None, False, None, False, None
        if conflict_policy == "rename":
            renamed = _next_target(target, workspace, planned_targets, directory_entries, target_statuses, target_hashes)
            return renamed, "renamed", target, False, None, False, None
        if conflict_policy == "skip":
            return target, "skipped", None, False, None, False, None
        return target, "overwrite", None, False, None, False, _target_snapshot(workspace, target)

    existing = _workspace_target_status(workspace, target, source_sha256, directory_entries, target_statuses, target_hashes)
    if existing == "already_satisfied" and planned is None:
        return target, existing, None, False, None, False, None
    if planned is None and existing is None:
        return target, None, None, False, None, False, None
    if planned is not None:
        if conflict_policy == "rename":
            renamed = _next_target(target, workspace, planned_targets, directory_entries, target_statuses, target_hashes)
            return renamed, "renamed", target, False, None, False, None
        if conflict_policy == "skip":
            return target, "skipped", None, False, None, False, None
        return target, "conflict", None, False, "Another planned output uses this destination.", False, None
    if conflict_policy == "rename":
        renamed = _next_target(target, workspace, planned_targets, directory_entries, target_statuses, target_hashes)
        return renamed, "renamed", target, False, None, False, None
    if conflict_policy == "skip":
        return target, "skipped", None, False, None, False, None
    return target, "overwrite", None, False, None, False, _target_snapshot(workspace, target)


def _next_target(target: str, workspace: Workspace, planned_targets: dict[str, tuple[str, str, str | None]], directory_entries=None, target_statuses=None, target_hashes=None) -> str:
    path = PurePosixPath(target)
    suffix = path.suffix
    stem = path.name[:-len(suffix)] if suffix else path.name
    number = 1
    while True:
        candidate = str(path.with_name(f"{stem} ({number}){suffix}")).replace("\\", "/")
        key = candidate.casefold()
        if key not in planned_targets and _workspace_target_status(workspace, candidate, None, directory_entries, target_statuses, target_hashes) is None:
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
    def summarize(items):
        groups = {
            "delete": {"file_count": 0, "bytes": 0, "source_bytes": 0, "estimated_storage_delta_bytes": 0},
            "copy": {"file_count": 0, "bytes_added": 0, "estimated_storage_delta_bytes": 0},
            "move": {"file_count": 0, "bytes_moved": 0, "estimated_storage_delta_bytes": 0},
            "compress": {"file_count": 0, "source_bytes": 0, "estimated_output_bytes": 0, "estimated_bytes_saved": 0, "estimated_storage_delta_bytes": 0},
        }
        for operation in items:
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
        return groups

    candidate_groups = summarize(operations)
    executable_operations = [operation for operation in operations if _is_plan_executable(operation)]
    groups = summarize(executable_operations)
    for name, group in groups.items():
        group["candidate_file_count"] = candidate_groups[name]["file_count"]
        group["candidate_storage_delta_bytes"] = candidate_groups[name]["estimated_storage_delta_bytes"]

    connection = workspace.connect()
    try:
        assets = connection.execute("SELECT id FROM logical_asset").fetchall()
        files = connection.execute("SELECT id, logical_asset_id FROM physical_file WHERE in_scope = 1 AND is_online = 1").fetchall()
    finally:
        connection.close()
    operations_by_file = {}
    for operation in executable_operations:
        operations_by_file.setdefault(operation["physical_file_id"], []).append(operation)
    surviving_assets = set()
    for row in files:
        member_operations = operations_by_file.get(row["id"], ())
        has_delete = any(operation["operation"] == "delete" for operation in member_operations)
        has_replacement = any(operation["operation"] in {"copy", "move", "compress"} for operation in member_operations)
        if not has_delete or has_replacement:
            surviving_assets.add(row["logical_asset_id"])
    surviving = len(surviving_assets)
    candidate_storage_delta = sum(int(group["estimated_storage_delta_bytes"]) for group in candidate_groups.values())
    storage_delta = sum(int(group["estimated_storage_delta_bytes"]) for group in groups.values())
    return {
        "candidate_count": len(operations),
        "executable_count": len(executable_operations),
        "conflict_count": len(conflicts),
        "conflicted_count": sum(bool(operation["conflicts"]) for operation in operations),
        "safe_count": len(executable_operations),
        "blocked_count": sum(bool(operation.get("blockers")) for operation in operations),
        "delete": groups["delete"],
        "copy": groups["copy"],
        "move": groups["move"],
        "compress": groups["compress"],
        "candidate_storage_delta_bytes": candidate_storage_delta,
        "executable_storage_delta_bytes": storage_delta,
        "estimated_storage_delta_bytes": storage_delta,
        "estimated_net_bytes_freed": max(0, -storage_delta),
        "estimated_net_bytes_added": max(0, storage_delta),
        "temporary_space_upper_bound_bytes": sum(int(operation.get("bytes") or 0) for operation in executable_operations if operation["operation"] in {"copy", "move", "compress"}),
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


def _conflict_policy(action: dict[str, object] | None) -> str:
    action = action or {}
    policy = action.get("conflict_policy")
    if policy in CONFLICT_POLICIES:
        return str(policy)
    return "skip" if action.get("rename_on_conflict") is False else "rename"


def _normalize_action(action: dict[str, object]) -> dict[str, object]:
    normalized = dict(action)
    normalized["conflict_policy"] = _conflict_policy(action)
    normalized.pop("rename_on_conflict", None)
    return normalized


def _is_plan_executable(operation: dict[str, object]) -> bool:
    return not operation.get("conflicts") and not operation.get("blockers") and operation.get("destination_status") not in {"already_satisfied", "skipped"}


def _profile_snapshot(profile: dict[str, object] | None) -> dict[str, object] | None:
    if profile is None:
        return None
    settings = profile_settings(profile) if profile.get("codec") == "jpeg-xl" else profile.get("settings") or {}
    if profile.get("codec") == "jpeg-xl":
        settings = {**settings, "encoder_version": production_capability().get("encoder_version")}
    return {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "codec": profile.get("codec"),
        "container": profile.get("container"),
        "settings": settings,
    }


def _plan_digest(operations: list[dict[str, object]], ruleset: dict[str, object] | None) -> str:
    state = {
        "ruleset_id": ruleset.get("id") if ruleset else None,
        "operations": [
            {
                "position": position,
                "operation": operation.get("operation"),
                "physical_file_id": operation.get("physical_file_id"),
                "logical_asset_id": operation.get("logical_asset_id"),
                "source_relative_path": operation.get("source_relative_path"),
                "source_size_bytes": operation.get("source_size_bytes", operation.get("bytes")),
                "source_mtime_ns": operation.get("source_mtime_ns"),
                "source_sha256": operation.get("source_sha256"),
                "target_relative_path": operation.get("target_relative_path"),
                "conflict_policy": operation.get("conflict_policy", "rename"),
                "target_snapshot": operation.get("target_snapshot"),
                "source_disposition": operation.get("source_disposition"),
                "profile_id": operation.get("profile_id"),
                "profile_snapshot": operation.get("profile_snapshot"),
                "rule_id": operation.get("rule_id"),
                "rule_snapshot": operation.get("rule_snapshot"),
                "destination_status": operation.get("destination_status"),
                "conflicts": operation.get("conflicts") or [],
                "blockers": operation.get("blockers") or [],
                "estimated_output_bytes": operation.get("estimated_output_bytes"),
                "estimated_storage_delta_bytes": operation.get("estimated_storage_delta_bytes"),
                "coalesced_by_planned_target": bool(operation.get("coalesced_by_planned_target")),
            }
            for position, operation in enumerate(operations)
        ],
    }
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        "action": _normalize_action(_json(row["action_json"]) or {}),
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
            "executable_count": 0,
            "conflict_count": 0,
            "conflicted_count": 0,
            "safe_count": 0,
            "blocked_count": 0,
            "capability_blockers": [],
            "delete": {"file_count": 0, "bytes": 0, "source_bytes": 0, "estimated_storage_delta_bytes": 0},
            "copy": {"file_count": 0, "bytes_added": 0, "estimated_storage_delta_bytes": 0},
            "move": {"file_count": 0, "bytes_moved": 0, "estimated_storage_delta_bytes": 0},
            "compress": {"file_count": 0, "source_bytes": 0, "estimated_output_bytes": 0, "estimated_bytes_saved": 0, "estimated_storage_delta_bytes": 0},
            "candidate_storage_delta_bytes": 0,
            "executable_storage_delta_bytes": 0,
            "estimated_storage_delta_bytes": 0,
            "estimated_net_bytes_freed": 0,
            "estimated_net_bytes_added": 0,
            "temporary_space_upper_bound_bytes": 0,
            "available_space_bytes": None,
            "assets_with_no_surviving_representation": 0,
        },
        "empty_reason": reason,
        "plan_digest": _plan_digest([], None),
        "executor": {"available": True, "supported_operations": ["copy", "compress", "move", "delete"], "message": "Phase 10C JPEG XL keep-source execution is available. Source replacement, AV1, and AVIF remain blocked."},
    }


def _ensure_builtins(workspace: Workspace) -> None:
    now = _timestamp()
    profiles = [
        (BUILTIN_HIGH_QUALITY_PROFILE_ID, "JXL High Quality", "jpeg-xl", "jxl", {"quality": 80, "distance": quality_to_distance(80), "effort": 7}),
        (BUILTIN_BALANCED_PROFILE_ID, "JXL Balanced", "jpeg-xl", "jxl", {"quality": 60, "distance": quality_to_distance(60), "effort": 7}),
        (BUILTIN_HIGH_COMPRESSION_PROFILE_ID, "JXL High Compression", "jpeg-xl", "jxl", {"quality": 40, "distance": quality_to_distance(40), "effort": 7}),
        ("builtin-av1-1080p60-fast", "AV1 1080p60 Fast", "av1", "mp4", {"resolution_cap": [1920, 1080], "fps_cap": 60, "speed_class": "fast", "contract_version": "av1-pending-v1"}),
        ("builtin-av1-1080p60-very-fast", "AV1 1080p60 Very Fast", "av1", "mp4", {"resolution_cap": [1920, 1080], "fps_cap": 60, "speed_class": "very_fast", "contract_version": "av1-pending-v1"}),
        ("builtin-av1-4k120-fast", "AV1 4K120 Fast", "av1", "mp4", {"resolution_cap": [3840, 2160], "fps_cap": 120, "speed_class": "fast", "contract_version": "av1-pending-v1"}),
        (BUILTIN_AV1_PROFILE_ID, "AV1 4K120 Very Fast", "av1", "mp4", {"resolution_cap": [3840, 2160], "fps_cap": 120, "speed_class": "very_fast", "contract_version": "av1-pending-v1"}),
        (LEGACY_BUILTIN_AV1_PROFILE_ID, "AV1 Archival (pending)", "av1", "mp4", {"alias_of": BUILTIN_AV1_PROFILE_ID, "contract_version": "av1-pending-v1"}),
    ]
    cleanup_rules = [
        {"enabled": True, "match": {"selection_state": "undecided", "representation_class": "raw"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "rejected", "representation_class": "raw"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "selected", "representation_class": "raw"}, "action": {"operation": "move", "destination_dir": "raws", "preserve_relative_structure": False, "conflict_policy": "rename"}},
        {"enabled": True, "match": {"selection_state": "selected", "formats": ["jpeg", "png"]}, "action": {"operation": "copy", "destination_dir": "jpgs", "preserve_relative_structure": False, "conflict_policy": "rename"}},
        {"enabled": True, "match": {"selection_state": "undecided", "formats": ["jpeg", "png"]}, "action": {"operation": "compress", "profile_id": BUILTIN_BALANCED_PROFILE_ID, "source_disposition": "keep", "compress_in_place": True, "conflict_policy": "rename"}},
        {"enabled": True, "match": {"selection_state": "rejected", "formats": ["jpeg", "png"]}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "rejected", "representation_class": "video"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"representation_class": "video", "selection_state_not": "rejected"}, "action": {"operation": "compress", "profile_id": BUILTIN_AV1_PROFILE_ID, "source_disposition": "keep", "compress_in_place": True, "conflict_policy": "rename"}},
    ]
    selected_rules = [
        {"enabled": True, "match": {"selection_state": "rejected"}, "action": {"operation": "delete"}},
        {"enabled": True, "match": {"selection_state": "undecided"}, "action": {"operation": "delete"}},
    ]
    with workspace.transaction() as connection:
        for profile_id, name, codec, container, settings in profiles:
            connection.execute(
                "INSERT INTO compression_profile(id, name, codec, container, settings_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, codec=excluded.codec, container=excluded.container, settings_json=excluded.settings_json, updated_at=excluded.updated_at",
                (profile_id, name, codec, container, json.dumps(settings, sort_keys=True), now, now),
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


def _ensure_no_active_execution(workspace: Workspace) -> None:
    from .file_management_executor import ensure_settings_unlocked

    ensure_settings_unlocked(workspace)
