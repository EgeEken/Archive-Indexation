"""Common persistent job lifecycle used by indexing components."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event

from ..workspace import Workspace

LOGGER = logging.getLogger(__name__)
JOB_STATES = frozenset({"pending", "running", "complete", "failed", "cancelled", "interrupted"})


@dataclass(frozen=True)
class JobProgress:
    job_id: str
    processed: int
    total: int
    item: object
    stage: str | None = None
    failed: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class JobRunResult:
    job_id: str
    processed: int
    succeeded: int
    errors: int
    cancelled: bool
    skipped: int = 0


class JobStore:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def create(self, kind: str, total_items: int = 0, job_id: str | None = None) -> str:
        identifier = job_id or str(uuid.uuid4())
        now = _timestamp()
        with self.workspace.transaction() as connection:
            connection.execute(
                """
                INSERT INTO job(id, kind, status, total_items, stage, created_at, updated_at)
                VALUES (?, ?, 'pending', ?, ?, ?, ?)
                """,
                (identifier, kind, total_items, kind, now, now),
            )
        return identifier

    def start(self, job_id: str) -> None:
        now = _timestamp()
        with self.workspace.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE job
                SET status = 'running', stage = COALESCE(stage, kind), updated_at = ?, started_at = COALESCE(started_at, ?),
                    finished_at = NULL
                WHERE id = ? AND status IN ('pending', 'cancelled', 'interrupted')
                """,
                (now, now, job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"job cannot start: {job_id}")

    def set_total(self, job_id: str, total_items: int) -> None:
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE job SET total_items = ?, updated_at = ? WHERE id = ?",
                (total_items, _timestamp(), job_id),
            )

    def set_stage(self, job_id: str, stage: str) -> None:
        with self.workspace.transaction() as connection:
            connection.execute(
                "UPDATE job SET stage = ?, updated_at = ? WHERE id = ?",
                (stage, _timestamp(), job_id),
            )

    def checkpoint(
        self,
        job_id: str,
        completed_items: int,
        failed_items: int = 0,
        skipped_items: int = 0,
    ) -> None:
        with self.workspace.transaction() as connection:
            connection.execute(
                """
                UPDATE job
                SET completed_items = ?, failed_items = ?, skipped_items = ?, updated_at = ?
                WHERE id = ?
                """,
                (completed_items, failed_items, skipped_items, _timestamp(), job_id),
            )

    def complete(
        self,
        job_id: str,
        completed_items: int | None = None,
        failed_items: int | None = None,
        skipped_items: int | None = None,
    ) -> None:
        self._finish(job_id, "complete", completed_items, failed_items, skipped_items)

    def cancel(
        self,
        job_id: str,
        completed_items: int | None = None,
        failed_items: int | None = None,
        skipped_items: int | None = None,
    ) -> None:
        self._finish(job_id, "cancelled", completed_items, failed_items, skipped_items)

    def fail(self, job_id: str) -> None:
        self._finish(job_id, "failed", None, None, None)

    def record_error(
        self,
        job_id: str,
        error: BaseException,
        *,
        physical_file_id: str | None = None,
        relative_path: str | None = None,
    ) -> None:
        with self.workspace.transaction() as connection:
            connection.execute(
                """
                INSERT INTO job_error(
                    job_id, physical_file_id, relative_path, error_type, message, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    physical_file_id,
                    relative_path,
                    type(error).__name__,
                    str(error),
                    _timestamp(),
                ),
            )

    def recover_interrupted(self) -> int:
        with self.workspace.transaction() as connection:
            now = _timestamp()
            cursor = connection.execute(
                """
                UPDATE job
                SET status = 'interrupted', updated_at = ?, finished_at = ?
                WHERE status IN ('pending', 'running')
                """,
                (now, now),
            )
            connection.execute(
                """
                UPDATE component_state
                SET status = 'pending', started_at = NULL, error_message = NULL
                WHERE status = 'running'
                """
            )
        return cursor.rowcount

    def get(self, job_id: str):
        connection = self.workspace.connect()
        try:
            return connection.execute("SELECT * FROM job WHERE id = ?", (job_id,)).fetchone()
        finally:
            connection.close()

    def list_errors(self, job_id: str | None = None):
        connection = self.workspace.connect()
        try:
            if job_id is None:
                return connection.execute(
                    "SELECT * FROM job_error ORDER BY id DESC"
                ).fetchall()
            return connection.execute(
                "SELECT * FROM job_error WHERE job_id = ? ORDER BY id DESC", (job_id,)
            ).fetchall()
        finally:
            connection.close()

    def _finish(
        self,
        job_id: str,
        status: str,
        completed_items: int | None,
        failed_items: int | None,
        skipped_items: int | None,
    ) -> None:
        if status not in {"complete", "failed", "cancelled"}:
            raise ValueError(f"invalid terminal job state: {status}")
        now = _timestamp()
        with self.workspace.transaction() as connection:
            assignments = ["status = ?", "updated_at = ?", "finished_at = ?"]
            parameters: list[object] = [status, now, now]
            if completed_items is not None:
                assignments.append("completed_items = ?")
                parameters.append(completed_items)
            if failed_items is not None:
                assignments.append("failed_items = ?")
                parameters.append(failed_items)
            if skipped_items is not None:
                assignments.append("skipped_items = ?")
                parameters.append(skipped_items)
            parameters.append(job_id)
            connection.execute(
                f"UPDATE job SET {', '.join(assignments)} WHERE id = ?", parameters
            )


def run_items(
    workspace: Workspace,
    kind: str,
    items: Iterable[object],
    worker: Callable[[object], object],
    *,
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[JobProgress], None] | None = None,
    physical_file_id: Callable[[object], str | None] | None = None,
    relative_path: Callable[[object], str | None] | None = None,
    stage: str | None = None,
) -> JobRunResult:
    item_list = list(items)
    store = JobStore(workspace)
    identifier = job_id or store.create(kind, len(item_list))
    store.set_total(identifier, len(item_list))
    if stage is not None:
        store.set_stage(identifier, stage)
    store.start(identifier)
    processed = 0
    succeeded = 0
    errors = 0
    skipped = 0
    checkpoint_interval = 16

    try:
        for item in item_list:
            if cancel_event is not None and cancel_event.is_set():
                store.checkpoint(identifier, processed, errors, skipped)
                store.cancel(identifier, processed, errors, skipped)
                return JobRunResult(identifier, processed, succeeded, errors, True, skipped)
            try:
                outcome = worker(item)
            except Exception as error:
                errors += 1
                store.record_error(
                    identifier,
                    error,
                    physical_file_id=physical_file_id(item) if physical_file_id else None,
                    relative_path=relative_path(item) if relative_path else None,
                )
                LOGGER.warning("job %s failed for item: %s", identifier, error)
            else:
                succeeded += 1
                if outcome == "skipped":
                    skipped += 1
            processed += 1
            if processed % checkpoint_interval == 0:
                store.checkpoint(identifier, processed, errors, skipped)
            if progress is not None:
                progress(JobProgress(identifier, processed, len(item_list), item, stage or kind, errors, skipped))
        store.complete(identifier, processed, errors, skipped)
        return JobRunResult(identifier, processed, succeeded, errors, False, skipped)
    except Exception:
        store.fail(identifier)
        raise


def run_batches(
    workspace: Workspace,
    kind: str,
    items: Iterable[object],
    batch_worker: Callable[[list[object]], dict[str, object]],
    *,
    batch_size: int,
    item_key: Callable[[object], str],
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[JobProgress], None] | None = None,
    physical_file_id: Callable[[object], str | None] | None = None,
    relative_path: Callable[[object], str | None] | None = None,
    stage: str | None = None,
) -> JobRunResult:
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    item_list = list(items)
    store = JobStore(workspace)
    identifier = job_id or store.create(kind, len(item_list))
    store.set_total(identifier, len(item_list))
    if stage is not None:
        store.set_stage(identifier, stage)
    store.start(identifier)
    processed = succeeded = errors = skipped = 0
    try:
        for start in range(0, len(item_list), batch_size):
            batch = item_list[start : start + batch_size]
            if cancel_event is not None and cancel_event.is_set():
                store.checkpoint(identifier, processed, errors, skipped)
                store.cancel(identifier, processed, errors, skipped)
                return JobRunResult(identifier, processed, succeeded, errors, True, skipped)
            outcomes = batch_worker(batch)
            for item in batch:
                outcome = outcomes.get(item_key(item))
                if isinstance(outcome, BaseException):
                    errors += 1
                    store.record_error(
                        identifier,
                        outcome,
                        physical_file_id=physical_file_id(item) if physical_file_id else None,
                        relative_path=relative_path(item) if relative_path else None,
                    )
                    LOGGER.warning("job %s failed for item: %s", identifier, outcome)
                else:
                    succeeded += 1
                    if outcome == "skipped":
                        skipped += 1
                processed += 1
                if progress is not None:
                    progress(JobProgress(identifier, processed, len(item_list), item, stage or kind, errors, skipped))
            store.checkpoint(identifier, processed, errors, skipped)
        store.complete(identifier, processed, errors, skipped)
        return JobRunResult(identifier, processed, succeeded, errors, False, skipped)
    except Exception:
        store.fail(identifier)
        raise


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
