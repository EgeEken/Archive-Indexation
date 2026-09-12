"""Cached visual features and conservative near-identical image grouping."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from PIL import Image

from ..jobs.engine import JobProgress, JobRunResult, JobStore, run_batches
from ..media.metadata import UnsupportedDecoderError
from ..media.thumbnail import load_reduced_image
from ..workspace import Workspace
from ..timing import TimingRecorder, timed

FEATURE_COMPONENT = "group_feature"
FEATURE_ALGORITHM = "pillow-strict-group-features"
FEATURE_VERSION = "3"
GROUPING_ALGORITHM = "strict-temporal-complete-linkage"
GROUPING_VERSION = "4"
MAX_CAPTURE_SECONDS = 10.0
BURST_MAX_CAPTURE_SECONDS = 0.5
FEATURE_DECODE_SIZE = (160, 160)
FEATURE_BATCH_SIZE = 32
FEATURE_WORKERS = 8
LUMA_FEATURE_SIZE = (16, 16)
COLOR_FEATURE_SIZE = (32, 32)
MAX_DHASH_DISTANCE = 8
MAX_LUMA_RMSE = 0.16
MAX_COLOR_HIST_DISTANCE = 0.18
BURST_DHASH_VETO = 28
BURST_LUMA_VETO = 0.85
BURST_COLOR_HISTOGRAM_VETO = 0.60
BURST_VETO_SIGNAL_COUNT = 2
FEATURE_SETTINGS = {
    "decode_size": FEATURE_DECODE_SIZE,
    "luma_size": LUMA_FEATURE_SIZE,
    "color_size": COLOR_FEATURE_SIZE,
    "dhash": "9x8 grayscale adjacent-pixel hash",
    "luma": "16x16 grayscale normalized by mean and standard deviation",
    "color_histogram": "8 bins per RGB channel",
}
GROUPING_SETTINGS = {
    "max_capture_seconds": MAX_CAPTURE_SECONDS,
    "max_dhash_distance": MAX_DHASH_DISTANCE,
    "max_luma_rmse": MAX_LUMA_RMSE,
    "max_color_histogram_l1": MAX_COLOR_HIST_DISTANCE,
    "linkage": "all existing members must match",
    "burst_max_capture_seconds": BURST_MAX_CAPTURE_SECONDS,
    "burst_veto": {
        "dhash_distance": BURST_DHASH_VETO,
        "luma_rmse": BURST_LUMA_VETO,
        "color_histogram_l1": BURST_COLOR_HISTOGRAM_VETO,
        "required_signal_count": BURST_VETO_SIGNAL_COUNT,
    },
    "burst_linkage": "adjacent temporal chain with catastrophic-difference veto",
}


@dataclass(frozen=True)
class FeatureRecord:
    physical_file_id: str
    input_fingerprint: str
    dhash: str
    luma: tuple[float, ...]
    color_hist: tuple[float, ...]


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    capture_time: str | None
    capture_kind: str | None
    capture_value: float | None
    capture_semantics: str | None
    feature: FeatureRecord | None
    quality_score: float | None
    filename: str | None = None


@dataclass(frozen=True)
class GroupingResult:
    job_id: str
    eligible_images: int
    no_timestamp: int
    feature_failures: int
    total_groups: int
    multi_image_groups: int
    singleton_count: int
    group_sizes: tuple[int, ...]
    temporal_spans: tuple[float, ...]
    feature_seconds: float = 0.0
    grouping_seconds: float = 0.0
    cancelled: bool = False
    accepted_visual_pairs: int = 0
    rejected_temporal_candidates: int = 0
    tier_a_burst_chains: int = 0
    tier_a_pair_relations: int = 0
    tier_a_veto_rejections: int = 0
    tier_b_matches: int = 0


def feature_provenance() -> tuple[str, str, dict[str, object]]:
    return FEATURE_ALGORITHM, FEATURE_VERSION, FEATURE_SETTINGS


def extract_visual_features(
    workspace: Workspace,
    *,
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: callable | None = None,
    workers: int | None = None,
    timings: TimingRecorder | None = None,
) -> JobRunResult:
    connection = workspace.connect()
    try:
        rows = connection.execute(
            "SELECT * FROM physical_file WHERE is_online = 1 AND media_type = 'image' ORDER BY relative_path"
        ).fetchall()
    finally:
        connection.close()
    state_map = _prepare_feature_states(workspace, rows)
    worker_count = workers or FEATURE_WORKERS
    if worker_count < 1:
        raise ValueError("feature worker count must be positive")

    def feature_task(row):
        fingerprint = _input_fingerprint(row)
        decode_started = time.perf_counter()
        try:
            image = load_reduced_image(workspace.absolute_path(row["relative_path"]), FEATURE_DECODE_SIZE)
            decode_seconds = time.perf_counter() - decode_started
            calculation_started = time.perf_counter()
            try:
                feature = _feature_from_image(row["id"], fingerprint, image)
            finally:
                image.close()
            return row, fingerprint, feature, None, decode_seconds, time.perf_counter() - calculation_started
        except Exception as error:
            return row, fingerprint, None, error, time.perf_counter() - decode_started, 0.0

    def worker(batch) -> dict[str, object]:
        outcomes: dict[str, object] = {}
        pending = []
        for row in batch:
            fingerprint = _input_fingerprint(row)
            if _feature_ready(state_map.get(row["id"]), row, fingerprint):
                outcomes[row["id"]] = "skipped"
            else:
                pending.append(row)
        if not pending:
            return outcomes
        _mark_feature_running_batch(workspace, pending)
        results = list(pool.map(feature_task, pending))
        for _, _, _, error, decode_seconds, calculation_seconds in results:
            if timings is not None:
                timings.add("features.decode", decode_seconds)
                timings.add("features.calculation", calculation_seconds)
        try:
            with timed(timings, "features.persistence"):
                _persist_feature_results(workspace, results)
        except Exception:
            for row, fingerprint, feature, error, _, _ in results:
                try:
                    _persist_feature_results(workspace, [(row, fingerprint, feature, error, 0.0, 0.0)])
                except Exception as persist_error:
                    outcomes[row["id"]] = persist_error
                else:
                    outcomes[row["id"]] = error
        else:
            for row, _, _, error, _, _ in results:
                outcomes[row["id"]] = error if error is not None else None
        return outcomes

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        result = run_batches(
            workspace,
            "visual_features",
            rows,
            worker,
            batch_size=FEATURE_BATCH_SIZE,
            item_key=lambda row: row["id"],
            job_id=job_id,
            cancel_event=cancel_event,
            progress=progress,
            physical_file_id=lambda row: row["id"],
            relative_path=lambda row: row["relative_path"],
            stage="visual feature extraction",
        )
    if timings is not None:
        timings.add("features.total", time.perf_counter() - started)
    return result


def build_groups(
    workspace: Workspace,
    *,
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: callable | None = None,
) -> GroupingResult:
    started = time.perf_counter()
    records = _asset_records(workspace)
    store = JobStore(workspace)
    identifier = job_id or store.create("grouping", len(records))
    store.set_total(identifier, len(records))
    store.set_stage(identifier, "group construction")
    store.start(identifier)
    groups: list[list[AssetRecord]] = []
    active_by_semantics: dict[str, list[list[AssetRecord]]] = {"absolute": [], "local": []}
    errors = 0
    feature_failures = sum(record.feature is None for record in records)
    no_timestamp = 0
    eligible = 0
    accepted_visual_pairs, rejected_temporal_candidates = _candidate_counts(records)
    tier_a_pair_relations = 0
    tier_a_veto_rejections = 0
    tier_a_groups: set[int] = set()
    tier_b_matches = 0
    previous_by_semantics: dict[str, tuple[AssetRecord, list[AssetRecord]]] = {}
    try:
        for processed, record in enumerate(records, start=1):
            if cancel_event is not None and cancel_event.is_set():
                store.checkpoint(identifier, processed - 1, errors)
                store.cancel(identifier, processed - 1, errors)
                return _result(
                    identifier, groups, eligible, no_timestamp, feature_failures,
                    True, 0.0, started, accepted_visual_pairs,
                    rejected_temporal_candidates,
                    len(tier_a_groups), tier_a_pair_relations,
                    tier_a_veto_rejections, tier_b_matches,
                )
            if record.feature is None:
                errors += 1
                previous_by_semantics.pop(record.capture_semantics, None)
            elif record.capture_value is None:
                no_timestamp += 1
                groups.append([record])
            else:
                eligible += 1
                active = active_by_semantics[record.capture_semantics]
                active[:] = [group for group in active if _within_time(record, group[-1])]
                previous = previous_by_semantics.get(record.capture_semantics)
                burst_assigned = False
                if previous is not None:
                    previous_record, previous_group = previous
                    if _burst_adjacent(previous_record, record):
                        tier_a_pair_relations += 1
                        if _burst_visual_match(previous_record.feature, record.feature):
                            previous_group.append(record)
                            tier_a_groups.add(id(previous_group))
                            assigned = previous_group
                            burst_assigned = True
                        else:
                            tier_a_veto_rejections += 1
                if not burst_assigned:
                    group_count = len(groups)
                    assigned = _assign_record(groups, record, active)
                    if len(groups) == group_count:
                        tier_b_matches += 1
                if assigned not in active:
                    active.append(assigned)
                previous_by_semantics[record.capture_semantics] = (record, assigned)
            if record.capture_value is None:
                previous_by_semantics.pop(record.capture_semantics, None)
            if processed % 16 == 0:
                store.checkpoint(identifier, processed, errors)
            if progress is not None:
                progress(JobProgress(identifier, processed, len(records), record.asset_id, "group construction", errors, 0))
        _activate_groups(workspace, groups)
        store.complete(identifier, len(records), errors)
        return _result(
            identifier, groups, eligible, no_timestamp, feature_failures,
            False, 0.0, started, accepted_visual_pairs,
            rejected_temporal_candidates,
            len(tier_a_groups), tier_a_pair_relations,
            tier_a_veto_rejections, tier_b_matches,
        )
    except Exception:
        store.fail(identifier)
        raise


def _result(
    identifier, groups, eligible, no_timestamp, errors, cancelled,
    feature_seconds, started, accepted_visual_pairs=0,
    rejected_temporal_candidates=0, tier_a_burst_chains=0,
    tier_a_pair_relations=0, tier_a_veto_rejections=0, tier_b_matches=0,
):
    sizes = tuple(sorted((len(group) for group in groups), reverse=True))
    spans = tuple(sorted((_group_span(group) for group in groups if len(group) > 1)))
    return GroupingResult(
        identifier, eligible, no_timestamp, errors, len(groups),
        sum(size > 1 for size in sizes), sum(size == 1 for size in sizes),
        sizes, spans, feature_seconds, time.perf_counter() - started, cancelled,
        accepted_visual_pairs, rejected_temporal_candidates,
        tier_a_burst_chains, tier_a_pair_relations, tier_a_veto_rejections,
        tier_b_matches,
    )


def _asset_records(workspace: Workspace) -> list[AssetRecord]:
    connection = workspace.connect()
    try:
        rows = connection.execute(
            """
            SELECT la.id AS asset_id, la.capture_time, la.capture_time_kind,
                   pf.id AS physical_file_id, pf.filename, pf.relative_path,
                   pf.quality_score,
                   vf.input_fingerprint, vf.dhash, vf.luma_json, vf.color_hist_json,
                   vf.algorithm AS feature_algorithm, vf.version AS feature_version
            FROM logical_asset AS la
            LEFT JOIN physical_file AS pf
                ON pf.logical_asset_id = la.id AND pf.media_type = 'image' AND pf.is_online = 1
            LEFT JOIN visual_feature AS vf ON vf.physical_file_id = pf.id
            WHERE la.media_type = 'image'
            ORDER BY la.id, pf.relative_path
            """
        ).fetchall()
    finally:
        connection.close()
    records: list[AssetRecord] = []
    by_asset: dict[str, list] = {}
    for row in rows:
        by_asset.setdefault(row["asset_id"], []).append(row)
    for asset_id, asset_rows in by_asset.items():
        base = asset_rows[0]
        selected = next(
            (
                row for row in asset_rows
                if row["physical_file_id"] and row["dhash"]
                and row["feature_algorithm"] == FEATURE_ALGORITHM
                and row["feature_version"] == FEATURE_VERSION
            ),
            base,
        )
        feature = None
        if selected["physical_file_id"] and selected["dhash"] and selected["feature_algorithm"] == FEATURE_ALGORITHM and selected["feature_version"] == FEATURE_VERSION:
            feature = FeatureRecord(
                selected["physical_file_id"], selected["input_fingerprint"], selected["dhash"],
                tuple(json.loads(selected["luma_json"])), tuple(json.loads(selected["color_hist_json"])),
            )
        value, semantics = _capture_value(base["capture_time"], base["capture_time_kind"])
        records.append(AssetRecord(
            asset_id, base["capture_time"], base["capture_time_kind"], value,
            semantics, feature, max((row["quality_score"] for row in asset_rows if row["quality_score"] is not None), default=None),
            selected["filename"] if selected["physical_file_id"] else None,
        ))
    records.sort(key=_record_sort_key)
    return records


def _record_sort_key(record: AssetRecord):
    if record.capture_value is None:
        return (2, 0.0, record.asset_id)
    return (0 if record.capture_semantics == "absolute" else 1, record.capture_value, record.asset_id)


def _capture_value(value: str | None, kind: str | None) -> tuple[float | None, str | None]:
    if not value or not kind:
        return None, None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, None
    if kind in {"exif_offset", "aware", "container_metadata"}:
        if parsed.tzinfo is None:
            return None, None
        return parsed.astimezone(timezone.utc).timestamp(), "absolute"
    if kind == "exif_local_unknown":
        if parsed.tzinfo is not None:
            return None, None
        value = (
            parsed.toordinal() * 86400
            + parsed.hour * 3600
            + parsed.minute * 60
            + parsed.second
            + parsed.microsecond / 1_000_000
        )
        return value, "local"
    return None, None


def _assign_record(
    groups: list[list[AssetRecord]],
    record: AssetRecord,
    candidate_groups: list[list[AssetRecord]] | None = None,
) -> list[AssetRecord]:
    candidates: list[tuple[float, list[AssetRecord]]] = []
    for group in candidate_groups if candidate_groups is not None else groups:
        if not group or group[-1].capture_value is None:
            continue
        if not _within_time(record, group[-1]):
            continue
        similarities: list[float] = []
        if all(_within_time(record, member) and _visual_match(record.feature, member.feature, similarities) for member in group):
            candidates.append((min(similarities), group))
    if not candidates:
        groups.append([record])
        return groups[-1]
    candidates.sort(key=lambda item: (-item[0], tuple(member.asset_id for member in item[1])))
    candidates[0][1].append(record)
    return candidates[0][1]


def _within_time(left: AssetRecord, right: AssetRecord) -> bool:
    return (
        left.capture_value is not None
        and right.capture_value is not None
        and left.capture_semantics == right.capture_semantics
        and abs(left.capture_value - right.capture_value) <= MAX_CAPTURE_SECONDS
    )


def _burst_adjacent(left: AssetRecord, right: AssetRecord) -> bool:
    return (
        left.capture_value is not None
        and right.capture_value is not None
        and left.capture_semantics == right.capture_semantics
        and 0 <= right.capture_value - left.capture_value <= BURST_MAX_CAPTURE_SECONDS
    )


def _burst_visual_match(left: FeatureRecord | None, right: FeatureRecord | None) -> bool:
    if left is None or right is None:
        return False
    metrics = _visual_metrics(left, right)
    veto_signals = sum(
        (
            metrics["dhash_distance"] >= BURST_DHASH_VETO,
            metrics["luma_rmse"] >= BURST_LUMA_VETO,
            metrics["color_histogram_l1"] >= BURST_COLOR_HISTOGRAM_VETO,
        )
    )
    return veto_signals < BURST_VETO_SIGNAL_COUNT


def _visual_match(left: FeatureRecord | None, right: FeatureRecord | None, similarities: list[float] | None = None) -> bool:
    if left is None or right is None:
        return False
    metrics = _visual_metrics(left, right)
    match = metrics["visual_pass"]
    if similarities is not None:
        similarities.append(
            max(0.0, 1.0 - (
                metrics["dhash_distance"] / 64
                + metrics["luma_rmse"]
                + metrics["color_histogram_l1"] / 2
            ) / 3)
        )
    return match


def _visual_metrics(left: FeatureRecord, right: FeatureRecord) -> dict[str, object]:
    dhash_distance = _hamming(left.dhash, right.dhash)
    luma_distance = _rmse(left.luma, right.luma)
    color_distance = sum(abs(a - b) for a, b in zip(left.color_hist, right.color_hist))
    return {
        "dhash_distance": dhash_distance,
        "luma_rmse": luma_distance,
        "color_histogram_l1": color_distance,
        "dhash_pass": dhash_distance <= MAX_DHASH_DISTANCE,
        "luma_pass": luma_distance <= MAX_LUMA_RMSE,
        "color_pass": color_distance <= MAX_COLOR_HIST_DISTANCE,
        "visual_pass": (
            dhash_distance <= MAX_DHASH_DISTANCE
            and luma_distance <= MAX_LUMA_RMSE
            and color_distance <= MAX_COLOR_HIST_DISTANCE
        ),
    }


def _candidate_counts(records: list[AssetRecord]) -> tuple[int, int]:
    accepted = 0
    rejected = 0
    for left, right in _temporal_candidates(records):
        if _visual_match(left.feature, right.feature):
            accepted += 1
        else:
            rejected += 1
    return accepted, rejected


def _temporal_candidates(records: list[AssetRecord]):
    by_semantics: dict[str, list[AssetRecord]] = {"absolute": [], "local": []}
    for record in records:
        if record.capture_value is not None and record.feature is not None:
            by_semantics[record.capture_semantics].append(record)
    for candidates in by_semantics.values():
        candidates.sort(key=_record_sort_key)
        for index, left in enumerate(candidates):
            for right in candidates[index + 1:]:
                if right.capture_value - left.capture_value > MAX_CAPTURE_SECONDS:
                    break
                yield left, right


def _adjacent_temporal_candidates(records: list[AssetRecord]):
    by_semantics: dict[str, list[AssetRecord]] = {"absolute": [], "local": []}
    for record in records:
        if record.capture_value is not None and record.feature is not None:
            by_semantics[record.capture_semantics].append(record)
    for candidates in by_semantics.values():
        candidates.sort(key=_record_sort_key)
        for left, right in zip(candidates, candidates[1:]):
            if right.capture_value - left.capture_value <= BURST_MAX_CAPTURE_SECONDS:
                yield left, right


def grouping_diagnostics(workspace: Workspace, limit: int = 100) -> dict[str, object]:
    records = _asset_records(workspace)
    candidates = []
    for left, right in _temporal_candidates(records):
        metrics = _visual_metrics(left.feature, right.feature)
        candidates.append(
            {
                "left": left.filename or left.asset_id,
                "right": right.filename or right.asset_id,
                "left_asset_id": left.asset_id,
                "right_asset_id": right.asset_id,
                "time_delta_seconds": abs(left.capture_value - right.capture_value),
                **metrics,
            }
        )
    rejected = [candidate for candidate in candidates if not candidate["visual_pass"]]
    adjacent_pairs = {
        (left.asset_id, right.asset_id)
        for left, right in _adjacent_temporal_candidates(records)
    }
    burst_candidates = [
        candidate for candidate in candidates
        if (candidate["left_asset_id"], candidate["right_asset_id"]) in adjacent_pairs
    ]
    feature_by_asset = {record.asset_id: record.feature for record in records}
    burst_vetoed = [candidate for candidate in burst_candidates if not _burst_visual_match(
        feature_by_asset[candidate["left_asset_id"]], feature_by_asset[candidate["right_asset_id"]]
    )]
    rejected.sort(
        key=lambda candidate: (
            -sum(candidate[key] for key in ("dhash_pass", "luma_pass", "color_pass")),
            max(
                candidate["dhash_distance"] / MAX_DHASH_DISTANCE,
                candidate["luma_rmse"] / MAX_LUMA_RMSE,
                candidate["color_histogram_l1"] / MAX_COLOR_HIST_DISTANCE,
            ),
            candidate["time_delta_seconds"],
            candidate["left_asset_id"],
            candidate["right_asset_id"],
        )
    )
    return {
        "algorithm": GROUPING_ALGORITHM,
        "version": GROUPING_VERSION,
        "settings": GROUPING_SETTINGS,
        "feature_algorithm": FEATURE_ALGORITHM,
        "feature_version": FEATURE_VERSION,
        "candidate_count": len(candidates),
        "accepted_visual_pair_count": sum(candidate["visual_pass"] for candidate in candidates),
        "rejected_temporal_candidate_count": len(rejected),
        "tier_a_pair_relation_count": len(burst_candidates),
        "tier_a_veto_rejection_count": len(burst_vetoed),
        "tier_b_candidate_count": len(candidates) - len(burst_candidates),
        "tier_b_visual_match_count": sum(
            candidate["visual_pass"] for candidate in candidates
            if (candidate["left_asset_id"], candidate["right_asset_id"]) not in adjacent_pairs
        ),
        "candidates": candidates,
        "borderline_rejected": rejected[:max(0, limit)],
    }


def write_grouping_diagnostics(workspace: Workspace, limit: int = 100):
    destination = workspace.index_path("diagnostics/strict-group-candidates.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(grouping_diagnostics(workspace, limit), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return destination


def _hamming(left: str, right: str) -> int:
    return (int(left, 16) ^ int(right, 16)).bit_count()


def _rmse(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if len(left) != len(right) or not left:
        return math.inf
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(left, right)) / len(left))


def _group_span(group: list[AssetRecord]) -> float:
    values = [record.capture_value for record in group if record.capture_value is not None]
    return max(values) - min(values) if values else 0.0


def _activate_groups(workspace: Workspace, groups: list[list[AssetRecord]]) -> None:
    run_id = str(uuid.uuid4())
    now = _timestamp()
    with workspace.transaction() as connection:
        connection.execute(
            "INSERT INTO grouping_run(id, algorithm, version, settings_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, GROUPING_ALGORITHM, GROUPING_VERSION, json.dumps(GROUPING_SETTINGS, sort_keys=True), now, now),
        )
        for group in groups:
            group_id = hashlib.sha256(
                ("strict-group-v1:" + "|".join(record.asset_id for record in group)).encode()
            ).hexdigest()[:24]
            representative = choose_representative(group)
            connection.execute(
                "INSERT INTO strict_group(run_id, group_id, first_capture_time, member_count, representative_logical_asset_id) VALUES (?, ?, ?, ?, ?)",
                (run_id, group_id, group[0].capture_time, len(group), representative.asset_id),
            )
            for member_order, member in enumerate(group):
                similarities: list[float] = []
                for other in group:
                    if other is not member:
                        _visual_match(member.feature, other.feature, similarities)
                deltas = [
                    abs(member.capture_value - other.capture_value)
                    for other in group
                    if member.capture_value is not None and other.capture_value is not None
                    and member.capture_semantics == other.capture_semantics
                ]
                connection.execute(
                    """
                    INSERT INTO strict_group_member(
                        run_id, group_id, logical_asset_id, member_order,
                        is_representative, min_visual_similarity, max_capture_delta_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, group_id, member.asset_id, member_order,
                        int(member.asset_id == representative.asset_id),
                        min(similarities) if similarities else None,
                        max(deltas) if deltas else None,
                    ),
                )
        connection.execute(
            """
            INSERT INTO workspace_grouping(id, active_run_id, updated_at) VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET active_run_id = excluded.active_run_id, updated_at = excluded.updated_at
            """,
            (run_id, now),
        )
        connection.execute(
            "UPDATE workspace_recommendation SET active_run_id = NULL, updated_at = ? WHERE id = 1",
            (now,),
        )
        connection.execute("DELETE FROM recommendation_run")
        connection.execute("DELETE FROM grouping_run WHERE id <> ?", (run_id,))


def choose_representative(group: list[AssetRecord]) -> AssetRecord:
    available = [record for record in group if record.quality_score is not None]
    if available:
        return sorted(available, key=lambda record: (-record.quality_score, _record_sort_key(record)))[0]
    return sorted(group, key=_record_sort_key)[0]


def _prepare_feature_states(workspace: Workspace, rows) -> dict[str, object]:
    row_ids = [row["id"] for row in rows]
    state_map: dict[str, object] = {}
    if not row_ids:
        return state_map
    placeholders = ",".join("?" for _ in row_ids)
    settings = json.dumps(FEATURE_SETTINGS, sort_keys=True)
    with workspace.transaction() as connection:
        for state in connection.execute(
            f"SELECT * FROM component_state WHERE physical_file_id IN ({placeholders}) AND component = ?",
            [*row_ids, FEATURE_COMPONENT],
        ).fetchall():
            state_map[state["physical_file_id"]] = state
        for row in rows:
            state = state_map.get(row["id"])
            fingerprint = _input_fingerprint(row)
            if state is None:
                connection.execute(
                    "INSERT INTO component_state(physical_file_id, component, status, algorithm, version, settings_json) VALUES (?, ?, 'pending', ?, ?, ?)",
                    (row["id"], FEATURE_COMPONENT, FEATURE_ALGORITHM, FEATURE_VERSION, settings),
                )
            elif not _feature_ready(state, row, fingerprint):
                connection.execute(
                    """
                    UPDATE component_state
                    SET status = 'pending', algorithm = ?, version = ?, settings_json = ?,
                        input_fingerprint = NULL, started_at = NULL, completed_at = NULL, error_message = NULL
                    WHERE physical_file_id = ? AND component = ?
                    """,
                    (FEATURE_ALGORITHM, FEATURE_VERSION, settings, row["id"], FEATURE_COMPONENT),
                )
        for state in connection.execute(
            f"SELECT * FROM component_state WHERE physical_file_id IN ({placeholders}) AND component = ?",
            [*row_ids, FEATURE_COMPONENT],
        ).fetchall():
            state_map[state["physical_file_id"]] = state
    return state_map


def _mark_feature_running_batch(workspace: Workspace, rows) -> None:
    now = _timestamp()
    with workspace.transaction() as connection:
        for row in rows:
            connection.execute(
                """
                UPDATE component_state
                SET status = 'running', started_at = ?, completed_at = NULL, error_message = NULL
                WHERE physical_file_id = ? AND component = ?
                """,
                (now, row["id"], FEATURE_COMPONENT),
            )


def _persist_feature_results(workspace: Workspace, results) -> None:
    settings = json.dumps(FEATURE_SETTINGS, sort_keys=True)
    now = _timestamp()
    with workspace.transaction() as connection:
        for row, fingerprint, feature, error, _, _ in results:
            if error is not None:
                status = "unsupported" if isinstance(error, UnsupportedDecoderError) else "failed"
                connection.execute(
                    """
                    UPDATE component_state
                    SET status = ?, completed_at = NULL, error_message = ?
                    WHERE physical_file_id = ? AND component = ?
                    """,
                    (status, str(error), row["id"], FEATURE_COMPONENT),
                )
                continue
            connection.execute(
                """
                INSERT INTO visual_feature(
                    physical_file_id, algorithm, version, settings_json,
                    input_fingerprint, dhash, luma_json, color_hist_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(physical_file_id) DO UPDATE SET
                    algorithm = excluded.algorithm, version = excluded.version,
                    settings_json = excluded.settings_json,
                    input_fingerprint = excluded.input_fingerprint,
                    dhash = excluded.dhash, luma_json = excluded.luma_json,
                    color_hist_json = excluded.color_hist_json,
                    updated_at = excluded.updated_at
                """,
                (
                    row["id"], FEATURE_ALGORITHM, FEATURE_VERSION, settings,
                    fingerprint, feature.dhash, json.dumps(feature.luma),
                    json.dumps(feature.color_hist), now, now,
                ),
            )
            connection.execute(
                """
                UPDATE component_state
                SET status = 'complete', algorithm = ?, version = ?,
                    settings_json = ?, input_fingerprint = ?, completed_at = ?,
                    error_message = NULL
                WHERE physical_file_id = ? AND component = ?
                """,
                (
                    FEATURE_ALGORITHM, FEATURE_VERSION, settings, fingerprint,
                    now, row["id"], FEATURE_COMPONENT,
                ),
            )


def _feature_ready(state, row, fingerprint: str) -> bool:
    if state is None or state["version"] != FEATURE_VERSION or state["algorithm"] != FEATURE_ALGORITHM:
        return False
    if state["status"] == "unsupported":
        return True
    if state["status"] != "complete" or state["input_fingerprint"] != fingerprint:
        return False
    return True


def _feature_state(workspace: Workspace, file_id: str):
    connection = workspace.connect()
    try:
        return connection.execute(
            "SELECT * FROM component_state WHERE physical_file_id = ? AND component = ?",
            (file_id, FEATURE_COMPONENT),
        ).fetchone()
    finally:
        connection.close()


def _mark_feature_running(workspace: Workspace, file_id: str) -> None:
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE component_state SET status = 'running', started_at = ?, completed_at = NULL, error_message = NULL WHERE physical_file_id = ? AND component = ?",
            (_timestamp(), file_id, FEATURE_COMPONENT),
        )


def _mark_feature_failed(workspace: Workspace, file_id: str, error: Exception) -> None:
    status = "unsupported" if error.__class__.__name__ == "UnsupportedDecoderError" else "failed"
    with workspace.transaction() as connection:
        connection.execute(
            "UPDATE component_state SET status = ?, completed_at = NULL, error_message = ? WHERE physical_file_id = ? AND component = ?",
            (status, str(error), file_id, FEATURE_COMPONENT),
        )


def _feature_from_image(file_id: str, fingerprint: str, image: Image.Image) -> FeatureRecord:
    image = image.convert("RGB")
    gray = image.convert("L")
    dhash_image = gray.resize((9, 8), Image.Resampling.LANCZOS)
    pixels = _pixel_values(dhash_image)
    bits: list[bool] = []
    for row in range(8):
        offset = row * 9
        bits.extend(pixels[offset + column] > pixels[offset + column + 1] for column in range(8))
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    luma_image = gray.resize(LUMA_FEATURE_SIZE, Image.Resampling.BILINEAR)
    luma_pixels = [pixel / 255.0 for pixel in _pixel_values(luma_image)]
    mean = sum(luma_pixels) / len(luma_pixels)
    deviation = math.sqrt(sum((pixel - mean) ** 2 for pixel in luma_pixels) / len(luma_pixels)) or 1.0
    luma = tuple(round((pixel - mean) / deviation, 6) for pixel in luma_pixels)
    hist = [0] * 24
    color_pixels = _pixel_values(image.resize(COLOR_FEATURE_SIZE, Image.Resampling.BILINEAR))
    for red, green, blue in color_pixels:
        hist[red // 32] += 1
        hist[8 + green // 32] += 1
        hist[16 + blue // 32] += 1
    scale = float(len(color_pixels))
    color_hist = tuple(round(value / scale, 6) for value in hist)
    return FeatureRecord(file_id, fingerprint, f"{value:016x}", luma, color_hist)


def _pixel_values(image: Image.Image):
    flattened = getattr(image, "get_flattened_data", None)
    return list(flattened()) if flattened is not None else list(image.getdata())


def _input_fingerprint(row) -> str:
    if row["sha256"]:
        return row["sha256"]
    return f"stat:{row['size_bytes']}:{row['mtime_ns']}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
