"""Experimental algorithms using existing embeddings; no personal model training."""
import numpy as np

from photo_select import groups_for, percentile, quality_scores, unit


def context(records, arrays):
    dino = np.clip(unit(arrays["dino"]) @ unit(arrays["dino"]).T, -1, 1)
    clip = np.clip(unit(arrays["clip"]) @ unit(arrays["clip"]).T, -1, 1)
    affinity = (0.65 * np.clip((dino - 0.55) / 0.45, 0, 1)
                + 0.35 * np.clip((clip - 0.75) / 0.25, 0, 1))
    times = np.array([r["capture_time"] if r["capture_time"] is not None else np.nan for r in records])
    delta = np.abs(times[:, None] - times[None, :])
    temporal = np.nan_to_num(np.exp(-delta / 180), nan=0.0)
    affinity *= 0.65 + 0.35 * temporal
    np.fill_diagonal(affinity, 1)
    groups, _ = groups_for(records, arrays["dino"], threshold=0.20)
    technical = quality_scores(records)
    local = technical.copy()
    for g in np.unique(groups):
        mask = groups == g
        local[mask] = percentile(technical[mask])
    return {"affinity": affinity.astype(np.float32), "groups": groups,
            "technical": technical, "local": local,
            "aesthetic": percentile([r["aesthetic"] for r in records])}


def select(prepared, count, recipe, explain=False):
    affinity = prepared["affinity"]
    n = len(affinity)
    if not 0 <= count <= n:
        raise ValueError(f"Count must be between 0 and {n}")
    local_weight = recipe.get("local_weight", 0.35)
    technical = (1 - local_weight) * prepared["technical"] + local_weight * prepared["local"]
    aw = recipe["aesthetic_weight"]
    quality = (1 - aw) * technical + aw * prepared["aesthetic"]
    diversity = recipe["diversity"]
    density_weights = 1 / np.maximum(affinity.sum(axis=0), 1)
    covered, nearest = np.zeros(n), np.zeros(n)
    selected, available, details = [], np.ones(n, dtype=bool), {}
    for step in range(count + 1):
        if recipe["strategy"] == "coverage":
            gain = np.maximum(affinity - covered[None, :], 0) @ density_weights
            gain /= max(float(gain.max()), 1e-8)
            repetition = 0.15 * np.clip((nearest - 0.85) / 0.15, 0, 1)
        else:
            gain = 1 - nearest
            repetition = np.zeros(n)
        score = (1 - diversity) * quality + diversity * gain - repetition
        score[~available] = -np.inf
        best = int(np.argmax(score)) if step < count else None
        if explain:
            candidates = [best] if best is not None else np.flatnonzero(available)
            for i in candidates:
                details[int(i)] = {"selection_score": float(score[i]), "evaluated_at_pick": step + 1,
                                   "quality_contribution": float((1 - diversity) * quality[i]),
                                   "diversity_contribution": float(diversity * gain[i]),
                                   "duplicate_penalty": float(repetition[i]),
                                   "group_quality": float(quality[i]), "strategy": recipe["strategy"]}
        if best is None:
            break
        selected.append(best)
        available[best] = False
        nearest = np.maximum(nearest, affinity[best])
        covered = np.maximum(covered, affinity[best])
    return selected, quality, details
