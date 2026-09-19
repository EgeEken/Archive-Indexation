"""Versioned SQLite schema migrations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 23

DEFAULT_IMAGE_EXTENSIONS_JSON = json.dumps(sorted({
    ".arw", ".avif", ".cr2", ".cr3", ".dng", ".heic", ".heif", ".jpeg",
    ".jpg", ".jxl", ".nef", ".png", ".raf", ".rw2", ".webp",
}), separators=(",", ":"))
DEFAULT_VIDEO_EXTENSIONS_JSON = json.dumps(sorted({
    ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm",
}), separators=(",", ":"))

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
    8: (
        "ALTER TABLE workspace_info ADD COLUMN quality_provider TEXT NOT NULL DEFAULT 'off'",
        "UPDATE physical_file SET quality_raw_json = NULL, quality_components_json = NULL, quality_score = NULL, quality_algorithm = NULL, quality_version = NULL",
        "UPDATE component_state SET status = 'pending', algorithm = NULL, version = NULL, input_fingerprint = NULL, started_at = NULL, completed_at = NULL, error_message = NULL WHERE component = 'quality'",
    ),
    9: (
        "UPDATE workspace_info SET quality_provider = 'lar-iqa' WHERE quality_provider = 'off'",
    ),
    10: (
        "ALTER TABLE physical_file ADD COLUMN in_scope INTEGER NOT NULL DEFAULT 1 CHECK (in_scope IN (0, 1))",
        """
        CREATE TABLE workspace_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            quality_provider TEXT NOT NULL CHECK (quality_provider IN ('off', 'lar-iqa')),
            include_images INTEGER NOT NULL DEFAULT 1 CHECK (include_images IN (0, 1)),
            include_videos INTEGER NOT NULL DEFAULT 1 CHECK (include_videos IN (0, 1)),
            image_extensions_json TEXT NOT NULL,
            video_extensions_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE folder_scope_rule (
            path TEXT PRIMARY KEY,
            included INTEGER NOT NULL CHECK (included IN (0, 1))
        )
        """,
        f"""
        INSERT INTO workspace_config(
            id, quality_provider, include_images, include_videos,
            image_extensions_json, video_extensions_json, updated_at
        )
        SELECT 1, COALESCE(quality_provider, 'lar-iqa'), 1, 1,
               '{DEFAULT_IMAGE_EXTENSIONS_JSON}',
               '{DEFAULT_VIDEO_EXTENSIONS_JSON}', COALESCE(updated_at, datetime('now'))
        FROM workspace_info WHERE id = 1
        """,
    ),
    11: (
        "ALTER TABLE workspace_config ADD COLUMN configuration_version INTEGER NOT NULL DEFAULT 1",
    ),
    12: (
        """
        CREATE TABLE IF NOT EXISTS reconciliation_run (
            id TEXT PRIMARY KEY,
            algorithm TEXT NOT NULL,
            version TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS workspace_reconciliation (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active_run_id TEXT REFERENCES reconciliation_run(id),
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS physical_relationship (
            run_id TEXT NOT NULL REFERENCES reconciliation_run(id) ON DELETE CASCADE,
            source_physical_file_id TEXT NOT NULL REFERENCES physical_file(id) ON DELETE CASCADE,
            target_physical_file_id TEXT NOT NULL REFERENCES physical_file(id) ON DELETE CASCADE,
            relationship_type TEXT NOT NULL,
            algorithm TEXT NOT NULL,
            version TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            PRIMARY KEY (run_id, source_physical_file_id, target_physical_file_id, relationship_type)
        )
        """,
        "CREATE INDEX IF NOT EXISTS physical_relationship_target_idx ON physical_relationship(target_physical_file_id)",
        """
        CREATE TABLE IF NOT EXISTS reconciliation_conflict (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES reconciliation_run(id) ON DELETE CASCADE,
            left_logical_asset_id TEXT REFERENCES logical_asset(id) ON DELETE SET NULL,
            right_logical_asset_id TEXT REFERENCES logical_asset(id) ON DELETE SET NULL,
            conflict_type TEXT NOT NULL,
            message TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, left_logical_asset_id, right_logical_asset_id, conflict_type)
        )
        """,
        "CREATE INDEX IF NOT EXISTS reconciliation_conflict_run_idx ON reconciliation_conflict(run_id)",
    ),
    13: (
        "ALTER TABLE workspace_config ADD COLUMN video_sampling_fps REAL NOT NULL DEFAULT 2.0",
        "ALTER TABLE workspace_config ADD COLUMN video_sampling_min_frames INTEGER NOT NULL DEFAULT 2",
        "ALTER TABLE workspace_config ADD COLUMN video_sampling_max_frames INTEGER NOT NULL DEFAULT 32",
        """
        CREATE TABLE IF NOT EXISTS video_sample_run (
            id TEXT PRIMARY KEY,
            physical_file_id TEXT NOT NULL REFERENCES physical_file(id) ON DELETE CASCADE,
            sampler_algorithm TEXT NOT NULL,
            sampler_version TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            duration_seconds REAL NOT NULL,
            requested_count INTEGER NOT NULL,
            successful_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            aggregate_algorithm TEXT NOT NULL,
            aggregate_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS video_sample_run_file_idx ON video_sample_run(physical_file_id, created_at)",
        """
        CREATE TABLE IF NOT EXISTS video_sample (
            run_id TEXT NOT NULL REFERENCES video_sample_run(id) ON DELETE CASCADE,
            physical_file_id TEXT NOT NULL REFERENCES physical_file(id) ON DELETE CASCADE,
            sample_index INTEGER NOT NULL,
            requested_timestamp REAL NOT NULL,
            extraction_status TEXT NOT NULL DEFAULT 'pending',
            quality_status TEXT NOT NULL DEFAULT 'pending',
            quality_raw_json TEXT,
            quality_score REAL,
            error_message TEXT,
            PRIMARY KEY (run_id, sample_index),
            UNIQUE (run_id, physical_file_id, sample_index)
        )
        """,
        "CREATE INDEX IF NOT EXISTS video_sample_file_idx ON video_sample(physical_file_id, run_id, sample_index)",
        """
        CREATE TABLE IF NOT EXISTS workspace_video_sample (
            physical_file_id TEXT PRIMARY KEY REFERENCES physical_file(id) ON DELETE CASCADE,
            active_run_id TEXT NOT NULL REFERENCES video_sample_run(id)
        )
        """,
    ),
    14: (
        "ALTER TABLE video_sample ADD COLUMN actual_timestamp REAL",
        "ALTER TABLE video_sample ADD COLUMN timestamp_error_seconds REAL",
    ),
    15: (
        "ALTER TABLE workspace_config ADD COLUMN include_rendered_images INTEGER NOT NULL DEFAULT 1 CHECK (include_rendered_images IN (0, 1))",
        "ALTER TABLE workspace_config ADD COLUMN include_raw INTEGER NOT NULL DEFAULT 1 CHECK (include_raw IN (0, 1))",
        "ALTER TABLE workspace_config ADD COLUMN rendered_quality_provider TEXT NOT NULL DEFAULT 'lar-iqa' CHECK (rendered_quality_provider IN ('off', 'lar-iqa'))",
        "ALTER TABLE workspace_config ADD COLUMN raw_quality_provider TEXT NOT NULL DEFAULT 'off' CHECK (raw_quality_provider IN ('off', 'lar-iqa'))",
        "ALTER TABLE workspace_config ADD COLUMN video_quality_enabled INTEGER NOT NULL DEFAULT 1 CHECK (video_quality_enabled IN (0, 1))",
        "UPDATE workspace_config SET include_rendered_images = include_images, include_raw = include_images, rendered_quality_provider = quality_provider, raw_quality_provider = 'off', video_quality_enabled = CASE WHEN quality_provider = 'lar-iqa' THEN 1 ELSE 0 END, configuration_version = 2 WHERE id = 1",
    ),
    16: (
        "ALTER TABLE workspace_config ADD COLUMN semantic_search_enabled INTEGER NOT NULL DEFAULT 0 CHECK (semantic_search_enabled IN (0, 1))",
        "ALTER TABLE workspace_config ADD COLUMN embedding_provider TEXT NOT NULL DEFAULT 'openclip-b16-datacomp-xl'",
        "UPDATE workspace_config SET semantic_search_enabled = 0, embedding_provider = 'openclip-b16-datacomp-xl', configuration_version = 3 WHERE id = 1",
        """
        CREATE TABLE embedding_run (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            model_id TEXT NOT NULL,
            model_version TEXT NOT NULL,
            embedding_dimension INTEGER NOT NULL,
            settings_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS embedding_run_provider_idx ON embedding_run(provider, model_version, status, created_at)",
        """
        CREATE TABLE logical_asset_embedding (
            run_id TEXT NOT NULL REFERENCES embedding_run(id) ON DELETE CASCADE,
            logical_asset_id TEXT NOT NULL REFERENCES logical_asset(id) ON DELETE CASCADE,
            source_physical_file_id TEXT NOT NULL REFERENCES physical_file(id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL,
            input_fingerprint TEXT NOT NULL,
            embedding BLOB NOT NULL,
            embedding_dimension INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, logical_asset_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS logical_asset_embedding_asset_idx ON logical_asset_embedding(logical_asset_id, run_id)",
        """
        CREATE TABLE video_frame_embedding (
            run_id TEXT NOT NULL REFERENCES embedding_run(id) ON DELETE CASCADE,
            logical_asset_id TEXT NOT NULL REFERENCES logical_asset(id) ON DELETE CASCADE,
            physical_file_id TEXT NOT NULL REFERENCES physical_file(id) ON DELETE CASCADE,
            sample_run_id TEXT NOT NULL REFERENCES video_sample_run(id) ON DELETE CASCADE,
            sample_index INTEGER NOT NULL,
            timestamp_seconds REAL NOT NULL,
            input_fingerprint TEXT NOT NULL,
            embedding BLOB NOT NULL,
            embedding_dimension INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (run_id, sample_run_id, sample_index)
        )
        """,
        "CREATE INDEX IF NOT EXISTS video_frame_embedding_asset_idx ON video_frame_embedding(logical_asset_id, run_id)",
        """
        CREATE TABLE workspace_embedding (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active_provider TEXT,
            active_run_id TEXT REFERENCES embedding_run(id),
            updated_at TEXT NOT NULL
        )
        """,
        "INSERT INTO workspace_embedding(id, active_provider, active_run_id, updated_at) VALUES (1, NULL, NULL, datetime('now'))",
    ),
    17: (
        "ALTER TABLE workspace_config ADD COLUMN recommendation_threshold REAL NOT NULL DEFAULT 0 CHECK(recommendation_threshold BETWEEN 0 AND 1)",
        "UPDATE workspace_config SET recommendation_threshold = 0.70 WHERE id = 1",
    ),
    18: ("ALTER TABLE physical_file ADD COLUMN file_created_time TEXT",),
    19: (
        "ALTER TABLE workspace_config ADD COLUMN include_videos_in_semantic_search INTEGER NOT NULL DEFAULT 1 CHECK (include_videos_in_semantic_search IN (0, 1))",
    ),
    20: (
        "ALTER TABLE workspace_config ADD COLUMN quality_enabled INTEGER NOT NULL DEFAULT 1 CHECK (quality_enabled IN (0, 1))",
        "ALTER TABLE workspace_config ADD COLUMN video_processing_enabled INTEGER NOT NULL DEFAULT 1 CHECK (video_processing_enabled IN (0, 1))",
        "UPDATE workspace_config SET quality_enabled = CASE WHEN rendered_quality_provider = 'lar-iqa' OR raw_quality_provider = 'lar-iqa' OR video_quality_enabled = 1 OR quality_provider = 'lar-iqa' THEN 1 ELSE 0 END, video_processing_enabled = CASE WHEN video_quality_enabled = 1 OR include_videos_in_semantic_search = 1 THEN 1 ELSE 0 END, embedding_provider = 'openclip-b16-datacomp-xl' WHERE id = 1",
    ),
    21: (
        "ALTER TABLE job ADD COLUMN timing_json TEXT",
    ),
    22: (
        """
        CREATE TABLE IF NOT EXISTS browser_revision (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            generation INTEGER NOT NULL DEFAULT 0
        )
        """,
        "INSERT OR IGNORE INTO browser_revision(id, generation) VALUES (1, 0)",
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_logical_insert AFTER INSERT ON logical_asset
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_logical_update AFTER UPDATE ON logical_asset
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_logical_delete AFTER DELETE ON logical_asset
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_physical_insert AFTER INSERT ON physical_file
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_physical_update AFTER UPDATE ON physical_file
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_physical_delete AFTER DELETE ON physical_file
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_component_insert AFTER INSERT ON component_state
        WHEN NEW.component IN ('metadata', 'thumbnail', 'quality', 'raw_quality', 'video_quality')
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_component_update AFTER UPDATE ON component_state
        WHEN NEW.component IN ('metadata', 'thumbnail', 'quality', 'raw_quality', 'video_quality')
          OR OLD.component IN ('metadata', 'thumbnail', 'quality', 'raw_quality', 'video_quality')
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_component_delete AFTER DELETE ON component_state
        WHEN OLD.component IN ('metadata', 'thumbnail', 'quality', 'raw_quality', 'video_quality')
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_config_update AFTER UPDATE ON workspace_config
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_folder_insert AFTER INSERT ON folder_scope_rule
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_folder_update AFTER UPDATE ON folder_scope_rule
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_folder_delete AFTER DELETE ON folder_scope_rule
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_group_member_insert AFTER INSERT ON strict_group_member
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_group_member_update AFTER UPDATE ON strict_group_member
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_group_member_delete AFTER DELETE ON strict_group_member
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_grouping_update AFTER UPDATE ON workspace_grouping
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_recommendation_insert AFTER INSERT ON asset_recommendation
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_recommendation_update AFTER UPDATE ON asset_recommendation
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_recommendation_delete AFTER DELETE ON asset_recommendation
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_workspace_recommendation_update AFTER UPDATE ON workspace_recommendation
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS browser_revision_embedding_workspace_update AFTER UPDATE ON workspace_embedding
        BEGIN UPDATE browser_revision SET generation = generation + 1 WHERE id = 1; END
        """,
    ),
    23: (
        """
        CREATE TABLE IF NOT EXISTS semantic_projection_run (
            id TEXT PRIMARY KEY,
            source_embedding_run_id TEXT NOT NULL REFERENCES embedding_run(id) ON DELETE CASCADE,
            algorithm TEXT NOT NULL,
            version TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            asset_count INTEGER NOT NULL,
            asset_set_fingerprint TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            error_message TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS semantic_projection_run_source_idx ON semantic_projection_run(source_embedding_run_id, status, created_at)",
        """
        CREATE TABLE IF NOT EXISTS semantic_projection_point (
            run_id TEXT NOT NULL REFERENCES semantic_projection_run(id) ON DELETE CASCADE,
            logical_asset_id TEXT NOT NULL REFERENCES logical_asset(id) ON DELETE CASCADE,
            x REAL NOT NULL,
            y REAL NOT NULL,
            PRIMARY KEY (run_id, logical_asset_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS semantic_projection_point_asset_idx ON semantic_projection_point(logical_asset_id, run_id)",
        """
        CREATE TABLE IF NOT EXISTS workspace_semantic_projection (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            active_run_id TEXT REFERENCES semantic_projection_run(id),
            updated_at TEXT NOT NULL
        )
        """,
        "INSERT OR IGNORE INTO workspace_semantic_projection(id, active_run_id, updated_at) VALUES (1, NULL, datetime('now'))",
        """
        CREATE TRIGGER IF NOT EXISTS semantic_projection_embedding_activation AFTER UPDATE OF active_run_id ON workspace_embedding
        WHEN NEW.active_run_id IS NOT OLD.active_run_id
        BEGIN UPDATE workspace_semantic_projection SET active_run_id = NULL, updated_at = datetime('now') WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS semantic_projection_embedding_state_change AFTER INSERT ON component_state
        WHEN NEW.component LIKE 'embedding:%'
        BEGIN UPDATE workspace_semantic_projection SET active_run_id = NULL, updated_at = datetime('now') WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS semantic_projection_embedding_state_update AFTER UPDATE ON component_state
        WHEN NEW.component LIKE 'embedding:%' OR OLD.component LIKE 'embedding:%'
        BEGIN UPDATE workspace_semantic_projection SET active_run_id = NULL, updated_at = datetime('now') WHERE id = 1; END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS semantic_projection_embedding_state_delete AFTER DELETE ON component_state
        WHEN OLD.component LIKE 'embedding:%'
        BEGIN UPDATE workspace_semantic_projection SET active_run_id = NULL, updated_at = datetime('now') WHERE id = 1; END
        """,
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
                if (
                    version == 17
                    and statement.startswith("ALTER TABLE workspace_config ADD COLUMN")
                    and _has_column(connection, "workspace_config", "recommendation_threshold")
                ):
                    continue
                if version == 18 and _has_column(connection, "physical_file", "file_created_time"):
                    continue
                if version == 19 and _has_column(connection, "workspace_config", "include_videos_in_semantic_search"):
                    continue
                if version == 20 and statement.startswith("ALTER TABLE workspace_config ADD COLUMN") and _has_column(connection, "workspace_config", statement.split()[5]):
                    continue
                if version == 21 and _has_column(connection, "job", "timing_json"):
                    continue
                if (
                    version == 8
                    and statement.startswith("ALTER TABLE workspace_info ADD COLUMN quality_provider")
                    and _has_column(connection, "workspace_info", "quality_provider")
                ):
                    continue
                if (
                    version == 10
                    and statement.startswith("ALTER TABLE physical_file ADD COLUMN in_scope")
                    and _has_column(connection, "physical_file", "in_scope")
                ):
                    continue
                if (
                    version == 10
                    and statement.lstrip().startswith("CREATE TABLE workspace_config")
                    and _has_table(connection, "workspace_config")
                ):
                    continue
                if (
                    version == 10
                    and statement.lstrip().startswith("CREATE TABLE folder_scope_rule")
                    and _has_table(connection, "folder_scope_rule")
                ):
                    continue
                if (
                    version == 10
                    and statement.lstrip().startswith("INSERT INTO workspace_config")
                    and connection.execute("SELECT 1 FROM workspace_config WHERE id = 1").fetchone() is not None
                ):
                    continue
                if (
                    version == 11
                    and statement.startswith("ALTER TABLE workspace_config ADD COLUMN configuration_version")
                    and _has_column(connection, "workspace_config", "configuration_version")
                ):
                    continue
                if (
                    version == 13
                    and statement.startswith("ALTER TABLE workspace_config ADD COLUMN")
                    and _has_column(connection, "workspace_config", statement.split()[5])
                ):
                    continue
                if (
                    version == 14
                    and statement.startswith("ALTER TABLE video_sample ADD COLUMN")
                    and _has_column(connection, "video_sample", statement.split()[5])
                ):
                    continue
                if (
                    version == 15
                    and statement.startswith("ALTER TABLE workspace_config ADD COLUMN")
                    and _has_column(connection, "workspace_config", statement.split()[5])
                ):
                    continue
                if (
                    version == 16
                    and statement.startswith("ALTER TABLE workspace_config ADD COLUMN")
                    and _has_column(connection, "workspace_config", statement.split()[5])
                ):
                    continue
                if (
                    version == 16
                    and statement.lstrip().startswith("CREATE TABLE embedding_run")
                    and _has_table(connection, "embedding_run")
                ):
                    continue
                if (
                    version == 16
                    and statement.lstrip().startswith("CREATE TABLE logical_asset_embedding")
                    and _has_table(connection, "logical_asset_embedding")
                ):
                    continue
                if (
                    version == 16
                    and statement.lstrip().startswith("CREATE TABLE video_frame_embedding")
                    and _has_table(connection, "video_frame_embedding")
                ):
                    continue
                if (
                    version == 16
                    and statement.lstrip().startswith("CREATE TABLE workspace_embedding")
                    and _has_table(connection, "workspace_embedding")
                ):
                    continue
                if (
                    version == 16
                    and statement.lstrip().startswith("INSERT INTO workspace_embedding")
                    and _has_table(connection, "workspace_embedding")
                    and connection.execute("SELECT 1 FROM workspace_embedding WHERE id = 1").fetchone() is not None
                ):
                    continue
                try:
                    connection.execute(statement)
                except sqlite3.Error as error:
                    compact_statement = " ".join(statement.split())
                    raise type(error)(
                        f"schema migration {version} failed for {compact_statement!r}: {error}"
                    ) from error
            connection.execute(
                "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
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


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row[1] == column
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    )


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None
