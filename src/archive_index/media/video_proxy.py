"""On-demand browser playback proxies for incompatible video sources."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from ..indexing.scanner import hash_file
from ..workspace import Workspace, WorkspaceError

PROXY_CONTRACT_VERSION = "h264-aac-1080p60-v1"
_workers: dict[tuple[str, str], tuple[threading.Thread, threading.Event]] = {}
_lock = threading.Lock()
_session_cache: dict[tuple[str, str, str, int, int], str] = {}
_active_serves: set[tuple[str, str]] = set()
MAX_PROXY_BYTES = 10 * 1024 * 1024 * 1024
MAX_PROXY_COUNT = 32


def browser_compatible(codec: str | None) -> bool:
    return str(codec or "").casefold() in {"h264", "avc1", "av1", "vp8", "vp9"}


def proxy_status(workspace: Workspace, physical_id: str) -> dict[str, object]:
    connection = workspace.connect()
    try:
        row = connection.execute(
            "SELECT pf.relative_path, pf.sha256, pf.size_bytes, pf.mtime_ns, pf.codec, pf.is_online, pf.in_scope, vp.id AS proxy_id, vp.status AS proxy_status, vp.source_sha256 AS proxy_source_sha256, vp.source_size_bytes AS proxy_source_size_bytes, vp.source_mtime_ns AS proxy_source_mtime_ns, vp.proxy_relative_path, vp.proxy_sha256, vp.proxy_size_bytes, vp.proxy_mtime_ns, vp.error_message, vp.progress_json FROM physical_file AS pf LEFT JOIN video_playback_proxy AS vp ON vp.physical_file_id = pf.id AND vp.contract_version = ? WHERE pf.id = ?",
            (PROXY_CONTRACT_VERSION, physical_id),
        ).fetchone()
    finally:
        connection.close()
    if row is None or not row["is_online"] or not row["in_scope"]:
        raise ValueError("Video is offline.")
    if browser_compatible(row["codec"]):
        return {"status": "not_required", "direct": True}
    status = row["proxy_status"]
    if status == "complete" and _proxy_is_current(workspace, row):
        key = _cache_key(workspace, physical_id, row)
        with _lock:
            _session_cache[key] = row["proxy_relative_path"]
        with workspace.transaction() as connection:
            connection.execute("UPDATE video_playback_proxy SET last_accessed_at = ?, updated_at = ? WHERE id = ?", (_timestamp(), _timestamp(), row["proxy_id"]))
        return {"status": "complete", "proxy_url": f"/api/files/{physical_id}/playback-proxy/media"}
    if status == "complete":
        with workspace.transaction() as connection:
            connection.execute("UPDATE video_playback_proxy SET status = 'pending', proxy_sha256 = NULL, proxy_size_bytes = NULL, proxy_mtime_ns = NULL, updated_at = datetime('now') WHERE id = ?", (row["proxy_id"],))
        status = "pending"
    with _lock:
        worker = _workers.get((str(workspace.root), physical_id))
        running = worker is not None and worker[0].is_alive()
    return {"status": "running" if running or status == "running" else (status or "pending"), "error": row["error_message"], "progress": _json(row["progress_json"]) or {}}


def request_proxy(workspace: Workspace, physical_id: str) -> dict[str, object]:
    status = proxy_status(workspace, physical_id)
    if status["status"] in {"complete", "not_required", "running"}:
        if status["status"] == "running":
            return status
        return status
    connection = workspace.connect()
    try:
        source = connection.execute("SELECT id, relative_path, sha256, size_bytes, mtime_ns, duration_seconds, codec, is_online, in_scope FROM physical_file WHERE id = ?", (physical_id,)).fetchone()
    finally:
        connection.close()
    if source is None or not source["is_online"] or not source["in_scope"]:
        raise ValueError("Video is offline.")
    if browser_compatible(source["codec"]):
        return {"status": "not_required", "direct": True}
    proxy_relative = f"video-proxies/{source['sha256']}-{PROXY_CONTRACT_VERSION}.mp4"
    proxy_id = str(uuid.uuid4())
    with workspace.transaction() as connection:
        connection.execute(
            """
            INSERT INTO video_playback_proxy(
                id, physical_file_id, source_relative_path, source_sha256, source_size_bytes,
                source_mtime_ns, contract_version, proxy_relative_path, status, progress_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '{}', datetime('now'), datetime('now'))
            ON CONFLICT(physical_file_id, contract_version) DO UPDATE SET
                source_relative_path = excluded.source_relative_path,
                source_sha256 = excluded.source_sha256,
                source_size_bytes = excluded.source_size_bytes,
                source_mtime_ns = excluded.source_mtime_ns,
                proxy_relative_path = excluded.proxy_relative_path,
                status = 'pending', error_message = NULL, updated_at = datetime('now')
            """,
            (proxy_id, physical_id, source["relative_path"], source["sha256"], source["size_bytes"], source["mtime_ns"], PROXY_CONTRACT_VERSION, proxy_relative),
        )
    cancel = threading.Event()
    thread = threading.Thread(target=_run, args=(workspace, physical_id, cancel), name=f"archive-video-proxy-{physical_id[:8]}", daemon=True)
    with _lock:
        _workers[(str(workspace.root), physical_id)] = (thread, cancel)
    thread.start()
    return {"status": "running", "progress": {}}


def cancel_proxy(workspace: Workspace, physical_id: str) -> bool:
    with _lock:
        worker = _workers.get((str(workspace.root), physical_id))
    if worker is None:
        return False
    worker[1].set()
    return True


def proxy_path(workspace: Workspace, physical_id: str) -> Path:
    connection = workspace.connect()
    try:
        row = connection.execute("SELECT proxy_relative_path, status, proxy_size_bytes, proxy_mtime_ns FROM video_playback_proxy WHERE physical_file_id = ? AND contract_version = ?", (physical_id, PROXY_CONTRACT_VERSION)).fetchone()
    finally:
        connection.close()
    if row is None or row["status"] != "complete":
        raise ValueError("Playable proxy is not ready.")
    try:
        path = workspace.index_path(row["proxy_relative_path"])
        stat = path.stat()
    except (OSError, WorkspaceError) as error:
        raise ValueError("Playable proxy is not ready.") from error
    if not path.is_file() or int(row["proxy_size_bytes"] or 0) != stat.st_size or int(row["proxy_mtime_ns"] or 0) != stat.st_mtime_ns:
        raise ValueError("Playable proxy is not ready.")
    with _lock:
        _active_serves.add((str(workspace.root), physical_id))
    with workspace.transaction() as connection:
        connection.execute("UPDATE video_playback_proxy SET last_accessed_at = ?, updated_at = ? WHERE physical_file_id = ? AND contract_version = ?", (_timestamp(), _timestamp(), physical_id, PROXY_CONTRACT_VERSION))
    return path


def release_proxy(workspace: Workspace, physical_id: str) -> None:
    with _lock:
        _active_serves.discard((str(workspace.root), physical_id))


def clear_playback_cache(workspace: Workspace) -> dict[str, int]:
    removed = 0
    skipped = 0
    with workspace.transaction() as connection:
        rows = connection.execute("SELECT id, physical_file_id, proxy_relative_path FROM video_playback_proxy WHERE status = 'complete'").fetchall()
        for row in rows:
            key = (str(workspace.root), row["physical_file_id"])
            with _lock:
                active = key in _active_serves or key in _workers
            if active:
                skipped += 1
                continue
            _remove_owned_proxy(workspace, row["proxy_relative_path"])
            connection.execute("DELETE FROM video_playback_proxy WHERE id = ?", (row["id"],))
            removed += 1
    with _lock:
        _session_cache.clear()
    return {"removed": removed, "skipped": skipped}


def _run(workspace: Workspace, physical_id: str, cancel: threading.Event) -> None:
    process = None
    try:
        connection = workspace.connect()
        try:
            row = connection.execute("SELECT * FROM video_playback_proxy WHERE physical_file_id = ? AND contract_version = ?", (physical_id, PROXY_CONTRACT_VERSION)).fetchone()
            source = connection.execute("SELECT relative_path, sha256, size_bytes, mtime_ns, duration_seconds FROM physical_file WHERE id = ?", (physical_id,)).fetchone()
        finally:
            connection.close()
        if row is None or source is None:
            raise ValueError("Video proxy source is unavailable.")
        source_path = workspace.absolute_path(source["relative_path"])
        if not _source_current(source_path, source):
            raise ValueError("Video changed before proxy creation.")
        output = workspace.index_path(row["proxy_relative_path"])
        output.parent.mkdir(parents=True, exist_ok=True)
        temp = output.with_name(f".{output.name}.{row['id']}.tmp")
        temp.unlink(missing_ok=True)
        filters = ["scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2"]
        source_fps = _source_fps(source_path)
        if source_fps is not None and source_fps > 60.001:
            filters.append("fps=60")
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source_path),
            "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "23", "-pix_fmt", "yuv420p", "-vf", ",".join(filters),
            "-fps_mode", "passthrough", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-f", "mp4", str(temp),
        ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        progress = {}
        assert process.stdout is not None
        for line in process.stdout:
            if cancel.is_set():
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                raise InterruptedError("Playback proxy creation cancelled.")
            if "=" not in line:
                continue
            key, value = line.rstrip().split("=", 1)
            if key in {"out_time_ms", "speed", "frame", "fps"}:
                progress[key] = value
                _update_progress(workspace, row["id"], progress, source["duration_seconds"])
        stderr = process.stderr.read() if process.stderr else ""
        if process.wait() != 0:
            raise RuntimeError(_clean_error(stderr) or "FFmpeg could not create a playable proxy.")
        if not temp.is_file() or temp.stat().st_size <= 0:
            raise RuntimeError("FFmpeg returned an empty playable proxy.")
        if not _source_current(source_path, source):
            temp.unlink(missing_ok=True)
            raise ValueError("Video changed during proxy creation.")
        os.replace(temp, output)
        digest = hash_file(output)
        with workspace.transaction() as connection:
            output_stat = output.stat()
            connection.execute("UPDATE video_playback_proxy SET status = 'complete', proxy_sha256 = ?, proxy_size_bytes = ?, proxy_mtime_ns = ?, progress_json = ?, error_message = NULL, last_accessed_at = ?, updated_at = ? WHERE id = ?", (digest, output_stat.st_size, output_stat.st_mtime_ns, json.dumps(progress, sort_keys=True), _timestamp(), _timestamp(), row["id"]))
        _evict_completed(workspace)
    except InterruptedError as error:
        _finish(workspace, physical_id, "cancelled", str(error))
    except Exception as error:
        _finish(workspace, physical_id, "failed", _clean_error(str(error)))
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        with _lock:
            _workers.pop((str(workspace.root), physical_id), None)


def _source_current(path: Path, source) -> bool:
    try:
        stat = path.stat()
        return path.is_file() and stat.st_size == int(source["size_bytes"]) and stat.st_mtime_ns == int(source["mtime_ns"]) and hash_file(path) == source["sha256"]
    except OSError:
        return False


def _source_fps(path: Path) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=5, check=False,
        )
        value = result.stdout.strip()
        if result.returncode or not value or value in {"0/0", "N/A"}:
            return None
        return float(Fraction(value))
    except (OSError, ValueError, ZeroDivisionError, subprocess.TimeoutExpired):
        return None


def _proxy_is_current(workspace: Workspace, row) -> bool:
    try:
        if row["proxy_source_sha256"] != row["sha256"] or int(row["proxy_source_size_bytes"] or 0) != int(row["size_bytes"] or 0) or int(row["proxy_source_mtime_ns"] or 0) != int(row["mtime_ns"] or 0):
            return False
        source = workspace.absolute_path(row["relative_path"])
        proxy = workspace.index_path(row["proxy_relative_path"])
        source_stat = source.stat()
        proxy_stat = proxy.stat()
        return source.is_file() and source_stat.st_size == int(row["size_bytes"] or 0) and source_stat.st_mtime_ns == int(row["mtime_ns"] or 0) and proxy.is_file() and proxy_stat.st_size == int(row["proxy_size_bytes"] or 0) and proxy_stat.st_mtime_ns == int(row["proxy_mtime_ns"] or 0)
    except (OSError, WorkspaceError):
        return False


def _cache_key(workspace: Workspace, physical_id: str, row) -> tuple[str, str, str, int, int]:
    return (str(workspace.root), physical_id, str(row["sha256"] or ""), int(row["size_bytes"] or 0), int(row["mtime_ns"] or 0))


def _evict_completed(workspace: Workspace) -> None:
    with workspace.transaction() as connection:
        rows = connection.execute("SELECT id, physical_file_id, proxy_relative_path, proxy_size_bytes FROM video_playback_proxy WHERE status = 'complete' ORDER BY COALESCE(last_accessed_at, updated_at) DESC, id DESC").fetchall()
        total = sum(int(row["proxy_size_bytes"] or 0) for row in rows)
        candidates = list(reversed(rows))
        while candidates and (len(rows) > MAX_PROXY_COUNT or total > MAX_PROXY_BYTES):
            row = candidates.pop(0)
            with _lock:
                active = (str(workspace.root), row["physical_file_id"]) in _active_serves or (str(workspace.root), row["physical_file_id"]) in _workers
            if active:
                continue
            _remove_owned_proxy(workspace, row["proxy_relative_path"])
            connection.execute("DELETE FROM video_playback_proxy WHERE id = ?", (row["id"],))
            total -= int(row["proxy_size_bytes"] or 0)
            rows.pop()


def _remove_owned_proxy(workspace: Workspace, relative_path: str) -> None:
    if not str(relative_path).startswith("video-proxies/"):
        return
    path = workspace.index_path(relative_path)
    if path.is_file():
        path.unlink()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _update_progress(workspace: Workspace, proxy_id: str, progress: dict[str, object], duration) -> None:
    out_time = _number(progress.get("out_time_ms"))
    value = dict(progress)
    value["fraction"] = min(1.0, out_time / 1_000_000 / float(duration)) if out_time is not None and duration else None
    with workspace.transaction() as connection:
        connection.execute("UPDATE video_playback_proxy SET status = 'running', progress_json = ?, updated_at = datetime('now') WHERE id = ?", (json.dumps(value, sort_keys=True), proxy_id))


def _finish(workspace: Workspace, physical_id: str, status: str, error: str) -> None:
    with workspace.transaction() as connection:
        connection.execute("UPDATE video_playback_proxy SET status = ?, error_message = ?, updated_at = datetime('now') WHERE physical_file_id = ? AND contract_version = ?", (status, error, physical_id, PROXY_CONTRACT_VERSION))


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _clean_error(value: str) -> str:
    return " ".join(str(value or "").split())[:800]
