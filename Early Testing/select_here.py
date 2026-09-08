"""Index the folder containing this script, open its local photo review page, and serve it."""

import argparse
import functools
import hashlib
import http.server
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
JPEG = {".jpg", ".jpeg"}


def source_fingerprint(source):
    paths = sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in JPEG)
    values = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in paths]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def choose_output(source, output_dir, method, count, ratio):
    base = output_dir / f"{source.name}.json"
    if not base.exists():
        return base, None
    try:
        manifest = json.loads(base.read_text(encoding="utf-8"))
        expected_count = count if count is not None else max(1, int((len(list(source.rglob("*.jpg"))) + len(list(source.rglob("*.jpeg")))) * ratio + 0.999999))
        options = manifest.get("options", {})
        if (manifest.get("fingerprint") == source_fingerprint(source)
                and manifest.get("count") == expected_count
                and options.get("method") == method
                and base.with_suffix(".html").exists()):
            return base, manifest
    except (OSError, json.JSONDecodeError):
        pass
    number = 2
    while True:
        candidate = output_dir / f"{source.name}-{number}.json"
        if not candidate.exists():
            return candidate, None
        number += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int)
    parser.add_argument("--ratio", type=float, default=0.10)
    parser.add_argument("--method", default="handcrafted",
                        choices=["handcrafted", "mobile", "dino", "clip", "mmr", "coverage"])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", type=Path,
                        help="Where to keep the JSON, HTML, and cache (default: Early Testing/.runs)")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    source = Path.cwd().resolve()
    output_dir = (args.output_dir or ROOT / ".runs" / source.parent.name / source.name).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output, existing = choose_output(source, output_dir, args.method, args.count, args.ratio)
    cache = output_dir / "cache"
    if existing is None:
        command = [sys.executable, str(ROOT / "photo_select.py"), "select", str(source),
                   "--output", str(output), "--cache", str(cache), "--method", args.method]
        if args.count is None:
            command.extend(["--ratio", str(args.ratio)])
        else:
            command.extend(["--count", str(args.count)])
        subprocess.run(command, check=True)
    else:
        print(f"Existing selection is unchanged; reusing {output}")

    server_root = Path(os.path.commonpath([source.parent, output_dir]))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(server_root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    page = output.with_suffix(".html")
    relative = page.relative_to(server_root).as_posix()
    url = f"http://127.0.0.1:{args.port}/{relative.replace(' ', '%20').replace('#', '%23')}"
    page_text = page.read_text(encoding="utf-8")
    browser_root = f"http://127.0.0.1:{args.port}"
    page_text = re.sub(r"http://127\.0\.0\.1:\d+", browser_root, page_text)
    page_text = page_text.replace(server_root.as_uri().rstrip("/"), browser_root)
    page.write_text(page_text, encoding="utf-8")
    print(f"Review page: {url}")
    print("Press Ctrl+C to stop the local viewer.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping local viewer.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
