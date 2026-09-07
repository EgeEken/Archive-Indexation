# Early photo-selection experiments

This directory contains the JPEG selection proof of concept and two experiment rounds completed on 6–7 September 2026. It is independent of the full archive application described in the repository's `AGENTS.md`. It does not implement that application's SQLite catalog, search, video processing, compression, or production grouping requirements.

The system indexes an existing JPEG folder, scores technical quality, groups visually similar images, and selects a requested number while discouraging repetition. It produces a JSON shortlist and an offline HTML review page. Indexing reads originals; optional export copies a reviewed shortlist into a new destination. There is no source deletion or move operation.

## What was built

- Technical focus/detail/contrast/clipping measurements and optional MobileNet, DINOv2-small, or CLIP embeddings. No model training.
- Original grouping/diversity selector, simple baselines, and experimental maximal marginal relevance (MMR) and weighted-coverage selectors.
- Review UI with chronological group rows, score ranking, bounded group filters, filename search, score details, selection order, and back navigation. The archive review ranks photos across indexed sessions using a shared quality reference.
- Interactive match-versus-coverage and match-versus-time charts, including first indexing and cached reselection measurements.
- Read-only archive integrity checks, label validation, fixed experiment configurations, per-session metrics, and synthetic safety tests.

Quality and selection priority are separate. Technical quality excludes diversity and aesthetics; optional aesthetics contributes to a separate preference score. Selection priority depends on earlier picks. Quality uses empirical percentiles, so it is **not an absolute calibrated photographic-quality score**. The archive view uses one common reference to make its session scores comparable. Groups are approximate and do not yet enforce the production specification's strict time-and-visual rule.

## Findings

The archive contained 7,308 source JPEGs in 18 sessions. Evaluation used 17 sessions with 985 manual picks: eight development sessions and nine validation sessions. Of the manual pairs, 984 were byte-identical; one was a verified crop of the same photograph. Selection counts were supplied from the manual examples, not predicted.

| Method / nine validation sessions | Exact match | Group coverage | Near-duplicate picks |
|---|---:|---:|---:|
| Development-chosen MobileNet default | 23.2% | 70.9% | 2.6% |
| Round-two development-chosen coverage | 26.1% | 53.5% | 28.3% |
| Round-two MMR, aesthetics 65%, diversity 40% | 28.5% | 72.3% | 3.7% |
| Highest observed round-two exact match | 29.5% | 66.2% | 8.5% |

Values are averages across sessions, not pooled percentages. The last two recipes were identified after inspecting validation results. Round two reused previously seen data, so these are exploratory comparisons, not independent validation. Leave-one-session-out recipe selection reached 22.2% average exact match. A 40% agreement rate was not established; MobileNet remains the default because the frozen coverage recipe substantially increased repetition.

Group coverage is a proxy based on visual groups, not human-annotated subjects. Exact overlap also penalizes reasonable alternate frames. See [the original report](REPORT.md) and [the second report](REPORT-V2.md) for metric definitions, splits, caveats, and full results.

On the RTX 3070 Ti laptop / i9-12900H, fresh feature indexing of 1,369 photos took 64–76 seconds depending on pipeline. Cached MobileNet reselection took 0.660 seconds including cache read; the chosen coverage recipe took 2.048 seconds. These were single runs with a warmed GPU and no OS-cache flush, excluding model loading and HTML generation. All downloaded weights totaled approximately 464 MB.

## Run a new selection

Use Python with the versions recorded in `requirements.txt` (tested on Python 3.13.5). Install the appropriate PyTorch build for your CPU/CUDA environment before installing remaining requirements. Model weights are not included.

```powershell
cd "Early Testing"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

# Optional additional protection for the whole archive, beyond the source folder:
$env:PHOTO_ARCHIVE_ROOT = 'D:\Photos'

# Start without downloading a model:
python photo_select.py select 'D:\Photos\session\all-jpgs' --count 100 --method handcrafted --output shortlist.json

# Default embedding-based selection:
python download_models.py --models mobile
python photo_select.py select 'D:\Photos\session\all-jpgs' --count 100 --output mobile-shortlist.json
```

Open the generated HTML beside its JSON. `--ratio 0.15` requests 15%; the default is 10%. Inputs are recursively discovered JPEGs. `--device cpu` disables CUDA use. Output filenames must be new. Keep outputs/cache outside the source; setting `PHOTO_ARCHIVE_ROOT` additionally protects the entire archive tree. Cache reuse checks source paths, sizes, and modification times. Relocated archives require a new index.

Download all supported weights with `python download_models.py` before trying `--method mmr --aesthetic-weight 0.65 --diversity 0.40` or `--method coverage --aesthetic-weight 0.85 --diversity 0.20`. Downloads are explicit and SHA-256 checked with a 5 GB model directory budget. No vendor packages or weights are redistributed here; upstream URLs and hashes are recorded under `evidence/models/`.

To copy an approved selection, run `python photo_select.py export shortlist.json 'D:\Selected-New'`. The destination must not exist and must be outside the protected source/archive. Source metadata and filename collisions are checked before copying. A failed copy can leave a partial new destination. Tests use synthetic images, not archive photographs.

## Evidence and reproducibility

`evidence/` contains all original JSON experiment records and selection manifests, the per-session CSV, model download provenance, and a checksum manifest. Absolute workstation/archive prefixes were replaced with `${EXPERIMENT_ROOT}` and `${PHOTO_ARCHIVE_ROOT}`; scores, timings, hashes, selected indices, session names, and relative filenames are retained. These tokens are documentary and are **not automatically expanded** by the prototype. Historical evidence is kept separate from new runtime `results/` and `selections/`.

Photos, thumbnails/contact sheets, feature caches, weights, environment packages, generated private galleries, and redundant console logs are excluded. Consequently the original photo galleries cannot be viewed from a clone alone. Original local galleries remain in the earlier experimentation workspace. New selections generate working galleries from their own local inputs.

Run `python plot_recorded_experiments.py` to generate `experiments.html` from the committed measurements without the archive or models. Run `python build_ui_fixture.py` for a synthetic UI demo, then `python -m http.server 8769 --bind 127.0.0.1 --directory ui-test`; open `http://127.0.0.1:8769/review.html`. This serves only generated test content. Stop the server with Ctrl+C.

Research workflow, in a fresh copy of this directory: set `PHOTO_ARCHIVE_ROOT`, then run `inventory.py`, `validate_labels.py`, `run_index.py`, `evaluate.py --phase development`, `evaluate.py --phase holdout`, `benchmark_speed.py`, `make_deliverables.py`, and `verify_archive.py`. Round two uses `experiment_v2.py`, `benchmark_v2.py`, `benchmark_cached_v2.py`, and `rebuild_review.py`. These research scripts retain the original experiment's `all-jpgs`/`jpgs` dataset convention and some session-specific benchmark choices; adapt those explicitly for a different study. They are not a generic experiment runner. Existing measurement outputs are generally refused rather than overwritten; the UI rebuild intentionally refreshes derived pages/manifests while checking shortlist preservation.

```powershell
python -m unittest -v test_photo_select
node --check ui/review.js
```

Seven backend tests cover blur scoring, selection counts/determinism, one-to-one matching, score reconstruction, and safe output/export behavior. Earlier browser checks covered filters, ranking, details, pagination, navigation, and a 390-pixel viewport using synthetic images. Recorded archive comparisons found no changes to the 10,406 inventoried files' paths, sizes, or modification times. This is a metadata integrity check, not a full-file checksum audit of every archive file.

The selectors use pairwise matrices with quadratic memory growth; 1,369 images per session was the largest tested folder. Indexing caches completed sessions, but an interrupted individual session may need recomputation. The full project's stricter resumability, absolute quality calibration, and conservative grouping remain future work.

For repository portability, the published code replaces the local hard-coded archive path with `PHOTO_ARCHIVE_ROOT` and uses the declared `wcwidth` dependency instead of the workstation's temporary vendor ZIP. Historical numerical outputs were not rerun or changed by packaging.
