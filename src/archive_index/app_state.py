"""Small application-level convenience state for recent workspaces."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .workspace import Workspace, WorkspaceError

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[2] / ".archive-index-workspaces.json"


class WorkspaceRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_REGISTRY_PATH

    def entries(self) -> list[dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    def add(self, workspace: Workspace) -> None:
        handle = workspace_id(workspace)
        entries = [entry for entry in self.entries() if entry.get("id") != handle]
        entries.insert(0, {"id": handle, "path": str(workspace.root)})
        self._write_entries(entries)

    def remove(self, handle: str) -> bool:
        entries = self.entries()
        remaining = [entry for entry in entries if entry.get("id") != handle]
        if len(remaining) == len(entries):
            return False
        self._write_entries(remaining)
        return True

    def _write_entries(self, entries: list[dict[str, object]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(entries, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def open(self, handle: str) -> Workspace:
        for entry in self.entries():
            if entry.get("id") == handle and isinstance(entry.get("path"), str):
                try:
                    workspace = Workspace.open(entry["path"])
                except (WorkspaceError, OSError, sqlite3.Error) as error:
                    raise WorkspaceError(f"workspace is unavailable: {entry['path']}") from error
                if workspace_id(workspace) != handle:
                    raise WorkspaceError("workspace identity does not match recent entry")
                return workspace
        raise WorkspaceError("workspace is not in the recent list")


def workspace_id(workspace: Workspace) -> str:
    connection = workspace.connect()
    try:
        row = connection.execute("SELECT workspace_id FROM workspace_info WHERE id = 1").fetchone()
    finally:
        connection.close()
    if row is None:
        raise WorkspaceError("workspace metadata is missing")
    return row[0]
