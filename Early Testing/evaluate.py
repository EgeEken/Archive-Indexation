"""Session-level benchmark. Presets are frozen before evaluating the holdout."""
import argparse
import json
import time
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import maximum_bipartite_matching

import photo_select as ps

PRESETS = {
    "uniform": {"method": "uniform"},
    "laplacian": {"method": "laplacian"},
    "technical_quality": {"method": "quality"},
    "aesthetic_only": {"method": "aesthetic"},
    "time_quality": {"method": "time"},
    "handcrafted": {"method": "handcrafted"},
    "mobilenet": {"method": "mobile"},
    "dino_technical": {"method": "dino"},
    "dino_aesthetic35": {"method": "dino", "aesthetic_weight": 0.35},
    "dino_aesthetic65": {"method": "dino", "aesthetic_weight": 0.65},
    "dino_tight": {"method": "dino", "threshold": 0.20, "aesthetic_weight": 0.35},
    "dino_loose": {"method": "dino", "threshold": 0.40, "aesthetic_weight": 0.35},
    "dino_diverse": {"method": "dino", "diversity": 0.75, "aesthetic_weight": 0.35},
    "clip_technical": {"method": "clip"},
    "clip_aesthetic35": {"method": "clip", "aesthetic_weight": 0.35},
    "clip_aesthetic65": {"method": "clip", "aesthetic_weight": 0.65},
}


def metrics(chosen, human, dino_similarity, times, reference_groups):
    k = len(human)
    exact = len(set(chosen) & set(human))
    deltas = np.abs(times[np.array(chosen), None] - times[None, human])
    edges = (dino_similarity[np.ix_(chosen, human)] >= 0.90) & (deltas <= 120)
    for row, i in enumerate(chosen):
        for col, j in enumerate(human):
            if i == j:
                edges[row, col] = True
    matching = maximum_bipartite_matching(csr_matrix(edges), perm_type="column")
    matched = int((matching >= 0).sum())
    reference = set(reference_groups[human])
    coverage = len(reference & set(reference_groups[chosen])) / len(reference)
    near = dino_similarity[np.ix_(chosen, chosen)].copy()
    np.fill_diagonal(near, -1)
    duplicate_fraction = float((near.max(axis=1) >= 0.95).mean()) if chosen else 0
    return {"exact_hits": exact, "exact_overlap": exact / k, "near_match_hits": matched,
            "near_match_overlap": matched / k, "manual_group_coverage": coverage,
            "selected_near_duplicate_fraction": duplicate_fraction}


def aggregate(rows):
    result = {}
    for method in sorted({r["method"] for r in rows}):
        batch = [r for r in rows if r["method"] == method]
        result[method] = {"sessions": len(batch), "n": sum(r["n"] for r in batch), "k": sum(r["k"] for r in batch)}
        for field in ["exact_overlap", "near_match_overlap", "manual_group_coverage", "selected_near_duplicate_fraction", "selection_seconds"]:
            result[method][field] = float(np.mean([r[field] for r in batch]))
        result[method]["micro_exact_overlap"] = sum(r["exact_hits"] for r in batch) / sum(r["k"] for r in batch)
        result[method]["micro_near_match_overlap"] = sum(r["near_match_hits"] for r in batch) / sum(r["k"] for r in batch)
    return result


def run_phase(names, validation, cached, presets, verified_crops):
    rows = []
    for name in names:
        label = next(s for s in validation["sessions"] if s["name"] == name)
        if not all(p["identical"] or p["source"] in verified_crops for p in label["pairs"]):
            raise ValueError(f"Unverified labels in {name}")
        folder = Path(next(s for s in cached["sessions"] if s["name"] == name)["cache"])
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
        with np.load(folder / "features.npz", allow_pickle=False) as data:
            arrays = {k: data[k] for k in data.files}
        if index["errors"]:
            raise ValueError(f"Decode failures invalidate this benchmark: {name}")
        labels = {p["source"] for p in label["pairs"]}
        human = [i for i, r in enumerate(index["records"]) if r["path"] in labels]
        if len(human) != len(labels):
            raise ValueError("Labels are absent from source index")
        n, k = len(index["records"]), len(human)
        groups, similarity = ps.groups_for(index["records"], arrays["dino"], threshold=0.25)
        times = np.array([r["capture_time"] if r["capture_time"] is not None else np.nan for r in index["records"]])
        for method, options in presets.items():
            tick = time.perf_counter()
            chosen, _, _ = ps.select_indices(index["records"], arrays, k, **options)
            elapsed = time.perf_counter() - tick
            result = metrics(chosen, human, similarity, times, groups)
            rows.append({"session": name, "method": method, "n": n, "k": k,
                         "selection_seconds": elapsed, **result, "chosen_indices": chosen})
        random_runs = []
        for seed in range(100):
            chosen = np.random.default_rng(seed).choice(n, k, replace=False).tolist()
            random_runs.append(metrics(chosen, human, similarity, times, groups))
        random = {key: float(np.mean([r[key] for r in random_runs])) for key in random_runs[0]}
        rows.append({"session": name, "method": "random_100_seeds", "n": n, "k": k,
                     "selection_seconds": 0.0, **random,
                     "exact_overlap_std": float(np.std([r["exact_overlap"] for r in random_runs]))})
        rows.append({"session": name, "method": "human_reference", "n": n, "k": k,
                     "selection_seconds": 0.0, **metrics(human, human, similarity, times, groups)})
        print(f"Evaluated {name}: N={n}, K={k}", flush=True)
    return rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["development", "holdout"], required=True)
    args = parser.parse_args()
    def read(name):
        return json.loads((ps.ROOT / "results" / name).read_text(encoding="utf-8"))
    split = read("split.json")
    validation, cached = read("label_validation.json"), read("index_run.json")
    verified_crops = {p["source"] for p in read("label_exceptions.json")["verified_crops"] if p["template_correlation"] > 0.95}
    if args.phase == "development":
        if not (ps.ROOT / "results/presets.json").exists():
            ps.write_json(ps.ROOT / "results/presets.json", PRESETS)
    presets = read("presets.json")
    if args.phase == "holdout":
        frozen = read("frozen_choice.json")
    rows = run_phase(split[args.phase], validation, cached, presets, verified_crops)
    summary = aggregate(rows)
    ps.write_json(ps.ROOT / f"results/{args.phase}.json", {"phase": args.phase, "summary": summary, "rows": rows})
    if args.phase == "development":
        candidates = [name for name, options in presets.items() if options["method"] in ["dino", "clip", "mobile", "handcrafted"]]
        objective = {name: summary[name]["exact_overlap"] + 0.25 * summary[name]["near_match_overlap"] for name in candidates}
        winner = max(objective, key=objective.get)
        ps.write_json(ps.ROOT / "results/frozen_choice.json", {"name": winner, "options": presets[winner],
                      "objective": "Macro exact overlap + 0.25 * macro one-to-one near-match overlap; choose among visual grouping methods only.",
                      "development_objectives": objective, "frozen_at_unix": time.time()})
    print(json.dumps(summary, indent=2), flush=True)
