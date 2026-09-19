from __future__ import annotations

import os
import unittest

import numpy as np

from archive_index.indexing.projection import _fit_pca


@unittest.skipUnless(os.getenv("ARCHIVE_INDEX_BENCHMARKS"), "visualization benchmarks are opt-in")
class VisualizationBenchmarkTests(unittest.TestCase):
    def test_pca_fit_handles_archive_scale(self):
        generator = np.random.default_rng(9)
        vectors = {str(index): generator.normal(size=8).astype("float32") for index in range(100_000)}
        asset_ids = sorted(vectors)
        for count in (10_000, 50_000, 100_000):
            points = _fit_pca(vectors, asset_ids[:count], 10_000)
            self.assertEqual(len(points), count)
            self.assertTrue(all(np.isfinite(values).all() for values in points.values()))


if __name__ == "__main__":
    unittest.main()
