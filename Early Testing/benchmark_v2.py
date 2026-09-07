import json
import time
from pathlib import Path

import torch

import photo_select as ps


if __name__ == "__main__":
    inv = json.loads((ps.ROOT / "results/archive_inventory_before.json").read_text(encoding="utf-8"))
    source = Path(next(s["source"] for s in inv["sessions"] if s["name"].startswith("2026.09.03")))
    models = ps.load_models(["mobile", "dino", "clip"], ps.ROOT / "models", "cuda")
    with torch.inference_mode(), torch.autocast(device_type="cuda"):
        x = torch.zeros(32, 3, 224, 224, device="cuda")
        for name, (model, _, _) in models.items():
            model.encode_image(x) if name == "clip" else model(x)
        torch.cuda.synchronize()
    rows = []
    for name, keys in [("handcrafted", []), ("mobile", ["mobile"]), ("dino", ["dino"]), ("clip", ["clip"]), ("dino_clip", ["dino", "clip"])]:
        cache = ps.ROOT / "speed-cache/v2" / (name + "-" + str(time.time_ns()))
        start = time.perf_counter()
        index, arrays, _ = ps.index_folder(source, cache, {k: models[k] for k in keys}, "cuda" if keys else "cpu")
        elapsed = time.perf_counter() - start
        start = time.perf_counter()
        ps.index_folder(source, cache, keys)
        rows.append({"pipeline": name, "n": len(index["records"]), "index_seconds": elapsed,
                     "cached_read_seconds": time.perf_counter() - start, "stage_timings": index["timings"]})
        print(rows[-1], flush=True)
    ps.write_json(ps.ROOT / "results/v2/speed.json", {
        "source": str(source), "conditions": "1,369 originals per pipeline; one run; fresh feature cache; six CPU workers; batch 32; warmed GPU; model load excluded; OS file cache not flushed; local laptop. Pipelines run sequentially.",
        "rows": rows})
