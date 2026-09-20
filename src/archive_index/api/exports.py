"""Source-safe archive exports."""

from __future__ import annotations

import tempfile
import zipfile
import os
from pathlib import Path

from ..indexing.representations import preferred_physical
from ..workspace import WorkspaceError


def selected_zip(workspace) -> tuple[Path, int, int]:
    connection = workspace.connect()
    try:
        asset_rows = connection.execute(
            "SELECT id FROM logical_asset WHERE selection_state = 'selected' ORDER BY id"
        ).fetchall()
        physical_rows = connection.execute(
            """
            SELECT *
            FROM physical_file
            WHERE logical_asset_id IN (
                SELECT id FROM logical_asset WHERE selection_state = 'selected'
            )
            ORDER BY logical_asset_id, relative_path
            """
        ).fetchall()
    finally:
        connection.close()
    by_asset: dict[str, list] = {}
    for row in physical_rows:
        by_asset.setdefault(row["logical_asset_id"], []).append(row)
    selected = []
    for asset in asset_rows:
        rows = [row for row in by_asset.get(asset["id"], []) if row["in_scope"] and row["is_online"]]
        source = preferred_physical(rows)
        if source is None:
            raise WorkspaceError("a selected asset has no online source representation")
        try:
            path = workspace.absolute_path(source["relative_path"])
        except WorkspaceError as error:
            raise WorkspaceError("a selected asset has an invalid source path") from error
        if not path.is_file():
            raise WorkspaceError("a selected asset source is unavailable")
        selected.append((asset["id"], path, source["relative_path"]))

    output_directory = workspace.index_path("tmp")
    output_directory.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(prefix="selected-", suffix=".zip", dir=output_directory)
    os.close(handle)
    Path(name).unlink(missing_ok=True)
    output = Path(name)
    used_names: set[str] = set()
    total_bytes = 0
    try:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for asset_id, source, relative_path in selected:
                arcname = Path(relative_path).as_posix()
                if arcname.casefold() in used_names:
                    path = Path(arcname)
                    arcname = path.with_name(path.stem + "__" + asset_id[:12] + path.suffix).as_posix()
                used_names.add(arcname.casefold())
                archive.write(source, arcname)
                total_bytes += source.stat().st_size
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output, len(selected), total_bytes
