"""Compare archive path, size and modification-time inventory with the initial snapshot."""
import json
from pathlib import Path

from inventory import inventory
from photo_select import ROOT, write_json


if __name__ == "__main__":
    before = json.loads((ROOT / "results/archive_inventory_before.json").read_text(encoding="utf-8"))
    after = inventory(Path(before["archive"]))
    left = {p["path"]: p for p in before["files"]}
    right = {p["path"]: p for p in after["files"]}
    result = {"before_files": len(left), "after_files": len(right),
              "added": sorted(right.keys() - left.keys()), "removed": sorted(left.keys() - right.keys()),
              "changed_size_or_mtime": [p for p in left.keys() & right.keys() if left[p] != right[p]],
              "scope": "Every archive file: relative path, byte size and mtime_ns. This is not an all-file cryptographic content comparison."}
    result["unchanged"] = not any(result[k] for k in ["added", "removed", "changed_size_or_mtime"])
    write_json(ROOT / "results/archive_integrity_after.json", result)
    print(json.dumps(result, indent=2))
    if not result["unchanged"]:
        raise SystemExit(1)
