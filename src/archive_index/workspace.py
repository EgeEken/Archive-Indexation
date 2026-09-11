"""Workspace lifecycle and path-boundary rules."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import __version__
from .db.connection import connect
from .db.schema import apply_migrations

INDEX_DIRECTORY = ".archive-index"
DATABASE_FILENAME = "index.sqlite"


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
                    id, workspace_id, created_at, updated_at, app_version
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), now, now, __version__),
            )
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
