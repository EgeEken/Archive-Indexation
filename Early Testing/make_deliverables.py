import csv
import html
import json
import math
from pathlib import Path

import numpy as np

import photo_select as ps
from contact_sheets import sheet


if __name__ == "__main__":
    def read(name):
        return json.loads((ps.ROOT / "results" / name).read_text(encoding="utf-8"))
    run, inventory, frozen = read("index_run.json"), read("archive_inventory_before.json"), read("frozen_choice.json")
    development, holdout, split = read("development.json"), read("holdout.json"), read("split.json")
    out = ps.ROOT / "selections"
    out.mkdir(exist_ok=True)
    links = []
    for cached in run["sessions"]:
        session = next(s for s in inventory["sessions"] if s["name"] == cached["name"])
        folder = Path(cached["cache"])
        index = json.loads((folder / "index.json").read_text(encoding="utf-8"))
        with np.load(folder / "features.npz", allow_pickle=False) as data:
            arrays = {k: data[k] for k in data.files}
        k = session["n_selected"] or max(1, math.ceil(session["n_source"] * 0.1))
        selection = ps.make_selection(index, arrays, k, **frozen["options"])
        selection["count_source"] = "manual selection count for comparison" if session["n_selected"] else "10% default; no manual selection exists"
        path = out / (cached["name"] + ".json")
        ps.write_json(path, selection)
        ps.write_review(selection, path.with_suffix(".html"), [p["source"] for p in session["matches"]])
        links.append((cached["name"], path, k, session["n_source"], selection["count_source"]))
        if cached["name"].startswith("2026.09.03"):
            demo = ps.make_selection(index, arrays, 100, **frozen["options"])
            ps.write_json(out / "kadikoy-100.json", demo)
            ps.write_review(demo, out / "kadikoy-100.html", [p["source"] for p in session["matches"]])
            chosen = [r for r in demo["records"] if r["selected"]]
            ordered = sorted(chosen, key=lambda r: r["capture_time"])
            sample = [ordered[int(i)] for i in np.linspace(0, len(ordered) - 1, 30)]
            manual = {p["source"] for p in session["matches"]}
            sheet([(r["thumbnail"], Path(r["path"]).name + (" · manual" if r["path"] in manual else "")) for r in sample],
                  ps.ROOT / "results/kadikoy-100-preview.jpg", "30 evenly spaced picks from the 100-photo shortlist")
    rows = development["rows"] + holdout["rows"]
    fields = ["split", "session", "method", "n", "k", "exact_hits", "exact_overlap", "near_match_hits", "near_match_overlap", "manual_group_coverage", "selected_near_duplicate_fraction", "selection_seconds"]
    with (ps.ROOT / "results/per_session_metrics.csv").open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "split": "development" if r["session"] in split["development"] else "holdout"})
    bootstraps = {}
    for baseline in ["random_100_seeds", "uniform", "technical_quality"]:
        difference = []
        for name in split["holdout"]:
            method = next(r for r in holdout["rows"] if r["session"] == name and r["method"] == frozen["name"])
            base = next(r for r in holdout["rows"] if r["session"] == name and r["method"] == baseline)
            difference.append(method["exact_overlap"] - base["exact_overlap"])
        samples = np.random.default_rng(20260906).choice(difference, size=(10000, len(difference)), replace=True).mean(axis=1)
        bootstraps[baseline] = {"macro_difference": float(np.mean(difference)),
                                "paired_session_bootstrap_95_percent": np.quantile(samples, [0.025, 0.975]).tolist()}
    ps.write_json(ps.ROOT / "results/uncertainty.json", bootstraps)
    page = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Photo selection experiment</title>
<style>body{font:17px system-ui;max-width:1000px;margin:45px auto;padding:0 24px;background:#15191d;color:#e5e9ed}a{color:#9cddbb}td,th{text-align:left;padding:12px;border-bottom:1px solid #3b4248}table{width:100%;border-collapse:collapse}p{line-height:1.6;color:#c6cdd4}.hero{background:#252d33;padding:24px;border-radius:12px}small{color:#aab7c1}img{max-width:100%}</style>
<h1>Photo selection experiment</h1><p>7,308 source JPEGs · 17 manually curated sessions · 3 pretrained models · all processing local.</p>
<div class="hero"><h2>Try the 100-photo shortlist</h2><p><a href="selections/kadikoy-100.html">Review 100 of 1,369 Kadıköy photos</a> · <a href="selections/kadikoy-100.json">Selection JSON</a></p><p>The provisional default uses MobileNet visual groups, capture times, focus and exposure. No original photos were copied for these results.</p></div>
<p><a href="REPORT.md">Evaluation report</a> · <a href="README.md">Commands and setup</a> · <a href="results/per_session_metrics.csv">Per-session metrics</a></p>
<h2>Comparison selections</h2><p>These use the same number of picks as your manual selections, where available. “Manual pick” labels are shown for comparison; selection algorithms do not read those labels.</p><table><tr><th>Session</th><th>Selected / source</th><th>Index</th></tr>__ROWS__</table>
<h2>Preview</h2><img src="results/kadikoy-100-preview.jpg" alt="Thirty sample photographs from the shortlist"></html>'''
    lines = []
    for name, path, k, n, count_source in links:
        relative = path.relative_to(ps.ROOT).as_posix()
        lines.append(f'<tr><td><a href="{html.escape(str(Path(relative).with_suffix(".html")).replace(chr(92), "/"))}">{html.escape(name)}</a><br><small>{html.escape(count_source)}</small></td><td>{k} / {n}</td><td><a href="{html.escape(relative)}">JSON</a></td></tr>')
    with (ps.ROOT / "review.html").open("x", encoding="utf-8") as f:
        f.write(page.replace("__ROWS__", "".join(lines)))
