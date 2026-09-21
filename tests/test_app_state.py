from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from archive_index.app_state import DEFAULT_REGISTRY_PATH, WorkspaceRegistry, workspace_id
from archive_index.workspace import Workspace


class WorkspaceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.registry_path = root / "recent.json"
        self.workspaces: list[Workspace] = []

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _workspace(self, number: int) -> Workspace:
        root = Path(self.temporary_directory.name) / f"workspace-{number}"
        workspace = Workspace.create(root)
        self.workspaces.append(workspace)
        return workspace

    def test_registry_preserves_more_than_thirty_and_readding_moves_without_duplicates(self) -> None:
        registry = WorkspaceRegistry(self.registry_path)
        workspaces = [self._workspace(number) for number in range(35)]

        for workspace in workspaces:
            registry.add(workspace)

        entries = registry.entries()
        self.assertEqual(len(entries), 35)
        self.assertIn(workspace_id(workspaces[0]), {entry["id"] for entry in entries})

        registry.add(workspaces[10])
        entries = registry.entries()
        self.assertEqual(len(entries), 35)
        self.assertEqual(entries[0]["id"], workspace_id(workspaces[10]))
        self.assertEqual(sum(entry["id"] == workspace_id(workspaces[10]) for entry in entries), 1)
        self.assertEqual(list(self.registry_path.parent.glob(f".{self.registry_path.name}.*.tmp")), [])

    def test_unavailable_entry_remains_until_explicit_remove(self) -> None:
        registry = WorkspaceRegistry(self.registry_path)
        available = self._workspace(1)
        missing = {"id": "missing-workspace", "path": str(Path(self.temporary_directory.name) / "gone")}
        self.registry_path.write_text(json.dumps([missing]), encoding="utf-8")

        registry.add(available)
        ids = [entry["id"] for entry in registry.entries()]
        self.assertEqual(set(ids), {"missing-workspace", workspace_id(available)})
        self.assertTrue(registry.remove("missing-workspace"))
        self.assertEqual([entry["id"] for entry in registry.entries()], [workspace_id(available)])
        self.assertFalse(registry.remove("missing-workspace"))

    def test_explicit_registry_does_not_change_default_registry(self) -> None:
        before = DEFAULT_REGISTRY_PATH.read_bytes() if DEFAULT_REGISTRY_PATH.exists() else None
        registry = WorkspaceRegistry(self.registry_path)
        for number in range(35):
            registry.add(self._workspace(number))
        after = DEFAULT_REGISTRY_PATH.read_bytes() if DEFAULT_REGISTRY_PATH.exists() else None
        self.assertEqual(after, before)
