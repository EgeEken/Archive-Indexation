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
    run = commands.add_parser("run", help="start the localhost UI")
    run.add_argument("workspace", type=Path, nargs="?", help="optional workspace root")
    run.add_argument("--host", default="127.0.0.1", help="loopback host to bind")
    run.add_argument("--port", default=8765, type=int, help="TCP port to bind")
    serve = commands.add_parser("serve", help="start the localhost UI")
    serve.add_argument("workspace", type=Path, nargs="?", help="optional workspace root containing .archive-index")
    serve.add_argument("--host", default="127.0.0.1", help="loopback host to bind")
    serve.add_argument("--port", default=8765, type=int, help="TCP port to bind")
    index = commands.add_parser("index", help="scan and process a workspace")
    index.add_argument("workspace", type=Path, help="workspace root")
    index.add_argument("--quality-provider", choices=("off", "lar-iqa"), help="quality backend for this workspace")
    index.add_argument("--quality-batch-size", choices=(1, 2, 4, 8, 16), type=int)
    index.add_argument("--quality-preparation-workers", choices=(1, 2, 4, 8), type=int)
    index.add_argument("--thumbnail-workers", choices=(1, 2, 4, 8, 12), type=int)
    index.add_argument("--feature-workers", choices=(1, 2, 4, 8, 12), type=int)
    compact = commands.add_parser("compact", help="compact a workspace index database")
    compact.add_argument("workspace", type=Path, help="workspace root")
    recommend = commands.add_parser("recommend", help="rebuild automatic recommendations")
    recommend.add_argument("workspace", type=Path, help="workspace root")
    diagnostics = commands.add_parser("group-diagnostics", help="write strict-group candidate diagnostics")
    diagnostics.add_argument("workspace", type=Path, help="workspace root")
    diagnostics.add_argument("--limit", type=int, default=100, help="number of borderline rejected pairs to write")
    model = commands.add_parser("model", help="manage optional inference models")
    model_commands = model.add_subparsers(dest="model_command")
    install = model_commands.add_parser("install", help="download an optional model")
    install.add_argument("model_id", choices=("lar-iqa",), help="model to install")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    if args.command == "doctor":
        print(f"{APP_NAME} {__version__}: ready")
        return 0

    if args.command == "index":
        return _index_command(
            args.workspace,
            args.quality_provider,
            args.quality_batch_size,
            args.quality_preparation_workers,
            args.thumbnail_workers,
            args.feature_workers,
        )

    if args.command == "compact":
        return _compact_command(args.workspace)

    if args.command == "recommend":
        return _recommend_command(args.workspace)

    if args.command == "group-diagnostics":
        from .indexing.grouping import write_grouping_diagnostics
        from .workspace import Workspace

        destination = write_grouping_diagnostics(Workspace.open(args.workspace), args.limit)
        print(destination)
        return 0

    if args.command == "model" and args.model_command == "install":
        return _model_install_command(args.model_id)

    if args.command in {None, "run", "serve"}:
        from .api.server import serve
        from .workspace import Workspace

        workspace_root = getattr(args, "workspace", None)
        host = getattr(args, "host", "127.0.0.1")
        port = getattr(args, "port", 8765)
        serve(Workspace.open(workspace_root) if workspace_root else None, host=host, port=port)
        return 0

    parser.print_help()
    return 0


def _index_command(
    root: Path,
    quality_provider: str | None = None,
    quality_batch_size: int | None = None,
    quality_preparation_workers: int | None = None,
    thumbnail_workers: int | None = None,
    feature_workers: int | None = None,
) -> int:
    from .indexing.media_pipeline import index_workspace
    from .indexing.grouping import build_groups, extract_visual_features
    from .indexing.recommendation import build_recommendations
    from .indexing.scanner import scan
    from .timing import TimingRecorder
    from .jobs.engine import JobStore
    from .workspace import Workspace

    workspace = Workspace.open(root) if (root / ".archive-index").is_dir() else Workspace.create(root)
    if quality_provider is not None:
        workspace.set_quality_provider(quality_provider)
    JobStore(workspace).recover_interrupted()

    cancel_event = Event()
    timings = TimingRecorder()
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
            timings=timings,
        )
        if scan_result.cancelled:
            return 130
        media_result = index_workspace(
            workspace,
            components=("metadata", "thumbnail"),
            cancel_event=cancel_event,
            progress=_progress_reporter("media"),
            thumbnail_workers=thumbnail_workers,
            timings=timings,
        )
        if media_result.cancelled:
            return 130
        quality_result = index_workspace(
            workspace,
            components=("quality",),
            cancel_event=cancel_event,
            progress=_progress_reporter("quality"),
            quality_provider=quality_provider,
            quality_batch_size=quality_batch_size,
            quality_preparation_workers=quality_preparation_workers,
            timings=timings,
        )
        if quality_result.cancelled:
            return 130
        with timings.measure("visual_features.total"):
            feature_result = extract_visual_features(
                workspace,
                cancel_event=cancel_event,
                progress=_progress_reporter("features"),
                workers=feature_workers,
                timings=timings,
            )
        if feature_result.cancelled:
            return 130
        with timings.measure("grouping.total"):
            grouping_result = build_groups(
                workspace,
                cancel_event=cancel_event,
                progress=_progress_reporter("groups"),
            )
        with timings.measure("recommendations.total"):
            recommendation_result = build_recommendations(
                workspace,
                cancel_event=cancel_event,
                progress=_progress_reporter("recommendations"),
            )
        if recommendation_result.cancelled:
            return 130
        print(
            f"\nIndex complete: {media_result.succeeded + quality_result.succeeded} processed, "
            f"{media_result.skipped + quality_result.skipped} skipped, "
            f"{media_result.errors + quality_result.errors} failed; "
            f"{grouping_result.multi_image_groups} multi-image groups; "
            f"{recommendation_result.auto_recommended} recommendations.",
            flush=True,
        )
        LOGGER.info("indexing timings: %s", timings.summary())
        return 130 if media_result.cancelled else 0
    finally:
        signal.signal(signal.SIGINT, old_handler)


def _recommend_command(root: Path) -> int:
    from .indexing.recommendation import build_recommendations
    from .jobs.engine import JobStore
    from .workspace import Workspace

    workspace = Workspace.open(root)
    JobStore(workspace).recover_interrupted()
    cancel_event = Event()
    old_handler = signal.getsignal(signal.SIGINT)

    def request_cancel(signum, frame) -> None:
        cancel_event.set()
        print("\nCancellation requested; finishing the current group...", flush=True)

    signal.signal(signal.SIGINT, request_cancel)
    try:
        result = build_recommendations(
            workspace,
            cancel_event=cancel_event,
            progress=_progress_reporter("recommendations"),
        )
        print(
            f"\nRecommendation rebuild complete: {result.auto_recommended} recommendations.",
            flush=True,
        )
        return 130 if result.cancelled else 0
    finally:
        signal.signal(signal.SIGINT, old_handler)


def _compact_command(root: Path) -> int:
    from .workspace import Workspace, WorkspaceError, compact_database

    try:
        report = compact_database(Workspace.open(root))
    except WorkspaceError as error:
        print(f"Compaction refused: {error}")
        return 2
    mib = lambda value: value / (1024 * 1024)
    print(
        "SQLite compaction complete\n"
        f"size: {mib(report['before_size_bytes']):.2f} -> {mib(report['after_size_bytes']):.2f} MiB\n"
        f"page_count: {report['before_page_count']} -> {report['after_page_count']}\n"
        f"freelist_count: {report['before_freelist_count']} -> {report['after_freelist_count']}\n"
        f"integrity_check: {report['integrity_check']}"
    )
    return 0 if report["integrity_check"] == "ok" else 1


def _model_install_command(model_id: str) -> int:
    from urllib.request import Request, urlopen

    from .media.quality_provider import (
        LAR_IQA_CHECKPOINT_SHA256,
        LAR_IQA_MODEL_FILENAME,
        LAR_IQA_MODEL_ID,
        LAR_IQA_OFFICIAL_FILE_ID,
        _sha256,
        default_model_path,
    )

    if model_id != "lar-iqa":
        raise ValueError(f"unsupported model: {model_id}")
    destination = default_model_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    url = (
        "https://drive.usercontent.google.com/download?export=download&confirm=t&id="
        + LAR_IQA_OFFICIAL_FILE_ID
    )
    print(f"Downloading {LAR_IQA_MODEL_ID} to {destination}")
    try:
        request = Request(url, headers={"User-Agent": "Archive-Indexation/0.1"})
        with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        if temporary.stat().st_size < 1024:
            raise RuntimeError("model download was unexpectedly small")
        if _sha256(temporary) != LAR_IQA_CHECKPOINT_SHA256:
            raise RuntimeError("downloaded LAR-IQA checkpoint SHA-256 does not match the published model")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Installed {LAR_IQA_MODEL_FILENAME}")
    return 0


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
