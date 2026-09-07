import json
import time
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

import photo_select as ps


if __name__ == "__main__":
    runs = json.loads((ps.ROOT / "results/index_run.json").read_text(encoding="utf-8"))
    folder = Path(next(s["cache"] for s in runs["sessions"] if s["name"].startswith("2026.09.03")))
    index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
    with np.load(folder / "features.npz", allow_pickle=False) as data:
        arrays = {k: data[k] for k in data.files}
    presets = json.loads((ps.ROOT / "results/presets.json").read_text())
    recipes = json.loads((ps.ROOT / "results/v2/recipes.json").read_text())
    presets.update({key: {"method": r["strategy"], "aesthetic_weight": r["aesthetic_weight"], "diversity": r["diversity"]} for key, r in recipes.items()})
    rows = []
    with threadpool_limits(limits=4):
        for name, options in presets.items():
            start = time.perf_counter()
            result = ps.make_selection(index, arrays, 138, **options)
            elapsed = time.perf_counter() - start
            rows.append({"method": name, "selection_seconds": elapsed, "n": 1369, "k": 138})
            if len(result["selected_paths"]) != 138:
                raise ValueError("Invalid shortlist size")
    ps.write_json(ps.ROOT / "results/v2/cached_speed.json", {"conditions": "1,369 cached photos, select 138 with score explanations; one run per method; 4 BLAS threads; excludes disk read, HTML generation and Python startup.", "rows": rows})
