import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

import photo_select as ps


class SelectionTests(unittest.TestCase):
    def test_focus_detects_blur(self):
        with tempfile.TemporaryDirectory(dir=ps.ROOT) as tmp:
            pattern = np.zeros((512, 512, 3), dtype=np.uint8)
            for y in range(0, 512, 32):
                for x in range(0, 512, 32):
                    pattern[y:y + 32, x:x + 32] = 240 if (x // 32 + y // 32) % 2 else 20
            sharp = Image.fromarray(pattern)
            a, b = Path(tmp) / "sharp.jpg", Path(tmp) / "blur.jpg"
            sharp.save(a)
            sharp.filter(ImageFilter.GaussianBlur(5)).save(b)
            fa, _, _ = ps.image_features(a)
            fb, _, _ = ps.image_features(b)
            self.assertGreater(fa["quality_features"]["focus"], fb["quality_features"]["focus"] * 2)

    def test_read_only_index_and_safe_export(self):
        with tempfile.TemporaryDirectory(dir=ps.ROOT) as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            for i, color in enumerate(["red", "blue", "green"]):
                Image.new("RGB", (96, 64), color).save(source / f"{i}.JPG")
            broken = source / "broken.jpg"
            broken.write_bytes(b"not an image")
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()}
            index, arrays, folder = ps.index_folder(source, root / "cache", workers=2)
            self.assertEqual(len(index["records"]), 3)
            self.assertEqual(len(index["errors"]), 1)
            self.assertEqual(index["records"][0]["capture_time"], None)
            second, _, cached_folder = ps.index_folder(source, root / "cache", workers=2)
            self.assertEqual(folder, cached_folder)
            self.assertEqual(index, second)
            selection = ps.make_selection(index, arrays, 2, method="handcrafted")
            manifest = root / "selection.json"
            ps.write_json(manifest, selection)
            destination = root / "export"
            self.assertEqual(ps.export_selection(manifest, destination), 2)
            self.assertEqual(len(list(destination.iterdir())), 2)
            for p in destination.iterdir():
                self.assertEqual(p.read_bytes(), (source / p.name).read_bytes())
            with self.assertRaises(FileExistsError):
                ps.export_selection(manifest, destination)
            with self.assertRaises(ValueError):
                ps.export_selection(manifest, source / "nested")
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in source.iterdir()})
            changed = Path(selection["selected_paths"][0])
            changed.write_bytes(b"changed synthetic fixture")
            with self.assertRaises(ValueError):
                ps.export_selection(manifest, root / "stale-export")
            self.assertFalse((root / "stale-export").exists())

    def test_group_diversity_count_and_determinism(self):
        features = dict(focus=1., focus_center=1., detail=1., contrast=1., black_fraction=0., white_fraction=0., laplacian=1.)
        records = [{"path": str(i), "capture_time": i, "quality_features": features} for i in range(6)]
        arrays = {"dino": np.array([[1, 0], [1, 0], [1, 0], [0, 1], [0, 1], [0, 1]], dtype=np.float32)}
        chosen, _, groups = ps.select_indices(records, arrays, 2)
        self.assertEqual(len(set(groups[chosen])), 2)
        self.assertEqual(chosen, ps.select_indices(records, arrays, 2)[0])
        self.assertEqual(ps.select_indices(records, arrays, 0)[0], [])
        self.assertEqual(len(set(ps.select_indices(records, arrays, 6)[0])), 6)
        for count in [-1, 7]:
            with self.assertRaises(ValueError):
                ps.select_indices(records, arrays, count)

    def test_archive_and_unowned_cache_protection(self):
        with patch.object(ps, "ARCHIVE", ps.ROOT / "protected-test-archive"):
            with self.assertRaises(ValueError):
                ps.safe_output(ps.ARCHIVE / "any-new-file.json")
        with tempfile.TemporaryDirectory(dir=ps.ROOT) as tmp:
            p = Path(tmp)
            (p / "existing.txt").write_text("untouched")
            with self.assertRaises(ValueError):
                ps.cache_directory(p)
            self.assertEqual((p / "existing.txt").read_text(), "untouched")

    def test_near_match_metric_cannot_reuse_one_manual_pick(self):
        from evaluate import metrics
        similarity = np.eye(4)
        similarity[0, 2] = similarity[1, 2] = 0.99
        result = metrics([0, 1], [2, 3], similarity, np.zeros(4), np.arange(4))
        self.assertEqual(result["near_match_hits"], 1)
        self.assertEqual(result["exact_hits"], 0)

    def test_score_explanations_reconstruct_quality_and_selection(self):
        features = dict(focus=1., focus_center=1., detail=1., contrast=1., black_fraction=.1, white_fraction=.02, laplacian=1.)
        records = [{"path": str(i), "capture_time": i, "quality_features": {**features, "focus": i + 1}, "aesthetic": float(i)} for i in range(6)]
        arrays = {"dino": np.array([[1, 0], [1, .05], [1, .1], [0, 1], [.05, 1], [.1, 1]], dtype=np.float32)}
        expected = ps.select_indices(records, arrays, 3, method="dino")[0]
        trace = {}
        chosen, _, _ = ps.select_indices(records, arrays, 3, method="dino", trace=trace)
        self.assertEqual(chosen, expected)
        self.assertEqual(len(trace), 6)
        for i, values in trace.items():
            self.assertAlmostEqual(values["selection_score"], values["quality_contribution"] + values["diversity_contribution"] - values["duplicate_penalty"])
            self.assertEqual(values["evaluated_at_pick"], chosen.index(i) + 1 if i in chosen else 4)
        components = ps.quality_components(records)
        reconstructed = np.clip(components["focus_contribution"] + components["detail_contribution"] + components["contrast_contribution"] - components["clipping_penalty"], 0, 1)
        np.testing.assert_allclose(components["technical"], reconstructed)
        np.testing.assert_allclose(ps.quality_scores(records), components["technical"])
        self.assertFalse(np.allclose(ps.quality_scores(records, .85), components["technical"]))

    def test_experimental_counts_and_explanations(self):
        from selection_methods import context, select
        features = dict(focus=1., focus_center=1., detail=1., contrast=1., black_fraction=0., white_fraction=0.)
        records = [{"path": str(i), "capture_time": i, "quality_features": {**features, "focus": i + 1}, "aesthetic": float(i)} for i in range(8)]
        vectors = np.random.default_rng(6).normal(size=(8, 4)).astype(np.float32)
        arrays = {"dino": vectors, "clip": vectors}
        prepared = context(records, arrays)
        for method in ["mmr", "coverage"]:
            recipe = {"strategy": method, "aesthetic_weight": .65, "diversity": .4}
            expected, _, trace = select(prepared, 3, recipe, explain=True)
            chosen, _, _ = ps.select_indices(records, arrays, 3, method=method, aesthetic_weight=.65, diversity=.4)
            self.assertEqual(chosen, expected)
            self.assertEqual(len(set(chosen)), 3)
            for values in trace.values():
                self.assertAlmostEqual(values["selection_score"], values["quality_contribution"] + values["diversity_contribution"] - values["duplicate_penalty"])
            self.assertEqual(select(prepared, 0, recipe)[0], [])
            self.assertEqual(len(set(select(prepared, 8, recipe)[0])), 8)


if __name__ == "__main__":
    unittest.main()
