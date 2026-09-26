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
                source_physical_file_id = excluded.source_physical_file_id,
                source_logical_asset_id = excluded.source_logical_asset_id,
                source_relative_path = excluded.source_relative_path,
                source_sha256 = excluded.source_sha256,
                output_relative_path = excluded.output_relative_path,
                output_sha256 = excluded.output_sha256,
                codec = excluded.codec,
                container = excluded.container,
                profile_id = excluded.profile_id,
                profile_name = excluded.profile_name,
                settings_json = excluded.settings_json,
                algorithm = excluded.algorithm,
                algorithm_version = excluded.algorithm_version,
                encoder_version = excluded.encoder_version,
                source_disposition = excluded.source_disposition,
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
        physical = connection.execute(
            "SELECT id, relative_path, sha256, logical_asset_id FROM physical_file WHERE is_online = 1 AND in_scope = 1"
        ).fetchall()
        by_identity = {
            (row["relative_path"].casefold(), row["sha256"]): row
            for row in physical
            if row["sha256"]
        }
        derivatives = connection.execute(
            """
            SELECT md.*
            FROM managed_derivative AS md
            LEFT JOIN file_management_execution_operation AS op ON op.id = md.execution_operation_id
            LEFT JOIN file_management_execution AS execution ON execution.id = op.execution_id
            ORDER BY COALESCE(execution.created_at, md.created_at) DESC, md.created_at DESC, md.id DESC
            """
        ).fetchall()
        connection.execute(
            "UPDATE managed_derivative SET physical_file_id = NULL, updated_at = ? WHERE physical_file_id IS NOT NULL",
            (_timestamp(),),
        )
        winners = {}
        for row in derivatives:
            output = by_identity.get((row["output_relative_path"].casefold(), row["output_sha256"]))
            if output is not None and output["id"] not in winners:
                winners[output["id"]] = row
        for output_id, row in winners.items():
            output = next(item for item in physical if item["id"] == output_id)
            connection.execute(
                "UPDATE managed_derivative SET physical_file_id = ?, updated_at = ? WHERE id = ?",
                (output_id, _timestamp(), row["id"]),
            )
            old_asset_id = output["logical_asset_id"]
            if row["source_logical_asset_id"] and old_asset_id != row["source_logical_asset_id"]:
                connection.execute(
                    "UPDATE physical_file SET logical_asset_id = ?, role = 'managed_jxl' WHERE id = ?",
                    (row["source_logical_asset_id"], output_id),
                )
            else:
                connection.execute(
                    "UPDATE physical_file SET role = 'managed_jxl' WHERE id = ?",
                    (output_id,),
                )
            linked += 1
            if old_asset_id != row["source_logical_asset_id"]:
                orphan = connection.execute(
                    "SELECT selection_state FROM logical_asset WHERE id = ? AND NOT EXISTS (SELECT 1 FROM physical_file WHERE logical_asset_id = ?)",
                    (old_asset_id, old_asset_id),
                ).fetchone()
                if orphan is not None and orphan["selection_state"] == "undecided":
                    connection.execute("DELETE FROM logical_asset WHERE id = ?", (old_asset_id,))
        if winners:
            placeholders = ",".join("?" for _ in winners)
            connection.execute(
                f"UPDATE physical_file SET role = 'source_original' WHERE role = 'managed_jxl' AND id NOT IN ({placeholders})",
                tuple(winners),
            )
        else:
            connection.execute("UPDATE physical_file SET role = 'source_original' WHERE role = 'managed_jxl'")
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
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
