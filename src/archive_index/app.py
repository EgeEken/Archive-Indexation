"""Command-line entry point for the production application."""

from __future__ import annotations

import argparse
import logging
import signal
import time
from collections.abc import Sequence
from pathlib import Path
from threading import Event

from . import APP_NAME, __version__
from .logging_config import configure_logging

LOGGER = logging.getLogger(APP_NAME)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive-index",
        description="Local-first tools for indexing and browsing personal media archives.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )

    commands = parser.add_subparsers(dest="command")
    commands.add_parser("doctor", help="check that the application shell can start")
    commands.add_parser("run", help="start the production application shell")
    serve = commands.add_parser("serve", help="start the localhost UI")
    serve.add_argument("workspace", type=Path, nargs="?", help="optional workspace root containing .archive-index")
    serve.add_argument("--host", default="127.0.0.1", help="loopback host to bind")
    serve.add_argument("--port", default=8765, type=int, help="TCP port to bind")
    index = commands.add_parser("index", help="scan and process a workspace")
    index.add_argument("workspace", type=Path, help="workspace root")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    if args.command == "doctor":
        print(f"{APP_NAME} {__version__}: ready")
        return 0

    if args.command == "run":
        LOGGER.info("starting production application shell")
        print("Archive Indexation application shell is ready.")
        return 0

    if args.command == "index":
        return _index_command(args.workspace)

    if args.command == "serve":
        from .api.server import serve
        from .workspace import Workspace

        serve(Workspace.open(args.workspace) if args.workspace else None, host=args.host, port=args.port)
        return 0

    parser.print_help()
    return 0


def _index_command(root: Path) -> int:
    from .indexing.media_pipeline import index_workspace
    from .indexing.scanner import scan
    from .workspace import Workspace

    workspace = Workspace.open(root) if (root / ".archive-index").is_dir() else Workspace.create(root)

    cancel_event = Event()
    old_handler = signal.getsignal(signal.SIGINT)

    def request_cancel(signum, frame) -> None:
        if not cancel_event.is_set():
            cancel_event.set()
            print("\nCancellation requested; finishing the current item...", flush=True)

    signal.signal(signal.SIGINT, request_cancel)
    try:
        scan_result = scan(
            workspace,
            cancel_event=cancel_event,
            progress=_progress_reporter("scan"),
        )
        if scan_result.cancelled:
            return 130
        media_result = index_workspace(
            workspace,
            cancel_event=cancel_event,
            progress=_progress_reporter("media"),
        )
        print(
            f"\nIndex complete: {media_result.succeeded} processed, "
            f"{media_result.skipped} skipped, {media_result.errors} failed.",
            flush=True,
        )
        return 130 if media_result.cancelled else 0
    finally:
        signal.signal(signal.SIGINT, old_handler)


def _progress_reporter(label: str):
    from .jobs.engine import JobProgress

    started = time.perf_counter()
    last_print = 0.0

    def report(progress: JobProgress) -> None:
        nonlocal last_print
        now = time.perf_counter()
        if progress.processed != progress.total and now - last_print < 0.25:
            return
        last_print = now
        elapsed = max(now - started, 0.001)
        rate = progress.processed / elapsed
        percent = (100 * progress.processed / progress.total) if progress.total else 100
        print(
            f"\r{label} [{progress.stage}]: {progress.processed}/{progress.total} "
            f"({percent:5.1f}%) {rate:6.2f}/s "
            f"failed {progress.failed} skipped {progress.skipped}",
            end="",
            flush=True,
        )

    return report


if __name__ == "__main__":
    raise SystemExit(main())
