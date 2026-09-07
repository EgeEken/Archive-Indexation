import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from inventory import ARCHIVE, inventory
from photo_select import ROOT, write_json


class PayloadParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reading = False
        self.chunks = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("id") == "review-data":
            self.reading = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.reading = False

    def handle_data(self, data):
        if self.reading:
            self.chunks.append(data)


if __name__ == "__main__":
    pages = [ROOT / "review.html", ROOT / "all-photos.html", ROOT / "experiments.html", *list((ROOT / "selections").glob("*.html"))]
    count = 0
    for page in pages:
        parser = PayloadParser()
        parser.feed(page.read_text(encoding="utf-8"))
        payload = json.loads("".join(parser.chunks))
        for record in payload.get("records", []):
            count += 1
            for field in ["original", "thumbnail", "session_url"]:
                if field in record:
                    path = Path(unquote(urlparse(record[field]).path).lstrip("/"))
                    if not path.is_file():
                        raise ValueError(f"Broken {field}: {path}")
            if not 0 <= record["quality"] <= 1:
                raise ValueError("Invalid quality range")
            values = record.get("selection")
            if values and abs(values["selection_score"] - (values["quality_contribution"] + values["diversity_contribution"] - values["duplicate_penalty"])) > 1e-8:
                raise ValueError("Selection contributions do not sum to score")
        if payload["type"] == "gallery":
            if sum(r["selected"] for r in payload["records"]) != payload["count"]:
                raise ValueError("Selection count changed")
        if payload["type"] == "archive":
            records = payload["records"]
            if len({r["path"] for r in records}) != len(records):
                raise ValueError("Archive view includes duplicate source paths")
            if len(records) != 7308:
                raise ValueError("Archive view lost records")
    before = json.loads((ROOT / "results/v2/archive_before.json").read_text(encoding="utf-8"))
    after = inventory(ARCHIVE)
    left = {p["path"]: p for p in before["files"]}
    right = {p["path"]: p for p in after["files"]}
    result = {"before_files": len(left), "after_files": len(right), "added": sorted(right.keys() - left.keys()),
              "removed": sorted(left.keys() - right.keys()),
              "changed": [p for p in left.keys() & right.keys() if left[p] != right[p]],
              "scope": "All archive relative paths, sizes and nanosecond modification timestamps."}
    result["unchanged"] = not result["added"] and not result["removed"] and not result["changed"]
    write_json(ROOT / "results/v2/archive_integrity.json", result)
    write_json(ROOT / "results/v2/ui_integrity.json", {"pages": len(pages), "record_references": count,
               "checks": ["Embedded JSON decodes", "Original and thumbnail references exist", "Selection totals reconstruct", "Selected counts match", "Archive source paths unique", "All indexed photos present"],
               "browser_checks": ["Synthetic 250-image gallery", "Descending quality", "Chronological timeline", "Bounded group dropdown", "Score modal", "Archive ranking", "Session rows", "Search empty state", "Pagination", "Back navigation", "Chart controls", "390px viewport without horizontal overflow", "No console errors"]})
    print(json.dumps({"pages": len(pages), "record_references": count, "archive": result}, indent=2))
    if not result["unchanged"]:
        raise SystemExit(1)
