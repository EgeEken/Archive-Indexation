"""Conservative automatic recommendations built from cached grouping and quality data."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Event

from ..jobs.engine import JobProgress, JobStore
from ..workspace import Workspace
from .grouping import FEATURE_ALGORITHM, FEATURE_VERSION, _hamming, _rmse

RECOMMENDATION_ALGORITHM = "strict-group-conservative-recommendation"
RECOMMENDATION_VERSION = "1"
SINGLETON_MIN_QUALITY = 0.60
ADDITIONAL_MIN_QUALITY = 0.72
MAX_RECOMMENDATIONS_PER_GROUP = 3
DISTINCT_DHASH_DISTANCE = 5
DISTINCT_LUMA_RMSE = 0.05
DISTINCT_COLOR_HISTOGRAM_L1 = 0.06
DISTINCT_SIGNAL_COUNT = 2
RECOMMENDATION_SETTINGS = {
    "singleton_min_quality": SINGLETON_MIN_QUALITY,
    "additional_min_quality": ADDITIONAL_MIN_QUALITY,
    "max_recommendations_per_group": MAX_RECOMMENDATIONS_PER_GROUP,
    "distinct_dhash_distance": DISTINCT_DHASH_DISTANCE,
    "distinct_luma_rmse": DISTINCT_LUMA_RMSE,
    "distinct_color_histogram_l1": DISTINCT_COLOR_HISTOGRAM_L1,
    "distinct_signal_count": DISTINCT_SIGNAL_COUNT,
    "feature_algorithm": FEATURE_ALGORITHM,
    "feature_version": FEATURE_VERSION,
}


@dataclass(frozen=True)
class VisualFeature:
    dhash: str
    luma: tuple[float, ...]
    color_hist: tuple[float, ...]


@dataclass(frozen=True)
class Candidate:
    group_id: str
    asset_id: str
    member_order: int
    quality_score: float | None
    feature: VisualFeature | None


@dataclass(frozen=True)
class RecommendationResult:
    job_id: str
    run_id: str | None
    total_assets: int
    auto_recommended: int
    recommended_multi_groups: int
    singleton_recommendations: int
    groups_recommending_one: int
    groups_recommending_two: int
    groups_recommending_three: int
    groups_without_recommendation: int
    elapsed_seconds: float
    cancelled: bool = False


def recommendation_provenance() -> tuple[str, str, dict[str, object]]:
    return RECOMMENDATION_ALGORITHM, RECOMMENDATION_VERSION, RECOMMENDATION_SETTINGS


def build_recommendations(
    workspace: Workspace,
    *,
    job_id: str | None = None,
    cancel_event: Event | None = None,
    progress: callable | None = None,
) -> RecommendationResult:
    started = time.perf_counter()
    grouping_run_id, groups = _load_groups(workspace)
    store = JobStore(workspace)
    identifier = job_id or store.create("recommendations", len(groups))
    store.set_total(identifier, len(groups))
    store.set_stage(identifier, "recommendations")
    store.start(identifier)
    if grouping_run_id is None:
        store.complete(identifier, 0, 0)
        return _result(identifier, None, groups, [], started)
    decisions: list[tuple[str, int, int | None, int, str]] = []
    processed = 0
    errors = 0

    try:
        for group_id, candidates in groups:
            if cancel_event is not None and cancel_event.is_set():
                store.checkpoint(identifier, processed, errors)
                store.cancel(identifier, processed, errors)
                return _result(
                    identifier, None, groups, decisions, started, cancelled=True
                )
            try:
                decisions.extend(_recommend_group(group_id, candidates))
            except Exception as error:
                errors += 1
                store.record_error(identifier, error)
                decisions.extend(
                    (group_id, candidate.asset_id, 0, None, "recommendation_error")
                    for candidate in candidates
                )
            processed += 1
            if processed % 16 == 0:
                store.checkpoint(identifier, processed, errors)
            if progress is not None:
                progress(JobProgress(identifier, processed, len(groups), group_id, "recommendations", errors, 0))

        if cancel_event is not None and cancel_event.is_set():
            store.checkpoint(identifier, processed, errors)
            store.cancel(identifier, processed, errors)
            return _result(identifier, None, groups, decisions, started, cancelled=True)

        run_id = str(uuid.uuid4())
        now = _timestamp()
        settings = json.dumps(RECOMMENDATION_SETTINGS, sort_keys=True)
        with workspace.transaction() as connection:
            connection.execute(
                "INSERT INTO recommendation_run(id, algorithm, version, settings_json, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, RECOMMENDATION_ALGORITHM, RECOMMENDATION_VERSION, settings, now, now),
            )
            connection.executemany(
                """
                INSERT INTO asset_recommendation(
                    run_id, logical_asset_id, auto_recommended, recommendation_rank,
                    is_primary, reason
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ((run_id, asset_id, recommended, rank, primary, reason)
                 for _, asset_id, recommended, rank, primary, reason in decisions),
            )
            connection.execute(
                """
                INSERT INTO workspace_recommendation(id, active_run_id, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET active_run_id = excluded.active_run_id,
                                              updated_at = excluded.updated_at
                """,
                (run_id, now),
            )
            connection.execute("DELETE FROM recommendation_run WHERE id <> ?", (run_id,))
        store.complete(identifier, processed, errors)
        return _result(identifier, run_id, groups, decisions, started)
    except Exception:
        store.fail(identifier)
        raise


def _load_groups(workspace: Workspace) -> tuple[str | None, list[tuple[str, list[Candidate]]]]:
    connection = workspace.connect()
    try:
        active = connection.execute(
            "SELECT active_run_id FROM workspace_grouping WHERE id = 1"
        ).fetchone()
        if active is None or active["active_run_id"] is None:
            return None, []
        rows = connection.execute(
            """
            SELECT sgm.group_id, sgm.logical_asset_id, sgm.member_order,
                   pf.quality_score, vf.dhash, vf.luma_json, vf.color_hist_json
            FROM strict_group_member AS sgm
            JOIN logical_asset AS la ON la.id = sgm.logical_asset_id AND la.media_type = 'image'
            LEFT JOIN physical_file AS pf
                ON pf.logical_asset_id = la.id AND pf.media_type = 'image' AND pf.is_online = 1
            LEFT JOIN visual_feature AS vf
                ON vf.physical_file_id = pf.id
               AND vf.algorithm = ? AND vf.version = ?
            WHERE sgm.run_id = ?
            ORDER BY sgm.group_id, sgm.member_order, pf.id
            """,
            (FEATURE_ALGORITHM, FEATURE_VERSION, active["active_run_id"]),
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[str, dict[str, list]] = {}
    for row in rows:
        grouped.setdefault(row["group_id"], {}).setdefault(row["logical_asset_id"], []).append(row)
    result: list[tuple[str, list[Candidate]]] = []
    for group_id, assets in grouped.items():
        candidates = []
        for asset_id, asset_rows in assets.items():
            feature_row = next((row for row in asset_rows if row["dhash"]), None)
            feature = None
            if feature_row is not None:
                try:
                    feature = VisualFeature(
                        feature_row["dhash"],
                        tuple(json.loads(feature_row["luma_json"])),
                        tuple(json.loads(feature_row["color_hist_json"])),
                    )
                except (TypeError, ValueError, KeyError):
                    feature = None
            candidates.append(
                Candidate(
                    group_id,
                    asset_id,
                    asset_rows[0]["member_order"],
                    max((row["quality_score"] for row in asset_rows if row["quality_score"] is not None), default=None),
                    feature,
                )
            )
        result.append((group_id, sorted(candidates, key=lambda candidate: candidate.member_order)))
    result.sort(key=lambda group: (group[1][0].member_order if group[1] else 0, group[0]))
    return active["active_run_id"], result


def _recommend_group(group_id: str, candidates: list[Candidate]) -> list[tuple[str, int, int | None, int, str]]:
    ranked = sorted(
        candidates,
        key=lambda candidate: (
            candidate.quality_score is None,
            -(candidate.quality_score or 0),
            candidate.member_order,
            candidate.asset_id,
        ),
    )
    recommended: list[Candidate] = []
    if len(ranked) == 1:
        if _quality_pass(ranked[0], SINGLETON_MIN_QUALITY):
            recommended.append(ranked[0])
    elif ranked:
        recommended.append(ranked[0])
        for candidate in ranked[1:]:
            if len(recommended) >= MAX_RECOMMENDATIONS_PER_GROUP:
                break
            if _quality_pass(candidate, ADDITIONAL_MIN_QUALITY) and all(
                _meaningfully_distinct(candidate, previous) for previous in recommended
            ):
                recommended.append(candidate)

    recommended_ids = {candidate.asset_id for candidate in recommended}
    decisions = []
    for rank, candidate in enumerate(recommended, start=1):
        reason = "singleton_quality_threshold" if len(ranked) == 1 else (
            "strict_group_primary" if rank == 1 else "strict_group_distinct_additional"
        )
        decisions.append((group_id, candidate.asset_id, 1, rank, int(rank == 1), reason))
    for candidate in candidates:
        if candidate.asset_id not in recommended_ids:
            reason = "below_singleton_quality" if len(ranked) == 1 else "not_distinct_or_quality"
            decisions.append((group_id, candidate.asset_id, 0, None, 0, reason))
    return decisions


def _quality_pass(candidate: Candidate, threshold: float) -> bool:
    return candidate.quality_score is not None and candidate.quality_score >= threshold


def _meaningfully_distinct(left: Candidate, right: Candidate) -> bool:
    if left.feature is None or right.feature is None:
        return False
    dhash = _hamming(left.feature.dhash, right.feature.dhash)
    luma = _rmse(left.feature.luma, right.feature.luma)
    color = sum(abs(a - b) for a, b in zip(left.feature.color_hist, right.feature.color_hist))
    signals = sum(
        (
            dhash >= DISTINCT_DHASH_DISTANCE,
            luma >= DISTINCT_LUMA_RMSE,
            color >= DISTINCT_COLOR_HISTOGRAM_L1,
        )
    )
    return signals >= DISTINCT_SIGNAL_COUNT


def _result(identifier, run_id, groups, decisions, started, cancelled=False):
    recommended = {asset_id for _, asset_id, is_recommended, _, _, _ in decisions if is_recommended}
    multi = sum(len(candidates) > 1 for _, candidates in groups)
    singleton_recommendations = sum(
        len(candidates) == 1 and candidates[0].asset_id in recommended
        for _, candidates in groups
    )
    sizes = []
    for group_id, candidates in groups:
        sizes.append(
            sum(
                is_recommended
                for decision_group, _, is_recommended, _, _, _ in decisions
                if decision_group == group_id
            )
        )
    return RecommendationResult(
        identifier,
        run_id,
        sum(len(candidates) for _, candidates in groups),
        len(recommended),
        sum(any(asset_id in recommended for asset_id in (candidate.asset_id for candidate in candidates)) for _, candidates in groups if len(candidates) > 1),
        singleton_recommendations,
        sizes.count(1),
        sizes.count(2),
        sizes.count(3),
        sum(size == 0 for size in sizes),
        time.perf_counter() - started,
        cancelled,
    )


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
