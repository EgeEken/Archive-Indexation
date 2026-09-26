"""Persistent provenance for application-created File Management derivatives."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from .workspace import Workspace


def record_managed_derivative(
    workspace: Workspace,
    operation,
    validation: dict[str, object],
    metadata_contract: dict[str, object],
    encoder_version: str | None,
) -> None:
    profile = operation.get("profile_snapshot") or _json(operation.get("profile_snapshot_json")) or {}
    now = _timestamp()
    with workspace.transaction() as connection:
        connection.execute(
            """
            INSERT INTO managed_derivative(
                id, execution_operation_id, source_physical_file_id, source_logical_asset_id,
                source_relative_path, source_sha256, output_relative_path, output_sha256,
                codec, container, profile_id, profile_name, settings_json, algorithm,
                algorithm_version, encoder_version, source_disposition, metadata_contract_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_operation_id) DO UPDATE SET
                output_relative_path = excluded.output_relative_path,
                output_sha256 = excluded.output_sha256,
                physical_file_id = NULL,
                metadata_contract_json = excluded.metadata_contract_json,
                updated_at = excluded.updated_at
            """,
            (
                str(uuid.uuid4()), operation["id"], operation["physical_file_id"], operation["logical_asset_id"],
                operation["source_relative_path"], operation["source_sha256"], operation["target_relative_path"],
                validation["sha256"], profile.get("codec", "jpeg-xl"), profile.get("container", "jxl"),
                operation["profile_id"], profile.get("name"), json.dumps(profile.get("settings") or {}, sort_keys=True),
                profile.get("settings", {}).get("algorithm", "imagecodecs-jpegxl-archival"),
                profile.get("settings", {}).get("algorithm_version", "1"), encoder_version,
                operation.get("source_disposition", "keep"), json.dumps(metadata_contract, sort_keys=True),
                now, now,
            ),
        )


def link_managed_derivatives(workspace: Workspace) -> int:
    linked = 0
    with workspace.transaction() as connection:
        rows = connection.execute(
            """
            SELECT md.id, md.source_logical_asset_id, pf.id AS output_physical_file_id,
                   pf.logical_asset_id AS output_logical_asset_id
            FROM managed_derivative AS md
            JOIN physical_file AS pf ON pf.relative_path = md.output_relative_path
            WHERE pf.is_online = 1 AND pf.in_scope = 1
            """
        ).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE managed_derivative SET physical_file_id = ?, updated_at = ? WHERE id = ?",
                (row["output_physical_file_id"], _timestamp(), row["id"]),
            )
            if row["source_logical_asset_id"] and row["output_logical_asset_id"] != row["source_logical_asset_id"]:
                connection.execute(
                    "UPDATE physical_file SET logical_asset_id = ?, role = 'managed_jxl' WHERE id = ?",
                    (row["source_logical_asset_id"], row["output_physical_file_id"]),
                )
            else:
                connection.execute(
                    "UPDATE physical_file SET role = 'managed_jxl' WHERE id = ?",
                    (row["output_physical_file_id"],),
                )
            linked += 1
    return linked


def managed_derivative_for_physical(workspace: Workspace, physical_file_id: str):
    connection = workspace.connect()
    try:
        return connection.execute(
            "SELECT * FROM managed_derivative WHERE physical_file_id = ? ORDER BY created_at DESC LIMIT 1",
            (physical_file_id,),
        ).fetchone()
    finally:
        connection.close()


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
