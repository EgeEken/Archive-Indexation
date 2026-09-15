"""Workspace lifecycle and path-boundary rules."""

from __future__ import annotations

import sqlite3
import stat
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import __version__
from .configuration import configuration_from_connection, default_configuration, save_configuration
from .db.connection import connect
from .db.schema import apply_migrations

INDEX_DIRECTORY = ".archive-index"
DATABASE_FILENAME = "index.sqlite"
QUALITY_PROVIDERS = frozenset({"off", "lar-iqa"})


class WorkspaceError(ValueError):
    """Raised when a workspace cannot be safely opened or addressed."""


def compact_database(workspace: "Workspace") -> dict[str, int | str]:
    """Checkpoint and compact a workspace database when no job is active."""

    connection = workspace.connect()
    try:
        active = connection.execute(
            "SELECT id, kind, status FROM job WHERE status IN ('pending', 'running') ORDER BY created_at LIMIT 1"
        ).fetchone()
        if active is not None:
            raise WorkspaceError(
                f"cannot compact while job {active['id']} is {active['status']}"
            )

        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before_page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        before_freelist_count = connection.execute("PRAGMA freelist_count").fetchone()[0]
        before_size_bytes = workspace.database_path.stat().st_size
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        after_page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        after_freelist_count = connection.execute("PRAGMA freelist_count").fetchone()[0]
        after_size_bytes = workspace.database_path.stat().st_size
        return {
            "before_size_bytes": before_size_bytes,
            "after_size_bytes": after_size_bytes,
            "before_page_count": before_page_count,
            "after_page_count": after_page_count,
            "before_freelist_count": before_freelist_count,
            "after_freelist_count": after_freelist_count,
            "integrity_check": str(integrity),
        }
    finally:
        connection.close()


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.index_directory = root / INDEX_DIRECTORY
        self.database_path = self.index_directory / DATABASE_FILENAME

    @classmethod
    def create(cls, root: str | Path) -> "Workspace":
        workspace_root = cls._normalize_root(root)
        workspace_root.mkdir(parents=True, exist_ok=True)
        index_directory = workspace_root / INDEX_DIRECTORY
        if index_directory.exists():
            raise WorkspaceError(f"workspace already exists: {workspace_root}")

        index_directory.mkdir()
        for directory in ("thumbnails", "logs", "tmp"):
            (index_directory / directory).mkdir()

        workspace = cls(workspace_root)
        connection = connect(workspace.database_path)
        try:
            apply_migrations(connection)
            now = _timestamp()
            connection.execute(
                """
                INSERT INTO workspace_info(
                    id, workspace_id, created_at, updated_at, app_version, quality_provider
                ) VALUES (1, ?, ?, ?, ?, 'lar-iqa')
                """,
                (str(uuid.uuid4()), now, now, __version__),
            )
            save_configuration(connection, default_configuration(), workspace_root)
            connection.commit()
        finally:
            connection.close()
        return workspace

    @classmethod
    def open(cls, root: str | Path) -> "Workspace":
        workspace_root = cls._normalize_root(root)
        workspace = cls(workspace_root)
        if not workspace.index_directory.is_dir():
            raise WorkspaceError(f"not an archive workspace: {workspace_root}")
        if _is_reparse_point(workspace.index_directory):
            raise WorkspaceError("the application index cannot be a symlink or reparse point")
        if not workspace.database_path.is_file():
            raise WorkspaceError(f"workspace database is missing: {workspace.database_path}")

        connection = workspace.connect()
        try:
            apply_migrations(connection)
            if connection.execute("SELECT 1 FROM workspace_info WHERE id = 1").fetchone() is None:
                raise WorkspaceError("workspace metadata is missing")
        finally:
            connection.close()
        return workspace

    def connect(self) -> sqlite3.Connection:
        return connect(self.database_path)

    def quality_provider(self) -> str:
        return self.rendered_quality_provider()

    def rendered_quality_provider(self) -> str:
        connection = self.connect()
        try:
            value = configuration_from_connection(connection)["rendered_quality_provider"]
        finally:
            connection.close()
        return value

    def raw_quality_provider(self) -> str:
        connection = self.connect()
        try:
            value = configuration_from_connection(connection)["raw_quality_provider"]
        finally:
            connection.close()
        return value

    def video_quality_enabled(self) -> bool:
        connection = self.connect()
        try:
            value = configuration_from_connection(connection)["video_quality_enabled"]
        finally:
            connection.close()
        return bool(value)

    def set_quality_provider(self, provider: str) -> None:
        if provider not in QUALITY_PROVIDERS:
            raise WorkspaceError(f"unsupported quality provider: {provider}")
        with self.transaction() as connection:
            active = connection.execute(
                "SELECT id FROM job WHERE status IN ('pending', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise WorkspaceError("quality provider cannot change while a job is running")
            config = configuration_from_connection(connection)
            config["rendered_quality_provider"] = provider
            config["quality_provider"] = provider
            save_configuration(connection, config, self.root)
            connection.execute(
                "UPDATE workspace_recommendation SET active_run_id = NULL, updated_at = ? WHERE id = 1",
                (_timestamp(),),
            )

    def configuration(self) -> dict[str, object]:
        connection = self.connect()
        try:
            return configuration_from_connection(connection)
        finally:
            connection.close()

    def apply_configuration(self, value: dict[str, object]) -> dict[str, object]:
        from .configuration import path_in_scope

        with self.transaction() as connection:
            active = connection.execute(
                "SELECT id FROM job WHERE status IN ('pending', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                raise WorkspaceError("workspace configuration cannot change while a job is running")
            previous = configuration_from_connection(connection)
            config = save_configuration(connection, value, self.root)
            rows = connection.execute("SELECT id, relative_path FROM physical_file").fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE physical_file SET in_scope = ? WHERE id = ?",
                    (int(path_in_scope(row["relative_path"], config)), row["id"]),
                )
            connection.execute(
                "UPDATE workspace_info SET updated_at = ? WHERE id = 1",
                (_timestamp(),),
            )
            scope_changed = any(
                previous[name] != config[name]
                for name in (
                    "include_rendered_images",
                    "include_raw",
                    "include_videos",
                    "image_extensions",
                    "video_extensions",
                    "folder_rules",
                )
            )
            quality_changed = any(
                previous[name] != config[name]
                for name in ("rendered_quality_provider", "raw_quality_provider")
            )
            if scope_changed:
                connection.execute(
                    "UPDATE workspace_grouping SET active_run_id = NULL, updated_at = ? WHERE id = 1",
                    (_timestamp(),),
                )
            if scope_changed or quality_changed:
                connection.execute(
                    "UPDATE workspace_recommendation SET active_run_id = NULL, updated_at = ? WHERE id = 1",
                    (_timestamp(),),
                )
            return config

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def relative_path(self, path: str | Path) -> str:
        resolved = Path(path).resolve(strict=False)
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as error:
            raise WorkspaceError(f"path is outside workspace: {path}") from error
        if relative.parts and relative.parts[0].casefold() == INDEX_DIRECTORY.casefold():
            raise WorkspaceError(".archive-index is reserved for application state")
        return relative.as_posix()

    def absolute_path(self, relative_path: str) -> Path:
        candidate = (self.root / Path(relative_path)).resolve(strict=False)
        return_path = self.relative_path(candidate)
        if return_path != Path(relative_path).as_posix():
            raise WorkspaceError(f"invalid workspace-relative path: {relative_path}")
        return candidate

    def index_relative_path(self, path: str | Path) -> str:
        resolved = Path(path).resolve(strict=False)
        try:
            relative = resolved.relative_to(self.index_directory.resolve(strict=False))
        except ValueError as error:
            raise WorkspaceError(f"path is outside application state: {path}") from error
        return relative.as_posix()

    def index_path(self, relative_path: str) -> Path:
        candidate = (self.index_directory / Path(relative_path)).resolve(strict=False)
        if self.index_relative_path(candidate) != Path(relative_path).as_posix():
            raise WorkspaceError(f"invalid application-relative path: {relative_path}")
        return candidate

    def is_index_path(self, path: str | Path) -> bool:
        resolved = Path(path).resolve(strict=False)
        try:
            resolved.relative_to(self.index_directory.resolve(strict=False))
        except ValueError:
            return False
        return True

    @staticmethod
    def _normalize_root(root: str | Path) -> Path:
        path = Path(root).expanduser().resolve(strict=False)
        if path.exists() and not path.is_dir():
            raise WorkspaceError(f"workspace root is not a directory: {path}")
        return path


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
