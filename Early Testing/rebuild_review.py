"""Refresh generated UI/score manifests; preserve original experiment measurements."""
import json
from pathlib import Path

import numpy as np

import photo_select as ps
from review_ui import gallery_records, write_page


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def experiment_data():
    speed = read(ps.ROOT / "results/v2/speed.json")
    pipelines = {r["pipeline"]: r for r in speed["rows"]}
    preset = read(ps.ROOT / "results/presets.json")
    recipes = read(ps.ROOT / "results/v2/recipes.json")
    labels = {"uniform": "Even time spacing", "laplacian": "Sharpness only", "technical_quality": "Technical quality only",
              "aesthetic_only": "Aesthetic score only", "time_quality": "Time groups + quality", "handcrafted": "Handcrafted groups",
              "mobilenet": "MobileNet + quality", "dino_technical": "DINO + quality", "dino_aesthetic35": "DINO · 35% aesthetic",
              "dino_aesthetic65": "DINO · 65% aesthetic", "dino_tight": "DINO · tighter groups", "dino_loose": "DINO · looser groups",
              "dino_diverse": "DINO · stronger diversity", "clip_technical": "CLIP + quality", "clip_aesthetic35": "CLIP · 35% aesthetic",
              "clip_aesthetic65": "CLIP · 65% aesthetic", "random_100_seeds": "Random · 100 seeds"}
    frozen = read(ps.ROOT / "results/v2/frozen_choice.json")["name"]
    cached = {r["method"]: r["selection_seconds"] for r in read(ps.ROOT / "results/v2/cached_speed.json")["rows"]}
    sets = {}
    for phase in ["development", "holdout"]:
        old = read(ps.ROOT / f"results/{phase}.json")
        new = read(ps.ROOT / f"results/v2/{phase}.json")
        points = []
        for round_name, result in [("Original", old), ("New experiment", new)]:
            for name, s in result["summary"].items():
                if name == "human_reference":
                    continue
                if name in recipes:
                    pipe = "dino_clip"
                    description = str(recipes[name])
                    recipe = recipes[name]
                    label = f'{"MMR" if recipe["strategy"] == "mmr" else "Coverage"} · {recipe["aesthetic_weight"]:.0%} aesthetic / {recipe["diversity"]:.0%} diversity'
                else:
                    options = preset.get(name, {"method": "random"})
                    method = options["method"]
                    pipe = method if method in ["dino", "mobile", "clip"] else "handcrafted"
                    if method == "aesthetic" or options.get("aesthetic_weight"):
                        pipe = "dino_clip" if method == "dino" else "clip"
                    if method in ["uniform", "random"]:
                        pipe = None
                    description = str(options)
                    label = labels[name]
                times = pipelines[pipe] if pipe else None
                points.append({"name": name, "label": label, "round": round_name,
                               "exact": s["exact_overlap"], "near": s["near_match_overlap"],
                               "coverage": s["manual_group_coverage"], "duplicates": s["selected_near_duplicate_fraction"],
                               "index_seconds": times["index_seconds"] if times else None,
                               "cached_read_seconds": times["cached_read_seconds"] if times else None,
                               "selection_seconds": cached.get(name, 0),
                               "pipeline": pipe, "default": name == "mobilenet", "frozen": name == frozen,
                               "description": description})
        sets[phase] = points
    return {"type": "experiments", "title": "Selection tradeoffs", "sets": sets,
            "timing": speed, "frozen": frozen, "home": (ps.ROOT / "review.html").as_uri(),
            "csv": (ps.ROOT / "results/per_session_metrics.csv").as_uri(),
            "report": (ps.ROOT / "REPORT-V2.md").as_uri(),
            "preview": (ps.ROOT / "selections/kadikoy-experimental.html").as_uri()}


if __name__ == "__main__":
    run = read(ps.ROOT / "results/index_run.json")
    inventory = read(ps.ROOT / "results/archive_inventory_before.json")
    indexes, embeddings = {}, {}
    for s in run["sessions"]:
        cache = Path(s["cache"])
        index = read(cache / "index.json")
        indexes[index["source"]] = index
        with np.load(cache / "features.npz", allow_pickle=False) as arrays:
            embeddings[index["source"]] = {k: arrays[k] for k in arrays.files}
    archive, sessions = [], []
    for path in sorted((ps.ROOT / "selections").glob("*.json")):
        if path.name == "kadikoy-experimental.json":
            continue
        original = read(path)
        source = original["source"]
        selection = ps.make_selection(indexes[source], embeddings[source], original["count"], **original["options"])
        if selection["selected_paths"] != original["selected_paths"]:
            raise ValueError(f"UI refresh changed an existing shortlist: {path}")
        selection["count_source"] = original.get("count_source", "explicit count")
        selection["previous_selection_seconds"] = original["selection_seconds"]
        path.write_text(json.dumps(selection, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        session = next(s for s in inventory["sessions"] if s["source"] == source)
        manual = [p["source"] for p in session["matches"]]
        ps.write_review(selection, path.with_suffix(".html"), manual, replace=True)
        if path.stem == session["name"]:
            cards = gallery_records(selection, manual)
            url = path.with_suffix(".html").as_uri()
            for r in cards:
                r["session_url"] = url
            archive.extend(cards)
            cover = max((r for r in cards if r["selected"]), key=lambda r: r["quality"], default=cards[0])
            name = session["name"]
            sessions.append({"name": name, "date": name.split(" (")[0], "label": name.split(" (", 1)[-1].rstrip(")"),
                             "url": url, "cover": cover["thumbnail"], "count": selection["count"], "n": len(cards)})
        print("Updated", path.name, flush=True)
    raw_records = [{"quality_features": r["raw"]} for r in archive]
    components = ps.quality_components(raw_records)
    for i, r in enumerate(archive):
        r["archive_quality"] = float(components["technical"][i])
        r["archive_components"] = {key: float(values[i]) for key, values in components.items()}
    if not (ps.ROOT / "results/v2/archive_scores.json").exists():
        ps.write_json(ps.ROOT / "results/v2/archive_scores.json", {"score_scope": f"Shared empirical ranks across all {len(archive)} indexed images; not an absolute calibrated probability.",
                      "records": [{"path": r["path"], "image_quality_score": r["archive_quality"],
                                   "components": r["archive_components"]} for r in archive]})
    write_page(ps.ROOT / "all-photos.html", {"type": "archive", "title": "Highest-scoring photos", "records": archive,
               "home": (ps.ROOT / "review.html").as_uri()}, replace=True)
    write_page(ps.ROOT / "review.html", {"type": "dashboard", "title": "Sessions", "sessions": sessions,
               "total": len(archive), "archive": (ps.ROOT / "all-photos.html").as_uri(),
               "demo": (ps.ROOT / "selections/kadikoy-100.html").as_uri(),
               "report": (ps.ROOT / "REPORT-V2.md").as_uri(), "readme": (ps.ROOT / "README.md").as_uri()}, replace=True)
    chosen = read(ps.ROOT / "results/v2/frozen_choice.json")["recipe"]
    source = next(s for s in indexes if "2026.09.03" in s)
    experimental = ps.make_selection(indexes[source], embeddings[source], 100, method=chosen["strategy"],
                                     aesthetic_weight=chosen["aesthetic_weight"], diversity=chosen["diversity"])
    exp_path = ps.ROOT / "selections/kadikoy-experimental.json"
    exp_path.write_text(json.dumps(experimental, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    session = next(s for s in inventory["sessions"] if s["source"] == source)
    ps.write_review(experimental, exp_path.with_suffix(".html"), [p["source"] for p in session["matches"]], replace=True)
    if (ps.ROOT / "results/v2/speed.json").exists():
        write_page(ps.ROOT / "experiments.html", experiment_data(), replace=True)
