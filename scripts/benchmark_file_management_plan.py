"""Measure cold, warm, and review File Management planning on a disposable fixture."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image

from archive_index.file_management import build_dry_run_plan, list_profiles, plan_analysis_status, save_ruleset, start_plan_analysis
from archive_index.file_management_executor import prepare_execution
from archive_index.indexing.scanner import scan
from archive_index.workspace import Workspace


def _make_fixture(root: Path) -> None:
    base = root / "base.jpg"
    Image.effect_noise((6000, 4000), 70).convert("RGB").save(base, quality=95)
    original = base.read_bytes()
    base.unlink()
    payload = b"UNKNOWN-APP2" + b"x" * 300
    segment = b"\xff\xe2" + (len(payload) + 2).to_bytes(2, "big") + payload
    for index in range(11):
        (root / f"photo-{index:02d}.jpg").write_bytes(original[:2] + segment + original[2:])
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", "testsrc2=size=640x360:rate=24", "-t", "2", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(root / "video-template.mp4"),
        ],
        check=True,
    )
    video = (root / "video-template.mp4").read_bytes()
    (root / "video-template.mp4").unlink()
    for index in range(2):
        (root / f"clip-{index:02d}.mp4").write_bytes(video)
    with Image.new("RGB", (32, 32), (80, 120, 160)) as image:
        for index in range(21):
            image.save(root / f"delete-{index:02d}.png")


def _wait(workspace: Workspace, session_id: str) -> dict[str, object]:
    while True:
        status = plan_analysis_status(workspace, session_id)
        if status["status"] != "running":
            return status
        time.sleep(0.02)


def _timed_plan(workspace: Workspace, ruleset_id: str) -> tuple[dict[str, object], dict[str, float]]:
    phases: dict[str, float] = {}
    started = time.perf_counter()
    active = None

    def progress(phase, completed, total):
        nonlocal active
        if phase != active:
            phases[phase] = time.perf_counter() - started
            active = phase

    plan = build_dry_run_plan(workspace, ruleset_id, progress=progress)
    phases["total"] = time.perf_counter() - started
    return plan, phases


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="archive-index-plan-benchmark-") as directory:
        root = Path(directory)
        _make_fixture(root)
        workspace = Workspace.create(root)
        scan(workspace)
        jxl = next(item for item in list_profiles(workspace) if item["name"] == "JXL Balanced")
        av1 = next(item for item in list_profiles(workspace) if item["name"] == "AV1 4K120 Very Fast")
        ruleset = save_ruleset(
            workspace,
            name="benchmark",
            rules=[
                {"match": {"format": "jpeg"}, "action": {"operation": "compress", "profile_id": jxl["id"], "source_disposition": "replace"}},
                {"match": {"format": "mp4"}, "action": {"operation": "compress", "profile_id": av1["id"], "source_disposition": "replace"}},
                {"match": {"format": "png"}, "action": {"operation": "delete"}},
            ],
        )
        cold, cold_phases = _timed_plan(workspace, ruleset["id"])
        warm, warm_phases = _timed_plan(workspace, ruleset["id"])
        session_id = start_plan_analysis(workspace, ruleset["id"])
        session = _wait(workspace, session_id)
        review_started = time.perf_counter()
        execution = prepare_execution(workspace, ruleset["id"], session["result"]["plan_digest"], session_id)
        review_seconds = time.perf_counter() - review_started
        connection = workspace.connect()
        try:
            cache_count = connection.execute("SELECT COUNT(*) FROM compression_source_analysis").fetchone()[0]
            cached_rows = connection.execute("SELECT codec, source_size_bytes, analysis_json FROM compression_source_analysis ORDER BY codec").fetchall()
        finally:
            connection.close()
        analyses = [(row["codec"], row["source_size_bytes"], json.loads(row["analysis_json"])) for row in cached_rows]
        jxl_analysis = [(size, value) for codec, size, value in analyses if codec == "jpeg-xl"]
        av1_analysis = [value for codec, _, value in analyses if codec == "av1"]
        print(json.dumps({
            "files": 34,
            "cold_seconds": cold_phases["total"],
            "warm_seconds": warm_phases["total"],
            "review_seconds": review_seconds,
            "cold_phases": cold_phases,
            "warm_phases": warm_phases,
            "candidate_count": cold["summary"]["candidate_count"],
            "blocker_count": cold["summary"]["blocked_count"],
            "cache_rows": cache_count,
            "jxl_source_bytes": sum(int(size or 0) for size, _ in jxl_analysis),
            "jxl_metadata_bytes_read": sum(int((value.get("inventory") or {}).get("bytes_read", 0)) for _, value in jxl_analysis),
            "av1_cached_timing": [value.get("timing") for value in av1_analysis],
            "execution_status": execution["status"],
        }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
