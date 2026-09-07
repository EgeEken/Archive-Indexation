import json
import os
from collections import Counter
from pathlib import Path

ARCHIVE = Path(os.environ["PHOTO_ARCHIVE_ROOT"]).resolve() if os.environ.get("PHOTO_ARCHIVE_ROOT") else None
OUTPUT = Path(__file__).resolve().parent / "results"
JPEG = {".jpg", ".jpeg"}


def inventory(root):
    if root is None:
        raise ValueError("Set PHOTO_ARCHIVE_ROOT to the archive to inventory")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            files.append({"path": str(path.relative_to(root)), "size": stat.st_size,
                          "mtime_ns": stat.st_mtime_ns})
    sessions = []
    for source in sorted(root.rglob("all-jpgs")):
        if not source.is_dir():
            continue
        originals = [p for p in sorted(source.rglob("*")) if p.suffix.lower() in JPEG and p.is_file()]
        selected_dir = source.parent / "jpgs"
        selected = [p for p in sorted(selected_dir.rglob("*")) if p.suffix.lower() in JPEG and p.is_file()]
        by_name = {}
        for p in originals:
            by_name.setdefault(p.name.casefold(), []).append(p)
        matches, unmatched, ambiguous = [], [], []
        for p in selected:
            candidates = by_name.get(p.name.casefold(), [])
            if len(candidates) == 1:
                original = candidates[0]
                matches.append({"source": str(original), "selected": str(p),
                                "same_size": original.stat().st_size == p.stat().st_size})
            elif candidates:
                ambiguous.append(str(p))
            else:
                unmatched.append(str(p))
        sessions.append({"name": str(source.parent.relative_to(root)), "source": str(source),
                         "n_source": len(originals), "n_selected": len(selected),
                         "matches": matches, "unmatched": unmatched, "ambiguous": ambiguous,
                         "source_paths": [str(p) for p in originals]})
    return {"archive": str(root), "files": files, "sessions": sessions}


if __name__ == "__main__":
    OUTPUT.mkdir(exist_ok=True)
    result = inventory(ARCHIVE)
    with (OUTPUT / "archive_inventory_before.json").open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    for s in result["sessions"]:
        print(f'{s["name"]}: {s["n_source"]} originals, {s["n_selected"]} selected, '
              f'{len(s["unmatched"])} unmatched, {len(s["ambiguous"])} ambiguous', flush=True)
    print("All files:", len(result["files"]), "bytes:", sum(p["size"] for p in result["files"]))
    print("Extensions:", Counter(Path(p["path"]).suffix.lower() for p in result["files"]))
