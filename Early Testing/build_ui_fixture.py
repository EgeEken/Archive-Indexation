"""Synthetic browser-test data only. Does not read photographs or serve the archive."""
import base64
import io
from pathlib import Path

from PIL import Image, ImageDraw

from review_ui import write_page

ROOT = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:8769"


if __name__ == "__main__":
    out = ROOT / "ui-test"
    out.mkdir(exist_ok=True)
    thumbs = []
    for j in range(6):
        im = Image.new("RGB", (360 if j % 2 else 240, 240 if j % 2 else 360), (36 + j * 10, 70 + j * 9, 85 + j * 5))
        draw = ImageDraw.Draw(im)
        draw.ellipse((30, 50, 140, 160), fill=(130 + j * 10, 170, 150))
        draw.polygon([(0, 240), (190, 85), (360, 240)], fill=(55, 110 + j * 6, 105))
        stream = io.BytesIO()
        im.save(stream, "PNG")
        thumbs.append("data:image/png;base64," + base64.b64encode(stream.getvalue()).decode())
    records = []
    for i in range(250):
        q = ((i * 37) % 250) / 250
        components = {"focus": q, "detail": q, "contrast": q, "exposure": 1., "focus_contribution": .65 * q,
                      "detail_contribution": .2 * q, "contrast_contribution": .15 * q, "clipping_penalty": 0., "technical": q}
        records.append({"name": f"SYNTHETIC-{i:04}.jpg", "path": "synthetic fixture", "original": thumbs[i % 6],
                        "thumbnail": thumbs[i % 6], "session": "2026.01.01 (Synthetic coast)" if i < 125 else "2026.01.02 (Synthetic park)",
                        "time": 1700000000 + i * 60, "group": i // 8 + 1, "group_id": i // 8,
                        "selected": i % 3 == 0, "manual": i % 5 == 0, "rank": i // 3 + 1 if i % 3 == 0 else None,
                        "quality": q, "archive_quality": 1 - q, "components": components,
                        "archive_components": {**components, "technical": 1 - q}, "preference": q,
                        "selection": {"selection_score": .3 * q + .4, "quality_contribution": .3 * q,
                                      "diversity_contribution": .4, "duplicate_penalty": 0., "evaluated_at_pick": 1},
                        "aesthetic_raw": 5., "aesthetic_weight": 0., "raw": {"focus": .234, "laplacian": 40.},
                        "width": 4240, "height": 2832, "iso": 200, "camera": "Synthetic fixture", "session_url": BASE + "/session.html"})
    write_page(out / "session.html", {"type": "gallery", "title": "Synthetic interaction test", "method": "mobile",
               "records": records, "count": sum(r["selected"] for r in records), "home": BASE + "/review.html", "navigation_base": BASE}, replace=True)
    write_page(out / "all-photos.html", {"type": "archive", "title": "Synthetic archive ranking", "records": records,
               "home": BASE + "/review.html", "navigation_base": BASE}, replace=True)
    write_page(out / "review.html", {"type": "dashboard", "title": "Synthetic sessions", "total": 250,
               "sessions": [{"name": "Synthetic session", "date": "2026.01.01", "label": "Generated browser-test images",
                             "cover": thumbs[0], "count": 84, "n": 250, "url": BASE + "/session.html"}],
               "archive": BASE + "/all-photos.html", "demo": BASE + "/session.html", "report": BASE + "/experiments.html",
               "readme": BASE + "/experiments.html", "navigation_base": BASE}, replace=True)
    points = [{"name": f"method_{i}", "label": f"Test method {i}", "round": "Original" if i < 5 else "New experiment",
               "exact": .15 + .012 * i, "near": .3 + .025 * i, "coverage": .4 + .04 * i,
               "duplicates": .3 - .02 * i, "index_seconds": 35 + 5 * (i % 3), "cached_read_seconds": .2,
               "selection_seconds": .03 + i * .01, "default": i == 1, "frozen": i == 7} for i in range(10)]
    points[1]["name"] = "mobilenet"
    write_page(out / "experiments.html", {"type": "experiments", "title": "Synthetic chart test",
               "sets": {"development": points, "holdout": points}, "home": BASE + "/review.html",
               "navigation_base": BASE, "report": BASE + "/review.html", "preview": BASE + "/session.html"}, replace=True)
