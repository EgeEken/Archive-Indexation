import json
import platform
import time

import torch
import photo_select as ps


if __name__ == "__main__":
    inventory = json.loads((ps.ROOT / "results/archive_inventory_before.json").read_text(encoding="utf-8"))
    tick = time.perf_counter()
    models = ps.load_models(["mobile", "dino", "clip"], ps.ROOT / "models", "cuda")
    load_seconds = time.perf_counter() - tick
    sessions = []
    for session in inventory["sessions"]:
        index, arrays, folder = ps.index_folder(session["source"], ps.ROOT / "cache", models, "cuda")
        sessions.append({"name": session["name"], "cache": str(folder), "n": len(index["records"]),
                         "timings": index["timings"], "errors": index["errors"]})
    ps.write_json(ps.ROOT / "results/index_run.json", {"model_load_download_seconds": load_seconds,
                  "total_seconds": time.perf_counter() - tick, "python": platform.python_version(),
                  "torch": torch.__version__, "gpu": torch.cuda.get_device_name(), "sessions": sessions})
