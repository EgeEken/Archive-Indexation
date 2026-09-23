"""Incremental semantic embedding generation for logical assets and video samples."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PIL import Image

from ..embeddings.providers import EmbeddingProvider, create_embedding_provider
from ..embeddings.runtime import embedding_runtime
from ..embeddings.vector import vector_to_blob
from ..indexing.representations import preferred_physical
from ..jobs.engine import SUBSTAGE_CONTEXT, report_substage, JobProgress, JobRunResult, JobStore
from ..media.metadata import UnsupportedDecoderError
from ..media.image_decode import load_full_image
from ..media.raw_preview import extract_embedded_preview
from ..media_types import is_raw_extension, is_rendered_image_extension
from ..workspace import Workspace
from .video_quality import (
    VIDEO_SAMPLER_ALGORITHM,
    VIDEO_SAMPLER_VERSION,
    VIDEO_SINGLE_PROCESS_MAX_DURATION_SECONDS,
    VideoExtractionCancelled,
    extract_video_frames,
    sample_count,
    sample_timestamps,
)

EMBEDDING_ALGORITHM = "semantic-embedding"
EMBEDDING_COMPONENT_PREFIX = "embedding:"
VIDEO_EMBEDDING_AGGREGATE = "max-frame-similarity"
DEFAULT_EMBEDDING_PREPARATION_WORKERS = 8
EMBEDDING_ESTIMATE_SECONDS_PER_VECTOR = 0.10


@dataclass
class _PreparedImageBatch:
    prepared: object | None
    prepared_sources: list[dict[str, object]]
    individual: list[tuple[dict[str, object], object, list[Image.Image]]]
    outcomes: list[tuple[dict[str, object], object]]
    cleanup_images: list[Image.Image]


class EmbeddingCancelled(RuntimeError):
    pass


class _ImagePreparationCancelled(RuntimeError):
    pass


def embedding_component(provider: EmbeddingProvider | str) -> str:
    return f"{EMBEDDING_COMPONENT_PREFIX}{provider.provider_id if not isinstance(provider, str) else provider}"


def index_embeddings(
    workspace: Workspace,
    *,
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[JobProgress], None] | None = None,
    provider: EmbeddingProvider | None = None,
    batch_size: int = 16,
    precision: str = "fp32",
    preparation_workers: int = DEFAULT_EMBEDDING_PREPARATION_WORKERS,
    video_frame_consumer: Callable | None = None,
) -> JobRunResult:
    configuration = workspace.configuration()
    sources = _embedding_sources(workspace, configuration)
    total_units = sum(_source_units(source) for source in sources)
    store = JobStore(workspace)
    identifier = job_id or store.create("embeddings", total_units)
    store.set_total(identifier, total_units)
    store.set_stage(identifier, "embeddings")
    store.start(identifier)
    if not configuration["semantic_search_enabled"]:
        store.complete(identifier, 0, 0, total_units)
        _mark_disabled_states(workspace, sources, configuration["embedding_provider"])
        return JobRunResult(identifier, 0, 0, 0, False, total_units)

    runtime_provider = provider is None
    if provider is None:
        try:
            provider = embedding_runtime.provider(
                configuration["embedding_provider"],
                batch_size=batch_size,
                precision=precision,
                factory=create_embedding_provider,
            )
        except Exception as error:
            try:
                _mark_unavailable_states(workspace, sources, configuration["embedding_provider"], error)
                store.complete(identifier, 0, 0, total_units)
            except Exception:
                return JobRunResult(identifier, 0, 0, 0, False, total_units)
            return JobRunResult(identifier, 0, 0, 0, False, total_units)
    infer = (
        (lambda operation: embedding_runtime.run(
            provider.provider_id, operation, factory=create_embedding_provider
        ))
        if runtime_provider
        else (lambda operation: operation(provider))
    )
    settings_json = json.dumps(provider.settings, ensure_ascii=False, sort_keys=True)
    run = _get_or_create_run(workspace, provider, settings_json)
    _ensure_states(workspace, sources, provider, settings_json, run["id"])
    pending = [source for source in sources if not _state_ready(workspace, source, provider, run["id"])]
    if not pending:
        _finish_run(workspace, run["id"], None)
        store.complete(identifier, 0, 0, total_units)
        _activate_run(workspace, provider, run["id"])
        _report_cached(progress, identifier, total_units, sources)
        return JobRunResult(identifier, total_units, 0, 0, False, total_units)

    try:
        if not runtime_provider:
            provider.preflight()
    except Exception as error:
        failed = 0
        for source in pending:
            if _cancelled(cancel_event):
                store.cancel(identifier, failed, failed, total_units - failed)
                _finish_run(workspace, run["id"], "cancelled", str(error))
                return JobRunResult(identifier, failed, 0, failed, True, total_units - failed)
            _mark_state(workspace, source, provider, settings_json, _input_fingerprint(source, provider), "failed", error)
            store.record_error(identifier, error, physical_file_id=source["physical_file_id"], relative_path=source["relative_path"])
            failed += _source_units(source)
            _report(progress, identifier, failed, total_units, source, failed, total_units - failed)
        _finish_run(workspace, run["id"], "failed", str(error))
        store.complete(identifier, total_units, failed, total_units - sum(_source_units(source) for source in pending))
        return JobRunResult(identifier, total_units, total_units - failed, failed, False, total_units - sum(_source_units(source) for source in pending))

    processed = 0
    succeeded = 0
    errors = 0
    skipped = 0
    try:
        for source in sources:
            if _state_ready(workspace, source, provider, run["id"]):
                processed += _source_units(source)
                skipped += _source_units(source)
                _report(progress, identifier, processed, total_units, source, errors, skipped)
        pending_images = [source for source in pending if source["source_kind"] != "video"]

        def consume_image_outcomes(outcomes):
            nonlocal processed, succeeded, errors
            for source, outcome in outcomes:
                processed += _source_units(source)
                if isinstance(outcome, BaseException):
                    errors += _source_units(source)
                    store.record_error(identifier, outcome, physical_file_id=source["physical_file_id"], relative_path=source["relative_path"])
                else:
                    succeeded += _source_units(source)
                _report(progress, identifier, processed, total_units, source, errors, skipped)
            store.checkpoint(identifier, processed, errors, skipped)

        cancelled = _process_image_batches(
            workspace,
            pending_images,
            provider,
            run["id"],
            settings_json,
            cancel_event,
            max(1, int(preparation_workers)),
            max(1, batch_size),
            consume_image_outcomes,
            infer,
        )
        if cancelled:
            _finish_run(workspace, run["id"], "cancelled")
            store.cancel(identifier, processed, errors, skipped)
            return JobRunResult(identifier, processed, succeeded, errors, True, skipped)
        video_sources = [item for item in pending if item["source_kind"] == "video"]
        for video_index, source in enumerate(video_sources):
            if _cancelled(cancel_event):
                SUBSTAGE_CONTEXT.pop(identifier, None)
                _finish_run(workspace, run["id"], "cancelled")
                store.cancel(identifier, processed, errors, skipped)
                return JobRunResult(identifier, processed, succeeded, errors, True, skipped)
            SUBSTAGE_CONTEXT[identifier] = {
                "completed": video_index + 1,
                "total": len(video_sources),
            }
            try:
                _process_video(
                    workspace,
                    source,
                    provider,
                    run["id"],
                    settings_json,
                    cancel_event,
                    infer,
                    lambda stage, current, total: report_substage(
                        identifier, stage, current, total, source["relative_path"]
                    ),
                    video_frame_consumer,
                )
            except EmbeddingCancelled:
                SUBSTAGE_CONTEXT.pop(identifier, None)
                _finish_run(workspace, run["id"], "cancelled")
                store.cancel(identifier, processed, errors, skipped)
                return JobRunResult(identifier, processed, succeeded, errors, True, skipped)
            except Exception as error:
                errors += _source_units(source)
                store.record_error(identifier, error, physical_file_id=source["physical_file_id"], relative_path=source["relative_path"])
            else:
                succeeded += _source_units(source)
            processed += _source_units(source)
            _report(progress, identifier, processed, total_units, source, errors, skipped)
            store.checkpoint(identifier, processed, errors, skipped)
        SUBSTAGE_CONTEXT.pop(identifier, None)
        _finish_run(workspace, run["id"], "complete", None if not errors else f"{errors} source(s) failed")
        store.complete(identifier, processed, errors, skipped)
        _activate_run(workspace, provider, run["id"])
        return JobRunResult(identifier, processed, succeeded, errors, False, skipped)
    except Exception as error:
        SUBSTAGE_CONTEXT.pop(identifier, None)
        _finish_run(workspace, run["id"], "failed", str(error))
        store.fail(identifier)
        raise


def _embedding_sources(workspace: Workspace, configuration) -> list[dict[str, object]]:
    connection = workspace.connect()
    try:
        rows = connection.execute(
            "SELECT * FROM physical_file WHERE in_scope = 1 AND is_online = 1 ORDER BY logical_asset_id, relative_path"
        ).fetchall()
    finally:
        connection.close()
    grouped: dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row["logical_asset_id"]].append(row)
    sources: list[dict[str, object]] = []
    for asset_id, members in grouped.items():
        media_type = members[0]["media_type"]
        if media_type == "video":
            if not configuration.get("include_videos_in_semantic_search", True):
                continue
            selected = preferred_physical(members)
            if selected is not None:
                sources.append(_source(selected, asset_id, "video", configuration))
            continue
        rendered = [row for row in members if is_rendered_image_extension(row["extension"])]
        raw = [row for row in members if is_raw_extension(row["extension"])]
        selected = preferred_physical(rendered or raw)
        if selected is not None:
            sources.append(_source(selected, asset_id, "rendered" if rendered else "raw_preview", configuration))
    return sources


def _source(row, asset_id: str, source_kind: str, configuration) -> dict[str, object]:
    return {
        "asset_id": asset_id,
        "physical_file_id": row["id"],
        "relative_path": row["relative_path"],
        "extension": row["extension"],
        "media_type": row["media_type"],
        "source_kind": source_kind,
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "mtime_ns": row["mtime_ns"],
        "duration_seconds": row["duration_seconds"],
        "configuration": configuration,
    }


def _get_or_create_run(workspace: Workspace, provider: EmbeddingProvider, settings_json: str):
    connection = workspace.connect()
    try:
        row = connection.execute(
            "SELECT * FROM embedding_run WHERE provider = ? AND model_version = ? AND settings_json = ? AND status IN ('complete', 'running', 'cancelled', 'failed') ORDER BY created_at DESC LIMIT 1",
            (provider.provider_id, provider.version, settings_json),
        ).fetchone()
    finally:
        connection.close()
    if row is not None:
        with workspace.transaction() as connection:
            connection.execute(
                "UPDATE embedding_run SET status = 'running', completed_at = NULL, error_message = NULL WHERE id = ?",
                (row["id"],),
            )
        return row
    run_id = str(uuid.uuid4())
    now = _timestamp()
    with workspace.transaction() as connection:
        connection.execute(
            "INSERT INTO embedding_run(id, provider, model_id, model_version, embedding_dimension, settings_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
            (run_id, provider.provider_id, provider.model_id, provider.version, provider.dimension, settings_json, now),
        )
    return {"id": run_id, "provider": provider.provider_id}


def _ensure_states(workspace, sources, provider, settings_json, run_id):
    component = embedding_component(provider)
    with workspace.transaction() as connection:
        for source in sources:
            fingerprint = _input_fingerprint(source, provider)
            connection.execute(
                """
                INSERT INTO component_state(physical_file_id, component, status, algorithm, version, settings_json, input_fingerprint)
                VALUES (?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(physical_file_id, component) DO UPDATE SET
                    status = CASE WHEN component_state.status = 'complete' AND component_state.input_fingerprint = excluded.input_fingerprint THEN component_state.status ELSE 'pending' END,
                    algorithm = excluded.algorithm, version = excluded.version, settings_json = excluded.settings_json,
                    input_fingerprint = excluded.input_fingerprint, started_at = CASE WHEN component_state.status = 'complete' AND component_state.input_fingerprint = excluded.input_fingerprint THEN component_state.started_at ELSE NULL END,
                    completed_at = CASE WHEN component_state.status = 'complete' AND component_state.input_fingerprint = excluded.input_fingerprint THEN component_state.completed_at ELSE NULL END,
                    error_message = CASE WHEN component_state.status = 'complete' AND component_state.input_fingerprint = excluded.input_fingerprint THEN component_state.error_message ELSE NULL END
                """,
                (source["physical_file_id"], component, EMBEDDING_ALGORITHM, provider.version, settings_json, fingerprint),
            )


def _mark_disabled_states(workspace, sources, provider_id):
    component = embedding_component(provider_id)
    with workspace.transaction() as connection:
        for source in sources:
            connection.execute(
                "UPDATE component_state SET status = CASE WHEN status = 'complete' THEN status ELSE 'not_requested' END WHERE physical_file_id = ? AND component = ?",
                (source["physical_file_id"], component),
            )


def _mark_unavailable_states(workspace, sources, provider_id, error):
    component = embedding_component(provider_id)
    with workspace.transaction() as connection:
        for source in sources:
            connection.execute(
                """
                INSERT INTO component_state(
                    physical_file_id, component, status, algorithm, version,
                    input_fingerprint, error_message
                ) VALUES (?, ?, 'not_requested', ?, ?, ?, ?)
                ON CONFLICT(physical_file_id, component) DO UPDATE SET
                    status = CASE WHEN component_state.status = 'complete' THEN component_state.status ELSE 'not_requested' END,
                    error_message = CASE WHEN component_state.status = 'complete' THEN component_state.error_message ELSE excluded.error_message END
                """,
                (source["physical_file_id"], component, EMBEDDING_ALGORITHM, "unavailable", source.get("sha256"), str(error)),
            )


def _state_ready(workspace, source, provider, run_id) -> bool:
    component = embedding_component(provider)
    fingerprint = _input_fingerprint(source, provider)
    connection = workspace.connect()
    try:
        state = connection.execute(
            "SELECT status, input_fingerprint FROM component_state WHERE physical_file_id = ? AND component = ?",
            (source["physical_file_id"], component),
        ).fetchone()
        if state is None or state["status"] != "complete" or state["input_fingerprint"] != fingerprint:
            return False
        if source["source_kind"] == "video":
            expected = _video_sample_count(source)
            count = connection.execute(
                """
                SELECT COUNT(*)
                FROM video_frame_embedding AS vfe
                JOIN workspace_video_sample AS wvs
                  ON wvs.physical_file_id = vfe.physical_file_id
                 AND wvs.active_run_id = vfe.sample_run_id
                WHERE vfe.run_id = ? AND vfe.logical_asset_id = ? AND vfe.physical_file_id = ?
                """,
                (run_id, source["asset_id"], source["physical_file_id"]),
            ).fetchone()[0]
            return count == expected
        row = connection.execute(
            "SELECT 1 FROM logical_asset_embedding WHERE run_id = ? AND logical_asset_id = ? AND source_physical_file_id = ? AND input_fingerprint = ?",
            (run_id, source["asset_id"], source["physical_file_id"], fingerprint),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _process_image_batches(
    workspace,
    sources,
    provider,
    run_id,
    settings_json,
    cancel_event,
    preparation_workers,
    batch_size,
    consume,
    infer,
):
    if not sources:
        return False
    executor = ThreadPoolExecutor(
        max_workers=preparation_workers,
        thread_name_prefix="archive-index-embedding-prep",
    )
    batches = iter(sources[start : start + batch_size] for start in range(0, len(sources), batch_size))
    pending: list[tuple[Future, list[dict[str, object]]]] = []
    queue_depth = max(2, min(preparation_workers, 4))

    def submit_next() -> bool:
        try:
            batch = next(batches)
        except StopIteration:
            return False
        pending.append((executor.submit(_prepare_image_batch, workspace, batch, provider, cancel_event), batch))
        return True

    try:
        for _ in range(queue_depth):
            if not submit_next():
                break
        while pending:
            if _cancelled(cancel_event):
                return True
            future, batch = pending.pop(0)
            try:
                prepared = future.result()
            except _ImagePreparationCancelled:
                return True
            except Exception as error:
                prepared = _PreparedImageBatch(
                    None,
                    [],
                    [],
                    [(source, error) for source in batch],
                    [],
                )
            if _cancelled(cancel_event):
                return True
            if prepared.outcomes or prepared.prepared_sources or prepared.individual:
                consume(_consume_prepared_image_batch(workspace, prepared, provider, run_id, settings_json, infer))
            if _cancelled(cancel_event):
                return True
            submit_next()
        return False
    finally:
        for future, _ in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def _prepare_image_batch(workspace, sources, provider, cancel_event):
    images: list[Image.Image] = []
    loaded: list[tuple[dict[str, object], Image.Image]] = []
    outcomes: list[tuple[dict[str, object], object]] = []
    try:
        for source in sources:
            if _cancelled(cancel_event):
                raise _ImagePreparationCancelled("embedding preparation cancelled")
            try:
                image = _load_source_image(workspace, source)
            except Exception as error:
                outcomes.append((source, error))
            else:
                loaded.append((source, image))
                images.append(image)
        if not loaded:
            return _PreparedImageBatch(None, [], [], outcomes, [])
        try:
            prepared = provider.prepare_images(images)
        except Exception:
            individual: list[tuple[dict[str, object], object, list[Image.Image]]] = []
            for source, image in loaded:
                if _cancelled(cancel_event):
                    raise _ImagePreparationCancelled("embedding preparation cancelled")
                try:
                    single = provider.prepare_images([image])
                except Exception as error:
                    outcomes.append((source, error))
                else:
                    individual.append((source, single, [image]))
            return _PreparedImageBatch(None, [], individual, outcomes, [])
        cleanup_images = images if type(provider).prepare_images is EmbeddingProvider.prepare_images else []
        return _PreparedImageBatch(prepared, [source for source, _ in loaded], [], outcomes, cleanup_images)
    except _ImagePreparationCancelled:
        raise
    except Exception as error:
        reported = {id(item[0]) for item in outcomes}
        outcomes.extend((source, error) for source in sources if id(source) not in reported)
        return _PreparedImageBatch(None, [], [], outcomes, [])
    finally:
        cleanup_ids = {id(image) for image in locals().get("cleanup_images", [])}
        cleanup_ids.update(id(image) for _, _, images_to_close in locals().get("individual", []) for image in images_to_close)
        for image in images:
            if id(image) not in cleanup_ids:
                image.close()


def _consume_prepared_image_batch(workspace, batch, provider, run_id, settings_json, infer):
    outcomes = list(batch.outcomes)
    try:
        if batch.prepared_sources:
            try:
                vectors = infer(lambda active: active.encode_prepared_images(batch.prepared))
                if len(vectors) != len(batch.prepared_sources):
                    raise ValueError("embedding provider returned an unexpected image count")
            except Exception:
                for source in batch.prepared_sources:
                    try:
                        _process_single_image(workspace, source, provider, run_id, infer)
                    except Exception as error:
                        outcomes.append((source, error))
                    else:
                        outcomes.append((source, None))
            else:
                for source, vector in zip(batch.prepared_sources, vectors):
                    try:
                        _store_image_embedding(workspace, source, provider, run_id, vector)
                    except Exception as error:
                        outcomes.append((source, error))
                    else:
                        outcomes.append((source, None))
        for source, prepared, cleanup_images in batch.individual:
            try:
                vectors = infer(lambda active: active.encode_prepared_images(prepared))
                if len(vectors) != 1:
                    raise ValueError("embedding provider returned an unexpected image count")
                _store_image_embedding(workspace, source, provider, run_id, vectors[0])
            except Exception as error:
                outcomes.append((source, error))
            else:
                outcomes.append((source, None))
            finally:
                for image in cleanup_images:
                    image.close()
    finally:
        for image in batch.cleanup_images:
            image.close()
    return outcomes


def _process_single_image(workspace, source, provider, run_id, infer):
    image = _load_source_image(workspace, source)
    try:
        prepared = provider.prepare_images([image])
        vectors = infer(lambda active: active.encode_prepared_images(prepared))
        if len(vectors) != 1:
            raise ValueError("embedding provider returned an unexpected image count")
        _store_image_embedding(workspace, source, provider, run_id, vectors[0])
    finally:
        image.close()


def _load_source_image(workspace, source):
    path = workspace.absolute_path(source["relative_path"])
    if source["source_kind"] == "raw_preview":
        return extract_embedded_preview(path).image
    return load_full_image(path)


def _store_image_embedding(workspace, source, provider, run_id, vector):
    blob, dimension = vector_to_blob(vector)
    if dimension != provider.dimension:
        raise ValueError(f"embedding dimension mismatch: expected {provider.dimension}, got {dimension}")
    now = _timestamp()
    fingerprint = _input_fingerprint(source, provider)
    with workspace.transaction() as connection:
        connection.execute(
            """
            INSERT INTO logical_asset_embedding(run_id, logical_asset_id, source_physical_file_id, source_kind, input_fingerprint, embedding, embedding_dimension, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, logical_asset_id) DO UPDATE SET
                source_physical_file_id = excluded.source_physical_file_id, source_kind = excluded.source_kind,
                input_fingerprint = excluded.input_fingerprint, embedding = excluded.embedding,
                embedding_dimension = excluded.embedding_dimension, updated_at = excluded.updated_at
            """,
            (run_id, source["asset_id"], source["physical_file_id"], source["source_kind"], fingerprint, blob, dimension, now, now),
        )
        connection.execute(
            "UPDATE component_state SET status = 'complete', algorithm = ?, version = ?, input_fingerprint = ?, started_at = NULL, completed_at = ?, error_message = NULL WHERE physical_file_id = ? AND component = ?",
            (EMBEDDING_ALGORITHM, provider.version, fingerprint, now, source["physical_file_id"], embedding_component(provider)),
        )


def _process_video(
    workspace,
    source,
    provider,
    run_id,
    settings_json,
    cancel_event,
    infer,
    substage=None,
    video_frame_consumer=None,
):
    duration = float(source["duration_seconds"] or 0)
    count = _video_sample_count(source)
    sample_run_id, sample_rows = _sample_context(workspace, source, duration, count)
    timestamps = [row["requested_timestamp"] for row in sample_rows]
    if substage:
        substage("Extracting frames", 0, count)
    try:
        frames = extract_video_frames(
            workspace.absolute_path(source["relative_path"]),
            timestamps,
            cancel_event,
            seek_per_frame=duration > VIDEO_SINGLE_PROCESS_MAX_DURATION_SECONDS,
            **({"progress": lambda current, total: substage("Extracting frames", current, total)} if substage else {}),
        )
    except VideoExtractionCancelled as error:
        raise EmbeddingCancelled("embedding cancelled") from error
    try:
        connection = workspace.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM video_sample WHERE run_id = ? ORDER BY sample_index", (sample_run_id,)
            ).fetchall()
        finally:
            connection.close()
        if substage:
            substage("Embedding frames", 0, len(frames))
        for index in range(0, len(frames), max(1, provider.batch_size)):
            if cancel_event is not None and cancel_event.is_set():
                raise EmbeddingCancelled("embedding cancelled")
            frame_batch = frames[index : index + max(1, provider.batch_size)]
            vectors = infer(lambda active: active.encode_images([frame.image for frame in frame_batch]))
            if len(vectors) != len(frame_batch):
                raise ValueError("embedding provider returned an unexpected frame count")
            if substage:
                substage("Embedding frames", index + len(frame_batch), len(frames))
            for offset, vector in enumerate(vectors):
                sample = rows[index + offset]
                blob, dimension = vector_to_blob(vector)
                if dimension != provider.dimension:
                    raise ValueError(
                        f"embedding dimension mismatch: expected {provider.dimension}, got {dimension}"
                    )
                frame_fingerprint = _frame_fingerprint(source, provider, sample_run_id, sample["sample_index"])
                now = _timestamp()
                with workspace.transaction() as connection:
                    connection.execute(
                        "UPDATE video_sample SET extraction_status = 'complete', actual_timestamp = COALESCE(actual_timestamp, requested_timestamp), error_message = NULL WHERE run_id = ? AND sample_index = ?",
                        (sample_run_id, sample["sample_index"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO video_frame_embedding(run_id, logical_asset_id, physical_file_id, sample_run_id, sample_index, timestamp_seconds, input_fingerprint, embedding, embedding_dimension, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(run_id, sample_run_id, sample_index) DO UPDATE SET
                            timestamp_seconds = excluded.timestamp_seconds, input_fingerprint = excluded.input_fingerprint,
                            embedding = excluded.embedding, embedding_dimension = excluded.embedding_dimension, updated_at = excluded.updated_at
                        """,
                        (run_id, source["asset_id"], source["physical_file_id"], sample_run_id, sample["sample_index"], sample["requested_timestamp"], frame_fingerprint, blob, dimension, now, now),
                    )
        fingerprint = _input_fingerprint(source, provider)
        with workspace.transaction() as connection:
            connection.execute(
                "UPDATE component_state SET status = 'complete', algorithm = ?, version = ?, settings_json = ?, input_fingerprint = ?, started_at = NULL, completed_at = ?, error_message = NULL WHERE physical_file_id = ? AND component = ?",
                (EMBEDDING_ALGORITHM, provider.version, settings_json, fingerprint, _timestamp(), source["physical_file_id"], embedding_component(provider)),
            )
            connection.execute(
                "UPDATE video_sample_run SET status = 'complete', successful_count = ?, completed_at = ?, error_message = NULL WHERE id = ?",
                (len(frames), _timestamp(), sample_run_id),
            )
        if video_frame_consumer is not None:
            try:
                video_frame_consumer(source, rows, frames)
            except Exception:
                pass
            if cancel_event is not None and cancel_event.is_set():
                raise EmbeddingCancelled("embedding cancelled")
    finally:
        for frame in frames:
            frame.image.close()


def _sample_context(workspace, source, duration, count):
    configuration = workspace.configuration()
    settings = {
        "sampler_algorithm": VIDEO_SAMPLER_ALGORITHM,
        "sampler_version": VIDEO_SAMPLER_VERSION,
        "target_fps": configuration["video_sampling_fps"],
        "min_frames": configuration["video_sampling_min_frames"],
        "max_frames": configuration["video_sampling_max_frames"],
        "sample_position": "duration * (index + 0.5) / sample_count",
    }
    settings_json = json.dumps(settings, ensure_ascii=False, sort_keys=True)
    input_fingerprint = _video_source_fingerprint(source, duration)
    connection = workspace.connect()
    try:
        active = connection.execute(
            "SELECT vsr.* FROM workspace_video_sample AS wvs JOIN video_sample_run AS vsr ON vsr.id = wvs.active_run_id WHERE wvs.physical_file_id = ?",
            (source["physical_file_id"],),
        ).fetchone()
        if active is not None:
            try:
                old_settings = json.loads(active["settings_json"])
            except json.JSONDecodeError:
                old_settings = {}
            if (
                active["sampler_algorithm"] == VIDEO_SAMPLER_ALGORITHM
                and active["sampler_version"] == VIDEO_SAMPLER_VERSION
                and active["input_fingerprint"] == input_fingerprint
                and all(old_settings.get(key) == settings[key] for key in ("target_fps", "min_frames", "max_frames"))
                and active["requested_count"] == count
            ):
                return active["id"], connection.execute(
                    "SELECT * FROM video_sample WHERE run_id = ? ORDER BY sample_index", (active["id"],)
                ).fetchall()
    finally:
        connection.close()
    run_id = str(uuid.uuid4())
    now = _timestamp()
    with workspace.transaction() as connection:
        connection.execute(
            "INSERT INTO video_sample_run(id, physical_file_id, sampler_algorithm, sampler_version, settings_json, input_fingerprint, duration_seconds, requested_count, successful_count, status, aggregate_algorithm, aggregate_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'sampling_only', ?, '1', ?)",
            (run_id, source["physical_file_id"], VIDEO_SAMPLER_ALGORITHM, VIDEO_SAMPLER_VERSION, settings_json, input_fingerprint, duration, count, VIDEO_EMBEDDING_AGGREGATE, now),
        )
        connection.executemany(
            "INSERT INTO video_sample(run_id, physical_file_id, sample_index, requested_timestamp) VALUES (?, ?, ?, ?)",
            ((run_id, source["physical_file_id"], index, timestamp) for index, timestamp in enumerate(sample_timestamps(duration, count))),
        )
        connection.execute(
            "INSERT INTO workspace_video_sample(physical_file_id, active_run_id) VALUES (?, ?) ON CONFLICT(physical_file_id) DO UPDATE SET active_run_id = excluded.active_run_id",
            (source["physical_file_id"], run_id),
        )
        rows = connection.execute("SELECT * FROM video_sample WHERE run_id = ? ORDER BY sample_index", (run_id,)).fetchall()
    return run_id, rows


def _video_sample_count(source):
    configuration = source.get("configuration")
    if configuration is None:
        raise ValueError("video source is missing sampling configuration")
    return sample_count(float(source["duration_seconds"] or 0), float(configuration["video_sampling_fps"]), int(configuration["video_sampling_min_frames"]), int(configuration["video_sampling_max_frames"]))


def _input_fingerprint(source, provider):
    preview = "|raw-preview:v1" if source["source_kind"] == "raw_preview" else ""
    return hashlib.sha256(
        f"{source['sha256'] or f'stat:{source['size_bytes']}:{source['mtime_ns']}'}|{source['source_kind']}|{provider.provider_id}|{provider.version}{preview}".encode()
    ).hexdigest()


def _video_source_fingerprint(source, duration):
    return hashlib.sha256(f"{source['sha256'] or f'stat:{source['size_bytes']}:{source['mtime_ns']}'}|duration:{duration!r}".encode()).hexdigest()


def _frame_fingerprint(source, provider, sample_run_id, sample_index):
    return hashlib.sha256(f"{_input_fingerprint(source, provider)}|sample:{sample_run_id}:{sample_index}".encode()).hexdigest()


def _mark_state(workspace, source, provider, settings_json, fingerprint, status, error):
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE component_state SET status = ?, algorithm = ?, version = ?, settings_json = ?, input_fingerprint = ?, started_at = NULL, completed_at = NULL, error_message = ? WHERE physical_file_id = ? AND component = ?",
            ("unsupported" if isinstance(error, UnsupportedDecoderError) else status, EMBEDDING_ALGORITHM, provider.version, settings_json, fingerprint, str(error), source["physical_file_id"], embedding_component(provider)),
        )


def _finish_run(workspace, run_id, status, error_message=None):
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE embedding_run SET status = ?, completed_at = ?, error_message = ? WHERE id = ?",
            (status or "complete", _timestamp(), error_message, run_id),
        )


def _activate_run(workspace, provider, run_id):
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE workspace_embedding SET active_provider = ?, active_run_id = ?, updated_at = ? WHERE id = 1",
            (provider.provider_id, run_id, _timestamp()),
        )


def _report(progress, job_id, processed, total, source, failed, skipped):
    if progress is not None:
        progress(JobProgress(job_id, processed, total, source, "embeddings", failed, skipped))


def _report_cached(progress, job_id, total, sources):
    if progress is not None:
        completed = 0
        for source in sources:
            completed += _source_units(source)
            _report(progress, job_id, completed, total, source, 0, completed)


def _cancelled(event):
    return event is not None and event.is_set()


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_units(source):
    return _video_sample_count(source) if source["source_kind"] == "video" else 1
