# Review interface and selection experiments — 7 September 2026

The review interface now supports score ranking, chronological group rows, bounded group/session filters, an archive-wide quality view, score explanations, and direct navigation back to sessions. Existing shortlists are preserved. Open `review.html`; the new **All photos** and **Experiments** links are in the main navigation.

## Scores and navigation

- **Image quality** is the technical score, independent of aesthetics and diversity. Details show focus, detail, contrast, exposure/clipping and their actual weighted contributions. Exposure describes clipping rather than a claim about the photographer's intended brightness.
- **Quality + aesthetics** is a separate preference score. It can be ranked within a session. The current MobileNet default has no aesthetic weighting.
- **Selection priority** records the actual greedy decision: quality/local preference contribution + diversity reward − repetition penalty. Selected images show the score at their pick number; alternatives show the score if considered next. These scores change as the shortlist grows, so “Selection order” sorts by actual pick order rather than pretending scores from different steps are directly comparable.
- **Archive image quality** recomputes the same technical formula using one shared percentile reference across all 7,308 indexed originals. It does not compare incompatible session-relative ranks. This is still a relative estimate, not a calibrated absolute measure; a different archive reference can change ranks. Aesthetic scores and selection scores are not pooled into the archive ranking.
- Group numbers now start at 1 in chronological order. A dropdown only offers groups that exist. Group rows retain capture order within each group; archive session rows order sessions chronologically and photos by shared archive quality. An approximate group can still span a subject revisited later.
- Views have filename search, empty states, incremental loading, lazy thumbnails, keyboard-accessible dialogs and browser-session persistence of filters. The UI remains an offline viewer, with no photo writes or network requests. Exporting originals remains a separate command.

The 19 existing manifests were regenerated with score explanations and checked against their previous selected paths before replacement: no existing shortlist changed. The experimental shortlist is separate.

## Additional experiments

Tested **18 fixed recipes**, with no model downloads or training. Both new families use the existing DINO/CLIP embeddings, a soft time influence, technical/local quality and three aesthetic weights (35%, 65%, 85%) crossed with three diversity weights (20%, 40%, 60%):

1. **MMR:** trade image preference against similarity to the most similar already selected photo. This is adapted from the [maximal marginal relevance criterion](https://aclanthology.org/X98-1025/).
2. **Weighted coverage:** prefer candidates that represent still-unrepresented images, downweighting dense bursts. This uses a facility-location-style coverage gain, normalized at each step, combined with preference and a repetition penalty. It is a heuristic adaptation, not a claim to the standard objective's optimization guarantee. [Facility location definition](https://submodlib.readthedocs.io/en/latest/functions/facilityLocation.html).

Recipe definitions and evaluation policy were saved before the new measurements. Selection used the same manual counts and matching/coverage metrics as the first round. All 17 sessions had already been seen in round one, so **these are exploratory results on reused data**, even where files retain the old `holdout` name.

The development objective was exact agreement + 0.10 × subject-group coverage. Its chosen recipe was **coverage with 85% aesthetic weighting / 20% diversity**. The choice was frozen before examining this round's results on the nine validation sessions.

| Method on the nine reused validation sessions | Exact agreement | Subject coverage | Near-duplicate picks |
|---|---:|---:|---:|
| Existing MobileNet default | 23.2% | 70.9% | 2.6% |
| New development-chosen coverage recipe | 26.1% | 53.5% | 28.3% |
| New MMR, 65% aesthetic / 40% diversity | 28.5% | 72.3% | 3.7% |
| Highest observed new exact score: coverage, 35% aesthetic / 20% diversity | 29.5% | 66.2% | 8.5% |

These are session averages. The last two rows were identified after viewing validation results; they are **not independently validated winners**. The chosen coverage recipe's pooled exact agreement is 128/539 = 23.7%, versus 103/539 = 19.1% for MobileNet. Its increased repetition and lost coverage are substantial, so the default stays MobileNet. The experiment page links a separate 100-photo shortlist using the development-chosen recipe to make that tradeoff reviewable.

Also ran leave-one-session-out **recipe selection**: choose among the 18 recipes on the other 16 sessions, then evaluate that choice on the omitted session. Across 17 omitted sessions, exact agreement was **22.2% macro / 22.3% pooled**, with **66.2% coverage**. This separates recipe choice from each fold's labels, but the overall method design was informed by this already-seen archive. It is not evidence of generalization to new outings.

This round did not establish 40% agreement. It suggests that simply increasing aesthetic weight can improve overlap while producing an undesirable shortlist. Better intended-subject focus, expressions/pose timing, and distinguishing repeated views from meaningful variations remain more promising than broad parameter searches. Fresh manually reviewed sessions would be the next honest external check.

## Tradeoff graphics and larger timing runs

`experiments.html` plots **match versus subject coverage** and **match versus measured time**. Switch between exact/one-to-one near match, development/validation, and first indexing/cached reselection. Points and table rows are linked; click either for values. Times are not silently filled with zero where no measurement exists.

Sequential full indexing on **1,369 Kadıköy originals** per pipeline:

| Feature pipeline | First index | Cache read |
|---|---:|---:|
| Handcrafted + technical features | 64.40 s | 0.344 s |
| MobileNet + technical features | 72.09 s | 0.332 s |
| DINO + technical features | 73.11 s | 0.360 s |
| CLIP/aesthetic + technical features | 69.49 s | 0.347 s |
| DINO + CLIP + technical features | 76.34 s | 0.336 s |

Single runs, six CPU workers, batch 32, GPU warmed, fresh feature caches, OS cache not flushed. Model loading and Python startup are excluded. Differences this small should not be treated as a stable hardware ranking.

All 34 original/new recipes were separately timed selecting **138 from the same 1,369 cached photos with score explanations**. MobileNet's selection step took 0.328 s (0.660 s including its measured cache read); the development-chosen coverage recipe took 1.712 s (2.048 s with cache read). HTML generation is excluded. Pipeline-equivalent variants share indexing measurements; the chart states this, since different weighting recipes require the same image features.

Measurements: `results/v2/speed.json`, `cached_speed.json`, `development.json`, `holdout.json`, `cross_validation.json`, `recipes.json`, `protocol.json`. Original round-one experiment results remain intact. The new timing caches add about 118 MB of generated previews/features; model storage remains about 464 MB.

## Verification and integration

Seven backend tests pass, including reconstruction of quality/selection totals, exact selected-count behavior, experimental-method determinism, and existing read-only/copy protections. Browser interaction tests used 250 generated sample images because the browser tool blocks direct local-file URLs. Tests covered score order, valid group filtering, score dialogs, archive sorting/grouping, timeline order, search/empty states, pagination, navigation and a 390-pixel responsive viewport. No browser console errors or horizontal page overflow were observed. Actual archive pages are checked separately for valid embedded records, existing image links and preservation of selections.

`rebuild_review.py` refreshes the generated review pages from cached data. `photo_select.py select` generates the same new UI for future selections. `score_schema: 2` manifests expose technical components and per-step selection contributions for reuse by a larger archive index. `results/v2/archive_scores.json` stores the shared archive ranking explicitly. The UI code is separated into `review_ui.py`, `ui/review.js`, and `ui/review.css`.

The archive remains read-only. `results/v2/archive_integrity.json` compares the complete archive inventory before and after this round; no original files were copied, modified, moved or removed by this work.
