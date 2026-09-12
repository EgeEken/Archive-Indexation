"""Resumable metadata and thumbnail processing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from time import perf_counter

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
from ..timing import TimingRecorder, timed

METADATA_COMPONENT = "metadata"
THUMBNAIL_COMPONENT = "thumbnail"
QUALITY_COMPONENT = "quality"
METADATA_ALGORITHM = "pillow-curated-exif-ffprobe"
METADATA_VERSION = "4"
COMPONENTS = frozenset({METADATA_COMPONENT, THUMBNAIL_COMPONENT, QUALITY_COMPONENT})
MEDIA_BATCH_SIZE = 32
THUMBNAIL_WORKERS = 12


def index_workspace(
    workspace: Workspace,
    *,
    components: Iterable[str] = (METADATA_COMPONENT, THUMBNAIL_COMPONENT, QUALITY_COMPONENT),
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[JobProgress], None] | None = None,
    quality_provider: str | QualityProvider | None = None,
    quality_batch_size: int | None = None,
    quality_preparation_workers: int | None = None,
    thumbnail_workers: int | None = None,
    timings: TimingRecorder | None = None,
) -> JobRunResult:
    started = perf_counter()
    selected_components = tuple(dict.fromkeys(components))
    if not selected_components or not set(selected_components) <= COMPONENTS:
        raise ValueError(f"components must be selected from {sorted(COMPONENTS)}")

    provider = create_quality_provider(
        workspace.quality_provider() if quality_provider is None else quality_provider,
        batch_size=quality_batch_size,
        preparation_workers=quality_preparation_workers,
    )
    with timed(timings, "media.row_load"):
        connection = workspace.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM physical_file WHERE is_online = 1 ORDER BY relative_path"
            ).fetchall()
        finally:
            connection.close()

    if selected_components == (QUALITY_COMPONENT,) and not provider.enabled:
        with timed(timings, "quality.off_state_check"):
            quality_state_current = _quality_off_state_current(workspace, rows)
        if quality_state_current:
            result = run_batches(
                workspace,
                "media_index",
                rows,
                lambda batch: {row["id"]: "skipped" for row in batch},
                batch_size=MEDIA_BATCH_SIZE,
                item_key=lambda row: row["id"],
                job_id=job_id,
                cancel_event=cancel_event,
                progress=progress,
                physical_file_id=lambda row: row["id"],
                relative_path=lambda row: row["relative_path"],
                stage="quality disabled",
            )
            if timings is not None:
                timings.add("media.total", perf_counter() - started)
            return result

    with timed(timings, "media.component_state_prep", len(rows)):
        state_map = _prepare_component_states(workspace, rows, selected_components, provider)

    effective_batch_size = getattr(provider, "batch_size", 1)
    if quality_batch_size is not None:
        effective_batch_size = quality_batch_size
    if selected_components == (QUALITY_COMPONENT,) and provider.enabled and effective_batch_size > 1:
        result = _index_quality_batches(
            workspace,
            rows,
            provider,
            effective_batch_size,
            state_map,
            job_id=job_id,
            cancel_event=cancel_event,
            progress=progress,
            timings=timings,
        )
        if timings is not None:
            timings.add("media.total", perf_counter() - started)
        return result

    if set(selected_components) == {METADATA_COMPONENT, THUMBNAIL_COMPONENT}:
        result = _index_media_batches(
            workspace,
            rows,
            provider,
            state_map,
            workers=thumbnail_workers or THUMBNAIL_WORKERS,
            job_id=job_id,
            cancel_event=cancel_event,
            progress=progress,
            timings=timings,
        )
        if timings is not None:
            timings.add("media.total", perf_counter() - started)
        return result

    if QUALITY_COMPONENT in selected_components and provider.enabled and any(
        row["media_type"] == "image"
        and not _state_ready(
            workspace,
            state_map.get((row["id"], QUALITY_COMPONENT)),
            row,
            QUALITY_COMPONENT,
            _input_fingerprint(row),
            _provenance(QUALITY_COMPONENT, row["media_type"], provider)[1],
            provider,
        )
        for row in rows
    ):
        with timed(timings, "quality.preflight"):
            provider.preflight()

    def worker(row):
        return _process_file(workspace, row, selected_components, provider, state_map, timings)

    result = run_items(
        workspace,
        "media_index",
        rows,
        worker,
        job_id=job_id,
        cancel_event=cancel_event,
        progress=progress,
        physical_file_id=lambda row: row["id"],
        relative_path=lambda row: row["relative_path"],
        stage="metadata + thumbnails"
        if QUALITY_COMPONENT not in selected_components
        else "metadata + thumbnails + quality",
    )
    if timings is not None:
        timings.add("media.total", perf_counter() - started)
    return result


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
) -> dict[tuple[str, str], object]:
    row_ids = [row["id"] for row in rows]
    state_map: dict[tuple[str, str], object] = {}
    if not row_ids:
        return state_map
    placeholders = ",".join("?" for _ in row_ids)
    with workspace.transaction() as connection:
        for state in connection.execute(
            f"SELECT * FROM component_state WHERE physical_file_id IN ({placeholders})",
            row_ids,
        ).fetchall():
            state_map[(state["physical_file_id"], state["component"])] = state
        for row in rows:
            fingerprint = _input_fingerprint(row)
            for component in components:
                algorithm, version = _provenance(component, row["media_type"], provider)
                state = state_map.get((row["id"], component))
                if component == QUALITY_COMPONENT and not provider.enabled:
                    if (
                        state is not None
                        and state["status"] == "complete"
                        and state["input_fingerprint"] == fingerprint
                        and row["quality_score"] is not None
                    ):
                        continue
                    if state is None:
                        connection.execute(
                            """
                            INSERT INTO component_state(
                                physical_file_id, component, status, algorithm, version
                            ) VALUES (?, ?, 'not_requested', ?, ?)
                            """,
                            (row["id"], component, algorithm, version),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE component_state
                            SET status = 'not_requested', algorithm = ?, version = ?,
                                started_at = NULL, completed_at = NULL, error_message = NULL
                            WHERE physical_file_id = ? AND component = ?
                            """,
                            (algorithm, version, row["id"], component),
                        )
                    connection.execute(
                        """
                        UPDATE physical_file
                        SET quality_raw_json = NULL, quality_components_json = NULL,
                            quality_score = NULL, quality_algorithm = NULL, quality_version = NULL
                        WHERE id = ?
                        """,
                        (row["id"],),
                    )
                    continue
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

        for state in connection.execute(
            f"SELECT * FROM component_state WHERE physical_file_id IN ({placeholders})",
            row_ids,
        ).fetchall():
            state_map[(state["physical_file_id"], state["component"])] = state
    return state_map


def _quality_off_state_current(workspace: Workspace, rows) -> bool:
    row_ids = [row["id"] for row in rows]
    if not row_ids:
        return True
    placeholders = ",".join("?" for _ in row_ids)
    connection = workspace.connect()
    try:
        states = {
            state["physical_file_id"]: state
            for state in connection.execute(
                f"SELECT * FROM component_state WHERE component = ? AND physical_file_id IN ({placeholders})",
                [QUALITY_COMPONENT, *row_ids],
            ).fetchall()
        }
    finally:
        connection.close()
    for row in rows:
        state = states.get(row["id"])
        if state is None or state["status"] in {"pending", "running", "failed"}:
            return False
        if state["status"] == "complete" and (
            state["input_fingerprint"] != _input_fingerprint(row)
            or row["quality_score"] is None
        ):
            return False
    return True


def _index_media_batches(
    workspace: Workspace,
    rows,
    provider: QualityProvider,
    state_map: dict[tuple[str, str], object],
    *,
    workers: int,
    job_id: str | None,
    cancel_event: Event | None,
    progress: Callable[[JobProgress], None] | None,
    timings: TimingRecorder | None,
) -> JobRunResult:
    if workers < 1:
        raise ValueError("thumbnail worker count must be positive")

    def prepare(row):
        fingerprint = _input_fingerprint(row)
        pending = []
        for component in (METADATA_COMPONENT, THUMBNAIL_COMPONENT):
            version = _provenance(component, row["media_type"], provider)[1]
            if not _state_ready(
                workspace,
                state_map.get((row["id"], component)),
                row,
                component,
                fingerprint,
                version,
                provider,
            ):
                pending.append(component)
        result = {
            "row": row,
            "fingerprint": fingerprint,
            "pending": pending,
            "metadata": None,
            "thumbnail": None,
            "errors": {},
            "timings": TimingRecorder(),
        }
        if not pending:
            return result
        source = workspace.absolute_path(row["relative_path"])
        if METADATA_COMPONENT in pending:
            try:
                with timed(result["timings"], "metadata.extract"):
                    result["metadata"] = extract_metadata(source, row["media_type"])
            except Exception as error:
                result["errors"][METADATA_COMPONENT] = error
        if THUMBNAIL_COMPONENT in pending:
            prepared_image = None
            try:
                if row["media_type"] == "image":
                    with timed(result["timings"], "thumbnail.decode"):
                        prepared_image = load_reduced_image(source, THUMBNAIL_SIZE)
                destination = workspace.index_directory / "thumbnails" / f"{row['id']}.jpg"
                timing_kwargs = {"timings": result["timings"]}
                if row["media_type"] == "video":
                    generate_thumbnail(source, destination, THUMBNAIL_SIZE, media_type="video", **timing_kwargs)
                else:
                    generate_thumbnail(source, destination, THUMBNAIL_SIZE, prepared_image, **timing_kwargs)
                with timed(result["timings"], "thumbnail.hash"):
                    output_fingerprint = _file_fingerprint(destination)
                result["thumbnail"] = (
                    destination,
                    workspace.index_relative_path(destination),
                    output_fingerprint,
                )
            except Exception as error:
                result["errors"][THUMBNAIL_COMPONENT] = error
            finally:
                if prepared_image is not None:
                    prepared_image.close()
        return result

    def persist_batch(batch_results) -> None:
        with workspace.transaction() as connection:
            for result in batch_results:
                row = result["row"]
                fingerprint = result["fingerprint"]
                metadata = result["metadata"]
                if metadata is not None:
                    _persist_metadata_sql(connection, row, fingerprint, metadata)
                thumbnail = result["thumbnail"]
                if thumbnail is not None:
                    _persist_thumbnail_sql(connection, row, fingerprint, thumbnail)
                for component, error in result["errors"].items():
                    _mark_failed_sql(connection, row["id"], component, error)

    def worker(batch) -> dict[str, object]:
        pending_rows = []
        outcomes: dict[str, object] = {}
        for row in batch:
            fingerprint = _input_fingerprint(row)
            pending = []
            for component in (METADATA_COMPONENT, THUMBNAIL_COMPONENT):
                if not _state_ready(
                    workspace,
                    state_map.get((row["id"], component)),
                    row,
                    component,
                    fingerprint,
                    _provenance(component, row["media_type"], provider)[1],
                    provider,
                ):
                    pending.append(component)
            if pending:
                pending_rows.append(row)
            else:
                outcomes[row["id"]] = "skipped"
        if not pending_rows:
            return outcomes
        _mark_components_running_batch(workspace, pending_rows, provider)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            batch_results = list(pool.map(prepare, pending_rows))
        for result in batch_results:
            if timings is not None:
                for name, seconds in result["timings"].seconds.items():
                    timings.add(name, seconds)
        try:
            with timed(timings, "media.persistence"):
                persist_batch(batch_results)
        except Exception:
            for result in batch_results:
                try:
                    persist_batch([result])
                except Exception as error:
                    for component in result["pending"]:
                        result["errors"].setdefault(component, error)
        for result in batch_results:
            errors = result["errors"]
            outcomes[result["row"]["id"]] = next(iter(errors.values())) if errors else None
        return outcomes

    return run_batches(
        workspace,
        "media_index",
        rows,
        worker,
        batch_size=MEDIA_BATCH_SIZE,
        item_key=lambda row: row["id"],
        job_id=job_id,
        cancel_event=cancel_event,
        progress=progress,
        physical_file_id=lambda row: row["id"],
        relative_path=lambda row: row["relative_path"],
        stage="metadata + thumbnails",
    )


def _mark_components_running_batch(workspace: Workspace, rows, provider: QualityProvider) -> None:
    now = _timestamp()
    with workspace.transaction() as connection:
        for row in rows:
            for component in (METADATA_COMPONENT, THUMBNAIL_COMPONENT):
                algorithm, version = _provenance(component, row["media_type"], provider)
                connection.execute(
                    """
                    UPDATE component_state
                    SET status = 'running', algorithm = ?, version = ?, started_at = ?,
                        completed_at = NULL, error_message = NULL
                    WHERE physical_file_id = ? AND component = ?
                    """,
                    (algorithm, version, now, row["id"], component),
                )


def _persist_metadata_sql(connection, row, fingerprint: str, result) -> None:
    values = json.dumps(result.values, ensure_ascii=False, sort_keys=True)
    connection.execute(
        """
        UPDATE physical_file
        SET metadata_json = ?, width = ?, height = ?, duration_seconds = ?, codec = ?, updated_at = ?
        WHERE id = ?
        """,
        (values, result.width, result.height, result.duration_seconds, result.codec, _timestamp(), row["id"]),
    )
    connection.execute(
        """
        UPDATE logical_asset
        SET capture_time = ?, capture_time_kind = ?, updated_at = ?
        WHERE id = ?
        """,
        (result.capture_time, result.capture_time_kind, _timestamp(), row["logical_asset_id"]),
    )
    _mark_complete_sql(connection, row["id"], METADATA_COMPONENT, METADATA_ALGORITHM, METADATA_VERSION, fingerprint)


def _persist_thumbnail_sql(connection, row, fingerprint: str, thumbnail) -> None:
    algorithm, version, provenance = thumbnail_provenance(row["media_type"])
    _, output_path, output_fingerprint = thumbnail
    _mark_complete_sql(
        connection,
        row["id"],
        THUMBNAIL_COMPONENT,
        algorithm,
        version,
        fingerprint,
        json.dumps(provenance, sort_keys=True),
    )
    connection.execute(
        """
        UPDATE component_state
        SET output_path = ?, output_fingerprint = ?
        WHERE physical_file_id = ? AND component = ?
        """,
        (output_path, output_fingerprint, row["id"], THUMBNAIL_COMPONENT),
    )


def _mark_failed_sql(connection, file_id: str, component: str, error: Exception) -> None:
    status = "unsupported" if isinstance(error, UnsupportedDecoderError) else "failed"
    connection.execute(
        """
        UPDATE component_state
        SET status = ?, completed_at = NULL, error_message = ?
        WHERE physical_file_id = ? AND component = ?
        """,
        (status, str(error), file_id, component),
    )


def _process_file(
    workspace: Workspace,
    row,
    components: tuple[str, ...],
    provider: QualityProvider,
    state_map: dict[tuple[str, str], object] | None = None,
    timings: TimingRecorder | None = None,
) -> str | None:
    source = workspace.absolute_path(row["relative_path"])
    fingerprint = _input_fingerprint(row)
    failures: list[tuple[str, Exception]] = []
    pending_components: list[str] = []

    for component in components:
        state = (
            state_map.get((row["id"], component))
            if state_map is not None
            else _component_state(workspace, row["id"], component)
        )
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
            if QUALITY_COMPONENT in image_components and provider.algorithm == "lar-iqa":
                with timed(timings, "quality.decode_rgb"):
                    prepared_image = load_full_image(source)
            else:
                with timed(timings, "thumbnail.decode"):
                    prepared_image = load_reduced_image(source, THUMBNAIL_SIZE)
        except Exception as error:
            image_error = error

    try:
        for component in pending_components:
            try:
                if component == METADATA_COMPONENT:
                    with timed(timings, "metadata.processing"):
                        _process_metadata(workspace, row, source, fingerprint, timings)
                elif row["media_type"] == "video" and component == QUALITY_COMPONENT:
                    _mark_not_requested(workspace, row["id"], component, fingerprint, provider)
                elif image_error is not None:
                    raise image_error
                elif component == THUMBNAIL_COMPONENT:
                    with timed(timings, "thumbnail.processing"):
                        _process_thumbnail(workspace, row, source, fingerprint, prepared_image, timings)
                elif component == QUALITY_COMPONENT:
                    with timed(timings, "quality.processing"):
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


def _process_metadata(
    workspace: Workspace,
    row,
    source: Path,
    fingerprint: str,
    timings: TimingRecorder | None = None,
) -> None:
    with timed(timings, "metadata.extract"):
        result = extract_metadata(source, row["media_type"])
    values = json.dumps(result.values, ensure_ascii=False, sort_keys=True)
    with timed(timings, "metadata.persistence"):
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
    timings: TimingRecorder | None = None,
) -> None:
    algorithm, version, provenance = thumbnail_provenance(row["media_type"])
    destination = workspace.index_directory / "thumbnails" / f"{row['id']}.jpg"
    timing_kwargs = {"timings": timings} if timings is not None else {}
    if row["media_type"] == "video":
        generate_thumbnail(source, destination, THUMBNAIL_SIZE, media_type="video", **timing_kwargs)
    else:
        generate_thumbnail(source, destination, THUMBNAIL_SIZE, prepared_image, **timing_kwargs)
    output_fingerprint = _file_fingerprint(destination)
    output_path = workspace.index_relative_path(destination)
    settings = json.dumps(provenance, sort_keys=True)
    with timed(timings, "thumbnail.persistence"):
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
    with workspace.transaction() as connection:
        _store_quality_result_sql(connection, row, fingerprint, provider, result)


def _store_quality_results(
    workspace: Workspace,
    pending,
    provider: QualityProvider,
    results,
) -> None:
    with workspace.transaction() as connection:
        for (row, fingerprint), result in zip(pending, results):
            _store_quality_result_sql(connection, row, fingerprint, provider, result)


def _store_quality_result_sql(
    connection,
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
    state_map: dict[tuple[str, str], object],
    *,
    job_id: str | None,
    cancel_event: Event | None,
    progress: Callable[[JobProgress], None] | None,
    timings: TimingRecorder | None,
) -> JobRunResult:
    pending_exists = any(
        row["media_type"] == "image"
        and not _state_ready(
            workspace,
            state_map.get((row["id"], QUALITY_COMPONENT)),
            row,
            QUALITY_COMPONENT,
            _input_fingerprint(row),
            _provenance(QUALITY_COMPONENT, row["media_type"], provider)[1],
            provider,
        )
        for row in rows
    )

    def run(scorer: Callable[[list[Path]], list[object]]) -> JobRunResult:
        def worker(batch) -> dict[str, object]:
            outcomes: dict[str, object] = {}
            pending = []
            skipped_video = []
            for row in batch:
                fingerprint = _input_fingerprint(row)
                state = state_map.get((row["id"], QUALITY_COMPONENT))
                version = _provenance(QUALITY_COMPONENT, row["media_type"], provider)[1]
                if row["media_type"] == "video":
                    skipped_video.append((row, fingerprint))
                    outcomes[row["id"]] = None
                    continue
                if _state_ready(workspace, state, row, QUALITY_COMPONENT, fingerprint, version, provider):
                    outcomes[row["id"]] = "skipped"
                    continue
                pending.append((row, fingerprint))
            if skipped_video:
                _mark_not_requested_batch(workspace, skipped_video, provider)
            if not pending:
                return outcomes

            _mark_running_batch(workspace, pending, provider)
            paths = [workspace.absolute_path(row["relative_path"]) for row, _ in pending]
            try:
                with timed(timings, "quality.preparation_and_inference", len(paths)):
                    results = scorer(paths)
                if len(results) != len(pending):
                    raise RuntimeError("quality provider returned an unexpected batch size")
                try:
                    _store_quality_results(workspace, pending, provider, results)
                except Exception:
                    for (row, fingerprint), result in zip(pending, results):
                        try:
                            _store_quality_result(workspace, row, fingerprint, provider, result)
                        except Exception as error:
                            _mark_failed(workspace, row["id"], QUALITY_COMPONENT, error)
                            outcomes[row["id"]] = error
                        else:
                            outcomes[row["id"]] = None
                else:
                    for row, _ in pending:
                        outcomes[row["id"]] = None
            except Exception:
                for row, fingerprint in pending:
                    try:
                        result = scorer([workspace.absolute_path(row["relative_path"])])[0]
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

    if not pending_exists:
        result = run(provider.score_paths)
        _merge_provider_timings(timings, provider)
        return result
    batch_session = getattr(provider, "batch_session", None)
    if batch_session is not None:
        with batch_session() as session:
            result = run(session.score_paths)
        _merge_provider_timings(timings, provider)
        return result
    provider.preflight()
    result = run(provider.score_paths)
    _merge_provider_timings(timings, provider)
    return result


def _merge_provider_timings(timings: TimingRecorder | None, provider: QualityProvider) -> None:
    if timings is None:
        return
    for name, seconds in getattr(provider, "last_timings", {}).items():
        timings.add(name, seconds)


def _mark_running_batch(workspace: Workspace, pending, provider: QualityProvider) -> None:
    now = _timestamp()
    with workspace.transaction() as connection:
        for row, _ in pending:
            algorithm, version = _provenance(QUALITY_COMPONENT, row["media_type"], provider)
            connection.execute(
                """
                UPDATE component_state
                SET status = 'running', algorithm = ?, version = ?, started_at = ?,
                    completed_at = NULL, error_message = NULL
                WHERE physical_file_id = ? AND component = ?
                """,
                (algorithm, version, now, row["id"], QUALITY_COMPONENT),
            )


def _mark_not_requested_batch(workspace: Workspace, rows, provider: QualityProvider) -> None:
    algorithm, version = _provenance(QUALITY_COMPONENT, provider=provider)
    settings = json.dumps(getattr(provider, "settings", {}), sort_keys=True)
    now = _timestamp()
    with workspace.transaction() as connection:
        for row, fingerprint in rows:
            connection.execute(
                """
                UPDATE component_state
                SET status = 'not_requested', algorithm = ?, version = ?, settings_json = ?,
                    input_fingerprint = ?, completed_at = ?, error_message = NULL
                WHERE physical_file_id = ? AND component = ?
                """,
                (algorithm, version, settings, fingerprint, now, row["id"], QUALITY_COMPONENT),
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
    if state is None:
        return False
    if state["status"] == "unsupported":
        return True
    if component == QUALITY_COMPONENT and not provider.enabled:
        return (
            state["status"] == "not_requested"
            or (
                state["status"] == "complete"
                and state["input_fingerprint"] == fingerprint
                and row["quality_score"] is not None
            )
        )
    if component == QUALITY_COMPONENT and row["media_type"] == "video":
        return state["status"] == "not_requested"
    if state["version"] != version:
        return False
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
