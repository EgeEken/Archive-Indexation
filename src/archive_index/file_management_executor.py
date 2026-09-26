"""Persistent, sequential File Management execution for Copy, Move, and Delete."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .file_management import build_dry_run_plan
from .indexing.reconciliation import reconcile_workspace
from .indexing.scanner import hash_file, scan
from .workspace import INDEX_DIRECTORY, Workspace, WorkspaceError, _is_reparse_point

LOGGER = logging.getLogger(__name__)
CHUNK_SIZE = 1024 * 1024
SUPPORTED_OPERATIONS = ("copy", "move", "delete")
RUN_ACTIVE = {"draft", "running", "cancelling", "interrupted"}
RUN_TERMINAL = {"cancelled", "completed", "completed_with_errors", "failed"}
OPERATION_PHASE = {"copy": 0, "compress": 1, "move": 2, "delete": 3}


class ExecutionConflict(ValueError):
    pass


class ExecutionNotFound(ValueError):
    pass


class OperationFailure(Exception):
    pass


class OperationCancelled(OperationFailure):
    pass


@dataclass
class _Worker:
    execution_id: str
    cancel: threading.Event
    thread: threading.Thread


_workers: dict[str, _Worker] = {}
_workers_lock = threading.Lock()


def prepare_execution(workspace: Workspace, ruleset_id: str | None, plan_digest: str) -> dict[str, object]:
    if not isinstance(plan_digest, str) or not plan_digest:
        raise ValueError("plan_digest is required")
    _recover_startup(workspace)
    plan = build_dry_run_plan(workspace, ruleset_id)
    if plan.get("plan_digest") != plan_digest:
        raise ExecutionConflict("The plan changed. Analyze again before executing.")
    if _active_job(workspace) is not None:
        raise ExecutionConflict("File Management cannot start while a workspace job is running.")
    with workspace.transaction() as connection:
        active = connection.execute(
            "SELECT id, status FROM file_management_execution WHERE status IN (?, ?, ?, ?) ORDER BY created_at DESC LIMIT 1",
            tuple(RUN_ACTIVE),
        ).fetchone()
        if active is not None:
            raise ExecutionConflict("A File Management execution is already active for this workspace.")
        execution_id = str(uuid.uuid4())
        now = _timestamp()
        operations = sorted(
            enumerate(plan.get("operations") or []),
            key=lambda item: (OPERATION_PHASE.get(str(item[1].get("operation")), 9), item[0]),
        )
        executable = [
            operation for _, operation in operations
            if _operation_status(operation) == "pending"
        ]
        estimated_written = sum(int(operation.get("bytes") or 0) for operation in executable if operation.get("operation") == "copy")
        estimated_removed = sum(int(operation.get("bytes") or 0) for operation in executable if operation.get("operation") == "delete")
        estimated_delta = sum(int(operation.get("estimated_storage_delta_bytes") or 0) for operation in executable)
        summary = dict(plan.get("summary") or {})
        summary["execution_executable_count"] = len(executable)
        connection.execute(
            """
            INSERT INTO file_management_execution(
                id, ruleset_id, plan_digest, status, summary_json, plan_metadata_json,
                created_at, updated_at, estimated_bytes_written, estimated_bytes_removed,
                estimated_storage_delta, temporary_space_upper_bound_bytes
            ) VALUES (?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id, plan.get("ruleset_id"), plan_digest,
                _json(summary), _json({"executor": plan.get("executor"), "ruleset_id": plan.get("ruleset_id")}),
                now, now, estimated_written, estimated_removed, estimated_delta,
                int((summary.get("temporary_space_upper_bound_bytes") or 0)),
            ),
        )
        for position, (plan_position, operation) in enumerate(operations):
            status = _operation_status(operation)
            reason = _operation_reason(operation, status)
            connection.execute(
                """
                INSERT INTO file_management_execution_operation(
                    id, execution_id, position, phase, operation, status, stage,
                    physical_file_id, logical_asset_id, filename, source_relative_path,
                    source_size_bytes, source_mtime_ns, source_sha256, target_relative_path,
                    profile_id, source_disposition, destination_status, conflicts_json,
                    blockers_json, rule_snapshot_json, profile_snapshot_json,
                    estimated_output_bytes, estimated_storage_delta, error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), execution_id, position,
                    OPERATION_PHASE.get(str(operation.get("operation")), 9),
                    operation.get("operation"), status, "excluded" if status == "excluded" else "pending",
                    operation.get("physical_file_id"), operation.get("logical_asset_id"), operation.get("filename") or "File",
                    operation.get("source_relative_path"), operation.get("source_size_bytes", operation.get("bytes")),
                    operation.get("source_mtime_ns"), operation.get("source_sha256"), operation.get("target_relative_path"),
                    operation.get("profile_id"), operation.get("source_disposition"), operation.get("destination_status"),
                    _json(operation.get("conflicts") or []), _json(operation.get("blockers") or []),
                    _json(operation.get("rule_snapshot") or {}), _json(operation.get("profile_snapshot")),
                    int(operation.get("estimated_output_bytes") or 0), int(operation.get("estimated_storage_delta_bytes") or 0),
                    reason, now,
                ),
            )
    return get_execution(workspace, execution_id)


def get_active_execution(workspace: Workspace) -> dict[str, object] | None:
    _recover_startup(workspace)
    connection = workspace.connect()
    try:
        row = connection.execute(
            "SELECT id FROM file_management_execution ORDER BY updated_at DESC, created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    return get_execution(workspace, row["id"]) if row is not None else None


def get_execution(workspace: Workspace, execution_id: str) -> dict[str, object]:
    _recover_startup(workspace)
    connection = workspace.connect()
    try:
        execution = connection.execute(
            "SELECT * FROM file_management_execution WHERE id = ?", (execution_id,)
        ).fetchone()
        if execution is None:
            raise ExecutionNotFound("File Management execution was not found.")
        operations = connection.execute(
            "SELECT * FROM file_management_execution_operation WHERE execution_id = ? ORDER BY position",
            (execution_id,),
        ).fetchall()
    finally:
        connection.close()
    return _execution_payload(execution, operations)


def start_execution(workspace: Workspace, execution_id: str) -> dict[str, object]:
    return _start_execution(workspace, execution_id, {"draft"})


def resume_execution(workspace: Workspace, execution_id: str) -> dict[str, object]:
    _recover_startup(workspace)
    _wait_for_worker(workspace, execution_id)
    with workspace.transaction() as connection:
        row = connection.execute("SELECT status FROM file_management_execution WHERE id = ?", (execution_id,)).fetchone()
        if row is None:
            raise ExecutionNotFound("File Management execution was not found.")
        if row["status"] not in {"cancelled", "interrupted"}:
            raise ExecutionConflict("Only cancelled or interrupted executions can be resumed.")
        now = _timestamp()
        connection.execute(
            "UPDATE file_management_execution_operation SET status = 'pending', stage = CASE WHEN status = 'interrupted' THEN 'resume' ELSE 'pending' END, error_message = NULL, bytes_completed = 0, updated_at = ? WHERE execution_id = ? AND status IN ('cancelled', 'interrupted')",
            (now, execution_id),
        )
        connection.execute(
            "UPDATE file_management_execution SET status = 'draft', cancel_requested = 0, error_message = NULL, updated_at = ?, finished_at = NULL WHERE id = ?",
            (now, execution_id),
        )
    return _start_execution(workspace, execution_id, {"draft"})


def retry_failed(workspace: Workspace, execution_id: str) -> dict[str, object]:
    _recover_startup(workspace)
    _wait_for_worker(workspace, execution_id)
    with workspace.transaction() as connection:
        row = connection.execute("SELECT status FROM file_management_execution WHERE id = ?", (execution_id,)).fetchone()
        if row is None:
            raise ExecutionNotFound("File Management execution was not found.")
        failed = connection.execute(
            "SELECT COUNT(*) FROM file_management_execution_operation WHERE execution_id = ? AND status = 'failed'",
            (execution_id,),
        ).fetchone()[0]
        if not failed:
            raise ExecutionConflict("There are no failed operations to retry.")
        if row["status"] in {"running", "cancelling"}:
            raise ExecutionConflict("This execution is already running.")
        now = _timestamp()
        connection.execute(
            "UPDATE file_management_execution_operation SET status = 'pending', stage = CASE WHEN stage IN ('finalizing', 'deleting') THEN 'resume' ELSE 'pending' END, error_message = NULL, bytes_completed = 0, updated_at = ? WHERE execution_id = ? AND status = 'failed'",
            (now, execution_id),
        )
        connection.execute(
            "UPDATE file_management_execution SET status = 'draft', cancel_requested = 0, error_message = NULL, updated_at = ?, finished_at = NULL WHERE id = ?",
            (now, execution_id),
        )
    return _start_execution(workspace, execution_id, {"draft"})


def cancel_execution(workspace: Workspace, execution_id: str) -> dict[str, object]:
    with workspace.transaction() as connection:
        row = connection.execute("SELECT status FROM file_management_execution WHERE id = ?", (execution_id,)).fetchone()
        if row is None:
            raise ExecutionNotFound("File Management execution was not found.")
        if row["status"] not in {"running", "cancelling"}:
            raise ExecutionConflict("This execution is not running.")
        now = _timestamp()
        connection.execute(
            "UPDATE file_management_execution SET status = 'cancelling', cancel_requested = 1, updated_at = ? WHERE id = ?",
            (now, execution_id),
        )
    with _workers_lock:
        worker = next((item for item in _workers.values() if item.execution_id == execution_id), None)
        if worker is not None:
            worker.cancel.set()
    return get_execution(workspace, execution_id)


def has_active_execution(workspace: Workspace) -> bool:
    _recover_startup(workspace)
    connection = workspace.connect()
    try:
        return connection.execute(
            "SELECT 1 FROM file_management_execution WHERE status IN ('draft', 'running', 'cancelling', 'interrupted') LIMIT 1"
        ).fetchone() is not None
    finally:
        connection.close()


def recover_workspace(workspace: Workspace) -> None:
    """Mark orphaned mutation runs interrupted when a workspace is reopened."""

    _recover_startup(workspace)


def _start_execution(workspace: Workspace, execution_id: str, allowed: set[str]) -> dict[str, object]:
    if _active_job(workspace) is not None:
        raise ExecutionConflict("File Management cannot start while a workspace job is running.")
    key = _workspace_key(workspace)
    with _workers_lock:
        worker = _workers.get(key)
        if worker is not None and worker.thread.is_alive():
            raise ExecutionConflict("A File Management execution is already running for this workspace.")
        with workspace.transaction() as connection:
            row = connection.execute("SELECT status FROM file_management_execution WHERE id = ?", (execution_id,)).fetchone()
            if row is None:
                raise ExecutionNotFound("File Management execution was not found.")
            if row["status"] not in allowed:
                raise ExecutionConflict("This execution cannot be started in its current state.")
            free = _available_space(workspace)
            required = connection.execute("SELECT temporary_space_upper_bound_bytes FROM file_management_execution WHERE id = ?", (execution_id,)).fetchone()
            required_bytes = int(required[0] or 0) if required else 0
            if free is not None and required_bytes > free:
                raise ExecutionConflict(f"Insufficient free space: {required_bytes} bytes required, {free} available.")
            now = _timestamp()
            connection.execute(
                "UPDATE file_management_execution SET status = 'running', cancel_requested = 0, started_at = COALESCE(started_at, ?), updated_at = ?, finished_at = NULL WHERE id = ?",
                (now, now, execution_id),
            )
        cancel = threading.Event()
        thread = threading.Thread(target=_run_execution, args=(workspace, execution_id, cancel), name=f"archive-file-management-{execution_id[:8]}", daemon=True)
        _workers[key] = _Worker(execution_id, cancel, thread)
        thread.start()
    return get_execution(workspace, execution_id)


def _run_execution(workspace: Workspace, execution_id: str, cancel: threading.Event) -> None:
    changed = False
    cancelled = False
    try:
        for operation in _pending_operations(workspace, execution_id):
            if cancel.is_set() or _cancel_requested(workspace, execution_id):
                cancelled = True
                break
            try:
                _execute_operation(workspace, execution_id, operation, cancel)
                changed = True
            except OperationCancelled as error:
                _set_operation(workspace, operation["id"], status="cancelled", stage="cancelled", error=str(error))
                cancelled = True
                break
            except Exception as error:
                LOGGER.warning("File Management operation %s failed: %s", operation["id"], error)
                changed = True
                stage = _operation_stage(workspace, operation["id"])
                _set_operation(workspace, operation["id"], status="failed", stage=stage if stage in {"finalizing", "deleting"} else "failed", error=str(error))
        if changed:
            _refresh_catalog(workspace, execution_id)
        if cancelled:
            _finish_cancelled(workspace, execution_id)
        else:
            _finish_run(workspace, execution_id)
    except Exception as error:
        LOGGER.exception("File Management execution %s failed", execution_id)
        with workspace.transaction() as connection:
            connection.execute(
                "UPDATE file_management_execution SET status = 'failed', error_message = ?, updated_at = ?, finished_at = ? WHERE id = ?",
                (str(error), _timestamp(), _timestamp(), execution_id),
            )
    finally:
        with _workers_lock:
            if _workers.get(_workspace_key(workspace), None) is not None and _workers[_workspace_key(workspace)].execution_id == execution_id:
                _workers.pop(_workspace_key(workspace), None)


def _execute_operation(workspace: Workspace, execution_id: str, operation, cancel: threading.Event) -> None:
    operation = dict(operation)
    recovery_eligible = operation.get("stage") == "resume"
    operation["recovery_eligible"] = recovery_eligible
    _set_operation(workspace, operation["id"], status="running", stage="starting", increment_attempt=True)
    kind = operation["operation"]
    if kind == "copy":
        _copy(workspace, execution_id, operation, cancel)
    elif kind == "move":
        _move(workspace, execution_id, operation, cancel)
    elif kind == "delete":
        _delete(workspace, execution_id, operation)
    else:
        raise OperationFailure("Production compression execution is not enabled until Phase 10C.")


def _copy(workspace: Workspace, execution_id: str, operation, cancel: threading.Event) -> None:
    source = _validate_source(workspace, operation)
    target = _validate_target(workspace, operation["target_relative_path"], allow_missing=True)
    occupied = _existing_case_insensitive_target(workspace, operation["target_relative_path"])
    if occupied is not None:
        if operation.get("recovery_eligible") and _matches_expected(occupied, operation):
            _accept_existing_copy(workspace, execution_id, operation, source, occupied)
            return
        raise OperationFailure("Destination now exists.")
    _ensure_target_directory(workspace, operation["target_relative_path"])
    target = _validate_target(workspace, operation["target_relative_path"], allow_missing=True)
    if _existing_case_insensitive_target(workspace, operation["target_relative_path"]) is not None:
        raise OperationFailure("Destination now exists.")
    _ensure_operation_space(workspace, int(operation["source_size_bytes"] or 0))
    temp = _owned_temp_path(workspace, execution_id, operation)
    _set_temp(workspace, operation["id"], temp)
    _remove_owned_temp(workspace, temp)
    copied = 0
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_file, temp.open("xb") as output_file:
            while chunk := input_file.read(CHUNK_SIZE):
                if cancel.is_set() or _cancel_requested(workspace, execution_id):
                    raise OperationCancelled("Execution cancelled.")
                output_file.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
                _update_progress(workspace, operation["id"], copied)
            output_file.flush()
            os.fsync(output_file.fileno())
        shutil.copystat(source, temp, follow_symlinks=False)
        if copied != int(operation["source_size_bytes"] or 0) or digest.hexdigest() != operation["source_sha256"]:
            raise OperationFailure("Output hash did not match source.")
        _set_stage(workspace, operation["id"], "finalizing")
        _validate_source(workspace, operation)
        target = _validate_target(workspace, operation["target_relative_path"], allow_missing=True)
        if _existing_case_insensitive_target(workspace, operation["target_relative_path"]) is not None:
            raise OperationFailure("Destination now exists.")
        _atomic_no_replace(temp, target, remove_source=False)
        output_sha = hash_file(target)
        output_size = target.stat().st_size
        if output_size != copied or output_sha != operation["source_sha256"]:
            raise OperationFailure("Output hash did not match source.")
        _complete_operation(workspace, execution_id, operation, output_size, output_sha, copied, copied, 0, 0)
    except Exception:
        if temp.exists():
            _remove_owned_temp(workspace, temp)
        raise


def _move(workspace: Workspace, execution_id: str, operation, cancel: threading.Event) -> None:
    source = _validate_source(workspace, operation)
    target = _validate_target(workspace, operation["target_relative_path"], allow_missing=True)
    occupied = _existing_case_insensitive_target(workspace, operation["target_relative_path"])
    if occupied is not None:
        if operation.get("recovery_eligible") and _matches_expected(occupied, operation):
            _validate_source(workspace, operation)
            _set_stage(workspace, operation["id"], "deleting")
            _unlink_source(workspace, operation)
            _complete_operation(workspace, execution_id, operation, occupied.stat().st_size, operation["source_sha256"], 0, 0, int(operation["source_size_bytes"] or 0), 0)
            return
        raise OperationFailure("Destination now exists.")
    _ensure_target_directory(workspace, operation["target_relative_path"])
    target = _validate_target(workspace, operation["target_relative_path"], allow_missing=True)
    if _existing_case_insensitive_target(workspace, operation["target_relative_path"]) is not None:
        raise OperationFailure("Destination now exists.")
    _set_stage(workspace, operation["id"], "finalizing")
    try:
        _atomic_no_replace(source, target, remove_source=True)
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}:
            raise
        _move_by_copy(workspace, execution_id, operation, cancel, source, target)
        return
    if not target.is_file() or hash_file(target) != operation["source_sha256"]:
        raise OperationFailure("Move target did not match source.")
    _complete_operation(workspace, execution_id, operation, target.stat().st_size, operation["source_sha256"], 0, 0, int(operation["source_size_bytes"] or 0), 0)


def _move_by_copy(workspace: Workspace, execution_id: str, operation, cancel: threading.Event, source: Path, target: Path) -> None:
    _ensure_operation_space(workspace, int(operation["source_size_bytes"] or 0))
    temp = _owned_temp_path(workspace, execution_id, operation)
    _set_temp(workspace, operation["id"], temp)
    _remove_owned_temp(workspace, temp)
    copied = _copy_to_temp(workspace, execution_id, operation, cancel, source, temp)
    _set_stage(workspace, operation["id"], "finalizing")
    _validate_source(workspace, operation)
    target = _validate_target(workspace, operation["target_relative_path"], allow_missing=True)
    if _existing_case_insensitive_target(workspace, operation["target_relative_path"]) is not None:
        raise OperationFailure("Destination now exists.")
    _atomic_no_replace(temp, target, remove_source=False)
    if not _matches_expected(target, operation):
        raise OperationFailure("Output hash did not match source.")
    _validate_source(workspace, operation)
    _set_stage(workspace, operation["id"], "deleting")
    _unlink_source(workspace, operation)
    _complete_operation(workspace, execution_id, operation, target.stat().st_size, operation["source_sha256"], copied, copied, int(operation["source_size_bytes"] or 0), 0)


def _delete(workspace: Workspace, execution_id: str, operation) -> None:
    _validate_source(workspace, operation)
    _set_stage(workspace, operation["id"], "deleting")
    _unlink_source(workspace, operation)
    _complete_operation(workspace, execution_id, operation, None, None, 0, 0, 0, int(operation["source_size_bytes"] or 0))


def _copy_to_temp(workspace, execution_id, operation, cancel, source: Path, temp: Path) -> int:
    copied = 0
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_file, temp.open("xb") as output_file:
            while chunk := input_file.read(CHUNK_SIZE):
                if cancel.is_set() or _cancel_requested(workspace, execution_id):
                    raise OperationCancelled("Execution cancelled.")
                output_file.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
                _update_progress(workspace, operation["id"], copied)
            output_file.flush()
            os.fsync(output_file.fileno())
        shutil.copystat(source, temp, follow_symlinks=False)
    except Exception:
        _remove_owned_temp(workspace, temp)
        raise
    if copied != int(operation["source_size_bytes"] or 0) or digest.hexdigest() != operation["source_sha256"]:
        _remove_owned_temp(workspace, temp)
        raise OperationFailure("Output hash did not match source.")
    return copied


def _validate_source(workspace: Workspace, operation) -> Path:
    relative = operation["source_relative_path"]
    connection = workspace.connect()
    try:
        indexed = connection.execute(
            "SELECT relative_path, size_bytes, mtime_ns, sha256, is_online FROM physical_file WHERE id = ?",
            (operation["physical_file_id"],),
        ).fetchone()
    finally:
        connection.close()
    if indexed is None or not indexed["is_online"] or indexed["relative_path"] != relative:
        raise OperationFailure("Source is no longer online.")
    path = _validate_target(workspace, relative, allow_missing=False)
    if _is_reparse_point(path) or not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise OperationFailure("Source is no longer online.")
    current = path.stat()
    if current.st_size != int(operation["source_size_bytes"] or 0) or current.st_mtime_ns != int(operation["source_mtime_ns"] or 0):
        raise OperationFailure("Source changed since the plan was confirmed.")
    if hash_file(path) != operation["source_sha256"]:
        raise OperationFailure("Source changed since the plan was confirmed.")
    return path


def _validate_target(workspace: Workspace, relative: str, *, allow_missing: bool) -> Path:
    if not relative:
        raise OperationFailure("Destination is missing.")
    normalized = str(relative).replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if not parts or normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise OperationFailure("Destination escapes the workspace.")
    for part in parts:
        _validate_windows_segment(part)
    if parts[0].casefold() == INDEX_DIRECTORY.casefold() or any(part.casefold() == INDEX_DIRECTORY.casefold() for part in parts):
        raise OperationFailure("Destination may not use the application state directory.")
    current = workspace.root
    for index, part in enumerate(parts):
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_reparse_point(current):
                raise OperationFailure("Destination escapes the workspace.")
            resolved = current.resolve(strict=True)
            try:
                resolved.relative_to(workspace.root.resolve(strict=True))
            except ValueError as error:
                raise OperationFailure("Destination escapes the workspace.") from error
        elif index == len(parts) - 1 and not allow_missing:
            raise OperationFailure("Source is no longer online.")
    return current


def _ensure_target_directory(workspace: Workspace, relative: str) -> None:
    parts = PurePosixPath(str(relative).replace("\\", "/")).parts[:-1]
    current = workspace.root
    for part in parts:
        _validate_windows_segment(part)
        if part.casefold() == INDEX_DIRECTORY.casefold():
            raise OperationFailure("Destination may not use the application state directory.")
        current = current / part
        if current.exists():
            if _is_reparse_point(current) or not current.is_dir():
                raise OperationFailure("Destination escapes the workspace.")
            continue
        try:
            current.mkdir()
        except FileExistsError:
            pass
        if _is_reparse_point(current) or not current.is_dir():
            raise OperationFailure("Destination escapes the workspace.")


def _atomic_no_replace(source: Path, target: Path, *, remove_source: bool) -> None:
    try:
        os.link(source, target)
    except FileExistsError as error:
        raise OperationFailure("Destination now exists.") from error
    except OSError:
        raise
    if remove_source:
        try:
            source.unlink()
        except Exception:
            raise OperationFailure("Move target completed but source removal failed.")
    else:
        try:
            source.unlink()
        except FileNotFoundError:
            pass


def _accept_existing_copy(workspace, execution_id, operation, source, target) -> None:
    if not _matches_expected(target, operation):
        raise OperationFailure("Destination now exists.")
    _complete_operation(workspace, execution_id, operation, target.stat().st_size, operation["source_sha256"], 0, 0, 0, 0)


def _matches_expected(path: Path, operation) -> bool:
    try:
        return path.is_file() and path.stat().st_size == int(operation["source_size_bytes"] or 0) and hash_file(path) == operation["source_sha256"]
    except OSError:
        return False


def _existing_case_insensitive_target(workspace: Workspace, relative: str) -> Path | None:
    current = workspace.root
    for part in PurePosixPath(str(relative).replace("\\", "/")).parts:
        if not current.is_dir():
            return None
        match = next((child for child in current.iterdir() if child.name.casefold() == part.casefold()), None)
        if match is None:
            return None
        current = match
    return current if current.exists() else None


def _unlink_source(workspace, operation) -> None:
    source = _validate_source(workspace, operation)
    source.unlink()


def _owned_temp_path(workspace, execution_id: str, operation) -> Path:
    target = _validate_target(workspace, operation["target_relative_path"], allow_missing=True)
    name = f".{target.name}.archive-index-{operation['id']}.tmp"
    return target.parent / name


def _remove_owned_temp(workspace, path: Path) -> None:
    if not path.name.endswith(".tmp") or ".archive-index-" not in path.name:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _pending_operations(workspace, execution_id):
    connection = workspace.connect()
    try:
        return connection.execute(
            "SELECT * FROM file_management_execution_operation WHERE execution_id = ? AND status = 'pending' ORDER BY phase, position",
            (execution_id,),
        ).fetchall()
    finally:
        connection.close()


def _set_operation(workspace, operation_id, *, status=None, stage=None, error=None, increment_attempt=False) -> None:
    fields = []
    values = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if stage is not None:
        fields.append("stage = ?")
        values.append(stage)
    if error is not None:
        fields.append("error_message = ?")
        values.append(error)
    if increment_attempt:
        fields.append("attempt_count = attempt_count + 1")
        fields.append("started_at = COALESCE(started_at, ?)")
        values.append(_timestamp())
    fields.append("updated_at = ?")
    values.append(_timestamp())
    values.append(operation_id)
    with workspace.transaction() as connection:
        connection.execute(f"UPDATE file_management_execution_operation SET {', '.join(fields)} WHERE id = ?", values)


def _operation_stage(workspace, operation_id):
    connection = workspace.connect()
    try:
        row = connection.execute("SELECT stage FROM file_management_execution_operation WHERE id = ?", (operation_id,)).fetchone()
    finally:
        connection.close()
    return row["stage"] if row is not None else "failed"


def _set_stage(workspace, operation_id, stage) -> None:
    _set_operation(workspace, operation_id, stage=stage)


def _set_temp(workspace, operation_id, temp: Path) -> None:
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE file_management_execution_operation SET temp_relative_path = ?, updated_at = ? WHERE id = ?",
            (temp.relative_to(workspace.root).as_posix(), _timestamp(), operation_id),
        )


def _update_progress(workspace, operation_id, bytes_completed: int) -> None:
    with workspace.transaction() as connection:
        row = connection.execute("SELECT execution_id FROM file_management_execution_operation WHERE id = ?", (operation_id,)).fetchone()
        if row is None:
            return
        connection.execute(
            "UPDATE file_management_execution_operation SET bytes_completed = ?, updated_at = ? WHERE id = ?",
            (bytes_completed, _timestamp(), operation_id),
        )
        connection.execute("UPDATE file_management_execution SET updated_at = ? WHERE id = ?", (_timestamp(), row["execution_id"]))


def _complete_operation(workspace, execution_id, operation, output_size, output_sha, bytes_completed, written, moved, removed) -> None:
    now = _timestamp()
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE file_management_execution_operation SET status = 'completed', stage = 'completed', actual_output_size_bytes = ?, actual_output_sha256 = ?, bytes_completed = ?, error_message = NULL, completed_at = ?, updated_at = ? WHERE id = ?",
            (output_size, output_sha, bytes_completed, now, now, operation["id"]),
        )
        connection.execute(
            "UPDATE file_management_execution SET actual_bytes_written = actual_bytes_written + ?, actual_bytes_removed = actual_bytes_removed + ?, actual_bytes_moved = actual_bytes_moved + ?, actual_storage_delta = actual_storage_delta + ?, updated_at = ? WHERE id = ?",
            (written, removed, moved, written - removed, now, execution_id),
        )


def _finish_cancelled(workspace, execution_id) -> None:
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE file_management_execution SET status = 'cancelled', cancel_requested = 1, finished_at = ?, updated_at = ? WHERE id = ?",
            (_timestamp(), _timestamp(), execution_id),
        )


def _finish_run(workspace, execution_id) -> None:
    with workspace.transaction() as connection:
        failed = connection.execute("SELECT COUNT(*) FROM file_management_execution_operation WHERE execution_id = ? AND status = 'failed'", (execution_id,)).fetchone()[0]
        status = "completed_with_errors" if failed else "completed"
        connection.execute(
            "UPDATE file_management_execution SET status = ?, finished_at = ?, updated_at = ? WHERE id = ?",
            (status, _timestamp(), _timestamp(), execution_id),
        )


def _refresh_catalog(workspace, execution_id) -> None:
    with workspace.transaction() as connection:
        connection.execute("UPDATE file_management_execution SET catalog_refresh_status = 'running', updated_at = ? WHERE id = ?", (_timestamp(), execution_id))
    try:
        scan(workspace)
        reconcile_workspace(workspace)
    except Exception as error:
        LOGGER.exception("catalog refresh failed after File Management execution %s", execution_id)
        with workspace.transaction() as connection:
            connection.execute(
                "UPDATE file_management_execution SET catalog_refresh_status = 'failed', catalog_refresh_error = ?, warning_message = ?, updated_at = ? WHERE id = ?",
                (str(error), "Filesystem changes completed, but catalog refresh failed. Re-index may be required.", _timestamp(), execution_id),
            )
    else:
        with workspace.transaction() as connection:
            connection.execute("UPDATE file_management_execution SET catalog_refresh_status = 'complete', updated_at = ? WHERE id = ?", (_timestamp(), execution_id))


def _recover_startup(workspace: Workspace) -> None:
    key = _workspace_key(workspace)
    with _workers_lock:
        worker = _workers.get(key)
        if worker is not None and worker.thread.is_alive():
            return
    connection = workspace.connect()
    try:
        rows = connection.execute("SELECT id FROM file_management_execution WHERE status IN ('running', 'cancelling')").fetchall()
    finally:
        connection.close()
    for row in rows:
        _recover_run(workspace, row["id"])


def _recover_run(workspace: Workspace, execution_id: str) -> None:
    connection = workspace.connect()
    try:
        operations = connection.execute("SELECT * FROM file_management_execution_operation WHERE execution_id = ? ORDER BY position", (execution_id,)).fetchall()
    finally:
        connection.close()
    for operation in operations:
        if operation["status"] not in {"running", "interrupted"}:
            continue
        try:
            if operation["operation"] == "copy":
                target = _validate_target(workspace, operation["target_relative_path"], allow_missing=True)
                if operation["stage"] in {"finalizing", "deleting"} and target.exists() and _matches_expected(target, operation):
                    _complete_operation(workspace, execution_id, operation, target.stat().st_size, operation["source_sha256"], target.stat().st_size, 0, 0, 0)
                else:
                    _set_operation(workspace, operation["id"], status="interrupted", stage="interrupted", error="Execution interrupted; Resume to retry from a safe boundary.")
            elif operation["operation"] == "move":
                target = _validate_target(workspace, operation["target_relative_path"], allow_missing=True)
                source = _validate_target(workspace, operation["source_relative_path"], allow_missing=True)
                if operation["stage"] in {"finalizing", "deleting"} and target.exists() and _matches_expected(target, operation) and not source.exists():
                    _complete_operation(workspace, execution_id, operation, target.stat().st_size, operation["source_sha256"], 0, 0, int(operation["source_size_bytes"] or 0), 0)
                else:
                    _set_operation(workspace, operation["id"], status="interrupted", stage="interrupted", error="Execution interrupted; Resume to reconcile this Move.")
            elif operation["operation"] == "delete":
                source = _validate_target(workspace, operation["source_relative_path"], allow_missing=True)
                if operation["stage"] == "deleting" and not source.exists():
                    _complete_operation(workspace, execution_id, operation, None, None, 0, 0, 0, int(operation["source_size_bytes"] or 0))
                else:
                    _set_operation(workspace, operation["id"], status="interrupted", stage="interrupted", error="Execution interrupted; Resume to retry this Delete.")
        except Exception as error:
            _set_operation(workspace, operation["id"], status="interrupted", stage="interrupted", error=str(error))
    with workspace.transaction() as connection:
        connection.execute("UPDATE file_management_execution SET status = 'interrupted', updated_at = ? WHERE id = ?", (_timestamp(), execution_id))


def _execution_payload(execution, operations) -> dict[str, object]:
    rows = [dict(row) for row in operations]
    counts = {status: sum(1 for row in rows if row["status"] == status) for status in ("completed", "failed", "skipped", "pending", "interrupted", "cancelled", "excluded")}
    total = sum(1 for row in rows if row["status"] not in {"excluded"})
    completed = counts["completed"] + counts["skipped"]
    bytes_total = sum(int(row["source_size_bytes"] or 0) for row in rows if row["operation"] in {"copy", "move"})
    bytes_done = sum(int(row["bytes_completed"] or 0) for row in rows)
    elapsed = _elapsed(execution)
    written = int(execution["actual_bytes_written"] or 0)
    throughput = written / elapsed if elapsed and written else None
    remaining = max(0, bytes_total - bytes_done)
    eta = remaining / throughput if throughput else None
    current = next((row for row in rows if row["status"] == "running"), None)
    return {
        "id": execution["id"],
        "ruleset_id": execution["ruleset_id"],
        "plan_digest": execution["plan_digest"],
        "status": execution["status"],
        "summary": _parse_json(execution["summary_json"]) or {},
        "created_at": execution["created_at"],
        "started_at": execution["started_at"],
        "updated_at": execution["updated_at"],
        "finished_at": execution["finished_at"],
        "cancel_requested": bool(execution["cancel_requested"]),
        "counts": {"completed": completed, "failed": counts["failed"], "skipped": counts["skipped"], "pending": counts["pending"] + counts["interrupted"] + counts["cancelled"], "excluded": counts["excluded"], "total": total},
        "current_operation": {"operation": current["operation"], "filename": current["filename"], "bytes_completed": current["bytes_completed"]} if current else None,
        "bytes_processed": bytes_done,
        "total_bytes": bytes_total,
        "elapsed_seconds": elapsed,
        "throughput_bytes_per_second": throughput,
        "eta_seconds": eta,
        "estimated_bytes_written": execution["estimated_bytes_written"],
        "estimated_bytes_removed": execution["estimated_bytes_removed"],
        "estimated_storage_delta": execution["estimated_storage_delta"],
        "temporary_space_upper_bound_bytes": execution["temporary_space_upper_bound_bytes"],
        "actual_bytes_written": execution["actual_bytes_written"],
        "actual_bytes_removed": execution["actual_bytes_removed"],
        "actual_bytes_moved": execution["actual_bytes_moved"],
        "actual_storage_delta": execution["actual_storage_delta"],
        "error": execution["error_message"],
        "warning": execution["warning_message"],
        "catalog_refresh_status": execution["catalog_refresh_status"],
        "catalog_refresh_error": execution["catalog_refresh_error"],
        "operations": [_operation_payload(row) for row in rows],
    }


def _operation_payload(row) -> dict[str, object]:
    value = dict(row)
    value["conflicts"] = _parse_json(value.pop("conflicts_json")) or []
    value["blockers"] = _parse_json(value.pop("blockers_json")) or []
    value["rule_snapshot"] = _parse_json(value.pop("rule_snapshot_json")) or {}
    value["profile_snapshot"] = _parse_json(value.pop("profile_snapshot_json"))
    return value


def _operation_status(operation) -> str:
    if operation.get("operation") not in SUPPORTED_OPERATIONS:
        return "excluded"
    if operation.get("conflicts") or operation.get("blockers"):
        return "excluded"
    if operation.get("destination_status") == "already_satisfied":
        return "skipped"
    return "pending"


def _operation_reason(operation, status) -> str | None:
    if status == "excluded":
        return "; ".join([*(operation.get("conflicts") or []), *(operation.get("blockers") or [])]) or "Excluded from execution."
    if status == "skipped":
        return "Already satisfied"
    return None


def _active_job(workspace: Workspace):
    connection = workspace.connect()
    try:
        return connection.execute("SELECT id, kind, status FROM job WHERE status IN ('pending', 'running') ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        connection.close()


def _cancel_requested(workspace, execution_id) -> bool:
    connection = workspace.connect()
    try:
        row = connection.execute("SELECT cancel_requested FROM file_management_execution WHERE id = ?", (execution_id,)).fetchone()
        return bool(row and row[0])
    finally:
        connection.close()


def _available_space(workspace):
    try:
        return shutil.disk_usage(workspace.root).free
    except OSError:
        return None


def _ensure_operation_space(workspace, required: int) -> None:
    available = _available_space(workspace)
    if available is not None and required > available:
        raise OperationFailure(f"Insufficient free space: {required} bytes required, {available} available.")


def _workspace_key(workspace):
    return str(workspace.root.resolve(strict=False)).casefold()


def _wait_for_worker(workspace: Workspace, execution_id: str) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        with _workers_lock:
            worker = _workers.get(_workspace_key(workspace))
        if worker is None or not worker.thread.is_alive() or worker.execution_id != execution_id:
            return
        worker.thread.join(timeout=0.02)


def _elapsed(execution) -> float:
    if not execution["started_at"]:
        return 0.0
    start = _parse_timestamp(execution["started_at"])
    end = _parse_timestamp(execution["finished_at"]) if execution["finished_at"] else datetime.now(timezone.utc)
    return max(0.0, (end - start).total_seconds())


def _parse_timestamp(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _validate_windows_segment(segment: str) -> None:
    if any(ord(character) < 32 or character in '<>:"|?*' for character in segment):
        raise OperationFailure("Destination contains characters that are invalid on Windows.")
    if segment.endswith((" ", ".")):
        raise OperationFailure("Destination segments may not end with a space or period.")
    device = segment.split(".", 1)[0].casefold()
    if device in {"con", "prn", "aux", "nul"} or (device.startswith("com") and device[3:].isdigit() and 1 <= int(device[3:]) <= 9) or (device.startswith("lpt") and device[3:].isdigit() and 1 <= int(device[3:]) <= 9):
        raise OperationFailure("Destination uses a reserved Windows device name.")
