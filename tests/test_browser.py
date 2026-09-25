from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from archive_index.api.server import _browser_assets, _asset_detail, _folders, _set_user_decision, _search_status
from archive_index.embeddings.search import SearchResult, search_text, clear_search_sessions, _database_fingerprint, _text_vectors
from archive_index.indexing.scanner import scan
from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.grouping import extract_visual_features, build_groups
from archive_index.indexing.recommendation import build_recommendations
from archive_index.indexing.embeddings import index_embeddings
from archive_index.jobs.engine import report_substage, SUBSTAGES
from archive_index.media.quality_provider import OffQualityProvider
from archive_index.workspace import Workspace
from test_embeddings import FakeProvider


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "nested" / "child").mkdir(parents=True)
        for name, size in [("filename.jpg", (80,40)), ("nested/other.jpg", (40,80)), ("nested/child/third.jpg", (60,40))]:
            Image.new("RGB", size, "red").save(root / name)
        self.workspace = Workspace.create(root)
        self.workspace.apply_configuration({**self.workspace.configuration(), "semantic_search_enabled": False})
        scan(self.workspace)
        index_workspace(self.workspace, components=("metadata", "thumbnail"), quality_provider=OffQualityProvider())
        self.items = _browser_assets(self.workspace, {}, "test")["items"]
        self.ids = {item["filename"]: item["asset_id"] for item in self.items}

    def browser(self, **params):
        return _browser_assets(self.workspace, {key:[str(value)] for key,value in params.items()}, "test")

    def test_merge_priority_threshold_and_no_duplicate_assets(self):
        scores = [SearchResult(self.ids["other.jpg"], .9), SearchResult(self.ids["filename.jpg"], .01), SearchResult(self.ids["third.jpg"], .3)]
        with patch("archive_index.api.server._search_status", return_value={"state":"ready","message":"Ready"}), patch("archive_index.api.server.search_text", return_value=scores) as search:
            data = self.browser(q="filename", threshold=.5, sort_by="search")
            self.assertEqual([i["filename"] for i in data["items"]], ["filename.jpg", "other.jpg"])
            self.assertEqual(data["items"][0]["similarity"], .01)
            self.assertTrue(data["items"][0]["filename_match"])
            window = self.browser(q="filename", threshold=.5, sort_by="search", offset=1, limit=1)
            self.assertEqual(window["items"][0]["filename"], "other.jpg")
            self.assertEqual(search.call_count, 1)
            self.assertEqual(len({i["asset_id"] for i in data["items"]}), data["total"])

    def test_filename_without_model_has_no_fabricated_similarity(self):
        result = self.browser(q="filename")
        self.assertEqual(result["total"], 1)
        self.assertIsNone(result["items"][0]["similarity"])
        self.assertEqual(result["search"]["state"], "unavailable")

    def test_filename_sort_is_lexical_in_gallery(self):
        ascending = self.browser(sort_by="filename", direction="asc")
        descending = self.browser(sort_by="filename", direction="desc")
        self.assertEqual(
            [item["filename"] for item in ascending["items"]],
            ["filename.jpg", "other.jpg", "third.jpg"],
        )
        self.assertEqual(
            [item["filename"] for item in descending["items"]],
            ["third.jpg", "other.jpg", "filename.jpg"],
        )

    def test_legacy_workspace_without_embeddings_renders_indexed_assets(self):
        self.assertFalse(self.workspace.semantic_search_enabled())
        self.assertIsNone(_database_fingerprint(self.workspace)[1])
        data = self.browser(semantic=0)
        self.assertEqual(data["total"], 3)
        self.assertEqual({item["filename"] for item in data["items"]}, {"filename.jpg", "other.jpg", "third.jpg"})

    def test_folders_layout_windows_and_timestamp_contract(self):
        self.assertEqual(self.browser(folders=json.dumps([]))["total"], 0)
        nested = self.browser(folders=json.dumps(["nested", "nested/child"]))
        self.assertEqual(nested["total"], 2)
        horizontal = self.browser(layout="horizontal")
        vertical = self.browser(layout="vertical")
        self.assertEqual(vertical["items"][0]["filename"], "other.jpg")
        self.assertEqual({item["filename"] for item in horizontal["items"]}, {"filename.jpg", "third.jpg"})
        self.assertEqual({item["filename"] for item in vertical["items"]}, {"other.jpg"})
        first = self.browser(limit=1)
        second = self.browser(limit=1, offset=1)
        self.assertNotEqual(first["items"][0]["asset_id"], second["items"][0]["asset_id"])
        self.assertIn("capture_time", first["items"][0])
        self.assertIn("width", first["items"][0])

    def test_folder_selection_is_exact_and_root_is_not_synthetic_select_all(self):
        self.assertIn("", _folders(self.workspace))
        cases = {
            "root": ({""}, {"filename.jpg"}),
            "parent": ({"nested"}, {"other.jpg"}),
            "child": ({"nested/child"}, {"third.jpg"}),
            "parent_child": ({"nested", "nested/child"}, {"other.jpg", "third.jpg"}),
            "all": (None, {"filename.jpg", "other.jpg", "third.jpg"}),
            "none": (set(), set()),
        }
        for name, (folders, expected) in cases.items():
            value = None if folders is None else json.dumps(sorted(folders))
            data = self.browser(folders=value) if value is not None else self.browser()
            self.assertEqual({item["filename"] for item in data["items"]}, expected, name)

    def test_layout_applies_exif_orientation_to_existing_cached_metadata(self):
        asset_id = self.ids["filename.jpg"]
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE physical_file SET width = 2832, height = 4240, metadata_json = ?, updated_at = '2026-09-17T00:00:00+00:00' WHERE logical_asset_id = ?",
                (json.dumps({"exif": {"Orientation": 8}}), asset_id),
            )
        self.assertIn("filename.jpg", {item["filename"] for item in self.browser(layout="vertical")["items"]})
        self.assertNotIn("filename.jpg", {item["filename"] for item in self.browser(layout="horizontal")["items"]})

    def test_persisted_portrait_dimensions_are_not_rotated_again(self):
        asset_id = self.ids["filename.jpg"]
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE physical_file SET width = 3060, height = 4080, metadata_json = ? WHERE logical_asset_id = ?",
                (json.dumps({"exif": {"Orientation": 6}}), asset_id),
            )
        self.assertIn("filename.jpg", {item["filename"] for item in self.browser(layout="vertical")["items"]})
        self.assertNotIn("filename.jpg", {item["filename"] for item in self.browser(layout="horizontal")["items"]})
        self.assertIn("third.jpg", {item["filename"] for item in self.browser(layout="horizontal")["items"]})
        self.assertIn("filename.jpg", {item["filename"] for item in self.browser(layout="vertical")["items"]})

    def test_layout_uses_rendered_dimensions_for_a_reconciled_raw_jpeg_asset(self):
        (self.workspace.root / "portrait.arw").write_bytes(b"raw")
        scan(self.workspace)
        with self.workspace.transaction() as connection:
            jpeg_id = connection.execute(
                "SELECT logical_asset_id FROM physical_file WHERE relative_path = 'nested/other.jpg'"
            ).fetchone()[0]
            connection.execute(
                "UPDATE physical_file SET logical_asset_id = ?, width = 240, height = 120 WHERE relative_path = 'portrait.arw'",
                (jpeg_id,),
            )
            connection.execute(
                "UPDATE physical_file SET width = 120, height = 240 WHERE relative_path = 'nested/other.jpg'"
            )
        self.assertIn("other.jpg", {item["filename"] for item in self.browser(layout="vertical")["items"]})
        self.assertNotIn("other.jpg", {item["filename"] for item in self.browser(layout="horizontal")["items"]})

    def test_manual_decisions_survive_filters_and_threshold_configuration(self):
        asset_id = self.ids["filename.jpg"]
        for decision in ("selected", "undecided", "rejected", "undecided", "selected", "rejected"):
            _set_user_decision(self.workspace, asset_id, decision)
            self.assertEqual(_asset_detail(self.workspace, asset_id, "test")["user_decision"], decision)
        config = self.workspace.configuration()
        config["recommendation_threshold"] = .95
        self.workspace.apply_configuration(config)
        self.browser(q="filename", manual="rejected", threshold=.99)
        self.assertEqual(_asset_detail(self.workspace, asset_id, "test")["user_decision"], "rejected")
        self.assertEqual(Workspace.open(self.workspace.root).configuration()["recommendation_threshold"], .95)

    def test_manual_decision_keeps_browser_catalog_warm_for_fullscreen_navigation(self):
        initial = self.browser(limit=1)
        asset_id = initial["items"][0]["asset_id"]
        with patch("archive_index.api.server._browser_catalog", side_effect=AssertionError("catalog must be reused")):
            _set_user_decision(self.workspace, asset_id, "selected")
            selected = self.browser(manual="selected", limit=1)
        self.assertEqual(selected["total"], 1)
        self.assertEqual(selected["items"][0]["asset_id"], asset_id)

    def test_recommendations_derive_from_group_representative_and_threshold(self):
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE logical_asset SET capture_time = '2026-09-03T12:00:00', capture_time_kind = 'exif_local_unknown'")
            connection.execute("UPDATE physical_file SET quality_score = 0.8")
        extract_visual_features(self.workspace)
        build_groups(self.workspace)
        build_recommendations(self.workspace)
        data = self.browser(auto="recommended")
        self.assertGreater(data["total"], 0)
        self.assertEqual(len({i["current_group_id"] for i in data["items"]}), data["total"])
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE workspace_config SET recommendation_threshold = 0.9")
        self.assertEqual(self.browser(auto="recommended")["total"], 0)

    def test_video_singletons_and_shared_filters(self):
        (self.workspace.root / "video.mp4").write_bytes(b"fixture")
        scan(self.workspace)
        data = self.browser(view="groups", media_type="video")
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["groups"], [])

    def test_group_labels_include_stable_ordinals(self):
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE logical_asset SET capture_time = '2026-09-03T12:00:00', capture_time_kind = 'exif_local_unknown'")
        extract_visual_features(self.workspace)
        build_groups(self.workspace)
        groups = self.browser(view="groups")['groups']
        self.assertTrue(groups)
        self.assertTrue(all(group["label"].startswith("Group ") for group in groups))

    def test_group_auto_review_qualifies_full_strict_groups(self):
        with self.workspace.transaction() as connection:
            connection.execute("UPDATE logical_asset SET capture_time = '2026-09-03T12:00:00', capture_time_kind = 'exif_local_unknown'")
            connection.execute("UPDATE physical_file SET quality_score = 0.8")
        extract_visual_features(self.workspace)
        build_groups(self.workspace)
        build_recommendations(self.workspace)

        representative_groups = self.browser(view="groups", auto="representatives")["groups"]
        recommended_groups = self.browser(view="groups", auto="recommended")["groups"]
        self.assertTrue(representative_groups)
        self.assertTrue(recommended_groups)
        for groups in (representative_groups, recommended_groups):
            self.assertTrue(all(len(group["members"]) == group["member_count"] >= 2 for group in groups))
            self.assertTrue(all(any(member["is_representative"] for member in group["members"]) for group in groups))
        self.assertEqual(
            {group["group_id"] for group in representative_groups},
            {group["group_id"] for group in recommended_groups},
        )

    def test_query_vector_and_ranking_reuse(self):
        config = self.workspace.configuration(); config["semantic_search_enabled"] = True
        self.workspace.apply_configuration(config)
        provider = FakeProvider()
        index_embeddings(self.workspace, provider=provider)
        clear_search_sessions(); _text_vectors.clear()
        self.addCleanup(clear_search_sessions)
        with patch("archive_index.embeddings.search.create_embedding_provider", return_value=provider) as create, patch.object(provider, "encode_text", wraps=provider.encode_text) as encode:
            first = search_text(self.workspace, "query", top_k=1)
            second = search_text(self.workspace, "query", top_k=3)
            self.assertEqual(first, second[:1])
            self.assertEqual(encode.call_count, 1)
            self.assertEqual(create.call_count, 1)
            search_text(self.workspace, "different", top_k=3)
            self.assertEqual(create.call_count, 1)

    def test_missing_model_and_missing_embeddings_states(self):
        config=self.workspace.configuration();config["semantic_search_enabled"]=True
        self.workspace.apply_configuration(config)
        with patch("archive_index.api.server.model_status", return_value={"installed":False}):
            self.assertEqual(_search_status(self.workspace)["state"], "missing_model")
        with patch("archive_index.api.server.model_status", return_value={"installed":True}), patch("archive_index.api.server.importlib.util.find_spec", return_value=object()):
            self.assertEqual(_search_status(self.workspace)["state"], "missing_embeddings")

    def test_substage_does_not_fabricate_rate(self):
        report_substage("test-job", "Extracting frames", 0, 32, "video.mp4")
        self.assertIsNone(SUBSTAGES["test-job"]["rate"])
        self.assertIsNone(SUBSTAGES["test-job"]["eta"])
        self.assertEqual(SUBSTAGES["test-job"]["total"], 32)
