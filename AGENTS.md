# Archive Indexation Project

## Mission

Build a local-first application for personal photo/video archives that combines:

1. **Browse and search** — semantic retrieval, metadata filtering, image-to-image similarity, tags, and later OCR/transcripts/faces.
2. **Archive management** — incremental indexing, strict near-duplicate grouping, quality-assisted image selection, compression, and derived-file tracking.
3. **Portability** — an indexed archive must remain browseable/searchable directly from the archive drive without requiring the original indexing models to be installed.

The first production target is Windows. The primary development machine has an RTX 3070 Ti laptop GPU, but the application must not assume this hardware. Lower-end systems matter, and eventual fully local mobile use is a long-term goal.

This file is the production handoff/specification for future coding agents. `Early Testing/` is experimental evidence and reference code only; it is **not** the base of the production application.

---

# Current project status

The production foundation and current Phase 0–7 work are implemented outside `Early Testing/`:

1. Phase 0: production package and `uv` setup;
2. Phase 1: workspace lifecycle and SQLite persistence;
3. Phase 2: incremental, source-safe scanner;
4. Phase 3: metadata, thumbnails, and persistent jobs;
5. Phase 4: localhost workspace/gallery UI;
6. Phase 5: fixed technical-quality baseline;
7. subsequent UI/UX improvements, image viewer, workspace registry/picker, video center-frame thumbnails, and the current selection/review workflow.

The real Kadıköy validation workspace contains 1,369 images. Its production indexing and thumbnail performance checkpoint was completed and accepted; the current application uses reduced JPEG decoding and shared image processing where applicable. Phase 6 strict near-identical grouping is implemented with versioned Pillow-only dHash, 16×16 normalized luminance, and 32×32 RGB histograms. The current grouping rule is a special adjacent ≤0.5-second burst-chain path with a loose catastrophic-difference veto, plus the conservative ≤10-second complete-linkage path for slower relationships. Candidate diagnostics are available through `archive-index group-diagnostics` and app-owned JSON under `.archive-index/diagnostics/`. Phase 7 uses at most one automatic recommendation per strict group: the persisted representative is recommended only when its fixed technical-quality score is at least 0.60. Recommended implies Representative, but a representative is not necessarily recommended. Manual selected/rejected/undecided decisions are persisted separately and visually override, never erase, machine state. Selection is a workflow inside Gallery and Groups rather than a separate top-level view; Gallery and Groups share the same filtering, sorting, paging, effective-state tags, and Select/Reject actions. Recommendation provenance records its source grouping run and stale results are not presented after grouping changes. Existing workspaces open without an automatic re-index; Re-index is an explicit workspace action, while new folders opened through the home flow start indexing. The home has a recent-workspace registry, reliable Windows folder picker helper, safe recent-entry removal, and optional deletion of only the app-owned `.archive-index/` after confirmation. The viewer supports reusable Details side-panel inspection, Grouping deep links, cursor-centered image zoom, drag panning, and a Smooth pixel-rendering toggle; video playback keeps separate codec/thumbnail limitations and safe single-range serving. HTTP Range support remains a deferred video-viewer concern, not a grouping requirement. Semantic embeddings/search, compression, OCR, faces, RAW/JPEG pairing, and semantic clustering remain later phases.

---

# Hard requirements and decisions

These are established project rules unless the user explicitly changes them.

## Source safety

- The application must **never automatically delete original/source media**.
- Compression may later tell the user which source files are safely represented by verified compressed outputs, but deletion remains an explicit manual user action.
- File-producing workflows must follow a transactional pattern where practical:
  `temporary output -> validation -> atomic/final rename`.
- An interrupted encode must never be mistaken for a valid finished asset.
- Per-file failures must not abort a long batch.
- Development against irreplaceable archives should use copied representative subsets until the relevant mutation path is well tested.

## Local-first

- Core functionality must not require cloud APIs, permanent hosting, or an account.
- Models should run locally.
- Search/browse of an existing valid index must work without the generation models installed.
- Future optional hosted compute is not part of current planning and should not influence the first architecture.

## Portability

- The canonical workspace state initially lives under the workspace itself, preferably:

```text
<workspace root>/
  ... user files ...
  .archive-index/
    index.sqlite
    workspace.json       # optional portable settings/version manifest
    thumbnails/          # rebuildable
    logs/
    tmp/                  # app-owned temporary files only
```

- SSD-local processing/cache mirroring is a later optimization, not a v1 synchronization requirement.
- Workspace paths stored in the database should be relative to the workspace root wherever possible.
- An indexed external drive should be usable on another machine after pointing the app at its workspace root.

## Incremental + resumable work

Every expensive stage must have independently persisted state. Examples:

```text
metadata        complete
hash            complete
thumbnail       complete
quality         complete
embedding       pending
ocr             not requested
faces           not requested
clustering      not requested
compression     43 / 838
```

- Closing the application must not discard completed work.
- `Update Index` processes only new/changed/missing assets and stale components.
- `Rebuild Index` and `Rebuild Component` must exist as explicit deliberate operations.
- If an external drive disappears, the active job may fail; automatic hot reconnect is not required initially. Reopening/resuming must be safe.
- A failed item should be logged and skipped so the rest of the batch continues.

## Compute tiers

Compute tiers are a first-class UX feature.

### Lightweight — default

- Lowest practical compute.
- CPU/weak-device viability matters.
- Reduced indexing/model quality is acceptable if it produces a meaningful speed/resource reduction.
- This should be the safest default for broad hardware support.

### High

- Better models and/or denser processing.
- Still intended for ordinary consumer hardware rather than datacenter GPUs.

### Advanced

Allow independent choice of components/models/features. Example: lightweight semantic embeddings while explicitly enabling OCR.

**Important:** preset names are UI conveniences only. Persist exact algorithms/models/settings/versions and completion states, not merely `lightweight` or `high`.

A feature omitted during one run must be addable later without forcing unrelated recomputation.

## Production core is independent from the prototype

Hard rule: do not convert the experimental scripts in `Early Testing/` into the production backend.

Reasons include:

- session-wide JSON/NPZ cache invalidation;
- absolute-path assumptions;
- generated-static-HTML architecture;
- whole-session pairwise matrices;
- percentile-relative quality scoring;
- experimental grouping semantics that differ from the production requirement;
- known-selection-count evaluation setup.

`Early Testing/` should remain a frozen/reproducible research record and source of benchmark ideas.

---

# Product shape

One application should contain both archive browsing/search and archive management, but expose separate sections to avoid forcing users through unrelated workflows.

Likely top-level areas:

- Home / workspace selection
- Browse / Search
- Selection
- Index / Process
- Compression
- Problems / Logs
- Settings / Advanced

The app should open to a workspace-selection/home view rather than silently treating the last workspace as the only entry point.

A localhost web application is acceptable for the first production UI. The likely initial interaction model is:

```text
run application
-> local backend starts
-> browser opens localhost UI
```

Later, package it into a convenient executable/application. Do not require a native desktop framework from day one. Python is the natural first backend language, but the long-term product is not required to remain purely Python if another architecture becomes justified later.

---

# Two supported archive use cases

The schema and workflows must be able to support both, even if only the first is fully polished initially.

## A. Existing archive browsing/management

Point the app at an already organized archive, possibly rooted at a year or above multiple years.

Example conceptual history:

```text
D:/Archive/2018/photos/example.jxl
D:/Archive/2018/selection/diğer/example.jpeg
D:/Archive/2019/...
```

A workspace may be `D:/Archive/2018` or `D:/Archive` itself. The latter recursively indexes all included years and preserves the nested folder hierarchy for filters/navigation.

## B. Raw phone/camera source -> archive creation

Point the app at a raw source such as a phone DCIM/Camera directory or a camera import.

Inputs may include:

- JPEG only;
- RAW + JPEG pairs;
- photos and videos mixed together;
- later compressed derivatives produced by the app.

This flow eventually combines indexing, selection assistance, and compression to help create the archive in the first place.

Direct phone connection/sync is not a priority; manually selecting a source folder and running `Update` is sufficient initially.

---

# Workspace semantics

A workspace is rooted at a user-selected folder and recursively indexes supported media underneath it.

Examples:

```text
D:\Archive\2024
D:\Archive
C:\TestArchive
```

Recursive indexing should be the default. Do not require users to predeclare a rigid folder convention.

Optional folder-role configuration may later identify locations such as:

- normal media;
- compressed derivatives;
- selected originals;
- ignored folders;
- app-managed exports.

Reasonable defaults should work without configuration. Never hard-code the user's historic `photos/selection/...` layout as the universal structure.

Future multiple-root/global search across workspaces is useful but not required for the first production version.

---

# Canonical data model

Use **SQLite** as the canonical workspace database.

JSON remains useful for:

- workspace configuration;
- export/manifests;
- debugging;
- reproducible benchmark records.

Do not use a large JSON manifest as the primary mutable application database.

## Logical asset vs physical file

The production schema should distinguish a conceptual photo/video from one or more physical file representations.

Conceptually:

```text
logical_asset
  id
  media_type
  capture time / metadata summary
  user selection state
  quality results
  semantic state
  grouping state
  tags/notes
  ...

physical_file
  id
  asset_id
  relative_path
  role
  format / codec
  size
  timestamps
  hash
  online/offline state
  ...
```

Possible physical-file roles:

```text
source_original
camera_raw
camera_jpeg
compressed_derivative
selected_original
proxy
```

Examples:

```text
Photo A
├── DSC0123.ARW       camera_raw
└── DSC0123.JPG       camera_jpeg

Photo B
├── IMG_456.jpg       source_original
└── IMG_456.jxl       compressed_derivative
```

Do not require sophisticated pairing logic in the first scanner, but design the schema so RAW+JPEG, original+JXL, and selected-copy relationships do not later require replacing the core identity model.

A selected copy or compressed derivative should not necessarily appear as a second unrelated photograph in normal browsing.

## Minimum database responsibilities

Track at least:

- workspace/version information;
- stable logical asset ID;
- stable physical file ID;
- workspace-relative path;
- filename/extension;
- media type;
- role/relationship where known;
- size and filesystem timestamps;
- cryptographic hash when computed;
- EXIF/media metadata;
- capture timestamp with source/precision/timezone semantics;
- width/height/duration/codec/etc.;
- component processing state;
- exact model/algorithm/settings/version provenance;
- embedding or embedding reference;
- quality score + component measurements;
- pHash/cheap visual signatures;
- strict photo-group assignment;
- optional semantic cluster assignment;
- user selection state;
- tags/notes;
- compression jobs and derived relationships;
- missing/offline state;
- error details/retry state.

## Embedding storage

Do not prematurely lock embeddings into either SQLite BLOBs or separate tensor/matrix files.

At the expected scale of thousands of assets, simplicity matters more than elaborate vector infrastructure. Benchmark:

- SQLite BLOB storage;
- separate contiguous NumPy/tensor file referenced by rows;
- exact brute-force retrieval.

FAISS or another ANN index is optional and should only be added if simple exact retrieval is measurably insufficient.

---

# File identity, scanning, moves, and offline files

Path is not permanent identity.

Suggested incremental scan strategy:

1. recursively enumerate supported media while excluding `.archive-index/`;
2. compare existing paths using cheap size/mtime data;
3. process genuinely new or changed files;
4. detect indexed paths that disappeared;
5. use content hashes to recognize moved/renamed files where possible;
6. filename may be a hint but never authoritative identity;
7. unresolved files become `offline/missing`, not silently deleted from the index;
8. offer later relink/remove actions.

Hashing policy should balance safety and IO cost. A reasonable early approach is:

- size + mtime for cheap unchanged detection;
- SHA-256 for stable identity/deduplication when an asset is first fully indexed or when identity must be resolved;
- do not rehash every unchanged multi-GB file on every scan.

Checksums help with identity, exact duplicates, copy verification, archive integrity, and derived-file relationships. They are not backups.

The actual external archive drive filesystem remains unknown. The development machine's `C:` drive is NTFS, but production behavior must not assume the external archive is NTFS until inspected.

---

# Metadata rules

Preserve and expose available metadata where useful:

- capture timestamps;
- GPS;
- orientation;
- EXIF camera fields;
- image dimensions;
- video duration/frame rate/codec;
- audio streams;
- relevant color/HDR metadata.

Historical compressed files may already have lost metadata; absence is valid.

## Capture-time correctness

Do not repeat the experimental shortcut of attaching UTC to an EXIF local clock merely because no timezone offset exists.

Store enough information to distinguish:

- timezone-aware capture time;
- timezone-unknown local capture clock;
- filesystem-derived fallback if one is ever used;
- missing time.

Short-range grouping can still compare timestamps from a common camera/session when timezone is unknown, but timeline/map code must not pretend the value is true UTC.

ExifTool and `ffprobe` are likely metadata tools, but implementation details should be benchmarked for deployment simplicity.

---

# Indexing pipeline

Every component is independently runnable and provenance-tracked.

## Initial/default image pipeline

Likely first useful components:

1. file discovery + metadata;
2. hash/stable identity;
3. thumbnail;
4. cheap visual signature/pHash;
5. technical quality baseline;
6. semantic embedding.

The implementation roadmap intentionally adds semantic embeddings after the core scanner/gallery, but the final default preset may include them.

## Optional enrichment components

- OCR;
- face detection/recognition/clustering;
- workspace semantic clustering;
- heavier learned quality model;
- larger semantic embedding model;
- richer video frame sampling;
- speech transcription.

The user must be able to run a previously omitted component later without rebuilding unrelated components.

---

# Search and browse

## Search scope

Semantic search defaults to the entire current workspace.

Structured constraints should be explicit filters rather than unnecessarily baked into natural-language text:

- subfolder/session/year;
- image vs video;
- date range;
- tags;
- people later.

Example: search `cat`, then filter `2021`, rather than requiring the language model to interpret `cat in 2021` as metadata logic.

English semantic queries are enough initially. Multilingual query translation can be evaluated later. Transcript text itself must retain/search the language actually spoken.

## Primary gallery

The conventional gallery is the main browse/search interface.

Search results should expose semantic similarity scores. Each asset should support `Find similar`, i.e. image-to-image retrieval from its existing embedding.

## Embedding map

A PCA/UMAP-like 2D embedding map is a secondary/experimental navigation feature, not a v1 blocker.

Potential hierarchy:

```text
workspace embedding map
-> coarse semantic clusters
-> strict photo groups
-> individual assets
```

It may become useful for selection/organization, but the gallery remains primary unless user testing proves otherwise.

## Workspace semantic clustering

Optional visual/navigation aid.

Tentative approach:

- k-means on semantic embeddings;
- evaluate a sensible candidate range of K;
- use silhouette score as a principled suggestion for K;
- persist K/model/algorithm provenance;
- allow manual K/recompute/disable.

This is not objective semantic truth and must not affect search correctness.

Approximate cluster labels such as `cats`, `food`, or `city streets` may be generated from representative images using a lightweight VLM later. Perfect cluster labels are not required.

Lightweight preset: clustering may be off.
High preset: clustering may be on.

---

# Strict image grouping

Strict grouping is for selection and means:

> essentially the same photograph of the same subject from substantially the same viewpoint/attempt.

It is **not** broad semantic similarity.

## Required behavior

False merges are worse than false splits.

Production candidate relationship should begin from something like:

```text
capture_time_delta <= ~10 seconds
AND
strong cheap visual similarity
```

The exact threshold/visual metric must be benchmarked rather than assumed.

Important consequences:

- time proximity alone is never sufficient;
- semantic embedding closeness alone is never sufficient;
- images months apart must not become one selection group because they look semantically similar;
- avoid global all-pairs comparison if temporal ordering can restrict comparisons to nearby candidates;
- manual group correction should remain possible;
- highest-quality member normally becomes group representative thumbnail.

Cheap candidate features to test include:

- pHash / Hamming distance;
- resized luminance/image comparisons;
- handcrafted spatial/color descriptors from the prototype;
- SSIM-like cheap comparison;
- lightweight visual embeddings only if cheaper methods are insufficient.

A strict `AND` between time and visual agreement is currently the intended production rule.

Do not conflate:

1. exact duplicate;
2. multiple file representations of the same logical asset;
3. strict near-identical burst group;
4. same broader event/moment;
5. semantic similarity.

Each may use a different relation/threshold.

---

# Quality scoring

Quality must be represented as a **workspace-comparable absolute-ish score**, not only a rank within one session/group.

The user wants to support a global `highest scoring images` view across a workspace.

## Baseline signals

Initial cheap signals:

- focus/sharpness;
- exposure/clipping;
- contrast;
- noise.

Raw resolution should not significantly reward an image. A mixed-device archive must not automatically rank every newer/higher-resolution camera above an older one.

## Quality vs diversity

Hard rule: **diversity/uniqueness is not photographic quality**.

Keep separate concepts, e.g. internally:

```text
quality_score
uniqueness/diversity signal
selection_score / recommendation logic
```

The main UI should show one composite quality score, with technical components available in a details popup.

## Calibration warning

The cheap metrics are known to correlate imperfectly with human judgment. The prototype demonstrated this clearly.

Therefore:

- treat handcrafted technical quality as a baseline;
- keep component details transparent;
- design the scoring interface so a lightweight learned/no-reference IQA model can later replace or augment it;
- benchmark learned candidates based on agreement/cost rather than model size or reputation.

Potential future face/person-presence may add a small **selection** bonus as a proxy for potentially important content, but must not be described as sentimental-value inference and should not contaminate the technical quality score.

---

# Selection philosophy

The app should assist rather than reproduce the user's historical subjective taxonomy.

Do not bake in the old mutually exclusive categories:

```text
family -> animals -> manzara -> diğer
```

Those categories are useful historical archive context only.

The generic app should focus on relatively objective-ish assistance:

- technical quality;
- strict redundancy;
- diversity/uniqueness;
- optional auxiliary signals such as faces later.

Users can create arbitrary tags/categories afterward using search, clustering, and manual organization.

## Automatic preselection

Desired behavior:

- usually recommend/select **1** image per strict group;
- occasionally 2–3 when several members are all strong and meaningfully different even by intra-group standards;
- conservative: too many candidates is preferable to silently dropping a valuable image, within reason;
- singleton images are candidates only if quality is acceptable; uniqueness alone does not imply keep;
- poor images can remain unselected even when singleton;
- underlying representation should be score/rank based;
- UI labels such as `probably keep` may be derived from scores but are not the canonical data model.

The selection system should not need to know a predetermined total K. Production must determine candidates from thresholds/group logic rather than being told the number of human selections.

## Selection UI ideas

Useful ideas from the prototype and planning:

- gallery of groups with one representative thumbnail;
- click group -> all members;
- chronological group view;
- optional best-to-worst ranking;
- global score ranking;
- quality details popup;
- preselected state editable by user;
- similar-image search from any asset;
- filters/search by filename/time/folder;
- later synchronized zoom/pan and face crops;
- keyboard-heavy review/selection;
- undo/history;
- later semantic cluster/embedding-map navigation.

Selection state stays in SQLite. Do not physically copy source files simply to represent a selection decision.

---

# Video scope

Videos are initially:

- indexed;
- searchable;
- browsable;
- compressible.

Automatic video quality selection is explicitly deferred.

## First semantic representation

File-level video retrieval is acceptable initially. Do not block the product on shot detection or temporal retrieval.

Later evolution may include:

```text
video
  global representation
  sampled frames / shots
    representative frame
    embedding
    start/end timestamps
  transcript segments
  OCR segments
  detected faces
```

Shot/timestamp-level search is bookmarked as valuable but optional later work.

---

# OCR, speech, and faces

These are modular enrichment passes, not core-scanner dependencies.

## OCR

- optional;
- user-selectable independently of overall compute preset;
- useful for screenshots/documents/signs.

## Speech

Eventually use a local Whisper/faster-whisper-class solution or equivalent.

Videos may contain Turkish, English, French, music, TV audio, or little/no speech. Transcripts must not assume English-only input.

Voice-activity detection may later avoid pointless transcription of silent/non-speech clips if useful.

## Faces

Potential Samsung-Gallery-like workflow:

- detect faces;
- create face embeddings;
- cluster recurring identities;
- show unlabeled clusters;
- user merge/split/name;
- optionally expose face/person presence as a selection signal.

Keep this module replaceable. Verify checkpoint/model licensing before distributing any pretrained face-recognition weights.

---

# Compression

Compression is a separate explicit workflow from indexing.

Default target is the whole workspace, with optional subfolder/file targeting as a secondary feature.

## Provenance, not codec guessing

Do not decide that media is app-managed/compression-complete merely because it is JXL or AV1.

A compressed derivative is considered known/complete only when:

1. the application created it and recorded provenance, or
2. the user explicitly imports/marks an existing derivative as accepted.

Historical manual compressions should otherwise be ignored by app-managed compression state. Backwards compatibility with old settings is not a goal.

## Source -> derivative relation

Track explicit relationships such as:

```text
physical source file
-> compression job
-> compressed physical derivative
```

Record:

- encoder;
- encoder/library version;
- codec;
- complete settings;
- output path;
- validation state;
- timestamps/errors.

## Resume semantics

Resume the **queue**, not byte-level encoder state.

If one JXL/AV1 encode is interrupted:

- discard/ignore its temporary partial output;
- restart that file later;
- continue from already-completed files.

## Failure UX

Support:

- retry one failed item;
- retry all failed items;
- explicit `retry with different settings`.

Never silently fall back to different encoding settings after failure.

## Validation before completion

At minimum:

- output exists;
- output decodes/probes;
- dimensions match expectation;
- video duration is sane/matches expectation;
- obvious/catastrophic size anomalies are rejected;
- intended metadata is preserved where applicable.

Expensive perceptual validation is optional/benchmarking-oriented, not required for every normal encode.

## Historical manual baseline

Image workflow previously used:

```text
JPEG XL quality 60
encoder effort 7
```

Video workflow previously used HandBrake's AV1 `4K Very Fast` preset/workflow.

These are baseline points to compare, not compatibility constraints. Consistency with historical outputs is not important.

Before production compression defaults are frozen:

- inspect exact HandBrake encoder/rate-control/audio/pixel-format/color settings;
- benchmark FFmpeg/encoder CLI equivalents;
- test representative media;
- choose canonical defaults based on quality, size, speed, metadata preservation, and robustness.

Prefer one canonical image preset and one canonical video preset for normal users; Advanced can expose full controls.

Likely naming:

```text
IMG_1234.jpg -> IMG_1234.jxl
VID_1234.mp4 -> VID_1234.mp4   # codec tracked in metadata, extension stays container-based
```

Open decision: behavior when an unknown pre-existing same-stem derivative already occupies the intended output location.

After successful verification, the app may report:

```text
423 originals successfully compressed
8 failed
423 source files are eligible for manual cleanup
```

The application itself does not delete them.

---

# Assessment of `Early Testing/`

`Early Testing/` contains two rounds of JPEG selection experiments completed 6–7 September 2026. It is a useful research artifact, not a production implementation.

## Dataset/evaluation that was actually tested

The experiment inventoried 10,406 archive files and found:

- 7,308 source JPEGs;
- 18 sessions;
- 17 sessions used for evaluation;
- 985 historical manual picks;
- 984 selected/source pairs were byte-identical;
- one additional pair was verified as a crop of the same photograph.

The evaluation therefore had real historical selection labels, but the number of selected images K was supplied to the algorithm. It evaluated *which* images were chosen, not whether the application could infer the appropriate total count.

## Features/models tested

The prototype computed:

- EXIF capture time/camera info;
- thumbnails;
- focus/detail/contrast/clipping/noise/entropy measurements;
- pHash;
- a 447-dimensional handcrafted visual descriptor;
- MobileNetV3 embeddings (1280D);
- DINOv2-small embeddings (384D);
- CLIP ViT-B/32 embeddings (512D);
- optional LAION aesthetic-head score.

No model training/fine-tuning was performed.

## Experimental quality score

The main technical score was based on percentile-normalized components:

```text
0.65 * focus
+ 0.20 * detail
+ 0.15 * contrast
- clipping penalty
```

Noise was measured but not used in the final technical formula.

Because components were empirical percentiles, the result was **not an absolute photographic-quality score**. The archive-wide UI recomputed all percentiles over one shared set of 7,308 images to make sessions comparable, but the score still depended on the reference population.

Production implication:

- preserve the interpretability/component idea;
- do not preserve percentile-relative scoring as the canonical global quality definition.

## Experimental grouping

The main grouping implementation used complete-linkage clustering over embedding cosine distance plus a **soft time penalty**.

This is not the production grouping rule.

Production implication:

- retain complete linkage as an idea worth comparing if needed;
- replace the soft global time treatment with strict nearby-time candidate gating and strong low-level visual agreement;
- avoid global N x N similarity where chronological windows can reduce the problem drastically.

## Experimental selection

Round one tried random/uniform/quality/aesthetic/time/handcrafted/MobileNet/DINO/CLIP variants.

On the nine validation sessions, the frozen development-chosen MobileNet method averaged approximately:

- 23.2% exact manual agreement;
- 70.9% visual-group coverage;
- 2.6% selected near-duplicates.

This was better understood as a reasonable diverse shortlist generator than a reproduction of subjective human taste.

Round two tested 18 MMR/weighted-coverage recipes over existing DINO+CLIP features. Some recipes improved exact overlap, but important trade-offs appeared in repetition and group coverage. The round reused previously seen data, so those comparisons were exploratory rather than clean new validation.

One observed MMR variant reached about 28.5% exact / 72.3% coverage / 3.7% near-duplicates, while the development-frozen coverage recipe reached about 26.1% exact / 53.5% coverage / 28.3% near-duplicates. The prototype correctly retained MobileNet as the practical default rather than overfitting to exploratory exact-agreement gains.

Production implication:

- do not inherit an experimental `winner` blindly;
- quality + strict grouping + conservative preselection is a better first production target than complex MMR/facility-location selection;
- maintain benchmark discipline and evaluate human usefulness, not only exact reconstruction of old picks.

## Experimental performance

On the i9-12900H / RTX 3070 Ti laptop:

- indexing all 7,308 images with MobileNet + DINO + CLIP took about 448 seconds;
- 1,369-image single-pipeline indexing runs were roughly 64–76 seconds depending on pipeline;
- cached reselection was sub-second to a few seconds depending on selection recipe;
- model files totaled about 464 MB.

A major observation was that image decode + technical measurements + thumbnail generation often cost more than neural embedding inference.

Production implication:

- Lightweight optimization must address preprocessing/IO/caching, not only model parameter count;
- decode once and share intermediate data between components where it keeps code safe/simple;
- persist per-asset results so updates do not redo a whole session.

## Experimental UI

The offline viewer demonstrated useful interactions:

- session dashboard;
- chronological group rows;
- score ranking;
- archive-wide ranking;
- group/session filters;
- filename search;
- lazy thumbnails;
- pagination;
- score-details dialog;
- selection order;
- responsive mobile-width layout;
- back navigation;
- experiment charts.

Synthetic/browser validation checked a 390px viewport and multiple sorting/filtering/navigation paths. UI verification covered 23 pages and 17,354 record references in the recorded experiment.

Production implication:

- keep these UX patterns as strong references;
- rebuild the interface as a live localhost application backed by SQLite/API state rather than generating static HTML files.

## Experimental safety/testing patterns worth keeping

The prototype already established valuable invariants:

- source indexing is read-only;
- export refuses unsafe destinations;
- existing non-app cache directories are not silently reused;
- stale sources are rejected before export;
- selection is deterministic for fixed inputs/settings;
- one corrupt image does not prevent indexing valid ones;
- score contributions reconstruct to their reported totals;
- model downloads are explicit and SHA-256 verified;
- archive before/after path/size/mtime inventories were compared;
- UI payload/link/count integrity was checked.

Production tests should preserve the spirit of these guarantees.

## Experimental infrastructure to replace

Do not carry forward as production architecture:

- session-wide JSON index as canonical state;
- `features.npz` cache as the only mutable feature store;
- folder fingerprint where one change invalidates the entire session cache;
- absolute source paths inside canonical records;
- generated one-off HTML pages as the app;
- all-pairs matrices for normal strict grouping/selection;
- a user-provided total selection K;
- assumption of `all-jpgs` / `jpgs` dataset folders;
- JPEG-only discovery.

---

# Implementation roadmap

The phases below are ordered to minimize rework. A future Codex session should generally implement the earliest incomplete phase rather than jumping ahead, unless explicitly asked to prototype a later component.

Each phase should include tests and a small realistic SSD test workspace. Do not use the only copy of the real archive as the primary development fixture.

## Phase 0 — production repository skeleton and conventions

### Goal

Create a clean application structure separate from `Early Testing/`.

### Implement

- choose a simple Python project/package layout;
- dependency management;
- CLI/entry point that can start the application;
- configuration/version constants;
- test layout;
- basic logging;
- `.gitignore` for generated workspace/app artifacts;
- production README with setup/run instructions;
- keep `Early Testing/` untouched except documentation fixes explicitly requested later.

### Suggested shape

Do not over-architect, but a separation roughly like this is reasonable:

```text
src/archive_index/
  app.py
  db/
  workspace/
  indexing/
  jobs/
  media/
  api/
  ...
web/ or static/
tests/
Early Testing/
```

Exact names are flexible.

### Acceptance criteria

- clean environment install works;
- one command starts a placeholder local application/CLI;
- tests run;
- no dependency on files under `Early Testing/` for normal startup.

---

## Phase 1 — workspace + SQLite foundation

### Goal

A workspace can be created/opened safely and store durable state under `.archive-index/`.

### Implement

- workspace root validation;
- create/open `.archive-index/`;
- SQLite connection/schema/migrations;
- workspace schema/version tracking;
- exclude `.archive-index/` from source discovery;
- path normalization and relative-path storage;
- basic logical-asset and physical-file tables;
- component-status/provenance table(s);
- job/error table(s);
- safe transaction helpers where necessary.

### Schema guidance

Prefer normalized, explicit state over a giant JSON column. JSON columns are fine for flexible metadata/settings snapshots, but primary queryable relationships/statuses should have real columns/tables.

Do not design for millions of assets. Design for correctness, migrations, and thousands/tens-of-thousands first.

### Acceptance criteria

- create a workspace on a temporary directory;
- reopen it and preserve state;
- moving the entire workspace folder to another path does not break stored relative media paths;
- database migration/version mechanism exists before schema starts changing frequently;
- scanner cannot recursively ingest `.archive-index/`.

---

## Phase 2 — recursive inventory and incremental scanner

### Goal

Reliably discover media and update SQLite without ML.

### Implement

- recursive traversal;
- supported extension/media-type registry;
- JPEG first, but structure for JXL/RAW/MP4/MOV and future formats;
- stat size/mtime;
- stable physical-file rows;
- SHA-256 calculation policy;
- new/unchanged/changed/missing detection;
- mark missing assets offline instead of deleting;
- basic move/rename reconciliation using hashes;
- per-file errors;
- scan progress + cancellation/resume boundaries.

### Important behavior

The scanner should be useful on arbitrary directory trees, not only historical year/session structures.

Do not yet attempt magical inference of all file roles. Unknown media can begin as generic `source_original`/`unclassified` and be refined later.

### Acceptance criteria

Automated tests should cover:

- initial scan;
- second scan does no unnecessary work;
- adding one file processes one file;
- modifying one file invalidates only that file's stale components;
- renaming/moving a hashed file is recognized where feasible;
- deleting/missing path marks offline;
- corrupt/unsupported file logs an issue without killing batch;
- source bytes/mtime remain unchanged.

---

## Phase 3 — metadata, thumbnails, and job engine

### Goal

Build the first genuinely useful browseable workspace and the generic execution system later ML/compression stages will use.

### Implement

- metadata extraction for images;
- preliminary video probe metadata;
- correct timestamp source/timezone representation;
- thumbnail generation;
- thumbnail cache naming by stable ID rather than source filename alone;
- independently tracked per-file component state;
- generic persistent job queue/state;
- stop/cancel between safe units;
- resume after process restart;
- Problems/log view data source;
- simple aggregate + detailed progress reporting.

### Thumbnail rules

- thumbnails are rebuildable;
- do not treat thumbnail loss as archive data loss;
- preserve aspect ratio/orientation;
- reasonable low-resolution/quality is fine;
- avoid unnecessarily huge thumbnail caches.

### Acceptance criteria

- interrupt indexing halfway, restart app, resume without recomputing completed files;
- one thumbnail failure is logged and batch continues;
- metadata/thumbnail provenance/version can be invalidated deliberately;
- test gallery can render hundreds of assets quickly from cached thumbnails.

---

## Phase 4 — first live localhost UI

### Goal

Replace static review pages with a minimal but real application.

### Implement

- home/workspace selection;
- create/open workspace;
- workspace summary;
- normal gallery;
- folder/subfolder filter;
- image/video type filter;
- filename search;
- asset details;
- online/offline/error indicators;
- indexing/update controls;
- progress display;
- Problems view;
- lazy/incremental gallery loading.

Reuse the successful visual patterns from `Early Testing/ui/` where appropriate, but the backend state must come from SQLite/API endpoints.

### Acceptance criteria

- app opens a copied SSD workspace;
- browse without models installed;
- large-ish synthetic/real test fixture stays responsive;
- 390px-ish viewport remains usable;
- no source mutations occur from browsing.

At the end of this phase, the application should already be a useful read-only media catalog even without embeddings.

---

## Phase 5 — quality baseline

### Goal

Port/replace the useful technical measurements into production with correct persistence and global comparability semantics.

### Implement

- focus/sharpness;
- exposure/clipping;
- contrast;
- noise;
- score-component schema;
- versioned composite score;
- quality details UI;
- global rank view;
- rebuild-quality action;
- benchmark dataset/visual review tooling.

### Important design point

Do **not** simply copy the prototype percentile formula and call it absolute quality.

Possible first iteration:

- store raw measurements in stable units;
- normalize using fixed/calibrated transforms where feasible;
- keep score versioned;
- if temporary workspace-distribution normalization is used, mark it explicitly and avoid pretending it is globally calibrated.

Then benchmark at least one lightweight learned/IQA alternative if practical.

### Acceptance criteria

- same image score does not arbitrarily change merely because one unrelated image is added, unless the scoring version explicitly uses dataset normalization;
- score components reconstruct/explain the composite;
- resolution does not dominate mixed-camera rankings;
- intentionally blurred/exposed synthetic fixtures behave sensibly;
- user can globally rank workspace images.

---

## Phase 6 — strict near-identical grouping

This phase is implemented and remains independently rebuildable. Large-video HTTP Range support for efficient playback/seeking remains a deferred video-viewer concern and is not a Phase 6 blocker.

### Goal

Implement the production selection-group definition independently of semantic search.

### Implement

- order images by capture time where available;
- generate candidate pairs only within roughly 10 seconds initially;
- cheap visual comparison;
- strict threshold;
- connected/group construction that cannot create unacceptable chaining;
- representative member = highest quality by default;
- singleton handling;
- persisted grouping algorithm/version/parameters;
- group browse/review UI;
- manual merge/split/correction mechanism can come after automatic results are validated.
- adjacent capture gaps up to 0.5 seconds use a versioned catastrophic-difference veto and intentional temporal chaining;
- slower relationships retain the ≤10-second visual complete-linkage rule;
- videos are excluded.

### Benchmark against prototype

Use the existing real archive evidence/data locally when available to compare:

- prototype groups;
- new strict groups;
- obvious false merges;
- obvious false splits;
- runtime/memory.

Primary objective is **low false-merge rate**, not maximizing cluster size.

### Algorithm notes

Start cheap. A good first experiment could combine:

```text
abs(time_i - time_j) <= 10 s
AND
pHash distance <= threshold
AND/OR resized-image similarity >= threshold
```

Only bring in MobileNet/DINO/etc. if cheap methods fail on real bursts.

Be careful with transitive chaining: if A~B and B~C but A is no longer visually equivalent to C, a simple connected component may create groups that are too broad. Complete-linkage-like validation or representative/all-member constraints may be preferable.

### Acceptance criteria

- unrelated temporally adjacent images rarely/never merge on review set;
- same burst may split conservatively without breaking the workflow;
- grouping avoids workspace-wide N x N memory;
- representative is stable/deterministic;
- recomputing groups does not alter unrelated quality scores.

---

## Phase 7 — automatic image preselection and review workflow

### Goal

Turn strict groups + quality into a practical assisted-selection tool.

### Implement

- selection state in SQLite;
- auto-preselection version/provenance;
- usual one-per-group choice;
- quality threshold for singleton/weak groups;
- maximum one automatic recommendation per strict group, and only for a current representative above the fixed quality threshold;
- user toggle/select/deselect;
- group review UI;
- rank by quality;
- selected-only view;
- global highest-selected / highest-quality views;
- persistent manual overrides;
- regeneration must not silently overwrite human decisions.

The current implementation supersedes the optional second/third-candidate design above: automatic recommendation is maximum one current representative per strict group, subject to the fixed minimum quality threshold. Manual selection can still include multiple members of a group.

Recommendation runs record their source grouping run. Recommended is a machine state, Representative is an organizational state, and Selected/Rejected are human decisions. The Gallery and Groups views share the same Select/Reject controls and effective-state presentation; there is no separate Selection tab. Unsupported or failed visual features do not create active strict-group membership, representatives, or recommendations.

### Important rule

Automatic recommendation state and user-final selection state should be distinguishable.

Example concept:

```text
auto_recommended = true
user_decision = selected / rejected / undecided
```

Do not erase manual review when the recommendation algorithm changes.

### Acceptance criteria

- automatic selections are conservative and understandable;
- no required global K;
- typically one chosen from a strict group;
- terrible singleton can remain unselected;
- user changes survive reindex/restart;
- changing preselection algorithm does not destroy user decisions.

---

## Phase 8 — semantic image embeddings and search

### Goal

Add the core archive-indexation/search feature after the catalog is stable.

### Implement

- model abstraction/interface;
- Lightweight and High embedding candidates;
- explicit model download + SHA-256 provenance pattern inspired by `Early Testing/download_models.py`;
- batched inference;
- CPU and CUDA paths;
- embedding persistence;
- exact cosine retrieval initially;
- text-to-image search;
- image-to-image `Find similar`;
- similarity score in UI;
- workspace/folder/type filters applied separately from semantic query;
- model/version invalidation/rebuild.

### Model selection

Do not assume CLIP, MobileNet, DINO, or SigLIP is automatically correct. They serve different purposes.

For text retrieval, benchmark lightweight vision-language models on actual desired queries.

For strict duplicate grouping, semantic text embeddings are not automatically the right representation.

### Acceptance criteria

- search `cat` returns sensible cat images in a representative workspace;
- `Find similar` works from an image;
- browse/search works after model files are removed, using stored embeddings;
- adding one new image embeds only that image;
- CPU path is functional;
- model provenance is visible/inspectable.

---

## Phase 9 — compression pipeline

### Goal

Add safe, resumable JXL/AV1 production after generic jobs/provenance are already reliable.

### Before coding defaults

Collect representative source media and inspect exact existing HandBrake settings. Benchmark candidate CLI commands.

### Implement

- canonical image compression preset;
- canonical video compression preset;
- advanced settings;
- queue + persisted jobs;
- temp output paths;
- cancellation at file boundaries;
- restart interrupted file;
- output validation;
- source -> derivative links;
- destination/folder-role handling;
- retry failed;
- batch retry;
- explicit retry with different settings;
- safe-to-manually-delete source report.

### Acceptance criteria

- kill/restart application during a large batch and safely resume completed queue items;
- partial output is never marked complete;
- output decodes/probes;
- provenance records exact settings;
- source file remains unchanged;
- retry behavior is explicit;
- pre-existing unknown JXL/AV1 files are not silently considered app-managed.

---

## Phase 10 — richer organization and exploratory navigation

After core search/selection/compression works:

- arbitrary user tags;
- notes;
- search history/saved searches if still useful;
- semantic k-means clustering;
- silhouette-based suggested K;
- representative/approximate cluster labels;
- 2D embedding map;
- timeline/calendar;
- GPS/map view where metadata exists;
- more powerful group comparison UI.

These are useful but must not delay the core application.

---

## Phase 11 — video semantic enrichment

Progressively add:

1. video thumbnails/proxies;
2. simple file-level visual representation using sampled frames;
3. semantic search over videos;
4. speech transcription;
5. transcript text search;
6. optional OCR;
7. optional shot detection/timestamp-level results;
8. faces later.

Do not attempt automatic video quality/selection until there is a separate validated design for it.

---

## Phase 12 — packaging, lower-end optimization, and mobile exploration

Only after the desktop architecture is stable:

- package localhost app into a convenient Windows executable/application;
- simplify model installation/download UX;
- hardware/resource benchmarking;
- tune Lightweight defaults;
- CPU-only testing;
- lower-memory batching;
- possible ONNX/CoreML/TFLite/mobile-friendly model paths;
- investigate local mobile filesystem/workspace handling.

Mobile should initially be capable of local browse/search/indexation itself rather than depending on sending an entire archive to a PC and copying indexes back.

---

# Testing strategy

Tests are part of every phase rather than a final cleanup step.

## Safety invariants

Always test:

- source files are not modified during read/index/search operations;
- app never deletes originals;
- temp output cannot be mistaken for final output;
- stale source detection where a write/export depends on indexed content;
- app-owned output/cache boundaries;
- corrupted media logs an error rather than crashing a batch;
- restart/resume preserves completed work.

## Incremental-index tests

Use synthetic temporary workspaces to verify:

- first scan;
- no-op second scan;
- add;
- modify;
- rename;
- move;
- remove/offline;
- rebuild one component;
- schema migration.

## Determinism/provenance

For deterministic algorithms, fixed files + fixed settings should produce the same grouping/ranking/selection.

Every derived component should expose enough provenance to answer:

> Why does this value exist, which version produced it, and does it need recomputation?

## UI tests

Retain useful prototype coverage ideas:

- sort descending;
- timeline/group order;
- bounded filters;
- search/empty states;
- lazy/paginated display;
- details dialog;
- navigation;
- responsive narrow viewport;
- no duplicate logical assets in normal archive views;
- offline/missing states.

## Benchmark discipline

Record:

- hardware;
- model versions;
- dataset size;
- first-run vs cached timings separately;
- warm/cold assumptions;
- memory/storage use where relevant.

Do not call small benchmark differences meaningful without repeated measurements.

---

# Performance principles

- Target thousands to tens of thousands of assets, not millions.
- Prefer straightforward exact algorithms while they are fast enough.
- Avoid O(N^2) workspace operations when temporal/local candidate generation can reduce complexity.
- Decode an image once per pipeline pass where feasible and safely share the decoded/resized representation among thumbnail/quality/embedding steps.
- Keep derived caches rebuildable.
- Optimize repeated browsing/search much more aggressively than one-time indexing; users will search often and index occasionally.
- External-drive random IO may matter more than CPU/GPU for browsing, so thumbnails and compact DB queries are important.

---

# Explicit non-goals / ruled-out behavior

Do **not** implement these as default behavior unless requirements change:

- automatic deletion of originals;
- mandatory cloud inference/hosting;
- hard-coded historical folder layout;
- hard-coded `family/animals/manzara/diğer` classification;
- subjective sentimental-value prediction;
- automatic video quality selection in early versions;
- shot-level video indexing as a prerequisite for v1;
- raw resolution as a major quality-score component;
- diversity folded into the technical quality score;
- semantic clustering used as search truth;
- preset names as the only provenance record;
- silent compression-setting fallback;
- treating every JXL/AV1 file as app-managed compression;
- byte-level resume inside one AV1/JXL encode;
- using `Early Testing/` as the production backend;
- rebuilding unrelated components when one model/feature changes;
- global fixed selection count K as a production requirement;
- global all-pairs strict grouping if local temporal candidate generation suffices;
- automatic reopening of the last workspace as the sole startup path.

---

# Known deferred questions

These remain open but should not block early production work.

## Archive/environment

- actual external archive filesystem (NTFS/exFAT/etc.);
- exact current historical archive tree;
- available free space on that drive;
- representative metadata inside JXL/MP4/MOV historical files.

The user's local development `C:` drive is currently NTFS with roughly 86 GB free of ~952 GB usable capacity, but this should not drive archive assumptions.

## Logical-file relationships

- exact automatic RAW+JPEG pairing rules;
- how to recognize historical selected JPEG copies as the same logical asset as a remaining JXL derivative when the original source JPEG was deleted;
- exact role-configuration UX.

These can initially remain manual/unknown rather than risking false associations.

## Quality

- fixed normalization/calibration strategy for the first truly workspace-comparable score;
- whether/which lightweight learned IQA model provides enough value.

## Grouping

- final cheap visual metric;
- exact thresholds;
- exact handling of missing capture times;
- chain-prevention/group construction rule.

## Embeddings

- final Lightweight/High text-image model choices;
- exact storage format;
- whether an ANN index is ever necessary.

## Compression

- exact HandBrake settings from historical workflow;
- final FFmpeg/JXL/AV1 commands;
- metadata/HDR/color handling;
- unknown same-stem derivative collision policy.

## UI/package

- specific backend/web framework;
- specific frontend stack;
- executable packaging method.

Choose the simplest approach that meets the current phase; do not over-design these early.

---

# Internship / provenance boundary

General techniques learned during the internship may inspire independent implementation.

Do not copy into this repository:

- proprietary company source code;
- internal configs;
- private model weights;
- client data;
- credentials;
- confidential documentation;
- undisclosed proprietary artifacts.

Use public libraries/papers and independently authored code for production implementation.

---

# Agent working rules

Future coding agents should follow these unless explicitly overridden by the user:

1. Read this `AGENTS.md` before changing production architecture.
2. Treat `Early Testing/` as experimental reference/evidence, not production source.
3. Work on the earliest relevant incomplete roadmap phase unless the user asks for a specific prototype.
4. Keep changes surgical and understandable; avoid framework-heavy abstractions without demonstrated need.
5. Preserve arbitrary folder structures, cameras, resolutions, and supported codecs.
6. Never implement automatic source deletion.
7. Keep file mutations transactional and validate outputs.
8. Make expensive stages resumable/idempotent.
9. Persist exact provenance for derived outputs.
10. Keep quality, diversity, semantic similarity, strict duplicate grouping, and source/derivative identity as distinct concepts.
11. Prefer false splits over false merges for strict selection groups.
12. Do not over-engineer for millions of assets.
13. Keep Lightweight as the default preset and maintain a functioning CPU path.
14. Keep optional components independently runnable.
15. Write tests for safety/incrementality while implementing each subsystem.
16. Do not commit or push unrelated changes without explicit user permission.
17. When a design choice is unresolved but not blocking, implement the simplest reversible option and document it rather than inventing a permanent policy.
18. When benchmarking, preserve raw measurements and conditions; do not overstate noisy differences.
19. Existing human decisions must survive algorithm/model upgrades unless the user explicitly resets them.
20. Browse/search consumption of a valid index must not require the generation model to be currently installed.
