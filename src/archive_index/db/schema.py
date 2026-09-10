"""Versioned SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 4

MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE workspace_info (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            workspace_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            app_version TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE logical_asset (
            id TEXT PRIMARY KEY,
            media_type TEXT NOT NULL,
            capture_time TEXT,
            capture_time_kind TEXT,
            selection_state TEXT NOT NULL DEFAULT 'undecided',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE physical_file (
            id TEXT PRIMARY KEY,
            logical_asset_id TEXT NOT NULL REFERENCES logical_asset(id) ON DELETE CASCADE,
            relative_path TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            extension TEXT NOT NULL,
            media_type TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'source_original',
            size_bytes INTEGER,
            mtime_ns INTEGER,
            sha256 TEXT,
            is_online INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX physical_file_asset_idx ON physical_file(logical_asset_id)",
        "CREATE INDEX physical_file_sha256_idx ON physical_file(sha256)",
        """
        CREATE TABLE component_state (
            physical_file_id TEXT NOT NULL REFERENCES physical_file(id) ON DELETE CASCADE,
            component TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            algorithm TEXT,
            version TEXT,
            settings_json TEXT,
            input_fingerprint TEXT,
            started_at TEXT,
            completed_at TEXT,
            error_message TEXT,
            PRIMARY KEY (physical_file_id, component)
        )
        """,
        """
        CREATE TABLE job (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            total_items INTEGER NOT NULL DEFAULT 0,
            completed_items INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """,
        """
        CREATE TABLE job_error (
            id INTEGER PRIMARY KEY,
            job_id TEXT REFERENCES job(id) ON DELETE CASCADE,
            physical_file_id TEXT REFERENCES physical_file(id) ON DELETE SET NULL,
            error_type TEXT NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            retry_count INTEGER NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX job_error_job_idx ON job_error(job_id)",
        "CREATE INDEX job_error_file_idx ON job_error(physical_file_id)",
    ),
    2: (
        "ALTER TABLE physical_file ADD COLUMN metadata_json TEXT",
        "ALTER TABLE physical_file ADD COLUMN width INTEGER",
        "ALTER TABLE physical_file ADD COLUMN height INTEGER",
        "ALTER TABLE physical_file ADD COLUMN duration_seconds REAL",
        "ALTER TABLE physical_file ADD COLUMN codec TEXT",
        "ALTER TABLE component_state ADD COLUMN output_path TEXT",
        "ALTER TABLE component_state ADD COLUMN output_fingerprint TEXT",
        "ALTER TABLE job_error ADD COLUMN relative_path TEXT",
    ),
    3: (
        "ALTER TABLE job ADD COLUMN stage TEXT",
        "ALTER TABLE job ADD COLUMN failed_items INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE job ADD COLUMN skipped_items INTEGER NOT NULL DEFAULT 0",
    ),
    4: (
        "ALTER TABLE physical_file ADD COLUMN quality_raw_json TEXT",
        "ALTER TABLE physical_file ADD COLUMN quality_components_json TEXT",
        "ALTER TABLE physical_file ADD COLUMN quality_score REAL",
        "ALTER TABLE physical_file ADD COLUMN quality_algorithm TEXT",
        "ALTER TABLE physical_file ADD COLUMN quality_version TEXT",
        "CREATE INDEX physical_file_quality_idx ON physical_file(quality_score)",
    ),
}


def apply_migrations(connection: sqlite3.Connection) -> None:
    """Bring a database to the current schema version."""

    current_version = connection.execute("PRAGMA user_version").fetchone()[0]
    if current_version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema {current_version} is newer than supported schema {SCHEMA_VERSION}"
        )

    for version in range(current_version + 1, SCHEMA_VERSION + 1):
        applied_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with connection:
            for statement in MIGRATIONS[version]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, applied_at),
            )
            connection.execute(f"PRAGMA user_version = {version}")
