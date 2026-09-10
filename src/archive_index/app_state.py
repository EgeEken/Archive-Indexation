"""Small application-level convenience state for recent workspaces."""

from __future__ import annotations

import json
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(entries[:30], ensure_ascii=False, indent=2), encoding="utf-8")

    def remove(self, handle: str) -> bool:
        entries = self.entries()
        remaining = [entry for entry in entries if entry.get("id") != handle]
        if len(remaining) == len(entries):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(remaining, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def open(self, handle: str) -> Workspace:
        for entry in self.entries():
            if entry.get("id") == handle and isinstance(entry.get("path"), str):
                try:
                    workspace = Workspace.open(entry["path"])
                except (WorkspaceError, OSError) as error:
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
