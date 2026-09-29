"""Persistent, source-fingerprinted compression eligibility analysis."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from PIL import Image

from .media.av1 import (
    AV1_ALGORITHM_VERSION,
    EXECUTION_PREFLIGHT_TIMEOUT_SECONDS,
    ProbeTimeoutError,
    analyze_source as analyze_av1,
    source_blocker_from_info,
)
from .media.capabilities import av1_capability, ffmpeg_capabilities, jpegxl_tool_capabilities
from .media.jpegxl import JXL_ALGORITHM_VERSION, JXL_ICC_BLOCKER, JXL_SOURCE_REPLACEMENT_BLOCKER, production_capability
from .media.jpegxl_tools import TOOL_VERSION, metadata_inventory

LOGGER = logging.getLogger(__name__)

ANALYSIS_CONTRACT_VERSION = {
    "jpeg-xl": f"jxl-source-v{JXL_ALGORITHM_VERSION}-inventory-v2",
    "av1": f"av1-preflight-v{AV1_ALGORITHM_VERSION}-timing-v3",
}


@dataclass(frozen=True)
class AnalysisOutcome:
    status: str
    analysis: dict[str, object]
    cache_hit: bool = False
    message: str | None = None


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


def load_or_analyze(workspace, row, codec: str, *, cancelled=None, progress=None, allow_probe: bool = False) -> AnalysisOutcome:
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
        status, analysis, message = _decode_record(cached["analysis_json"])
        return AnalysisOutcome(status, analysis, True, message)
    if cancelled and cancelled.is_set():
        raise InterruptedError("Plan analysis was cancelled.")
    if codec == "av1" and not allow_probe:
        return AnalysisOutcome("preflight_required", _indexed_av1_analysis(row), False, "AV1 compatibility requires execution preflight.")
    source = workspace.absolute_path(row["relative_path"])
    try:
        analysis = _analyze(source, codec, cancelled, progress, deep_timing=allow_probe)
    except InterruptedError:
        raise
    except ProbeTimeoutError as error:
        return AnalysisOutcome("timeout", {}, False, str(error))
    except (PermissionError, OSError) as error:
        return AnalysisOutcome("transient_failure", {}, False, str(error))
    except ValueError as error:
        status = "unsupported"
        analysis = {"error": str(error)}
        _store(workspace, row, codec, contract, runtime, source_sha256, source_size, source_mtime, status, analysis, str(error))
        return AnalysisOutcome(status, analysis, False, str(error))
    except Exception as error:
        LOGGER.warning("compression analysis failed for %s: %s", row["relative_path"], error)
        return AnalysisOutcome("transient_failure", {}, False, str(error))
    _store(workspace, row, codec, contract, runtime, source_sha256, source_size, source_mtime, "success", analysis, None)
    return AnalysisOutcome("success", analysis, False)


def preflight_av1(workspace, row, *, cancelled=None, progress=None, timeout: float = EXECUTION_PREFLIGHT_TIMEOUT_SECONDS) -> AnalysisOutcome:
    cached = load_or_analyze(workspace, row, "av1", cancelled=cancelled, progress=progress)
    if cached.status in {"success", "unsupported"} and cached.analysis.get("timing_cfr") is not None:
        return cached
    if cancelled and cancelled.is_set():
        raise InterruptedError("AV1 preflight was cancelled.")
    source = workspace.absolute_path(row["relative_path"])
    try:
        analysis = analyze_av1(
            source,
            cancelled=cancelled,
            progress=progress,
            deep_timing=True,
            metadata_timeout=timeout,
            timing_timeout=timeout,
        )
    except InterruptedError:
        raise
    except ProbeTimeoutError as error:
        return AnalysisOutcome("timeout", {}, False, str(error))
    except (PermissionError, OSError) as error:
        return AnalysisOutcome("transient_failure", {}, False, str(error))
    except ValueError as error:
        analysis = {"error": str(error)}
        _store_outcome(workspace, row, "av1", AnalysisOutcome("unsupported", analysis, False, str(error)))
        return AnalysisOutcome("unsupported", analysis, False, str(error))
    outcome = AnalysisOutcome("success", analysis, False)
    _store_outcome(workspace, row, "av1", outcome)
    return outcome


def _store(workspace, row, codec, contract, runtime, source_sha256, source_size, source_mtime, status, analysis, message):
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
            (row["id"], codec, contract, runtime, source_sha256, source_size, source_mtime, _json({"status": status, "analysis": analysis, "message": message}), now, now),
        )


def _store_outcome(workspace, row, codec, outcome: AnalysisOutcome) -> None:
    _store(
        workspace, row, codec, ANALYSIS_CONTRACT_VERSION[codec], runtime_fingerprint(codec),
        row["sha256"], row["size_bytes"], row["mtime_ns"], outcome.status, outcome.analysis, outcome.message,
    )


def blocker_for_analysis(codec: str, outcome: AnalysisOutcome, profile: dict[str, object] | None, source_disposition: str) -> str | None:
    if outcome.status == "preflight_required":
        return None
    if outcome.status == "timeout":
        return f"{codec} eligibility analysis exceeded its safety deadline; Analyze again before execution."
    if outcome.status in {"transient_failure", "cancelled"}:
        return f"{codec} eligibility could not be determined safely; Analyze again before execution."
    analysis = outcome.analysis
    if analysis.get("error"):
        return str(analysis["error"])
    if codec == "av1":
        return source_blocker_from_info(analysis, profile, require_timing=analysis.get("timing_cfr") is not None)
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


def _analyze(source: Path, codec: str, cancelled, progress, *, deep_timing: bool) -> dict[str, object]:
    if codec == "av1":
        return analyze_av1(source, cancelled=cancelled, progress=progress, deep_timing=deep_timing)
    if progress:
        progress("Inspecting JPEG XL metadata")
    with Image.open(source) as image:
        mode = image.mode
        has_icc = bool(image.info.get("icc_profile"))
    return {"mode": mode, "has_icc": has_icc, "inventory": metadata_inventory(source)}


def _indexed_av1_analysis(row) -> dict[str, object]:
    return {
        "width": _row_value(row, "width") or 0,
        "height": _row_value(row, "height") or 0,
        "duration": _row_value(row, "duration_seconds"),
        "codec": _row_value(row, "codec"),
        "preflight_required": True,
    }


def _row_value(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


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


def _decode_record(value: str) -> tuple[str, dict[str, object], str | None]:
    decoded = _restore(json.loads(value))
    if "analysis" in decoded and "status" in decoded:
        return str(decoded.get("status") or "success"), decoded.get("analysis") or {}, decoded.get("message")
    return "success", decoded, None


def _restore(value):
    if isinstance(value, dict):
        if set(value) == {"__fraction__"}:
            return Fraction(value["__fraction__"])
        return {key: _restore(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_restore(item) for item in value]
    return value
