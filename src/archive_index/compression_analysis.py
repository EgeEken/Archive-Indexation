"""Persistent, source-fingerprinted compression eligibility analysis."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from PIL import Image

from .media.av1 import AV1_ALGORITHM_VERSION, analyze_source as analyze_av1, source_blocker_from_info
from .media.capabilities import av1_capability, ffmpeg_capabilities, jpegxl_tool_capabilities
from .media.jpegxl import JXL_ALGORITHM_VERSION, JXL_ICC_BLOCKER, JXL_SOURCE_REPLACEMENT_BLOCKER, production_capability
from .media.jpegxl_tools import TOOL_VERSION, metadata_inventory

ANALYSIS_CONTRACT_VERSION = {
    "jpeg-xl": f"jxl-source-v{JXL_ALGORITHM_VERSION}-inventory-v2",
    "av1": f"av1-source-v{AV1_ALGORITHM_VERSION}-timing-v2",
}


def runtime_fingerprint(codec: str) -> str:
    if codec == "av1":
        return _json({
            "ffmpeg": ffmpeg_capabilities().get("version"),
            "encoder": av1_capability().get("encoder"),
        })
    if codec == "jpeg-xl":
        capability = production_capability()
        tools = jpegxl_tool_capabilities()
        return _json({
            "imagecodecs": capability.get("encoder_version"),
            "cjxl": tools.get("version"),
            "tool_contract": TOOL_VERSION,
        })
    raise ValueError(f"unsupported compression analysis codec: {codec}")


def load_or_analyze(workspace, row, codec: str, *, cancelled=None, progress=None) -> tuple[dict[str, object], bool]:
    contract = ANALYSIS_CONTRACT_VERSION[codec]
    runtime = runtime_fingerprint(codec)
    source_sha256 = row["sha256"]
    source_size = row["size_bytes"]
    source_mtime = row["mtime_ns"]
    connection = workspace.connect()
    try:
        cached = connection.execute(
            """
            SELECT analysis_json FROM compression_source_analysis
            WHERE physical_file_id = ? AND codec = ? AND analysis_contract_version = ?
              AND runtime_fingerprint = ? AND source_sha256 IS ?
              AND source_size_bytes IS ? AND source_mtime_ns IS ?
            """,
            (row["id"], codec, contract, runtime, source_sha256, source_size, source_mtime),
        ).fetchone()
    finally:
        connection.close()
    if cached is not None:
        return _decode(cached["analysis_json"]), True
    if cancelled and cancelled.is_set():
        raise InterruptedError("Plan analysis was cancelled.")
    source = workspace.absolute_path(row["relative_path"])
    try:
        analysis = _analyze(source, codec, cancelled, progress)
    except InterruptedError:
        raise
    except Exception as error:
        analysis = {"error": str(error)}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with workspace.transaction() as connection:
        connection.execute(
            """
            INSERT INTO compression_source_analysis(
                physical_file_id, codec, analysis_contract_version, runtime_fingerprint,
                source_sha256, source_size_bytes, source_mtime_ns, analysis_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(physical_file_id, codec) DO UPDATE SET
                analysis_contract_version = excluded.analysis_contract_version,
                runtime_fingerprint = excluded.runtime_fingerprint,
                source_sha256 = excluded.source_sha256,
                source_size_bytes = excluded.source_size_bytes,
                source_mtime_ns = excluded.source_mtime_ns,
                analysis_json = excluded.analysis_json,
                updated_at = excluded.updated_at
            """,
            (row["id"], codec, contract, runtime, source_sha256, source_size, source_mtime, _json(analysis), now, now),
        )
    return analysis, False


def blocker_for_analysis(codec: str, analysis: dict[str, object], profile: dict[str, object] | None, source_disposition: str) -> str | None:
    if analysis.get("error"):
        return str(analysis["error"])
    if codec == "av1":
        return source_blocker_from_info(analysis, profile)
    if codec != "jpeg-xl":
        return None
    if analysis.get("mode") not in {"RGB", "RGBA"}:
        return "Production JPEG XL supports 8-bit RGB and RGBA JPEG/PNG sources only."
    if source_disposition != "replace":
        return JXL_ICC_BLOCKER if analysis.get("has_icc") else None
    capability = production_capability()
    if not capability.get("source_replacement_available"):
        return JXL_SOURCE_REPLACEMENT_BLOCKER
    unsupported = analysis.get("inventory", {}).get("unsupported") or []
    return str(unsupported[0]) if unsupported else None


def _analyze(source: Path, codec: str, cancelled, progress) -> dict[str, object]:
    if codec == "av1":
        return analyze_av1(source, cancelled=cancelled, progress=progress)
    if progress:
        progress("Inspecting JPEG XL metadata")
    with Image.open(source) as image:
        mode = image.mode
        has_icc = bool(image.info.get("icc_profile"))
    return {"mode": mode, "has_icc": has_icc, "inventory": metadata_inventory(source)}


def _json(value: object) -> str:
    return json.dumps(_encode(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _encode(value):
    if isinstance(value, Fraction):
        return {"__fraction__": f"{value.numerator}/{value.denominator}"}
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    return value


def _decode(value: str) -> dict[str, object]:
    return _restore(json.loads(value))


def _restore(value):
    if isinstance(value, dict):
        if set(value) == {"__fraction__"}:
            return Fraction(value["__fraction__"])
        return {key: _restore(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore(item) for item in value]
    return value
