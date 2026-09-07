# Photo selection experiment — 6 September 2026

The result is a usable **first-pass shortlist generator**, not a reliable reproduction of personal photographic taste. It improves exact agreement over random selection and reduces embedding-defined repetition. It still misses many preferred frames and sometimes retains weak or repetitive photographs.

Open **`review.html`** for the results, including a 100-photo shortlist from the 1,369-image Kadıköy session. `README.md` contains selection and optional copy commands. No original photographs were copied for the experiments.

## Data and evaluation

- Inventoried 10,406 archive files. Found 18 `all-jpgs` folders containing 7,308 JPEGs; all decoded and had usable EXIF capture times.
- Evaluated 17 sessions with 985 manual picks from 7,295 originals. The 13-image cloud session had no picks and was excluded from evaluation. Older folders without `all-jpgs` and the optional `! BEST !` selections were not used.
- Matched picks by case-insensitive filename within each session. There were no ambiguous or unmatched names. **984 pairs were SHA-256-identical.** `DSC00148.JPG` in the Bodrum journey session was cropped: visual inspection and grayscale template matching confirmed correspondence (correlation 0.9937). This exception is recorded separately.
- Fixed the split before measuring performance: alternating chronological sessions, **8 development sessions / 3,579 images / 446 picks**, and **9 held-out sessions / 3,716 images / 539 picks**. No network was trained or fine-tuned. Sixteen fixed algorithm/preset combinations were compared, plus random selection over 100 seeds.
- Each benchmark selected exactly the manual count K. The selector received images, metadata and K, but no manual membership labels. This evaluates *which* images to select, not automatic estimation of the ideal count.

The default was frozen from development results using macro exact overlap plus 0.25 × macro near-match overlap, considering visual grouping methods only. MobileNet won narrowly over DINO with 65% aesthetic weighting. Held-out results were inspected afterward; they were not used to change the default. The group-based eligibility constraint was intentional: pure ranking can spend much of the budget on one burst.

## Results on held-out sessions

Percentages below average sessions equally. “Exact” is intersection/K; since predicted and manual counts match, it is both precision and recall. “Near match” uses **one-to-one matching** to manual picks, requiring DINO cosine similarity ≥0.90 and capture-time difference ≤120 seconds, or an exact identity. “Coverage” measures representation of groups containing manual picks, using fixed DINO/time groups. These two visual metrics are proxies, not human judgments, and may favor DINO-based methods.

| Method | Exact | Near match | Coverage | Picks with a near-duplicate |
|---|---:|---:|---:|---:|
| Random, 100 seeds | 17.6% | 43.3% | 57.0% | 20.7% |
| Evenly spaced in time | 18.6% | 49.8% | 66.3% | 3.4% |
| Global Laplacian sharpness | 22.1% | 25.2% | 28.4% | 43.4% |
| Technical quality alone | 22.7% | 29.6% | 29.8% | 45.7% |
| Aesthetic score alone | 24.5% | 30.7% | 32.9% | 65.3% |
| Time groups + quality | 25.7% | 39.6% | 48.4% | 40.4% |
| Handcrafted visual groups | 22.7% | 49.1% | 66.0% | 5.8% |
| **MobileNet + quality, frozen default** | **23.2%** | **47.5%** | **70.9%** | **2.6%** |
| DINO + quality | 21.1% | 44.4% | 77.0% | 0.0% |
| CLIP + quality | 20.6% | 43.6% | 73.5% | 0.3% |
| DINO, tighter groups + 35% aesthetic | 26.9% | 49.4% | 76.6% | 0.0% |
| CLIP + 65% aesthetic | 26.5% | 49.8% | 75.8% | 3.1% |

All 16 variants and every session are in `results/per_session_metrics.csv`. Near-duplicates here mean DINO similarity ≥0.95 to another selected image, regardless of time. This misses some semantically repetitive poses; zero does not mean perfect uniqueness. Manual picks themselves score 17.1% on this measure because some repetition is intentional.

The default recovered **103/539 exact picks (19.1% pooled)** and 240/539 near matches (44.5% pooled). Its macro exact improvement over the sampled random baseline was **5.5 percentage points**, with a paired session-bootstrap 95% interval of **+2.2 to +8.9 points** (10,000 resamples). The analytical random macro expectation is 17.5%. Against technical ranking alone the interval spans zero. Nine held-out sessions are a small sample, and small sessions affect macro averages strongly.

An important counterexample: on Kadıköy, the default recovered only **11/138 exact picks** at the manual budget. Even time spacing also slightly exceeded the default on the near-match proxy overall. These results do not justify claiming general aesthetic superiority. DINO with tighter groups is a useful alternate preset, but choosing it from these held-out scores would require another independent test to validate that choice.

## System

1. Read EXIF orientation, capture time/subseconds/offset and limited camera metadata. Decode reduced JPEGs, preserve aspect ratio and compute quality features at up to 768 pixels on the long edge. Missing times remain missing; filesystem times are not substituted for capture times.
2. Measure smoothed Laplacian energy relative to local contrast in a 4×4 grid, combining the 80th-percentile patch and central patches. Add gradient detail, contrast and soft clipping penalties. This allows a blurred background, but does not establish whether the intended eye or subject is in focus. Global raw Laplacian is a separate baseline. Noise and entropy are recorded for inspection, not used in the final score.
3. Extract normalized visual embeddings: **MobileNet 1,280 dimensions**, **DINO 384**, **CLIP 512**, or a **447-dimensional handcrafted color/spatial/DCT descriptor**. DINO uses 224×224 inputs for speed, below its model card's 518×518 setting; CLIP/MobileNet also use 224×224 preprocessing. Center crops can lose edge subjects.
4. Form complete-linkage groups from cosine distance plus a small capture-time penalty. Complete linkage avoids long chains of dissimilar burst images. Time is supporting evidence, not a hard boundary. Group thresholds are approximate concepts rather than identity recognition.
5. Greedily combine quality, within-group rank, diminishing reward for additional picks from a group, and a high-similarity penalty. Return exactly K distinct file paths. Optional LAION aesthetic scores are blended by rank; they reflect a general pretrained preference, not this photographer's taste.

## Speed and storage

On the i9-12900H / RTX 3070 Ti laptop, indexing all 7,308 images with all three neural encoders took **448 seconds**, including model loading but excluding downloads. Per-image work summed to 435 seconds: JPEG/quality/thumbnails 285 s, MobileNet 67 s, DINO 48 s, CLIP 35 s. Disk scanning, cache compression and setup account for the remainder.

Independent fresh-index runs on the same 265-image Montsouris session:

| Pipeline | Index time | Images/s | Read existing cache |
|---|---:|---:|---:|
| Handcrafted / CPU quality | 8.62 s | 30.7 | 0.083 s |
| MobileNet + CPU quality | 12.21 s | 21.7 | 0.090 s |
| DINO + CPU quality | 9.81 s | 27.0 | 0.093 s |
| CLIP/aesthetic + CPU quality | 10.50 s | 25.2 | 0.112 s |

These are single runs, batch 32, six CPU workers, warmed GPU, model loading excluded and OS file caches not flushed. They are not controlled cold-disk measurements. Grouping/selection on that session took 10–13 ms. A complete cached 1,369-image CLI invocation selecting 100 took about 2.2 seconds including Python startup. Image decoding and quality extraction dominate; smaller model weights did not imply fastest inference here.

Weights occupy **464 MB**: MobileNet 22 MB, DINO 88 MB, CLIP 354 MB, aesthetic head 3 KB. Main feature/thumbnail cache: **158 MB**; separate timing caches: **19 MB**; reports/manifests/review pages: about **22 MB**. No new GPU framework was installed. Only a tiny missing text-formatting dependency was added locally. Weights, URLs and SHA-256 values are recorded; photographs never left the laptop.

## Verification and next work

Five tests cover blur response, count/determinism/diversity, cache reuse and decode errors, archive/output protections, copy integrity/stale-source refusal, and one-to-one metric matching. The real 100-photo CLI run reproduced the saved shortlist. Final inventory comparison found **all 10,406 archive paths, sizes and modification timestamps unchanged**, with no additions or removals. This is a metadata comparison, not a full cryptographic rehash of every archive file.

Visual review found reasonable variety in cats, birds and street scenes, but also repeated gull/sparrow poses and dark or weakly framed photos. Full-resolution subject/eye focus, expressions, motion timing, crop potential and personal significance remain unresolved. The next useful experiment is human review of accepted/rejected suggestions, followed by better subject-region focus and grouping; more model size alone is not yet justified.

Model sources: [MobileNet model card](https://huggingface.co/timm/mobilenetv3_large_100.ra_in1k), [DINOv2-small model card](https://huggingface.co/timm/vit_small_patch14_dinov2.lvd142m), [OpenAI CLIP](https://github.com/openai/CLIP), [LAION aesthetic predictor](https://github.com/LAION-AI/aesthetic-predictor). These are pretrained feature/scoring models; this experiment did not train them.
