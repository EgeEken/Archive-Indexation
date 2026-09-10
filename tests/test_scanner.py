from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

import archive_index.indexing.scanner as scanner
from archive_index.indexing.scanner import scan
from archive_index.workspace import Workspace


class ScannerTests(unittest.TestCase):
    def test_initial_scan_excludes_index_and_unsupported_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            (root / "photos").mkdir(parents=True)
            photo = root / "photos" / "a.JPG"
            photo.write_bytes(b"a")
            photo_before = photo.stat()
            (root / "videos").mkdir()
            (root / "videos" / "clip.MP4").write_bytes(b"video")
            (root / "notes.txt").write_text("ignore", encoding="utf-8")
            workspace = Workspace.create(root)
            (root / ".archive-index" / "ignored.jpg").write_bytes(b"ignore")

            result = scan(workspace)

            self.assertEqual(result.discovered, 2)
            self.assertEqual(result.added, 2)
            self.assertEqual(result.hashed, 2)
            with workspace.connect() as connection:
                paths = [row[0] for row in connection.execute(
                    "SELECT relative_path FROM physical_file ORDER BY relative_path"
                ).fetchall()]
            connection.close()
            self.assertEqual(paths, ["photos/a.JPG", "videos/clip.MP4"])
            self.assertEqual(photo.read_bytes(), b"a")
            self.assertEqual(photo.stat().st_mtime_ns, photo_before.st_mtime_ns)

    def test_unchanged_scan_uses_only_stat_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "photo.jpg").write_bytes(b"photo")
            workspace = Workspace.create(root)
            scan(workspace)

            with patch(
                "archive_index.indexing.scanner.hash_file",
                side_effect=AssertionError("unchanged file was hashed"),
            ) as hash_mock:
                result = scan(workspace)

            self.assertEqual(result.unchanged, 1)
            self.assertEqual(result.hashed, 0)
            hash_mock.assert_not_called()

    def test_incremental_scan_handles_noop_add_modify_move_and_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            photo = root / "a.jpg"
            photo.parent.mkdir(parents=True)
            photo.write_bytes(b"original")
            workspace = Workspace.create(root)

            first = scan(workspace)
            second = scan(workspace)
            self.assertEqual((first.added, first.hashed), (1, 1))
            self.assertEqual((second.unchanged, second.hashed), (1, 0))

            (root / "b.jpg").write_bytes(b"new")
            added = scan(workspace)
            self.assertEqual((added.added, added.unchanged), (1, 1))

            with workspace.connect() as connection:
                file_id = connection.execute(
                    "SELECT id FROM physical_file WHERE relative_path = 'a.jpg'"
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO component_state(
                        physical_file_id, component, status, input_fingerprint, completed_at
                    ) VALUES (?, 'thumbnail', 'complete', 'old', 'now')
                    """,
                    (file_id,),
                )
            connection.close()

            photo.write_bytes(b"changed")
            changed = scan(workspace)
            self.assertEqual(changed.changed, 1)
            with workspace.connect() as connection:
                state = connection.execute(
                    "SELECT status, input_fingerprint FROM component_state WHERE physical_file_id = ?",
                    (file_id,),
                ).fetchone()
            connection.close()
            self.assertEqual(tuple(state), ("pending", None))

            original_id = file_id
            photo.rename(root / "renamed.jpg")
            moved = scan(workspace)
            self.assertEqual(moved.moved, 1)
            with workspace.connect() as connection:
                moved_row = connection.execute(
                    "SELECT id, relative_path, is_online FROM physical_file WHERE id = ?",
                    (original_id,),
                ).fetchone()
            connection.close()
            self.assertEqual(tuple(moved_row), (original_id, "renamed.jpg", 1))

            (root / "renamed.jpg").unlink()
            missing = scan(workspace)
            self.assertEqual(missing.missing, 1)
            with workspace.connect() as connection:
                online = connection.execute(
                    "SELECT is_online FROM physical_file WHERE id = ?", (original_id,)
                ).fetchone()[0]
            connection.close()
            self.assertEqual(online, 0)

    def test_identical_copy_is_not_treated_as_a_move(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            original = root / "z-original.jpg"
            original.write_bytes(b"same bytes")
            workspace = Workspace.create(root)
            scan(workspace)

            copy = root / "a-copy.jpg"
            copy.write_bytes(original.read_bytes())
            result = scan(workspace)

            self.assertEqual(result.moved, 0)
            self.assertEqual(result.added, 1)
            with workspace.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT physical_file.relative_path, physical_file.id, logical_asset.id
                    FROM physical_file
                    JOIN logical_asset ON logical_asset.id = physical_file.logical_asset_id
                    ORDER BY physical_file.relative_path
                    """
                ).fetchall()
            connection.close()
            self.assertEqual([row[0] for row in rows], ["a-copy.jpg", "z-original.jpg"])
            self.assertNotEqual(rows[0][2], rows[1][2])

    def test_ambiguous_missing_hash_match_creates_new_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            workspace = Workspace.create(root)
            for name in ("old-a.jpg", "old-b.jpg"):
                (root / name).write_bytes(b"same bytes")
            scan(workspace)
            (root / "old-a.jpg").unlink()
            (root / "old-b.jpg").unlink()
            scan(workspace)

            (root / "new-copy.jpg").write_bytes(b"same bytes")
            result = scan(workspace)

            self.assertEqual(result.moved, 0)
            self.assertEqual(result.added, 1)

    def test_unicode_and_case_only_rename_preserve_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            media_directory = root / "café" / "写真"
            media_directory.mkdir(parents=True)
            original = media_directory / "旅行.JPG"
            original.write_bytes(b"unicode")
            workspace = Workspace.create(root)
            scan(workspace)
            with workspace.connect() as connection:
                row = connection.execute(
                    "SELECT id, logical_asset_id FROM physical_file"
                ).fetchone()
            connection.close()

            renamed = media_directory / "旅行.jpg"
            original.rename(renamed)
            result = scan(workspace)

            self.assertEqual(result.moved, 1)
            with workspace.connect() as connection:
                current = connection.execute(
                    "SELECT id, logical_asset_id, relative_path FROM physical_file"
                ).fetchone()
            connection.close()
            self.assertEqual(tuple(current), (row[0], row[1], "café/写真/旅行.jpg"))

    def test_traversal_error_isolated_and_reparse_points_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            root = temporary_root / "archive"
            blocked = root / "blocked"
            blocked.mkdir(parents=True)
            (blocked / "hidden.jpg").write_bytes(b"hidden")
            (root / "good.jpg").write_bytes(b"good")
            outside = temporary_root / "outside"
            outside.mkdir()
            (outside / "outside.jpg").write_bytes(b"outside")
            workspace = Workspace.create(root)

            links_created = True
            try:
                (root / "outside-link").symlink_to(outside, target_is_directory=True)
                (root / "file-link.jpg").symlink_to(outside / "outside.jpg")
            except OSError:
                links_created = False

            real_scandir = scanner.os.scandir

            def failing_scandir(path):
                if Path(path).name == "blocked":
                    raise OSError("simulated traversal failure")
                return real_scandir(path)

            with patch("archive_index.indexing.scanner.os.scandir", side_effect=failing_scandir):
                result = scan(workspace)

            self.assertEqual(result.errors, 1)
            self.assertEqual(result.added, 1)
            with workspace.connect() as connection:
                paths = [row[0] for row in connection.execute(
                    "SELECT relative_path FROM physical_file"
                ).fetchall()]
            connection.close()
            self.assertEqual(paths, ["good.jpg"])
            if not links_created:
                return

    def test_cancelled_scan_does_not_mark_unvisited_rows_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            for name in ("a.jpg", "b.jpg", "c.jpg", "d.jpg"):
                (root / name).write_bytes(name.encode())
            workspace = Workspace.create(root)
            scan(workspace)
            (root / "c.jpg").unlink()
            (root / "d.jpg").unlink()
            cancel_event = Event()

            def cancel_after_first(progress) -> None:
                if progress.processed == 1:
                    cancel_event.set()

            result = scan(workspace, cancel_event=cancel_event, progress=cancel_after_first)

            self.assertTrue(result.cancelled)
            with workspace.connect() as connection:
                states = {
                    row[0]: row[1]
                    for row in connection.execute(
                        "SELECT relative_path, is_online FROM physical_file"
                    ).fetchall()
                }
            connection.close()
            self.assertEqual(states["c.jpg"], 1)
            self.assertEqual(states["d.jpg"], 1)

    def test_hash_error_is_logged_and_other_files_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "bad.jpg").write_bytes(b"bad")
            (root / "good.jpg").write_bytes(b"good")
            workspace = Workspace.create(root)

            def failing_hash(path: Path) -> str:
                if path.name == "bad.jpg":
                    raise OSError("simulated read failure")
                return "good-hash"

            with patch("archive_index.indexing.scanner.hash_file", side_effect=failing_hash):
                result = scan(workspace)

            self.assertEqual(result.errors, 1)
            self.assertEqual(result.added, 1)
            with workspace.connect() as connection:
                error_count = connection.execute("SELECT COUNT(*) FROM job_error").fetchone()[0]
                stored_paths = [row[0] for row in connection.execute(
                    "SELECT relative_path FROM physical_file"
                ).fetchall()]
            connection.close()
            self.assertEqual(error_count, 1)
            self.assertEqual(stored_paths, ["good.jpg"])

    def test_scan_can_cancel_between_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            for name in ("a.jpg", "b.jpg", "c.jpg"):
                (root / name).write_bytes(name.encode())
            workspace = Workspace.create(root)
            cancel_event = Event()

            def cancel_after_first(progress) -> None:
                if progress.processed == 1:
                    cancel_event.set()

            result = scan(workspace, cancel_event=cancel_event, progress=cancel_after_first)

            self.assertTrue(result.cancelled)
            self.assertEqual(result.added, 1)
            with workspace.connect() as connection:
                job_status = connection.execute(
                    "SELECT status FROM job WHERE id = ?", (result.job_id,)
                ).fetchone()[0]
            connection.close()
            self.assertEqual(job_status, "cancelled")


if __name__ == "__main__":
    unittest.main()
