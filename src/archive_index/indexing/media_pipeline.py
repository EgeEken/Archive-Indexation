"""Resumable metadata and thumbnail processing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from threading import Event

from ..jobs.engine import JobProgress, JobRunResult, JobStore, run_batches, run_items
from ..media.metadata import UnsupportedDecoderError, extract_metadata
from ..media.quality_provider import (
    QualityProvider,
    create_quality_provider,
)
from ..media.thumbnail import (
    THUMBNAIL_SIZE,
    generate_thumbnail,
    load_full_image,
    load_reduced_image,
    thumbnail_provenance,
)
from ..workspace import Workspace

METADATA_COMPONENT = "metadata"
THUMBNAIL_COMPONENT = "thumbnail"
QUALITY_COMPONENT = "quality"
METADATA_ALGORITHM = "pillow-curated-exif-ffprobe"
METADATA_VERSION = "4"
COMPONENTS = frozenset({METADATA_COMPONENT, THUMBNAIL_COMPONENT, QUALITY_COMPONENT})


def index_workspace(
    workspace: Workspace,
    *,
    components: Iterable[str] = (METADATA_COMPONENT, THUMBNAIL_COMPONENT, QUALITY_COMPONENT),
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[JobProgress], None] | None = None,
    quality_provider: str | QualityProvider | None = None,
    quality_batch_size: int = 1,
    quality_preparation_workers: int = 8,
) -> JobRunResult:
    selected_components = tuple(dict.fromkeys(components))
    if not selected_components or not set(selected_components) <= COMPONENTS:
        raise ValueError(f"components must be selected from {sorted(COMPONENTS)}")

    provider = create_quality_provider(
        workspace.quality_provider() if quality_provider is None else quality_provider,
        batch_size=quality_batch_size,
        preparation_workers=quality_preparation_workers,
    )
    if QUALITY_COMPONENT in selected_components:
        provider.preflight()

    connection = workspace.connect()
    try:
        rows = connection.execute(
            "SELECT * FROM physical_file WHERE is_online = 1 ORDER BY relative_path"
        ).fetchall()
    finally:
        connection.close()

    _prepare_component_states(workspace, rows, selected_components, provider)

    if selected_components == (QUALITY_COMPONENT,) and provider.enabled and quality_batch_size > 1:
        return _index_quality_batches(
            workspace,
            rows,
            provider,
            quality_batch_size,
            job_id=job_id,
            cancel_event=cancel_event,
            progress=progress,
        )

    def worker(row):
        return _process_file(workspace, row, selected_components, provider)

    return run_items(
        workspace,
        "media_index",
        rows,
        worker,
        job_id=job_id,
        cancel_event=cancel_event,
        progress=progress,
        physical_file_id=lambda row: row["id"],
        relative_path=lambda row: row["relative_path"],
        stage="metadata + thumbnails + quality",
    )


def invalidate_component(
    workspace: Workspace,
    component: str,
    physical_file_ids: Iterable[str] | None = None,
) -> int:
    _validate_component(component)
    ids = list(physical_file_ids or [])
    with workspace.transaction() as connection:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            cursor = connection.execute(
                f"""
                UPDATE component_state
                SET status = 'pending', input_fingerprint = NULL,
                    started_at = NULL, completed_at = NULL, error_message = NULL
                WHERE component = ? AND physical_file_id IN ({placeholders})
                """,
                [component, *ids],
            )
        else:
            cursor = connection.execute(
                """
                UPDATE component_state
                SET status = 'pending', input_fingerprint = NULL,
                    started_at = NULL, completed_at = NULL, error_message = NULL
                WHERE component = ?
                """,
                (component,),
            )
        if component == QUALITY_COMPONENT:
            quality_where = (
                f"id IN ({','.join('?' for _ in ids)})"
                if ids
                else "id IN (SELECT physical_file_id FROM component_state WHERE component = ?)"
            )
            quality_params = ids if ids else [component]
            connection.execute(
                f"""
                UPDATE physical_file
                SET quality_raw_json = NULL, quality_components_json = NULL,
                    quality_score = NULL, quality_algorithm = NULL, quality_version = NULL
                WHERE {quality_where}
                """,
                quality_params,
            )
    return cursor.rowcount


def list_problems(workspace: Workspace, job_id: str | None = None):
    return JobStore(workspace).list_errors(job_id)


def _prepare_component_states(
    workspace: Workspace,
    rows,
    components: tuple[str, ...],
    provider: QualityProvider,
) -> None:
    with workspace.transaction() as connection:
        for row in rows:
            fingerprint = _input_fingerprint(row)
            for component in components:
                algorithm, version = _provenance(component, row["media_type"], provider)
                state = connection.execute(
                    """
                    SELECT * FROM component_state
                    WHERE physical_file_id = ? AND component = ?
                    """,
                    (row["id"], component),
                ).fetchone()
                if state is None:
                    status = (
                        "not_requested"
                        if component == QUALITY_COMPONENT
                        and (row["media_type"] == "video" or not provider.enabled)
                        else "pending"
                    )
                    connection.execute(
                        """
                        INSERT INTO component_state(
                            physical_file_id, component, status, algorithm, version
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (row["id"], component, status, algorithm, version),
                    )
                    continue

                ready = _state_ready(workspace, state, row, component, fingerprint, version, provider)
                if ready:
                    continue
                status = (
                    "not_requested"
                    if component == QUALITY_COMPONENT and row["media_type"] == "video"
                    or component == QUALITY_COMPONENT and not provider.enabled
                    else "pending"
                )
                connection.execute(
                    """
                    UPDATE component_state
                    SET status = ?, algorithm = ?, version = ?,
                        started_at = NULL, completed_at = NULL, error_message = NULL
                    WHERE physical_file_id = ? AND component = ?
                    """,
                    (status, algorithm, version, row["id"], component),
                )
                if component == QUALITY_COMPONENT:
                    connection.execute(
                        """
                        UPDATE physical_file
                        SET quality_raw_json = NULL, quality_components_json = NULL,
                            quality_score = NULL, quality_algorithm = NULL, quality_version = NULL
                        WHERE id = ?
                        """,
                        (row["id"],),
                    )


def _process_file(
    workspace: Workspace,
    row,
    components: tuple[str, ...],
    provider: QualityProvider,
) -> str | None:
    source = workspace.absolute_path(row["relative_path"])
    fingerprint = _input_fingerprint(row)
    failures: list[tuple[str, Exception]] = []
    pending_components: list[str] = []

    for component in components:
        state = _component_state(workspace, row["id"], component)
        version = _provenance(component, row["media_type"], provider)[1]
        if _state_ready(workspace, state, row, component, fingerprint, version, provider):
            continue
        pending_components.append(component)

    if not pending_components:
        return "skipped"
    _mark_running_many(workspace, row["id"], pending_components, row["media_type"], provider)

    image_components = {
        component
        for component in pending_components
        if component in {THUMBNAIL_COMPONENT, QUALITY_COMPONENT}
    }
    prepared_image = None
    image_error = None
    needs_pixels = THUMBNAIL_COMPONENT in image_components or QUALITY_COMPONENT in image_components
    if row["media_type"] == "image" and needs_pixels:
        try:
            prepared_image = (
                load_full_image(source)
                if QUALITY_COMPONENT in image_components and provider.algorithm == "lar-iqa"
                else load_reduced_image(source, THUMBNAIL_SIZE)
            )
        except Exception as error:
            image_error = error

    try:
        for component in pending_components:
            try:
                if component == METADATA_COMPONENT:
                    _process_metadata(workspace, row, source, fingerprint)
                elif row["media_type"] == "video" and component == QUALITY_COMPONENT:
                    _mark_not_requested(workspace, row["id"], component, fingerprint, provider)
                elif image_error is not None:
                    raise image_error
                elif component == THUMBNAIL_COMPONENT:
                    _process_thumbnail(workspace, row, source, fingerprint, prepared_image)
                elif component == QUALITY_COMPONENT:
                    _process_quality(workspace, row, source, fingerprint, prepared_image, provider)
                else:
                    raise ValueError(f"unsupported component: {component}")
            except Exception as error:
                _mark_failed(workspace, row["id"], component, error)
                failures.append((component, error))
    finally:
        if prepared_image is not None:
            prepared_image.close()

    if failures:
        detail = "; ".join(f"{component}: {error}" for component, error in failures)
        raise RuntimeError(detail)


def _process_metadata(workspace: Workspace, row, source: Path, fingerprint: str) -> None:
    result = extract_metadata(source, row["media_type"])
    values = json.dumps(result.values, ensure_ascii=False, sort_keys=True)
    with workspace.transaction() as connection:
        connection.execute(
            """
            UPDATE physical_file
            SET metadata_json = ?, width = ?, height = ?, duration_seconds = ?, codec = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                values,
                result.width,
                result.height,
                result.duration_seconds,
                result.codec,
                _timestamp(),
                row["id"],
            ),
        )
        connection.execute(
            """
            UPDATE logical_asset
            SET capture_time = ?, capture_time_kind = ?, updated_at = ?
            WHERE id = ?
            """,
            (result.capture_time, result.capture_time_kind, _timestamp(), row["logical_asset_id"]),
        )
        _mark_complete_sql(
            connection,
            row["id"],
            METADATA_COMPONENT,
            METADATA_ALGORITHM,
            METADATA_VERSION,
            fingerprint,
        )


def _process_thumbnail(
    workspace: Workspace,
    row,
    source: Path,
    fingerprint: str,
    prepared_image,
) -> None:
    algorithm, version, provenance = thumbnail_provenance(row["media_type"])
    destination = workspace.index_directory / "thumbnails" / f"{row['id']}.jpg"
    if row["media_type"] == "video":
        generate_thumbnail(source, destination, THUMBNAIL_SIZE, media_type="video")
    else:
        generate_thumbnail(source, destination, THUMBNAIL_SIZE, prepared_image)
    output_fingerprint = _file_fingerprint(destination)
    output_path = workspace.index_relative_path(destination)
    settings = json.dumps(provenance, sort_keys=True)
    with workspace.transaction() as connection:
        connection.execute(
            """
            UPDATE component_state
            SET status = 'complete', algorithm = ?, version = ?, settings_json = ?,
                input_fingerprint = ?, output_path = ?, output_fingerprint = ?,
                completed_at = ?, error_message = NULL
            WHERE physical_file_id = ? AND component = ?
            """,
            (
                algorithm,
                version,
                settings,
                fingerprint,
                output_path,
                output_fingerprint,
                _timestamp(),
                row["id"],
                THUMBNAIL_COMPONENT,
            ),
        )


def _process_quality(
    workspace: Workspace,
    row,
    source: Path,
    fingerprint: str,
    prepared_image,
    provider: QualityProvider,
) -> None:
    if prepared_image is not None:
        score_prepared = getattr(provider, "score_prepared_images", provider.score_images)
        results = score_prepared([prepared_image])
    else:
        results = provider.score_paths([source])
    if len(results) != 1:
        raise RuntimeError("quality provider returned an unexpected number of results")
    _store_quality_result(workspace, row, fingerprint, provider, results[0])


def _store_quality_result(
    workspace: Workspace,
    row,
    fingerprint: str,
    provider: QualityProvider,
    result,
) -> None:
    raw = dict(result.raw)
    raw["input_fingerprint"] = fingerprint
    raw["provider_version"] = provider.version
    settings = getattr(provider, "settings", None)
    if settings is not None:
        raw["settings"] = settings
    with workspace.transaction() as connection:
        connection.execute(
            """
            UPDATE physical_file
            SET quality_raw_json = ?, quality_components_json = ?, quality_score = ?,
                quality_algorithm = ?, quality_version = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(raw, ensure_ascii=False, sort_keys=True),
                json.dumps(result.components, ensure_ascii=False, sort_keys=True)
                if result.components is not None
                else None,
                result.score,
                provider.algorithm,
                provider.version,
                _timestamp(),
                row["id"],
            ),
        )
        _mark_complete_sql(
            connection,
            row["id"],
            QUALITY_COMPONENT,
            provider.algorithm,
            provider.version,
            fingerprint,
            json.dumps(getattr(provider, "settings", {}), ensure_ascii=False, sort_keys=True),
        )


def _index_quality_batches(
    workspace: Workspace,
    rows,
    provider: QualityProvider,
    batch_size: int,
    *,
    job_id: str | None,
    cancel_event: Event | None,
    progress: Callable[[JobProgress], None] | None,
) -> JobRunResult:
    def worker(batch) -> dict[str, object]:
        outcomes: dict[str, object] = {}
        pending = []
        for row in batch:
            fingerprint = _input_fingerprint(row)
            state = _component_state(workspace, row["id"], QUALITY_COMPONENT)
            version = _provenance(QUALITY_COMPONENT, row["media_type"], provider)[1]
            if row["media_type"] == "video":
                _mark_not_requested(workspace, row["id"], QUALITY_COMPONENT, fingerprint, provider)
                outcomes[row["id"]] = None
                continue
            if _state_ready(workspace, state, row, QUALITY_COMPONENT, fingerprint, version, provider):
                outcomes[row["id"]] = "skipped"
                continue
            _mark_running_many(workspace, row["id"], [QUALITY_COMPONENT], row["media_type"], provider)
            pending.append((row, fingerprint))
        if not pending:
            return outcomes

        paths = [workspace.absolute_path(row["relative_path"]) for row, _ in pending]
        try:
            results = provider.score_paths(paths)
            if len(results) != len(pending):
                raise RuntimeError("quality provider returned an unexpected batch size")
            for (row, fingerprint), result in zip(pending, results):
                try:
                    _store_quality_result(workspace, row, fingerprint, provider, result)
                except Exception as error:
                    _mark_failed(workspace, row["id"], QUALITY_COMPONENT, error)
                    outcomes[row["id"]] = error
                else:
                    outcomes[row["id"]] = None
        except Exception:
            for row, fingerprint in pending:
                try:
                    result = provider.score_paths([workspace.absolute_path(row["relative_path"])])[0]
                    _store_quality_result(workspace, row, fingerprint, provider, result)
                except Exception as error:
                    _mark_failed(workspace, row["id"], QUALITY_COMPONENT, error)
                    outcomes[row["id"]] = error
                else:
                    outcomes[row["id"]] = None
        return outcomes

    return run_batches(
        workspace,
        "media_index",
        rows,
        worker,
        batch_size=batch_size * 2,
        item_key=lambda row: row["id"],
        job_id=job_id,
        cancel_event=cancel_event,
        progress=progress,
        physical_file_id=lambda row: row["id"],
        relative_path=lambda row: row["relative_path"],
        stage="quality",
    )


def _mark_running_many(
    workspace: Workspace,
    file_id: str,
    components: list[str],
    media_type: str = "image",
    provider: QualityProvider | None = None,
) -> None:
    with workspace.transaction() as connection:
        for component in components:
            algorithm, version = _provenance(component, media_type, provider)
            connection.execute(
                """
                UPDATE component_state
                SET status = 'running', algorithm = ?, version = ?, started_at = ?,
                    completed_at = NULL, error_message = NULL
                WHERE physical_file_id = ? AND component = ?
                """,
                (algorithm, version, _timestamp(), file_id, component),
            )


def _mark_complete_sql(
    connection,
    file_id: str,
    component: str,
    algorithm: str,
    version: str,
    fingerprint: str,
    settings: str | None = None,
) -> None:
    connection.execute(
        """
        UPDATE component_state
        SET status = 'complete', algorithm = ?, version = ?, settings_json = ?,
            input_fingerprint = ?, completed_at = ?, error_message = NULL
        WHERE physical_file_id = ? AND component = ?
        """,
        (algorithm, version, settings, fingerprint, _timestamp(), file_id, component),
    )


def _mark_not_requested(
    workspace: Workspace,
    file_id: str,
    component: str,
    fingerprint: str,
    provider: QualityProvider,
) -> None:
    algorithm, version = _provenance(component, provider=provider)
    with workspace.transaction() as connection:
        connection.execute(
            """
            UPDATE component_state
            SET status = 'not_requested', algorithm = ?, version = ?, settings_json = ?,
                input_fingerprint = ?, completed_at = ?, error_message = NULL
            WHERE physical_file_id = ? AND component = ?
            """,
            (algorithm, version, json.dumps(getattr(provider, "settings", {}), sort_keys=True), fingerprint, _timestamp(), file_id, component),
        )


def _mark_failed(workspace: Workspace, file_id: str, component: str, error: Exception) -> None:
    status = "unsupported" if isinstance(error, UnsupportedDecoderError) else "failed"
    with workspace.transaction() as connection:
        connection.execute(
            """
            UPDATE component_state
            SET status = ?, completed_at = NULL, error_message = ?
            WHERE physical_file_id = ? AND component = ?
            """,
            (status, str(error), file_id, component),
        )


def _component_state(workspace: Workspace, file_id: str, component: str):
    connection = workspace.connect()
    try:
        return connection.execute(
            "SELECT * FROM component_state WHERE physical_file_id = ? AND component = ?",
            (file_id, component),
        ).fetchone()
    finally:
        connection.close()


def _state_ready(
    workspace: Workspace,
    state,
    row,
    component: str,
    fingerprint: str,
    version: str,
    provider: QualityProvider,
) -> bool:
    if state is None or state["version"] != version:
        return False
    if state["status"] == "unsupported":
        return True
    if component == QUALITY_COMPONENT and (row["media_type"] == "video" or not provider.enabled):
        return state["status"] == "not_requested"
    if component == METADATA_COMPONENT:
        return state["status"] == "complete" and state["input_fingerprint"] == fingerprint
    if state["status"] != "complete" or state["input_fingerprint"] != fingerprint:
        return False
    if component == QUALITY_COMPONENT:
        return (
            row["quality_score"] is not None
            and row["quality_algorithm"] == provider.algorithm
            and row["quality_version"] == version
        )
    if not state["output_path"] or state["output_fingerprint"] is None:
        return False
    try:
        return workspace.index_path(state["output_path"]).is_file()
    except ValueError:
        return False


def _input_fingerprint(row) -> str:
    if row["sha256"]:
        return row["sha256"]
    return f"stat:{row['size_bytes']}:{row['mtime_ns']}"


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance(
    component: str,
    media_type: str | None = None,
    provider: QualityProvider | None = None,
) -> tuple[str, str]:
    _validate_component(component)
    if component == METADATA_COMPONENT:
        return METADATA_ALGORITHM, METADATA_VERSION
    if component == THUMBNAIL_COMPONENT:
        return thumbnail_provenance(media_type or "image")[:2]
    provider = provider or create_quality_provider(None)
    return provider.algorithm, provider.version


def _validate_component(component: str) -> None:
    if component not in COMPONENTS:
        raise ValueError(f"unknown component: {component}")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
