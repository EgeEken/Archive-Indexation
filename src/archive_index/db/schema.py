"""Versioned SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 7

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
    5: (
        """
        CREATE TABLE visual_feature (
            physical_file_id TEXT PRIMARY KEY REFERENCES physical_file(id) ON DELETE CASCADE,
            algorithm TEXT NOT NULL,
            version TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            dhash TEXT NOT NULL,
            luma_json TEXT NOT NULL,
            color_hist_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX visual_feature_fingerprint_idx ON visual_feature(input_fingerprint)",
        """
        CREATE TABLE grouping_run (
            id TEXT PRIMARY KEY,
            algorithm TEXT NOT NULL,
            version TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE workspace_grouping (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active_run_id TEXT REFERENCES grouping_run(id),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE strict_group (
            run_id TEXT NOT NULL REFERENCES grouping_run(id) ON DELETE CASCADE,
            group_id TEXT NOT NULL,
            first_capture_time TEXT,
            member_count INTEGER NOT NULL,
            representative_logical_asset_id TEXT REFERENCES logical_asset(id) ON DELETE SET NULL,
            PRIMARY KEY (run_id, group_id)
        )
        """,
        """
        CREATE TABLE strict_group_member (
            run_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            logical_asset_id TEXT NOT NULL REFERENCES logical_asset(id) ON DELETE CASCADE,
            member_order INTEGER NOT NULL,
            is_representative INTEGER NOT NULL DEFAULT 0,
            min_visual_similarity REAL,
            max_capture_delta_seconds REAL,
            PRIMARY KEY (run_id, logical_asset_id),
            FOREIGN KEY (run_id, group_id)
                REFERENCES strict_group(run_id, group_id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX strict_group_member_group_idx ON strict_group_member(run_id, group_id, member_order)",
        "CREATE INDEX strict_group_member_asset_idx ON strict_group_member(logical_asset_id)",
    ),
    6: (
        "ALTER TABLE logical_asset ADD COLUMN selection_updated_at TEXT",
        """
        CREATE TABLE recommendation_run (
            id TEXT PRIMARY KEY,
            algorithm TEXT NOT NULL,
            version TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE workspace_recommendation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active_run_id TEXT REFERENCES recommendation_run(id),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE asset_recommendation (
            run_id TEXT NOT NULL REFERENCES recommendation_run(id) ON DELETE CASCADE,
            logical_asset_id TEXT NOT NULL REFERENCES logical_asset(id) ON DELETE CASCADE,
            auto_recommended INTEGER NOT NULL CHECK (auto_recommended IN (0, 1)),
            recommendation_rank INTEGER,
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
            reason TEXT NOT NULL,
            PRIMARY KEY (run_id, logical_asset_id)
        )
        """,
        "CREATE INDEX asset_recommendation_asset_idx ON asset_recommendation(logical_asset_id)",
    ),
    7: (
        "ALTER TABLE recommendation_run ADD COLUMN source_grouping_run_id TEXT REFERENCES grouping_run(id)",
        "CREATE INDEX recommendation_run_grouping_idx ON recommendation_run(source_grouping_run_id)",
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
    _repair_known_schema_drift(connection)


def _repair_known_schema_drift(connection: sqlite3.Connection) -> None:
    logical_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(logical_asset)").fetchall()
    }
    if "selection_updated_at" not in logical_columns:
        with connection:
            connection.execute("ALTER TABLE logical_asset ADD COLUMN selection_updated_at TEXT")
