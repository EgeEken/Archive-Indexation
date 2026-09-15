from __future__ import annotations

import io
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from archive_index.indexing.media_pipeline import index_workspace
from archive_index.indexing.raw_quality import index_raw_quality
from archive_index.indexing.scanner import scan
from archive_index.media.metadata import UnsupportedDecoderError
from archive_index.media.quality_provider import OffQualityProvider, ProviderResult
from archive_index.media.raw_preview import RawPreview, extract_embedded_preview
from archive_index.workspace import Workspace


def _jpeg_payload(size=(1600, 1400)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, "navy").save(output, format="JPEG")
    return output.getvalue()


class _FakeRaw:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_thumb(self):
        return types.SimpleNamespace(
            format=types.SimpleNamespace(name="JPEG"),
            data=self.payload,
        )


class _FakeRawProvider:
    enabled = True
    algorithm = "fake-quality"
    version = "1"
    settings = {"test": True}
    batch_size = 4

    def preflight(self):
        return None

    def score_prepared_images(self, images):
        return [ProviderResult({"model_output": 0.8}, 0.8) for _ in images]

    def score_images(self, images):
        return self.score_prepared_images(images)


class RawQualityTests(unittest.TestCase):
    def test_embedded_preview_is_oriented_and_versioned(self):
        module = types.SimpleNamespace(imread=lambda path: _FakeRaw(_jpeg_payload()))
        with patch.dict(sys.modules, {"rawpy": module}):
            with tempfile.TemporaryDirectory() as temporary_directory:
                source = Path(temporary_directory) / "image.arw"
                source.write_bytes(b"raw")
                preview = extract_embedded_preview(source)

        self.assertEqual(
            (preview.method, preview.width, preview.height),
            ("rawpy-embedded-preview", 1600, 1400),
        )
        preview.image.close()

    def test_previewless_raw_is_unsupported(self):
        module = types.SimpleNamespace(imread=lambda path: _FakeRaw(_jpeg_payload((64, 64))))
        with patch.dict(sys.modules, {"rawpy": module}):
            with tempfile.TemporaryDirectory() as temporary_directory:
                source = Path(temporary_directory) / "small.arw"
                source.write_bytes(b"raw")
                with self.assertRaises(UnsupportedDecoderError):
                    extract_embedded_preview(source)

    def test_raw_quality_persists_embedded_preview_provenance(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "image.arw").write_bytes(b"raw")
            workspace = Workspace.create(root)
            scan(workspace)
            preview = RawPreview(Image.new("RGB", (1600, 1200), "navy"), "rawpy-embedded-preview", 1600, 1200)
            with patch("archive_index.indexing.raw_quality.extract_embedded_preview", return_value=preview):
                result = index_raw_quality(workspace, quality_provider=_FakeRawProvider())
            self.assertEqual((result.errors, result.skipped), (0, 0))
            with closing(workspace.connect()) as connection:
                row = connection.execute(
                    "SELECT quality_score, quality_algorithm, quality_version, quality_raw_json FROM physical_file"
                ).fetchone()
                state = connection.execute(
                    "SELECT status, algorithm, version FROM component_state WHERE component = 'quality'"
                ).fetchone()
            self.assertEqual(tuple(row[:3]), (0.8, "lar-iqa-raw-preview", "1"))
            self.assertEqual((state["status"], state["algorithm"], state["version"]), ("complete", "lar-iqa-raw-preview", "1"))
            self.assertEqual(__import__("json").loads(row["quality_raw_json"])["quality_source"], "raw_embedded_preview")

    def test_paired_raw_is_not_selected_for_raw_only_quality(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            Image.new("RGB", (100, 80), "navy").save(root / "image.jpg")
            (root / "image.arw").write_bytes(b"raw")
            workspace = Workspace.create(root)
            scan(workspace)
            with closing(workspace.connect()) as connection:
                jpeg_asset = connection.execute(
                    "SELECT logical_asset_id FROM physical_file WHERE extension = '.jpg'"
                ).fetchone()[0]
                raw_id = connection.execute(
                    "SELECT id, logical_asset_id FROM physical_file WHERE extension = '.arw'"
                ).fetchone()
            with workspace.transaction() as connection:
                connection.execute(
                    "UPDATE physical_file SET logical_asset_id = ? WHERE id = ?",
                    (jpeg_asset, raw_id["id"]),
                )
                connection.execute("DELETE FROM logical_asset WHERE id = ?", (raw_id["logical_asset_id"],))
            result = index_raw_quality(workspace, quality_provider=_FakeRawProvider())
            self.assertEqual((result.processed, result.errors), (0, 0))
            with closing(workspace.connect()) as connection:
                status = connection.execute(
                    "SELECT status FROM component_state WHERE physical_file_id = ? AND component = 'quality'",
                    (raw_id["id"],),
                ).fetchone()[0]
            self.assertEqual(status, "not_requested")

    def test_raw_thumbnail_uses_embedded_preview_provenance(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "archive"
            root.mkdir()
            (root / "image.arw").write_bytes(b"raw")
            workspace = Workspace.create(root)
            scan(workspace)
            preview = RawPreview(Image.new("RGB", (1600, 1200), "navy"), "rawpy-embedded-preview", 1600, 1200)
            with patch("archive_index.indexing.media_pipeline.load_raw_preview", return_value=preview.image.copy()):
                result = index_workspace(
                    workspace,
                    components=("thumbnail",),
                    quality_provider=OffQualityProvider(),
                )
            self.assertEqual(result.errors, 0)
            with closing(workspace.connect()) as connection:
                state = connection.execute(
                    "SELECT status, algorithm, version FROM component_state WHERE component = 'thumbnail'"
                ).fetchone()
            self.assertEqual(tuple(state), ("complete", "rawpy-preview-jpeg", "rawpy-preview-jpeg-v1"))


if __name__ == "__main__":
    unittest.main()
