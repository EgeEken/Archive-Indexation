import json
import time
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

import photo_select as ps
from evaluate import aggregate, metrics
from selection_methods import context, select


if __name__ == "__main__":
    folder = ps.ROOT / "results/v2"
    recipes = {}
    for strategy in ["mmr", "coverage"]:
        for aw in [0.35, 0.65, 0.85]:
            for diversity in [0.20, 0.40, 0.60]:
                name = f"{strategy}_a{round(aw * 100)}_d{round(diversity * 100)}"
                recipes[name] = {"strategy": strategy, "aesthetic_weight": aw, "diversity": diversity}
    ps.write_json(folder / "recipes.json", recipes)
    ps.write_json(folder / "protocol.json", {
        "data_status": "All 17 sessions were seen during the previous experiment; this is exploratory reuse, not a new untouched test set.",
        "candidates": 18, "choice_objective": "Macro exact overlap + 0.10 * manual-group coverage on the original development sessions.",
        "validation": "Freeze a development choice, report the original holdout separately, and leave-one-session-out recipe selection across all 17 sessions. No image model training.",
        "started_unix": time.time()})
    runs = json.loads((ps.ROOT / "results/index_run.json").read_text(encoding="utf-8"))
    labels = json.loads((ps.ROOT / "results/label_validation.json").read_text(encoding="utf-8"))
    split = json.loads((ps.ROOT / "results/split.json").read_text(encoding="utf-8"))
    all_rows = []
    with threadpool_limits(limits=4):
        for phase in ["development", "holdout"]:
            rows = []
            for name in split[phase]:
                saved = Path(next(s["cache"] for s in runs["sessions"] if s["name"] == name))
                index = json.loads((saved / "index.json").read_text(encoding="utf-8"))
                with np.load(saved / "features.npz", allow_pickle=False) as data:
                    arrays = {k: data[k] for k in data.files}
                human_paths = {p["source"] for s in labels["sessions"] if s["name"] == name for p in s["pairs"]}
                human = [i for i, r in enumerate(index["records"]) if r["path"] in human_paths]
                start = time.perf_counter()
                prepared = context(index["records"], arrays)
                preparation_seconds = time.perf_counter() - start
                reference, similarity = ps.groups_for(index["records"], arrays["dino"], threshold=0.25)
                times = np.array([r["capture_time"] if r["capture_time"] is not None else np.nan for r in index["records"]])
                for method, recipe in recipes.items():
                    start = time.perf_counter()
                    chosen, _, _ = select(prepared, len(human), recipe)
                    elapsed = time.perf_counter() - start + preparation_seconds
                    result = metrics(chosen, human, similarity, times, reference)
                    rows.append({"session": name, "method": method, "n": len(index["records"]), "k": len(human),
                                 "selection_seconds": elapsed, "chosen_indices": chosen, **result})
                print(phase, name, flush=True)
            summary = aggregate(rows)
            ps.write_json(folder / f"{phase}.json", {"summary": summary, "rows": rows})
            all_rows.extend(rows)
            if phase == "development":
                winner = max(summary, key=lambda key: summary[key]["exact_overlap"] + 0.10 * summary[key]["manual_group_coverage"])
                ps.write_json(folder / "frozen_choice.json", {"name": winner, "recipe": recipes[winner], "frozen_at_unix": time.time()})
                print("Frozen development choice:", winner, flush=True)
    folds = []
    for name in split["development"] + split["holdout"]:
        training = aggregate([r for r in all_rows if r["session"] != name])
        winner = max(training, key=lambda key: training[key]["exact_overlap"] + 0.10 * training[key]["manual_group_coverage"])
        row = next(r for r in all_rows if r["session"] == name and r["method"] == winner)
        folds.append({**row, "recipe_chosen": winner, "method": "leave_one_session_out"})
    ps.write_json(folder / "cross_validation.json", {"summary": aggregate(folds), "rows": folds})
