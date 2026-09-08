"""Index the folder containing this script, open its local photo review page, and serve it."""

import argparse
import functools
import http.server
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent


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
    output = output_dir / f"{source.name}.json"
    cache = output_dir / "cache"
    command = [sys.executable, str(ROOT / "photo_select.py"), "select", str(source),
               "--output", str(output), "--cache", str(cache), "--method", args.method]
    if args.count is None:
        command.extend(["--ratio", str(args.ratio)])
    else:
        command.extend(["--count", str(args.count)])
    subprocess.run(command, check=True)

    server_root = Path(os.path.commonpath([source.parent, output_dir]))
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(server_root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    page = output.with_suffix(".html")
    relative = page.relative_to(server_root).as_posix()
    url = f"http://127.0.0.1:{args.port}/{relative.replace(' ', '%20').replace('#', '%23')}"
    page_text = page.read_text(encoding="utf-8")
    page_text = page_text.replace(server_root.as_uri().rstrip("/"), f"http://127.0.0.1:{args.port}")
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
