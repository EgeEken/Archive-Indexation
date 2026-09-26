from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from archive_index.api.server import WorkspaceHTTPServer
from archive_index.file_management import build_dry_run_plan, save_ruleset
from archive_index.indexing.scanner import scan
from archive_index.workspace import Workspace


class Phase10BApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        (root / "photo.jpg").write_bytes(b"api fixture")
        self.workspace = Workspace.create(root)
        scan(self.workspace)
        self.ruleset = save_ruleset(
            self.workspace,
            name="API execution fixture",
            rules=[{"match": {"format": "jpeg"}, "action": {"operation": "copy", "destination_dir": "copies"}}],
        )
        self.server = WorkspaceHTTPServer(("127.0.0.1", 0), self.workspace, registry_path=Path(self.directory.name) / "registry.json")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.query = f"?workspace={self.server.default_handle}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.directory.cleanup()

    def _request(self, path: str, body: dict | None = None):
        request = Request(
            self.base + path + self.query,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method="POST" if body is not None else "GET",
        )
        with urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())

    def test_prepare_start_poll_and_cancel_routes_use_persisted_execution(self):
        plan = build_dry_run_plan(self.workspace, self.ruleset["id"])
        status, prepared = self._request(
            "/api/file-management/executions",
            {"ruleset_id": self.ruleset["id"], "plan_digest": plan["plan_digest"]},
        )
        self.assertEqual(status, 201)
        execution_id = prepared["execution"]["id"]
        status, started = self._request(f"/api/file-management/executions/{execution_id}/start", {})
        self.assertEqual(status, 202)
        self.assertIn(started["execution"]["status"], {"running", "completed"})
        for _ in range(200):
            _, current = self._request(f"/api/file-management/executions/{execution_id}")
            if current["execution"]["status"] not in {"running", "cancelling"}:
                break
            time.sleep(0.01)
        self.assertEqual(current["execution"]["status"], "completed")
        _, active = self._request("/api/file-management/executions/active")
        self.assertEqual(active["execution"]["id"], execution_id)
        self.assertTrue((self.workspace.root / "copies" / "photo.jpg").is_file())


if __name__ == "__main__":
    unittest.main()
