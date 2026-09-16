"""Conservative logical-asset reconciliation for exact copies and RAW/JPEG pairs."""

from __future__ import annotations

import hashlib
import itertools
import json
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from dataclasses import dataclass
from threading import Event

from ..jobs.engine import JobProgress, JobRunResult, JobStore
from ..media_types import is_raw_extension, is_rendered_image_extension
from ..workspace import Workspace

RECONCILIATION_ALGORITHM = "exact-sha-and-conservative-raw-jpeg"
RECONCILIATION_VERSION = "2"
RAW_JPEG_ALGORITHM = "same-stem-with-corroboration"
RAW_JPEG_VERSION = "1"
EXACT_DUPLICATE_ALGORITHM = "sha256-bytes"
EXACT_DUPLICATE_VERSION = "1"
RAW_JPEG_TIME_TOLERANCE_SECONDS = 5.0
RECONCILIATION_SETTINGS = {
    "exact_duplicate": "sha256_bytes",
    "raw_jpeg": {
        "stem": "casefolded_filename_stem",
        "capture_time_tolerance_seconds": RAW_JPEG_TIME_TOLERANCE_SECONDS,
        "unique_fallback": True,
        "preview_hash": False,
    },
    "survivor": "explicit_decision_then_created_at_then_id",
}


@dataclass(frozen=True)
class ReconciliationResult:
    job_id: str
    run_id: str | None
    physical_files: int
    logical_assets_before: int
    logical_assets_after: int
    exact_duplicate_families: int
    exact_duplicate_files: int
    raw_jpeg_pairs: int
    conflicts: int
    merged_assets: int
    cancelled: bool
    elapsed_seconds: float


def reconciliation_provenance() -> tuple[str, str, dict[str, object]]:
    return RECONCILIATION_ALGORITHM, RECONCILIATION_VERSION, RECONCILIATION_SETTINGS


def reconcile_workspace(
    workspace: Workspace,
    *,
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: Callable[[JobProgress], None] | None = None,
) -> ReconciliationResult:
    started = time.perf_counter()
    assets, physical = _snapshot(workspace)
    store = JobStore(workspace)
    identifier = job_id or store.create("reconciliation", len(physical))
    store.set_total(identifier, len(physical))
    store.set_stage(identifier, "reconciliation")
    store.start(identifier)
    input_fingerprint = _snapshot_fingerprint(assets, physical)
    active_run = _active_run(workspace)
    if active_run is not None and (
        active_run["algorithm"] == RECONCILIATION_ALGORITHM
        and active_run["version"] == RECONCILIATION_VERSION
        and active_run["input_fingerprint"] == input_fingerprint
    ):
        store.complete(identifier, 0, 0, len(physical))
        return ReconciliationResult(
            identifier, active_run["id"], len(physical), len(assets), len(assets),
            0, 0, 0, 0, 0, False, time.perf_counter() - started,
        )

    try:
        exact_families, blocked_assets, union_find, conflicts = _exact_plan(assets, physical)
        relationships = _exact_relationships(physical)
        processed = 0
        for family in exact_families:
            processed += len(family)
            if _cancelled(cancel_event, store, identifier, processed, len(physical)):
                return _cancelled_result(identifier, len(physical), len(assets), started)
            _report(progress, identifier, processed, len(physical), family[0], len(conflicts))

        canonical = _canonical_map(union_find, assets, blocked_assets)
        pair_families, ambiguities = _raw_jpeg_candidates(assets, physical, canonical)
        conflicts.extend(ambiguities)
        paired_assets: list[tuple[str, str, dict[str, object], list[str], list[str]]] = []
        for raw_id, rendered_id, evidence, raw_files, rendered_files in pair_families:
            processed += 1
            if _cancelled(cancel_event, store, identifier, min(processed, len(physical)), len(physical)):
                return _cancelled_result(identifier, len(physical), len(assets), started)
            left = canonical.get(raw_id, raw_id)
            right = canonical.get(rendered_id, rendered_id)
            if left == right:
                paired_assets.append((left, right, evidence, raw_files, rendered_files))
                continue
            if left in blocked_assets or right in blocked_assets:
                continue
            if _decision_conflict((left, right), assets):
                conflicts.append((left, right, "manual_decision_conflict", evidence))
                continue
            union_find.union(left, right)
            paired_assets.append((left, right, evidence, raw_files, rendered_files))

        canonical = _canonical_map(union_find, assets, blocked_assets)
        for left, right, evidence, raw_files, rendered_files in paired_assets:
            if canonical.get(left, left) != canonical.get(right, right):
                continue
            for raw_file, rendered_file in itertools.product(raw_files, rendered_files):
                relationships.append((raw_file, rendered_file, "raw_jpeg", RAW_JPEG_ALGORITHM, RAW_JPEG_VERSION, evidence))

        merges = {asset_id: survivor for asset_id, survivor in canonical.items() if asset_id != survivor}
        roles = _roles(physical, relationships)
        post_assets = [dict(row) for row in assets if canonical.get(row["id"], row["id"]) == row["id"]]
        post_physical = []
        for row in physical:
            value = dict(row)
            value["logical_asset_id"] = canonical.get(row["logical_asset_id"], row["logical_asset_id"])
            value["role"] = roles.get(row["id"], row["role"])
            post_physical.append(value)
        output_fingerprint = _snapshot_fingerprint(post_assets, post_physical)
        run_id = str(uuid.uuid4())
        now = _timestamp()
        with workspace.transaction() as connection:
            if merges:
                for asset_id, survivor in merges.items():
                    source = next(row for row in assets if row["id"] == asset_id)
                    target = next(row for row in assets if row["id"] == survivor)
                    if not target["capture_time"] and source["capture_time"]:
                        connection.execute(
                            "UPDATE logical_asset SET capture_time = ?, capture_time_kind = ?, updated_at = ? WHERE id = ?",
                            (source["capture_time"], source["capture_time_kind"], now, survivor),
                        )
                for asset_id, survivor in merges.items():
                    connection.execute(
                        "UPDATE physical_file SET logical_asset_id = ?, role = ? WHERE logical_asset_id = ?",
                        (survivor, "source_original", asset_id),
                    )
                for file_id, role in roles.items():
                    connection.execute("UPDATE physical_file SET role = ? WHERE id = ?", (role, file_id))
                for asset_id in merges:
                    connection.execute("DELETE FROM logical_asset WHERE id = ?", (asset_id,))
                connection.execute(
                    "UPDATE workspace_grouping SET active_run_id = NULL, updated_at = ? WHERE id = 1",
                    (now,),
                )
                connection.execute(
                    "UPDATE workspace_recommendation SET active_run_id = NULL, updated_at = ? WHERE id = 1",
                    (now,),
                )
            connection.execute(
                "INSERT INTO reconciliation_run(id, algorithm, version, settings_json, input_fingerprint, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, RECONCILIATION_ALGORITHM, RECONCILIATION_VERSION,
                 json.dumps(RECONCILIATION_SETTINGS, sort_keys=True), output_fingerprint, now, now),
            )
            for source_id, target_id, relationship_type, algorithm, version, evidence in relationships:
                connection.execute(
                    "INSERT INTO physical_relationship(run_id, source_physical_file_id, target_physical_file_id, relationship_type, algorithm, version, evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, source_id, target_id, relationship_type, algorithm, version, json.dumps(evidence, sort_keys=True)),
                )
            for left, right, conflict_type, evidence in conflicts:
                connection.execute(
                    "INSERT INTO reconciliation_conflict(run_id, left_logical_asset_id, right_logical_asset_id, conflict_type, message, evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (run_id, left, right, conflict_type, "Multiple RAW/JPEG candidates; automatic reconciliation was skipped." if conflict_type == "ambiguous_raw_jpeg" else "Manual decisions conflict; automatic reconciliation was skipped.", json.dumps(evidence, sort_keys=True), now),
                )
            connection.execute(
                "INSERT INTO workspace_reconciliation(id, active_run_id, updated_at) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET active_run_id = excluded.active_run_id, updated_at = excluded.updated_at",
                (run_id, now),
            )
            connection.execute("DELETE FROM reconciliation_run WHERE id <> ?", (run_id,))
        store.complete(identifier, len(physical), len(conflicts), 0)
        raw_jpeg_relation_count = sum(
            relationship[2] == "raw_jpeg" for relationship in relationships
        )
        return ReconciliationResult(
            identifier, run_id, len(physical), len(assets), len(post_assets),
            len(exact_families), _duplicate_file_count(exact_families, physical),
            raw_jpeg_relation_count, len(conflicts), len(merges), False,
            time.perf_counter() - started,
        )
    except Exception:
        store.fail(identifier)
        raise


def _snapshot(workspace: Workspace):
    connection = workspace.connect()
    try:
        assets = connection.execute(
            "SELECT id, media_type, capture_time, capture_time_kind, selection_state, created_at FROM logical_asset ORDER BY id"
        ).fetchall()
        physical = connection.execute("SELECT * FROM physical_file ORDER BY id").fetchall()
    finally:
        connection.close()
    return assets, physical


def _active_run(workspace: Workspace):
    connection = workspace.connect()
    try:
        return connection.execute(
            "SELECT rr.* FROM workspace_reconciliation AS wr JOIN reconciliation_run AS rr ON rr.id = wr.active_run_id WHERE wr.id = 1"
        ).fetchone()
    finally:
        connection.close()


def _exact_plan(assets, physical):
    by_hash: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in physical:
        if row["sha256"]:
            by_hash[(row["media_type"], row["sha256"])].add(row["logical_asset_id"])
    families = [sorted(ids) for ids in by_hash.values() if len(ids) > 1]
    families.sort(key=lambda ids: tuple(ids))
    decisions = {row["id"]: row["selection_state"] for row in assets}
    blocked: set[str] = set()
    conflicts: list[tuple[str, str, str, dict[str, object]]] = []
    union_find = _UnionFind(decisions)
    for family in families:
        if _decision_conflict(family, decisions):
            blocked.update(family)
            for left, right in itertools.combinations(family, 2):
                if {decisions[left], decisions[right]} == {"selected", "rejected"}:
                    conflicts.append((left, right, "exact_duplicate_decision_conflict", {"kind": "exact_sha"}))
            continue
        first = family[0]
        for asset_id in family[1:]:
            union_find.union(first, asset_id)
    return families, blocked, union_find, conflicts


def _raw_jpeg_candidates(assets, physical, canonical):
    asset_map = {row["id"]: row for row in assets}
    groups: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"raw": set(), "rendered": set()})
    files_by_asset: dict[str, list] = defaultdict(list)
    for row in physical:
        if row["media_type"] != "image":
            continue
        asset_id = canonical.get(row["logical_asset_id"], row["logical_asset_id"])
        files_by_asset[asset_id].append(row)
        stem = row["filename"].rsplit(".", 1)[0].casefold()
        if is_raw_extension(row["extension"]):
            groups[stem]["raw"].add(asset_id)
        elif is_rendered_image_extension(row["extension"]):
            groups[stem]["rendered"].add(asset_id)
    candidates = []
    ambiguities = []
    for stem, family in sorted(groups.items()):
        raw_ids = sorted(family["raw"])
        rendered_ids = sorted(family["rendered"])
        if len(raw_ids) == len(rendered_ids) == 1 and raw_ids[0] == rendered_ids[0]:
            members = files_by_asset[raw_ids[0]]
            candidates.append((raw_ids[0], rendered_ids[0], {"rule": "already_reconciled"},
                               [row["id"] for row in members if is_raw_extension(row["extension"])],
                               [row["id"] for row in members if is_rendered_image_extension(row["extension"])]))
            continue
        if len(raw_ids) != 1 or len(rendered_ids) != 1:
            if raw_ids and rendered_ids:
                ambiguities.append(
                    (
                        raw_ids[0] if len(raw_ids) == 1 else None,
                        rendered_ids[0] if len(rendered_ids) == 1 else None,
                        "ambiguous_raw_jpeg",
                        {
                            "stem": stem,
                            "raw_asset_ids": raw_ids,
                            "rendered_asset_ids": rendered_ids,
                        },
                    )
                )
            continue
        raw_id, rendered_id = raw_ids[0], rendered_ids[0]
        evidence = _pair_evidence(asset_map[raw_id], asset_map[rendered_id], files_by_asset)
        if evidence is not None:
            candidates.append((raw_id, rendered_id, evidence, [row["id"] for row in files_by_asset[raw_id] if is_raw_extension(row["extension"])], [row["id"] for row in files_by_asset[rendered_id] if is_rendered_image_extension(row["extension"])]))
    return candidates, ambiguities


def _pair_evidence(raw_asset, rendered_asset, files_by_asset):
    raw_time = _capture_value(raw_asset["capture_time"], raw_asset["capture_time_kind"])
    rendered_time = _capture_value(rendered_asset["capture_time"], rendered_asset["capture_time_kind"])
    if raw_time is not None and rendered_time is not None:
        if raw_time[1] != rendered_time[1] or abs(raw_time[0] - rendered_time[0]) > RAW_JPEG_TIME_TOLERANCE_SECONDS:
            return None
        reason = "same_stem+capture_time"
    else:
        reason = "unique_same_stem_no_conflict"
    raw_camera = _camera_identity(files_by_asset[raw_asset["id"]])
    rendered_camera = _camera_identity(files_by_asset[rendered_asset["id"]])
    if raw_camera and rendered_camera and raw_camera != rendered_camera:
        return None
    if raw_time is not None and rendered_time is not None and raw_camera and rendered_camera:
        reason = "same_stem+capture_time+camera"
    elif raw_camera and rendered_camera:
        reason = "same_stem+camera"
    return {"rule": reason}


def _capture_value(value, kind):
    if not value or not kind:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if kind in {"exif_offset", "aware", "container_metadata"}:
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).timestamp(), "absolute"
    if kind == "exif_local_unknown" and parsed.tzinfo is None:
        wall_clock = (
            parsed.toordinal() * 86400
            + parsed.hour * 3600
            + parsed.minute * 60
            + parsed.second
            + parsed.microsecond / 1_000_000
        )
        return wall_clock, "local"
    return None


def _camera_identity(rows):
    values = set()
    for row in rows:
        if not row["metadata_json"]:
            continue
        try:
            exif = json.loads(row["metadata_json"]).get("exif", {})
        except (TypeError, ValueError, AttributeError):
            continue
        make = str(exif.get("Make", "")).strip().casefold()
        model = str(exif.get("Model", "")).strip().casefold()
        if make or model:
            values.add((make, model))
    return next(iter(values)) if len(values) == 1 else None


def _decision_conflict(asset_ids, assets_or_decisions):
    if isinstance(assets_or_decisions, dict):
        decisions = assets_or_decisions
    else:
        decisions = {row["id"]: row["selection_state"] for row in assets_or_decisions}
    values = {decisions[asset_id] for asset_id in asset_ids if decisions.get(asset_id) in {"selected", "rejected"}}
    return len(values) > 1


def _canonical_map(union_find, assets, blocked):
    groups: dict[str, list[str]] = defaultdict(list)
    for row in assets:
        if row["id"] not in blocked:
            groups[union_find.find(row["id"])].append(row["id"])
    result = {asset_id: asset_id for row in assets for asset_id in [row["id"]]}
    asset_map = {row["id"]: row for row in assets}
    for members in groups.values():
        survivor = sorted(
            members,
            key=lambda asset_id: (
                asset_map[asset_id]["selection_state"] == "undecided",
                asset_map[asset_id]["created_at"],
                asset_id,
            ),
        )[0]
        for asset_id in members:
            result[asset_id] = survivor
    return result


def _roles(physical, relationships):
    roles = {}
    paired_raw = {source for source, target, relationship_type, *_ in relationships if relationship_type == "raw_jpeg"}
    paired_rendered = {target for source, target, relationship_type, *_ in relationships if relationship_type == "raw_jpeg"}
    for row in physical:
        if is_raw_extension(row["extension"]):
            roles[row["id"]] = "camera_raw"
        elif row["id"] in paired_rendered:
            roles[row["id"]] = "camera_jpeg"
        elif row["id"] in paired_raw:
            roles[row["id"]] = "camera_raw"
    return roles


def _exact_relationships(physical):
    rows_by_hash: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in physical:
        if row["sha256"]:
            rows_by_hash[(row["media_type"], row["sha256"])].append(row["id"])
    relationships = []
    for files in rows_by_hash.values():
        if len(files) < 2:
            continue
        evidence = {"kind": "sha256", "file_count": len(files)}
        for source_id, target_id in itertools.combinations(files, 2):
            relationships.append(
                (
                    source_id,
                    target_id,
                    "exact_duplicate",
                    EXACT_DUPLICATE_ALGORITHM,
                    EXACT_DUPLICATE_VERSION,
                    evidence,
                )
            )
    return relationships


def _duplicate_file_count(families, physical):
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in physical:
        if row["sha256"]:
            counts[(row["media_type"], row["sha256"])] += 1
    return sum(count - 1 for count in counts.values() if count > 1)


def _snapshot_fingerprint(assets, physical):
    values = {
        "assets": [
            [row["id"], row["media_type"], row["capture_time"], row["capture_time_kind"], row["selection_state"], row["created_at"]]
            for row in assets
        ],
        "physical": [
            [row["id"], row["logical_asset_id"], row["relative_path"], row["extension"], row["media_type"], row["role"], row["size_bytes"], row["mtime_ns"], row["sha256"], row["is_online"], row["in_scope"], row["metadata_json"]]
            for row in physical
        ],
    }
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _cancelled(cancel_event, store, job_id, processed, total):
    if cancel_event is None or not cancel_event.is_set():
        return False
    store.checkpoint(job_id, processed, 0, 0)
    store.cancel(job_id, processed, 0, 0)
    return True


def _cancelled_result(identifier, physical_count, asset_count, started):
    return ReconciliationResult(
        identifier, None, physical_count, asset_count, asset_count,
        0, 0, 0, 0, 0, True, time.perf_counter() - started,
    )


def _report(progress, job_id, processed, total, item, failed):
    if progress is not None:
        progress(JobProgress(job_id, min(processed, total), total, item, "reconciliation", failed, 0))


class _UnionFind:
    def __init__(self, values):
        self.parent = {value: value for value in values}

    def find(self, value):
        parent = self.parent.setdefault(value, value)
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left, right):
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
