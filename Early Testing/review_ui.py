"""Generate offline review pages with embedded data, styles and interactions."""
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def write_page(output, payload, replace=False):
    output = Path(output)
    if output.suffix.lower() != ".html":
        raise ValueError("Review output must be an HTML file")
    from photo_select import safe_output
    safe_output(output)
    css = (ROOT / "ui/review.css").read_text(encoding="utf-8")
    js = (ROOT / "ui/review.js").read_text(encoding="utf-8")
    base = payload.get("navigation_base", ROOT.as_uri())
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    page = f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(payload["title"])} · Photo review</title>
<style>{css}</style></head><body><a class="skip" href="#content">Skip to photos</a>
<header class="topbar"><a class="brand" href="{html.escape(base)}/review.html"><span class="brand-mark">▧</span> Photo review</a>
<nav aria-label="Main navigation"><a href="{html.escape(base)}/review.html">Sessions</a><a href="{html.escape(base)}/all-photos.html">All photos</a><a href="{html.escape(base)}/experiments.html">Experiments</a></nav></header>
<div id="app"></div><dialog id="info-dialog" aria-labelledby="info-title"><div id="info-content"></div></dialog>
<noscript>This local review page requires JavaScript. Selection JSON files remain available beside each session page.</noscript>
<script id="review-data" type="application/json">{data}</script><script>{js}</script></body></html>'''
    with output.open("w" if replace else "x", encoding="utf-8") as f:
        f.write(page)


def gallery_records(selection, manual_paths=None):
    from photo_select import quality_components
    manual = set(manual_paths or [])
    records = selection["records"]
    components = quality_components(records)
    group_times = {}
    for r in records:
        group_times[r["group"]] = min(group_times.get(r["group"], float("inf")),
                                       r["capture_time"] if r["capture_time"] is not None else float("inf"))
    groups = {g: i + 1 for i, g in enumerate(sorted(group_times, key=lambda g: (group_times[g], g)))}
    session = Path(selection["source"]).parent.name
    return [{"name": Path(r["path"]).name, "path": r["path"], "original": Path(r["path"]).as_uri(),
             "thumbnail": Path(r["thumbnail"]).as_uri(), "session": session,
             "time": r["capture_time"], "group": groups[r["group"]], "group_id": r["group"],
             "selected": r["selected"], "manual": r["path"] in manual, "rank": r["rank"],
             "quality": float(components["technical"][i]), "preference": r["quality_score"],
             "components": {key: float(values[i]) for key, values in components.items()},
             "selection": r.get("selection_details"), "aesthetic_raw": r.get("aesthetic"),
             "aesthetic_weight": selection["options"].get("aesthetic_weight", 0),
             "raw": r["quality_features"], "width": r["width"], "height": r["height"],
             "camera": r.get("camera", ""), "iso": r.get("iso", 0)} for i, r in enumerate(records)]


def write_gallery(selection, output, manual_paths=None, replace=False):
    write_page(output, {"type": "gallery", "title": Path(selection["source"]).parent.name,
                       "method": selection["options"].get("method", "dino"),
                       "count": selection["count"], "source": selection["source"],
                       "records": gallery_records(selection, manual_paths),
                       "manifest": Path(output).with_suffix(".json").resolve().as_uri(),
                       "home": ROOT.joinpath("review.html").as_uri()}, replace=replace)
