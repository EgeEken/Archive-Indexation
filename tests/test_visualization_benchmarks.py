from __future__ import annotations

import os
import json
import subprocess
import time
import unittest
from pathlib import Path

import numpy as np

from archive_index.indexing.projection import _fit_pca


@unittest.skipUnless(os.getenv("ARCHIVE_INDEX_BENCHMARKS"), "visualization benchmarks are opt-in")
class VisualizationBenchmarkTests(unittest.TestCase):
    def test_pca_fit_handles_archive_scale(self):
        generator = np.random.default_rng(9)
        vectors = {str(index): generator.normal(size=8).astype("float32") for index in range(100_000)}
        asset_ids = sorted(vectors)
        for count in (10_000, 50_000, 100_000):
            started = time.perf_counter()
            points = _fit_pca(vectors, asset_ids[:count], 10_000)
            elapsed = time.perf_counter() - started
            self.assertEqual(len(points), count)
            self.assertTrue(all(np.isfinite(values).all() for values in points.values()))
            print(f"pca-2d-v1 benchmark assets={count} seconds={elapsed:.3f}")

    def test_visualization_lod_prep_handles_archive_scale(self):
        source = (Path(__file__).parents[1] / "src" / "archive_index" / "web" / "app-visualizations.js").read_text(encoding="utf-8")
        timeline_start = source.index("function timelineBucketStart")
        timeline_end = source.index("function drawTimelineDensity", timeline_start)
        vector_start = source.index("function vectorLod")
        vector_end = source.index("function vectorDensityRasterPoint", vector_start)
        geo_start = source.index("function geoWorld")
        geo_end = source.index("function drawGeo(canvas", geo_start)
        script = f'''const TIMELINE_INTERVALS=[0.001,0.01,0.1,1,5,10,30,60,300,900,1800,3600,10800,21600,43200,86400,604800,2592000,7776000,31536000];
const TIMELINE_KERNEL=[1,4,6,4,1];
function representativeVisualizationPoint(points) {{ return points[0]; }}
function worldPoint(view,x,y,width,height) {{ return {{x:(x-width/2)/view.scale+view.centerX,y:(y-height/2)/view.scale+view.centerY}}; }}
{source[timeline_start:timeline_end]}
{source[vector_start:vector_end]}
{source[geo_start:geo_end]}
const results=[];
for (const count of [10000,50000,100000]) {{
  const timeline=Array.from({{length:count}},(_,i)=>({{asset_id:`t-${{i}}`,time:i*3600}}));
  const timelineView={{key:'bench',timeMode:'capture',timelineCaches:new Map(),centerTime:timeline.at(-1).time/2,visibleSpan:timeline.at(-1).time,baseScale:1}};
  let started=performance.now(); const timelineCache=buildTimelineCache(timelineView,timeline,60); const timelineVisible=timelineVisibleBins(timelineCache,timelineView.centerTime-1800,timelineView.centerTime+1800).length; const timelineMs=performance.now()-started;
  const vector=Array.from({{length:count}},(_,i)=>({{asset_id:`v-${{i}}`,x:(i%1000)/1000,y:Math.floor(i/1000)/100}}));
  const vectorView={{key:'bench',data:{{browser_revision:'1'}},lodCaches:new Map(),baseCellWorld:1,gridOriginX:0,gridOriginY:0,centerX:.5,centerY:.5,scale:400,baseScale:400}};
  started=performance.now(); const vectorCache=buildVectorLodCache(vectorView,vector,0); const vectorVisible=visibleVectorCells(vectorCache,vectorView,1000,600).length; const vectorMs=performance.now()-started;
  const geo=Array.from({{length:count}},(_,i)=>({{asset_id:`g-${{i}}`,longitude:-180+(i%1000)*.36,latitude:-80+Math.floor(i/1000)*.8}}));
  const geoView={{key:'bench',data:{{browser_revision:'1'}},lodCaches:new Map(),baseCellWorld:.1,centerX:.5,centerY:.5,scale:400,baseScale:400}};
  started=performance.now(); const geoCache=buildGeoLodCache(geoView,geo,0); const geoVisible=visibleGeoCells(geoCache,geoView,1000,600).length; const geoMs=performance.now()-started;
  results.push({{count,timelineMs,vectorMs,geoMs,timelineOccupied:timelineCache.occupied.size,vectorCells:vectorCache.cells.size,geoCells:geoCache.cells.size,timelineVisible,vectorVisible,geoVisible}});
}}
console.log(JSON.stringify(results));'''
        result = subprocess.run(["node", "--eval", script], capture_output=True, text=True, encoding="utf-8", check=True)
        results = json.loads(result.stdout)
        self.assertEqual([item["count"] for item in results], [10000, 50000, 100000])
        for item in results:
            self.assertEqual(item["timelineOccupied"], item["count"])
            self.assertGreater(item["vectorCells"], 0)
            self.assertGreater(item["geoCells"], 0)
            print(f"visualization-lod benchmark assets={item['count']} timeline={item['timelineMs'] / 1000:.3f}s vector={item['vectorMs'] / 1000:.3f}s geo={item['geoMs'] / 1000:.3f}s")


if __name__ == "__main__":
    unittest.main()
