import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from photo_select import ROOT, write_json


def verify(pair):
    hashes = []
    for field in ["source", "selected"]:
        with Path(pair[field]).open("rb") as f:
            hashes.append(hashlib.file_digest(f, "sha256").hexdigest())
    return {**pair, "source_sha256": hashes[0], "selected_sha256": hashes[1], "identical": hashes[0] == hashes[1]}


if __name__ == "__main__":
    inventory = json.loads((ROOT / "results/archive_inventory_before.json").read_text(encoding="utf-8"))
    eligible = [s for s in inventory["sessions"] if 0 < len(s["matches"]) < s["n_source"] and not s["ambiguous"] and not s["unmatched"]]
    split = {"rule": "Chronologically sorted eligible sessions: even positions held out, odd positions development (zero based). Fixed before looking at selection performance.",
             "development": [s["name"] for i, s in enumerate(eligible) if i % 2 == 1],
             "holdout": [s["name"] for i, s in enumerate(eligible) if i % 2 == 0]}
    write_json(ROOT / "results/split.json", split)
    tick = time.perf_counter()
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for s in eligible:
            pairs = list(pool.map(verify, s["matches"]))
            results.append({"name": s["name"], "pairs": pairs})
            print(s["name"], len(pairs), "identical:", sum(p["identical"] for p in pairs), flush=True)
    write_json(ROOT / "results/label_validation.json", {"seconds": time.perf_counter() - tick, "sessions": results})
    crops = []
    for session in results:
        for pair in session["pairs"]:
            if pair["identical"]:
                continue
            images = []
            for field in ["source", "selected"]:
                with Image.open(pair[field]) as im:
                    gray = np.asarray(ImageOps.exif_transpose(im).convert("L"))
                    images.append(cv2.resize(gray, None, fx=0.25, fy=0.25))
            original, selected = images
            if all(a >= b for a, b in zip(original.shape, selected.shape)):
                match = cv2.matchTemplate(original, selected, cv2.TM_CCOEFF_NORMED)
                _, score, _, origin = cv2.minMaxLoc(match)
                if score > 0.95:
                    crops.append({"source": pair["source"], "selected": pair["selected"],
                                  "template_correlation": score, "quarter_scale_crop_origin": origin,
                                  "verification": "Grayscale template match at original pixel scale, quarter-resolution."})
    write_json(ROOT / "results/label_exceptions.json", {"verified_crops": crops})
