"""Recursive, incremental workspace scanner."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from ..jobs.engine import JobStore
from ..workspace import INDEX_DIRECTORY, Workspace
from .media_types import media_type_for

LOGGER = logging.getLogger(__name__)
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ScanProgress:
    processed: int
    total: int
    relative_path: str
    stage: str = "scan"
    failed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class ScanResult:
    job_id: str
    discovered: int = 0
    added: int = 0
    unchanged: int = 0
    changed: int = 0
    moved: int = 0
    missing: int = 0
    errors: int = 0
    hashed: int = 0
    cancelled: bool = False


def scan(
    workspace: Workspace,
    *,
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[ScanProgress], None] | None = None,
) -> ScanResult:
    """Update one workspace from its current filesystem state."""

    job_store = JobStore(workspace)
    job_id = job_id or job_store.create("scan")
    job_store.start(job_id)
    connection = workspace.connect()
    counts = {
        "discovered": 0,
        "added": 0,
        "unchanged": 0,
        "changed": 0,
        "moved": 0,
        "missing": 0,
        "errors": 0,
        "hashed": 0,
    }
    try:
        job_store.set_stage(job_id, "discovery")
        paths, discovery_issues = _discover_files(workspace)
        counts["discovered"] = len(paths)
        job_store.set_total(job_id, len(paths))
        for relative_path, error in discovery_issues:
            counts["errors"] += 1
            job_store.record_error(job_id, error, relative_path=relative_path)
        existing_by_path = {
            row["relative_path"]: row
            for row in connection.execute("SELECT * FROM physical_file").fetchall()
        }
        job_store.set_stage(job_id, "hash and index")
        discovered_paths = {workspace.relative_path(path) for path in paths}
        blocked_paths = {relative_path for relative_path, _ in discovery_issues}
        seen_paths: set[str] = set()
        cancelled = False

        for processed, path in enumerate(paths, start=1):
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            relative_path = workspace.relative_path(path)
            seen_paths.add(relative_path)

            try:
                stat_result = path.stat()
                existing = existing_by_path.get(relative_path)
                if (
                    existing is not None
                    and existing["is_online"]
                    and existing["size_bytes"] == stat_result.st_size
                    and existing["mtime_ns"] == stat_result.st_mtime_ns
                ):
                    counts["unchanged"] += 1
                else:
                    file_hash = hash_file(path)
                    counts["hashed"] += 1
                    media_type = media_type_for(path)
                    if existing is not None:
                        _update_existing(
                            connection,
                            existing,
                            relative_path,
                            path,
                            media_type,
                            stat_result.st_size,
                            stat_result.st_mtime_ns,
                            file_hash,
                        )
                        counts["changed"] += 1
                    else:
                        moved = _find_missing_hash_match(
                            connection, discovered_paths, relative_path, file_hash
                        )
                        if moved is not None:
                            _update_existing(
                                connection,
                                moved,
                                relative_path,
                                path,
                                media_type,
                                stat_result.st_size,
                                stat_result.st_mtime_ns,
                                file_hash,
                            )
                            counts["moved"] += 1
                        else:
                            _insert_new(
                                connection,
                                relative_path,
                                path,
                                media_type,
                                stat_result.st_size,
                                stat_result.st_mtime_ns,
                                file_hash,
                            )
                            counts["added"] += 1
                connection.commit()
            except OSError as error:
                counts["errors"] += 1
                file_id = existing["id"] if existing is not None else None
                job_store.record_error(
                    job_id,
                    error,
                    physical_file_id=file_id,
                    relative_path=relative_path,
                )
                connection.commit()
                LOGGER.warning("could not index %s: %s", relative_path, error)

            if processed % 16 == 0:
                job_store.checkpoint(job_id, processed, counts["errors"])
            if progress is not None:
                progress(ScanProgress(processed, len(paths), relative_path, "scan", counts["errors"], 0))

        if not cancelled:
            missing_paths = set(existing_by_path) - seen_paths
            counts["missing"] = _mark_missing(connection, missing_paths, blocked_paths)
        if cancelled:
            job_store.cancel(job_id, len(seen_paths), counts["errors"])
        else:
            job_store.complete(job_id, len(seen_paths), counts["errors"])
        return ScanResult(job_id, cancelled=cancelled, **counts)
    except Exception:
        job_store.fail(job_id)
        raise
    finally:
        connection.close()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _discover_files(workspace: Workspace) -> tuple[list[Path], list[tuple[str, OSError]]]:
    paths: list[Path] = []
    issues: list[tuple[str, OSError]] = []
    pending_directories = [workspace.root]

    while pending_directories:
        directory = pending_directories.pop()
        try:
            with os.scandir(directory) as entries:
                entries = sorted(entries, key=lambda entry: entry.name.casefold(), reverse=True)
                for entry in entries:
                    if entry.name.casefold() == INDEX_DIRECTORY.casefold():
                        continue
                    entry_path = Path(entry.path)
                    try:
                        if _is_reparse_point(entry):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            pending_directories.append(entry_path)
                        elif entry.is_file(follow_symlinks=False) and media_type_for(entry_path):
                            paths.append(entry_path)
                    except OSError as error:
                        issues.append((workspace.relative_path(entry_path), error))
        except OSError as error:
            issues.append((workspace.relative_path(directory), error))

    paths.sort(key=lambda path: workspace.relative_path(path).casefold())
    return paths, issues


def _is_reparse_point(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _insert_new(
    connection,
    relative_path: str,
    path: Path,
    media_type: str,
    size_bytes: int,
    mtime_ns: int,
    file_hash: str,
) -> None:
    now = _timestamp()
    asset_id = str(uuid.uuid4())
    connection.execute(
        """
        INSERT INTO logical_asset(id, media_type, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (asset_id, media_type, now, now),
    )
    connection.execute(
        """
        INSERT INTO physical_file(
            id, logical_asset_id, relative_path, filename, extension, media_type,
            size_bytes, mtime_ns, sha256, is_online, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            asset_id,
            relative_path,
            path.name,
            path.suffix.casefold(),
            media_type,
            size_bytes,
            mtime_ns,
            file_hash,
            now,
            now,
        ),
    )


def _update_existing(
    connection,
    existing,
    relative_path: str,
    path: Path,
    media_type: str,
    size_bytes: int,
    mtime_ns: int,
    file_hash: str,
) -> None:
    changed_content = existing["sha256"] != file_hash
    now = _timestamp()
    connection.execute(
        """
        UPDATE physical_file
        SET relative_path = ?, filename = ?, extension = ?, media_type = ?,
            size_bytes = ?, mtime_ns = ?, sha256 = ?, is_online = 1, updated_at = ?
        WHERE id = ?
        """,
        (
            relative_path,
            path.name,
            path.suffix.casefold(),
            media_type,
            size_bytes,
            mtime_ns,
            file_hash,
            now,
            existing["id"],
        ),
    )
    if changed_content:
        connection.execute(
            """
            UPDATE component_state
            SET status = 'pending', input_fingerprint = NULL,
                started_at = NULL, completed_at = NULL, error_message = NULL
            WHERE physical_file_id = ?
            """,
            (existing["id"],),
        )


def _find_missing_hash_match(
    connection, discovered_paths: set[str], relative_path: str, file_hash: str
):
    rows = connection.execute(
        """
        SELECT * FROM physical_file
        WHERE sha256 = ? AND relative_path <> ?
        ORDER BY is_online DESC, updated_at DESC
        """,
        (file_hash, relative_path),
    ).fetchall()
    if any(row["relative_path"] in discovered_paths for row in rows):
        return None
    candidates = [row for row in rows if row["relative_path"] not in discovered_paths]
    return candidates[0] if len(candidates) == 1 else None


def _mark_missing(connection, paths: set[str], blocked_paths: set[str]) -> int:
    if not paths:
        return 0
    if "" in blocked_paths or "." in blocked_paths:
        return 0
    marked = 0
    for relative_path in paths:
        if any(
            relative_path == blocked_path
            or relative_path.startswith(f"{blocked_path}/")
            for blocked_path in blocked_paths
        ):
            continue
        cursor = connection.execute(
            "UPDATE physical_file SET is_online = 0, updated_at = ? WHERE relative_path = ? AND is_online = 1",
            (_timestamp(), relative_path),
        )
        marked += cursor.rowcount
    connection.commit()
    return marked


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
