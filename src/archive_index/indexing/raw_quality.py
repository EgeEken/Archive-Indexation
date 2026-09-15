"""Quality processing for RAW-only logical assets using embedded previews."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from threading import Event

from ..jobs.engine import JobProgress, JobRunResult, JobStore, run_batches
from ..media.quality_provider import QualityProvider, create_quality_provider
from ..media.raw_preview import RAW_PREVIEW_ALGORITHM, RAW_PREVIEW_VERSION, extract_embedded_preview
from ..workspace import Workspace
from ..media_types import is_raw_extension, is_rendered_image_extension
from .media_pipeline import QUALITY_COMPONENT, _input_fingerprint, _mark_failed

RAW_QUALITY_ALGORITHM = "lar-iqa-raw-preview"
RAW_QUALITY_VERSION = "1"
RAW_QUALITY_BATCH_SIZE = 16


def index_raw_quality(
    workspace: Workspace,
    *,
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[JobProgress], None] | None = None,
    quality_provider: str | QualityProvider | None = None,
    quality_batch_size: int | None = None,
) -> JobRunResult:
    configuration = workspace.configuration()
    provider_selection = (
        configuration["raw_quality_provider"]
        if quality_provider is None
        else quality_provider
    )
    provider = create_quality_provider(provider_selection, batch_size=quality_batch_size)
    raw_rows, paired_rows = _raw_quality_rows(workspace)
    _prepare_states(workspace, raw_rows, paired_rows, provider)
    pending = [row for row in raw_rows if not _state_ready(workspace, row, provider)]
    if pending and provider.enabled:
        try:
            provider.preflight()
        except Exception as error:
            for row in pending:
                _mark_failed(workspace, row["id"], QUALITY_COMPONENT, error)
            preflight_error = error
        else:
            preflight_error = None
    else:
        preflight_error = None

    def worker(batch) -> dict[str, object]:
        outcomes: dict[str, object] = {}
        pending_batch = []
        previews = []
        for row in batch:
            if _state_ready(workspace, row, provider):
                outcomes[row["id"]] = "skipped"
                continue
            if not provider.enabled:
                outcomes[row["id"]] = "skipped"
                continue
            if preflight_error is not None:
                outcomes[row["id"]] = preflight_error
                continue
            try:
                preview = extract_embedded_preview(workspace.absolute_path(row["relative_path"]))
            except Exception as error:
                _mark_failed(workspace, row["id"], QUALITY_COMPONENT, error)
                outcomes[row["id"]] = error
                continue
            pending_batch.append(row)
            previews.append(preview)
        if not pending_batch:
            return outcomes

        _mark_running(workspace, pending_batch, provider)
        images = [preview.image for preview in previews]
        try:
            score_function = getattr(provider, "score_prepared_images", None) or provider.score_images
            try:
                results = score_function(images)
                if len(results) != len(pending_batch):
                    raise RuntimeError("quality provider returned an unexpected batch size")
            except Exception:
                results = []
                for image in images:
                    try:
                        results.append(score_function([image])[0])
                    except Exception as error:
                        results.append(error)
            for row, preview, result in zip(pending_batch, previews, results):
                if isinstance(result, BaseException):
                    _mark_failed(workspace, row["id"], QUALITY_COMPONENT, result)
                    outcomes[row["id"]] = result
                    continue
                _store_raw_result(workspace, row, provider, preview, result)
                outcomes[row["id"]] = None
        finally:
            for image in images:
                image.close()
        return outcomes

    return run_batches(
        workspace,
        "raw_quality",
        raw_rows,
        worker,
        batch_size=quality_batch_size or getattr(provider, "batch_size", RAW_QUALITY_BATCH_SIZE),
        item_key=lambda row: row["id"],
        job_id=job_id,
        cancel_event=cancel_event,
        progress=progress,
        physical_file_id=lambda row: row["id"],
        relative_path=lambda row: row["relative_path"],
        stage="raw quality" if provider.enabled else "raw quality disabled",
    )


def _raw_quality_rows(workspace: Workspace):
    connection = workspace.connect()
    try:
        rows = connection.execute(
            """
            SELECT pf.*
            FROM physical_file AS pf
            WHERE pf.media_type = 'image' AND pf.in_scope = 1 AND pf.is_online = 1
            ORDER BY pf.logical_asset_id, pf.relative_path
            """
        ).fetchall()
    finally:
        connection.close()
    by_asset: dict[str, list] = {}
    for row in rows:
        by_asset.setdefault(row["logical_asset_id"], []).append(row)
    raw_rows = []
    paired_rows = []
    for asset_rows in by_asset.values():
        raw = [row for row in asset_rows if is_raw_extension(row["extension"])]
        rendered = [row for row in asset_rows if is_rendered_image_extension(row["extension"])]
        if rendered:
            paired_rows.extend(raw)
        elif raw:
            raw_rows.append(raw[0])
            paired_rows.extend(raw[1:])
    return raw_rows, paired_rows


def _prepare_states(workspace: Workspace, raw_rows, paired_rows, provider: QualityProvider) -> None:
    settings = json.dumps(_settings(provider), sort_keys=True)
    now = _timestamp()
    paired_ids = {row["id"] for row in paired_rows}
    with workspace.transaction() as connection:
        for row in [*raw_rows, *paired_rows]:
            fingerprint = _raw_input_fingerprint(row)
            state = connection.execute(
                "SELECT status FROM component_state WHERE physical_file_id = ? AND component = ?",
                (row["id"], QUALITY_COMPONENT),
            ).fetchone()
            if state is None:
                connection.execute(
                    "INSERT INTO component_state(physical_file_id, component, status, algorithm, version) VALUES (?, ?, 'not_requested', ?, ?)",
                    (row["id"], QUALITY_COMPONENT, RAW_QUALITY_ALGORITHM, RAW_QUALITY_VERSION),
                )
            elif row["id"] in paired_ids or not provider.enabled:
                if state["status"] != "complete":
                    connection.execute(
                        """
                        UPDATE component_state
                        SET status = 'not_requested', algorithm = ?, version = ?, settings_json = ?,
                            input_fingerprint = ?, started_at = NULL, completed_at = ?, error_message = NULL
                        WHERE physical_file_id = ? AND component = ?
                        """,
                        (RAW_QUALITY_ALGORITHM, RAW_QUALITY_VERSION, settings, fingerprint, now, row["id"], QUALITY_COMPONENT),
                    )
            elif state["status"] in {"running", "failed", "unsupported", "not_requested"}:
                connection.execute(
                    "UPDATE component_state SET status = 'pending', algorithm = ?, version = ?, settings_json = ?, input_fingerprint = ?, started_at = NULL, completed_at = NULL, error_message = NULL WHERE physical_file_id = ? AND component = ?",
                    (RAW_QUALITY_ALGORITHM, RAW_QUALITY_VERSION, settings, fingerprint, row["id"], QUALITY_COMPONENT),
                )


def _state_ready(workspace: Workspace, row, provider: QualityProvider) -> bool:
    connection = workspace.connect()
    try:
        state = connection.execute(
            "SELECT status, algorithm, version, input_fingerprint FROM component_state WHERE physical_file_id = ? AND component = ?",
            (row["id"], QUALITY_COMPONENT),
        ).fetchone()
        quality = connection.execute(
            "SELECT quality_score, quality_algorithm, quality_version FROM physical_file WHERE id = ?",
            (row["id"],),
        ).fetchone()
    finally:
        connection.close()
    if state is None:
        return False
    if not provider.enabled:
        return state["status"] in {"not_requested", "unsupported"} or (
            state["status"] == "complete" and quality["quality_score"] is not None
        )
    return bool(
        state["status"] == "complete"
        and state["algorithm"] == RAW_QUALITY_ALGORITHM
        and state["version"] == RAW_QUALITY_VERSION
        and state["input_fingerprint"] == _raw_input_fingerprint(row)
        and quality["quality_score"] is not None
        and quality["quality_algorithm"] == RAW_QUALITY_ALGORITHM
        and quality["quality_version"] == RAW_QUALITY_VERSION
    )


def _mark_running(workspace: Workspace, rows, provider: QualityProvider) -> None:
    with workspace.transaction() as connection:
        for row in rows:
            connection.execute(
                "UPDATE component_state SET status = 'running', algorithm = ?, version = ?, started_at = ?, completed_at = NULL, error_message = NULL WHERE physical_file_id = ? AND component = ?",
                (RAW_QUALITY_ALGORITHM, RAW_QUALITY_VERSION, _timestamp(), row["id"], QUALITY_COMPONENT),
            )


def _store_raw_result(workspace: Workspace, row, provider, preview, result) -> None:
    raw = dict(result.raw)
    raw.update(
        {
            "quality_source": "raw_embedded_preview",
            "source_physical_file_id": row["id"],
            "preview_method": preview.method,
            "preview_version": RAW_PREVIEW_VERSION,
            "preview_dimensions": [preview.width, preview.height],
            "provider_algorithm": provider.algorithm,
            "provider_version": provider.version,
        }
    )
    settings = _settings(provider)
    settings["preview_dimensions"] = [preview.width, preview.height]
    now = _timestamp()
    fingerprint = _raw_input_fingerprint(row)
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE physical_file SET quality_raw_json = ?, quality_components_json = ?, quality_score = ?, quality_algorithm = ?, quality_version = ?, updated_at = ? WHERE id = ?",
            (json.dumps(raw, ensure_ascii=False, sort_keys=True), json.dumps(result.components, ensure_ascii=False, sort_keys=True) if result.components is not None else None, result.score, RAW_QUALITY_ALGORITHM, RAW_QUALITY_VERSION, now, row["id"]),
        )
        connection.execute(
            "UPDATE component_state SET status = 'complete', algorithm = ?, version = ?, settings_json = ?, input_fingerprint = ?, started_at = NULL, completed_at = ?, error_message = NULL WHERE physical_file_id = ? AND component = ?",
            (RAW_QUALITY_ALGORITHM, RAW_QUALITY_VERSION, json.dumps(settings, sort_keys=True), fingerprint, now, row["id"], QUALITY_COMPONENT),
        )


def _settings(provider: QualityProvider) -> dict[str, object]:
    return {
        "raw_quality_algorithm": RAW_QUALITY_ALGORITHM,
        "raw_quality_version": RAW_QUALITY_VERSION,
        "preview_algorithm": RAW_PREVIEW_ALGORITHM,
        "preview_version": RAW_PREVIEW_VERSION,
        "provider_algorithm": provider.algorithm,
        "provider_version": provider.version,
        "provider_settings": getattr(provider, "settings", {}),
    }


def _raw_input_fingerprint(row) -> str:
    return f"{_input_fingerprint(row)}|raw-preview:{RAW_PREVIEW_ALGORITHM}:{RAW_PREVIEW_VERSION}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
