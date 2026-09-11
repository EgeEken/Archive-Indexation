from __future__ import annotations

import sqlite3
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from archive_index.db.schema import MIGRATIONS
from archive_index.jobs.engine import JobStore
from archive_index.workspace import Workspace, WorkspaceError, compact_database


class WorkspaceTests(unittest.TestCase):
    def test_create_and_reopen_preserves_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            workspace = Workspace.create(root)

            self.assertTrue((root / ".archive-index" / "index.sqlite").is_file())
            with closing(Workspace.open(root).connect()) as connection:
                info = connection.execute("SELECT * FROM workspace_info").fetchone()
                version = connection.execute("PRAGMA user_version").fetchone()[0]

            self.assertIsNotNone(info["workspace_id"])
            self.assertEqual(version, 7)
            self.assertEqual(workspace.root, root.resolve())

            with Workspace.open(root).connect() as connection:
                pragmas = {
                    name: connection.execute(f"PRAGMA {name}").fetchone()[0]
                    for name in ("foreign_keys", "journal_mode", "synchronous")
                }
            connection.close()
            self.assertEqual(pragmas["foreign_keys"], 1)
            self.assertEqual(str(pragmas["journal_mode"]).lower(), "wal")
            self.assertEqual(pragmas["synchronous"], 2)

            reopened = Workspace.open(root)
            reopened_again = Workspace.open(root)
            self.assertEqual(reopened.database_path, reopened_again.database_path)

    def test_relative_paths_survive_moving_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "archive"
            media = root / "photos" / "image.jpg"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"test image")
            workspace = Workspace.create(root)

            with workspace.transaction() as connection:
                connection.execute(
                    "INSERT INTO logical_asset(id, media_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    ("asset-1", "image", "now", "now"),
                )
                connection.execute(
                    """
                    INSERT INTO physical_file(
                        id, logical_asset_id, relative_path, filename, extension, media_type,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "file-1",
                        "asset-1",
                        workspace.relative_path(media),
                        media.name,
                        media.suffix.lower(),
                        "image",
                        "now",
                        "now",
                    ),
                )

            moved_root = temporary_root / "moved-archive"
            shutil.move(root, moved_root)
            moved_workspace = Workspace.open(moved_root)
            with closing(moved_workspace.connect()) as connection:
                relative_path = connection.execute(
                    "SELECT relative_path FROM physical_file WHERE id = 'file-1'"
                ).fetchone()[0]

            self.assertEqual(relative_path, "photos/image.jpg")
            self.assertEqual(moved_workspace.absolute_path(relative_path), moved_root / relative_path)
            self.assertEqual((moved_root / relative_path).read_bytes(), b"test image")

    def test_path_boundary_rejects_index_and_outside_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            workspace = Workspace.create(root)

            with self.assertRaises(WorkspaceError):
                workspace.relative_path(root / ".archive-index" / "index.sqlite")
            with self.assertRaises(WorkspaceError):
                workspace.relative_path(root / ".ARCHIVE-INDEX" / "index.sqlite")
            with self.assertRaises(WorkspaceError):
                workspace.relative_path(Path(temporary_directory) / "outside.jpg")
            with self.assertRaises(WorkspaceError):
                workspace.absolute_path("../outside.jpg")

            self.assertTrue(workspace.is_index_path(root / ".archive-index" / "index.sqlite"))
            self.assertFalse(workspace.is_index_path(root / "photos" / "image.jpg"))

    def test_transaction_rolls_back_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Workspace.create(Path(temporary_directory) / "archive")

            with self.assertRaises(RuntimeError):
                with workspace.transaction() as connection:
                    connection.execute(
                        "INSERT INTO logical_asset(id, media_type, created_at, updated_at) VALUES (?, ?, ?, ?)",
                        ("asset-1", "image", "now", "now"),
                    )
                    raise RuntimeError("stop")

            with closing(workspace.connect()) as connection:
                count = connection.execute("SELECT COUNT(*) FROM logical_asset").fetchone()[0]
            self.assertEqual(count, 0)

    def test_compaction_refuses_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Workspace.create(Path(temporary_directory) / "archive")
            store = JobStore(workspace)
            job_id = store.create("media_index", total_items=1)
            store.start(job_id)

            with self.assertRaises(WorkspaceError):
                compact_database(workspace)

    def test_compaction_reclaims_pages_and_preserves_index_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            source = root / "photo.jpg"
            root.mkdir()
            source.write_bytes(b"source")
            workspace = Workspace.create(root)
            with workspace.transaction() as connection:
                connection.execute(
                    "INSERT INTO logical_asset(id, media_type, selection_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    ("asset-1", "image", "selected", "now", "now"),
                )
                connection.execute(
                    """
                    INSERT INTO physical_file(
                        id, logical_asset_id, relative_path, filename, extension, media_type,
                        size_bytes, created_at, updated_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "file-1",
                        "asset-1",
                        "photo.jpg",
                        "photo.jpg",
                        ".jpg",
                        "image",
                        6,
                        "now",
                        "now",
                        "x" * 2_000_000,
                    ),
                )
            with workspace.transaction() as connection:
                connection.execute(
                    "UPDATE physical_file SET metadata_json = '{}' WHERE id = 'file-1'"
                )
            before = workspace.database_path.stat().st_size
            report = compact_database(workspace)

            self.assertEqual(report["integrity_check"], "ok")
            self.assertLess(report["after_size_bytes"], before)
            self.assertEqual(report["after_freelist_count"], 0)
            with closing(workspace.connect()) as connection:
                asset = connection.execute(
                    "SELECT selection_state FROM logical_asset WHERE id = 'asset-1'"
                ).fetchone()[0]
                count = connection.execute("SELECT COUNT(*) FROM physical_file").fetchone()[0]
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(asset, "selected")
            self.assertEqual(count, 1)
            self.assertEqual(source.read_bytes(), b"source")

    def test_existing_v1_database_migrates_to_current_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            index_directory = root / ".archive-index"
            index_directory.mkdir(parents=True)
            database_path = index_directory / "index.sqlite"
            connection = sqlite3.connect(database_path)
            for statement in MIGRATIONS[1]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (1, 'now')"
            )
            connection.execute("PRAGMA user_version = 1")
            connection.execute(
                """
                INSERT INTO workspace_info(
                    id, workspace_id, created_at, updated_at, app_version
                ) VALUES (1, ?, 'now', 'now', '0.1.0')
                """,
                (str(uuid4()),),
            )
            connection.commit()
            connection.close()

            workspace = Workspace.open(root)
            with closing(workspace.connect()) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                column = connection.execute(
                    """
                    SELECT 1 FROM pragma_table_info('physical_file')
                    WHERE name = 'metadata_json'
                    """
                ).fetchone()
                job_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info('job')").fetchall()
                }
                quality_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info('physical_file')").fetchall()
                }
                selection_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info('logical_asset')").fetchall()
                }
            self.assertEqual(version, 7)
            self.assertIsNotNone(column)
            self.assertTrue({"stage", "failed_items", "skipped_items"} <= job_columns)
            self.assertIn("selection_updated_at", selection_columns)
            self.assertTrue(
                {
                    "quality_raw_json",
                    "quality_components_json",
                    "quality_score",
                    "quality_algorithm",
                    "quality_version",
                }
                <= quality_columns
            )
            with closing(workspace.connect()) as connection:
                self.assertTrue(
                    {"visual_feature", "grouping_run", "workspace_grouping", "strict_group", "strict_group_member"}
                    <= {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                )
                self.assertTrue(
                    {"recommendation_run", "workspace_recommendation", "asset_recommendation"}
                    <= {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                )
                self.assertIn(
                    "source_grouping_run_id",
                    {row[1] for row in connection.execute("PRAGMA table_info(recommendation_run)").fetchall()},
                )

    def test_open_repairs_phase6_database_missing_selection_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            workspace = Workspace.create(root)
            with closing(sqlite3.connect(workspace.database_path)) as connection:
                connection.execute("ALTER TABLE logical_asset DROP COLUMN selection_updated_at")
                connection.execute("PRAGMA user_version = 7")
                connection.commit()

            Workspace.open(root)
            with closing(workspace.connect()) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(logical_asset)").fetchall()
                }
            self.assertIn("selection_updated_at", columns)


if __name__ == "__main__":
    unittest.main()
