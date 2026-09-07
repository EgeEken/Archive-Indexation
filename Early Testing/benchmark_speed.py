"""Independent full indexing runs on one session, with fresh feature caches."""
import json
import time
from pathlib import Path

import torch

import photo_select as ps


if __name__ == "__main__":
    inv = json.loads((ps.ROOT / "results/archive_inventory_before.json").read_text(encoding="utf-8"))
    source = Path(next(s["source"] for s in inv["sessions"] if "montsouris" in s["name"]))
    models = ps.load_models(["mobile", "dino", "clip"], ps.ROOT / "models", "cuda")
    with torch.inference_mode(), torch.autocast(device_type="cuda"):
        warmup = torch.zeros(32, 3, 224, 224, device="cuda")
        for name, (model, _, _) in models.items():
            model.encode_image(warmup) if name == "clip" else model(warmup)
        torch.cuda.synchronize()
    rows = []
    for name in ["handcrafted", "mobile", "dino", "clip"]:
        subset = {} if name == "handcrafted" else {name: models[name]}
        cache = ps.ROOT / "speed-cache" / (name + "-" + str(time.time_ns()))
        tick = time.perf_counter()
        index, arrays, _ = ps.index_folder(source, cache, subset, "cuda" if subset else "cpu")
        elapsed = time.perf_counter() - tick
        start = time.perf_counter()
        ps.select_indices(index["records"], arrays, 46, method=name)
        selection_seconds = time.perf_counter() - start
        start = time.perf_counter()
        ps.index_folder(source, cache, [name] if subset else [])
        cache_seconds = time.perf_counter() - start
        rows.append({"method": name, "n": len(index["records"]), "fresh_index_seconds": elapsed,
                     "images_per_second": len(index["records"]) / elapsed,
                     "selection_seconds": selection_seconds, "cache_read_seconds": cache_seconds,
                     "stage_timings": index["timings"]})
        print(rows[-1], flush=True)
    ps.write_json(ps.ROOT / "results/speed.json", {"source": str(source),
                  "conditions": "One run per method, 265 originals, GPU warmed, model loading excluded, fresh feature caches, OS file cache not flushed; six CPU workers, batch 32. Local laptop, not a controlled hardware comparison.",
                  "rows": rows})
