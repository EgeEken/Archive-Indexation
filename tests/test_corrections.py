from __future__ import annotations

import json
from contextlib import closing
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen

from archive_index.api import server
from archive_index.embeddings import search
from archive_index.indexing.embeddings import index_embeddings
from archive_index.indexing.reconciliation import reconcile_workspace
from archive_index.indexing.scanner import scan
import test_browser
from test_embeddings import FakeProvider


class CorrectionTests(unittest.TestCase):
    setUp = test_browser.BrowserTests.setUp
    browser = test_browser.BrowserTests.browser

    def enable_embeddings(self):
        config = self.workspace.configuration()
        config["semantic_search_enabled"] = True
        self.workspace.apply_configuration(config)
        provider = FakeProvider()
        index_embeddings(self.workspace, provider=provider)
        return provider

    def test_planner_cached_totals_and_changed_source(self):
        provider = self.enable_embeddings()
        with patch("archive_index.embeddings.providers.create_embedding_provider", return_value=provider):
            plan = server._embedding_plan(self.workspace.configuration(), self.workspace)
            self.assertEqual((plan["embedding_image_count"], plan["embedding_total_vectors"], plan["embedding_pending_count"], plan["embedding_cached_count"]), (3,3,0,3))
            self.assertEqual(plan["embedding_estimated_storage_bytes"], 0)
            with self.workspace.transaction() as c:
                c.execute("UPDATE component_state SET status='pending' WHERE physical_file_id=(SELECT id FROM physical_file LIMIT 1) AND component LIKE 'embedding:%'")
            plan = server._embedding_plan(self.workspace.configuration(), self.workspace)
            self.assertEqual((plan["embedding_pending_count"],plan["embedding_cached_count"]), (1,2))
            self.assertEqual(plan["embedding_estimated_storage_bytes"], 6)

    def test_planner_fresh_estimate_does_not_invent_video_durations(self):
        plan = server._embedding_plan(self.workspace.configuration(), filesystem_plan={"selected_categories":{"jpeg":4,"video":2}})
        self.assertEqual((plan["embedding_total_vectors"],plan["embedding_pending_count"],plan["embedding_cached_count"]),(4,4,0))
        self.assertEqual(plan["embedding_unknown_videos"],2)
        self.assertGreater(plan["embedding_estimated_seconds"],0)

    def test_plan_endpoint_populates_embedding_counts(self):
        provider = self.enable_embeddings()
        app = server.WorkspaceHTTPServer(("127.0.0.1",0),self.workspace,registry_path=self.workspace.root / "registry.json")
        self.addCleanup(app.server_close)
        with patch("archive_index.embeddings.providers.create_embedding_provider",return_value=provider):
            payload=app.plan_workspace_configuration(str(self.workspace.root),self.workspace.configuration())
        self.assertEqual(payload["plan"]["embedding_total_vectors"],3)
        self.assertEqual(payload["plan"]["embedding_cached_count"],3)

    def test_reconciled_pair_stays_reconciled_and_compatible_decision_survives(self):
        (self.workspace.root / "filename.ARW").write_bytes(b"raw")
        scan(self.workspace)
        with self.workspace.transaction() as c:
            c.execute("UPDATE logical_asset SET selection_state='selected' WHERE id=?",(self.ids["filename.jpg"],))
        reconcile_workspace(self.workspace)
        (self.workspace.root / "new.jpg").write_bytes(b"new")
        scan(self.workspace)
        result=reconcile_workspace(self.workspace)
        self.assertEqual(result.conflicts,0)
        with closing(self.workspace.connect()) as c:
            rows=c.execute("SELECT p.logical_asset_id,l.selection_state FROM physical_file p JOIN logical_asset l ON l.id=p.logical_asset_id WHERE lower(p.filename) LIKE 'filename.%'").fetchall()
            self.assertEqual(len({r[0] for r in rows}),1)
            self.assertEqual({r[1] for r in rows},{"selected"})
            self.assertEqual(c.execute("SELECT COUNT(*) FROM physical_relationship WHERE relationship_type='raw_jpeg'").fetchone()[0],1)
        self.assertFalse([p for p in server._problems(self.workspace,{}) if p["job_kind"]=="reconciliation"])

    def test_historical_self_conflict_excluded_active_manual_conflict_retained(self):
        (self.workspace.root / "filename.ARW").write_bytes(b"raw")
        scan(self.workspace)
        with self.workspace.transaction() as c:
            raw=c.execute("SELECT logical_asset_id FROM physical_file WHERE extension='.arw'").fetchone()[0]
            c.execute("UPDATE logical_asset SET selection_state='selected' WHERE id=?",(raw,))
            c.execute("UPDATE logical_asset SET selection_state='rejected' WHERE id=?",(self.ids["filename.jpg"],))
        result=reconcile_workspace(self.workspace)
        self.assertEqual(result.conflicts,1)
        with self.workspace.transaction() as c:
            c.execute("INSERT INTO reconciliation_conflict(run_id,left_logical_asset_id,right_logical_asset_id,conflict_type,message,evidence_json,created_at) VALUES(?,?,?,'ambiguous_raw_jpeg','old incorrect message','{}','2026-01-01')",(result.run_id,raw,raw))
        active=[p for p in server._problems(self.workspace,{}) if p["job_kind"]=="reconciliation"]
        self.assertEqual(len(active),1)
        self.assertEqual(active[0]["error_type"],"manual_decision_conflict")
        with self.workspace.transaction() as c:
            c.execute("UPDATE logical_asset SET selection_state='selected' WHERE id=?",(self.ids["filename.jpg"],))
        self.assertFalse([p for p in server._problems(self.workspace,{}) if p["job_kind"]=="reconciliation"])
        reconcile_workspace(self.workspace)
        self.assertEqual(len({i["asset_id"] for i in self.browser(q="filename")["items"]}),1)

    def test_provider_loading_searching_results_and_single_initialization(self):
        provider=self.enable_embeddings()
        search.clear_search_sessions(); search._text_vectors.clear()
        self.addCleanup(search.clear_search_sessions)
        load=threading.Event(); encode=threading.Event()
        self.addCleanup(load.set); self.addCleanup(encode.set)
        original=provider.encode_text
        with patch("archive_index.embeddings.search.create_embedding_provider",return_value=provider) as create, patch.object(provider,"preflight",side_effect=lambda:load.wait(5)), patch.object(provider,"encode_text",side_effect=lambda text:(encode.wait(5),original(text))[1]):
            first=search.prepare_provider(provider.provider_id)
            self.assertIs(first,search.prepare_provider(provider.provider_id))
            self.assertEqual(search.provider_state(provider.provider_id),"loading")
            results,phase=search.request_text(self.workspace,"query",allowed_asset_ids=set(self.ids.values()))
            self.assertEqual((results,phase),(None,"loading"))
            load.set();first.result(timeout=5)
            self.assertEqual(search.provider_state(provider.provider_id),"ready")
            results,phase=search.request_text(self.workspace,"query",allowed_asset_ids=set(self.ids.values()))
            self.assertEqual((results,phase),(None,"searching"))
            encode.set()
            for _ in range(100):
                results,phase=search.request_text(self.workspace,"query",allowed_asset_ids=set(self.ids.values()))
                if phase=="complete":break
                time.sleep(.01)
            self.assertEqual(phase,"complete");self.assertEqual(len(results),3)
            self.assertEqual(create.call_count,1)
            self.assertFalse(hasattr(search, "_eviction_timer"))
            self.assertIs(first, search.prepare_provider(provider.provider_id))

    def test_background_load_does_not_block_navigation_http(self):
        provider=self.enable_embeddings();search.clear_search_sessions()
        self.addCleanup(search.clear_search_sessions)
        gate=threading.Event();self.addCleanup(gate.set)
        app=server.WorkspaceHTTPServer(("127.0.0.1",0),self.workspace,registry_path=self.workspace.root / "registry.json")
        thread=threading.Thread(target=app.serve_forever,daemon=True);thread.start()
        self.addCleanup(app.server_close);self.addCleanup(app.shutdown)
        with patch("archive_index.api.server._embedding_model_status",return_value={}), patch("archive_index.api.server.model_status",return_value={"installed":True}), patch("archive_index.embeddings.search.create_embedding_provider",return_value=provider),patch.object(provider,"preflight",side_effect=lambda:gate.wait(5)):
            base=f"http://127.0.0.1:{app.server_port}"
            with urlopen(base+"/api/workspace",timeout=2) as r: json.load(r)
            self.assertEqual(search.provider_state(provider.provider_id),"loading")
            with urlopen(base+"/api/browser?q=cold&async=1",timeout=2) as r: data=json.load(r)
            self.assertEqual(data["search"]["state"],"loading")
            with urlopen(base+"/api/browser?view=groups",timeout=2) as r:data=json.load(r)
            self.assertEqual(data["total"],3)
            self.assertFalse(gate.is_set())
            gate.set();search.prepare_provider(provider.provider_id).result(timeout=5)

    def test_search_failure_is_reported_and_filename_survives(self):
        self.enable_embeddings()
        with patch("archive_index.api.server._search_status",return_value={"state":"available","provider":"OpenCLIP","message":"Available"}),patch("archive_index.api.server.request_text",side_effect=RuntimeError("checkpoint unreadable")):
            data=self.browser(q="filename",**{"async":1})
        self.assertEqual(data["search"]["state"],"failed")
        self.assertIn("checkpoint unreadable",data["search"]["message"])
        self.assertEqual(data["total"],1)

    def test_cached_group_locate_reuses_catalog(self):
        with patch("archive_index.api.server._assets", wraps=server._assets) as summaries:
            self.browser(q="file",semantic=0)
            count=summaries.call_count
            self.browser(q="file",semantic=0,view="groups",group_id="target")
            self.assertEqual(summaries.call_count,count)

    def test_install_compatible_and_stale_embeddings_never_indexes(self):
        provider=self.enable_embeddings()
        app=server.WorkspaceHTTPServer(("127.0.0.1",0),self.workspace,registry_path=self.workspace.root / "registry.json")
        threading.Thread(target=app.serve_forever,daemon=True).start()
        self.addCleanup(app.server_close);self.addCleanup(app.shutdown)
        with patch("archive_index.embeddings.models.install_model"),patch("archive_index.embeddings.providers.create_embedding_provider",return_value=provider),patch.object(app,"start_indexing") as indexing:
            def install():
                request=Request(f"http://127.0.0.1:{app.server_port}/api/embedding-models/install",data=json.dumps({"provider":provider.provider_id}).encode(),headers={"Content-Type":"application/json"})
                with urlopen(request) as response:return json.load(response)
            self.assertTrue(install()["compatible_embeddings"])
            with self.workspace.transaction() as c:c.execute("UPDATE component_state SET status='pending' WHERE component LIKE 'embedding:%'")
            self.assertFalse(install()["compatible_embeddings"])
            indexing.assert_not_called()

    def test_planner_counts_cached_video_frames_not_video_assets(self):
        from PIL import Image
        from archive_index.indexing.video_quality import ExtractedVideoFrame
        provider=self.enable_embeddings()
        (self.workspace.root / "clip.mp4").write_bytes(b"video")
        scan(self.workspace)
        with self.workspace.transaction() as c:c.execute("UPDATE physical_file SET duration_seconds=1 WHERE media_type='video'")
        frames=[ExtractedVideoFrame(float(i),Image.new("RGB",(20,20))) for i in range(2)]
        with patch("archive_index.indexing.embeddings.extract_video_frames",return_value=frames):index_embeddings(self.workspace,provider=provider)
        with patch("archive_index.embeddings.providers.create_embedding_provider",return_value=provider):plan=server._embedding_plan(self.workspace.configuration(),self.workspace)
        self.assertEqual((plan["embedding_image_count"],plan["embedding_video_sample_count"],plan["embedding_total_vectors"],plan["embedding_cached_count"]),(3,2,5,5))
        self.assertEqual(plan["embedding_pending_count"],0)

    def test_ready_available_missing_and_failed_status(self):
        self.enable_embeddings()
        with patch("archive_index.api.server.model_status",return_value={"installed":True}):
            for phase,word in [("ready","ready"),("available","available"),("loading","Preparing")]:
                with patch("archive_index.api.server.provider_state",return_value=phase):
                    status=server._search_status(self.workspace)
                    self.assertEqual(status["state"],phase)
                    self.assertIn(word,status["message"])
        with patch("archive_index.api.server.model_status",return_value={"installed":False}):
            self.assertEqual(server._search_status(self.workspace)["state"],"missing_model")
        with self.workspace.transaction() as c:c.execute("UPDATE workspace_embedding SET active_run_id=NULL")
        with patch("archive_index.api.server.model_status",return_value={"installed":True}):
            self.assertEqual(server._search_status(self.workspace)["state"],"missing_embeddings")

    def test_ambiguous_candidates_with_null_side_remain_actionable(self):
        (self.workspace.root / "filename.ARW").write_bytes(b"raw-a")
        (self.workspace.root / "nested/filename.ARW").write_bytes(b"raw-b")
        scan(self.workspace);reconcile_workspace(self.workspace)
        active=[p for p in server._problems(self.workspace,{}) if p["job_kind"]=="reconciliation"]
        self.assertEqual(len(active),1)
        self.assertEqual(active[0]["error_type"],"ambiguous_raw_jpeg")
        self.assertIn("Multiple RAW/JPEG candidates",active[0]["message"])

    def test_provider_switch_and_disable_release_inflight_preload(self):
        from unittest.mock import Mock
        search.clear_search_sessions()
        self.addCleanup(search.clear_search_sessions)
        gate = threading.Event()
        entered = threading.Event()
        old = Mock(version="old", _torch=None, last_timings={})
        new = Mock(version="new", _torch=None, last_timings={})
        old.preflight.side_effect = lambda: (entered.set(), gate.wait(5))
        with patch.object(search, "create_embedding_provider", side_effect=[old, new]) as create:
            search.prepare_provider("old")
            self.assertTrue(entered.wait(2))
            replacement = search.select_provider("new")
            self.assertFalse(replacement.done())
            self.assertEqual(search.provider_state("old"), "available")
            gate.set()
            replacement.result(timeout=5)
            self.assertEqual(list(search._providers), [("new", "new")])
            self.assertIs(replacement, search.prepare_provider("new"))
            search.select_provider(None)
            search._worker.submit(lambda: None).result(timeout=5)
            self.assertFalse(search._providers)
            self.assertFalse(search._loads)
            self.assertEqual(create.call_count, 2)

    def test_preload_uses_only_enabled_installed_provider_without_embeddings(self):
        config = self.workspace.configuration()
        config["semantic_search_enabled"] = True
        self.workspace.apply_configuration(config)
        with patch.object(server, "select_provider") as select, patch.object(server, "model_status", return_value={"installed": True}), patch.object(server.importlib.util, "find_spec", return_value=object()):
            server._prepare_search(self.workspace)
            select.assert_called_with(config["embedding_provider"])
            with patch.object(server, "model_status", return_value={"installed": False}):
                server._prepare_search(self.workspace)
                select.assert_called_with(None)
            config["semantic_search_enabled"] = False
            self.workspace.apply_configuration(config)
            server._prepare_search(self.workspace)
            select.assert_called_with(None)
