# Archive Indexation Project

## Purpose

Build a local-first application for personal photo/video archives that combines:

1. **Browse and search** — fast access to indexed media using semantic embeddings, metadata, tags, and later OCR/transcripts/faces.
2. **Archive management** — incremental indexing, grouping, quality-assisted image selection, compression, and derived-file tracking.
3. **Portability** — an indexed archive should remain browseable/searchable from the archive drive itself without requiring the original indexing models to be installed.

The application should eventually support arbitrary archive/folder layouts and possibly mobile devices, but the first implementation should stay simple and target Windows PCs.

## Primary constraints

- Primary development/testing machine: Windows laptop with RTX 3070 Ti.
- Do not hard-code assumptions about that hardware, phone model, image resolution, archive size, or directory layout.
- Typical processing unit is one archive/year containing roughly 10–30 GB, thousands of photos, and around hundreds of videos, but a workspace may also be rooted above multiple years.
- Full archive is over 100 GB.
- Current source media is mostly JPEG images and MP4/MOV videos.
- Existing compressed images use JPEG XL; existing compressed videos remain MP4 but use AV1.
- Selected originals are untouched phone JPEGs, not RAW/PNG/TIFF copies.
- All processing must be local-first. Permanent cloud hosting is not a requirement and should not become a dependency.
- Long-running jobs must be resumable and safe to stop.
- The application itself must never delete source/original files.
- User may manually delete originals after successful compression.

## Product structure

The final application should combine browsing/search and archive management in one UI, but expose distinct sections so users can enter only the workflow they need. Likely sections:

- Home / workspace selection
- Browse / Search
- Selection
- Index / Process
- Compression
- Problems / Logs
- Settings / Advanced

Do not automatically reopen the last workspace as the only startup behavior. The preferred entry point is a home screen listing/selecting workspaces.

## Compute tiers

Treat compute tiers as a first-class feature.

### Default presets

- **Lightweight** — default; designed for CPU-only / weak devices where practical; smaller models and/or reduced processing quality are acceptable.
- **High** — better models/denser processing, but still intended for normal consumer hardware rather than datacenter GPUs.

### Advanced mode

Allow advanced users to choose individual components/models/features rather than using a monolithic preset.

Important: presets are only UI conveniences. Persist the actual processing provenance, not merely `lightweight` or `high`. For each derived component, record the exact model/algorithm/settings/version used and its completion state.

The system should allow omitted features to be run later without rebuilding unrelated components. Compression is a separate explicit workflow, not part of the initial indexing call.

Long-term mobile support may use lower-quality/smaller models rather than running the same large models extremely slowly. Initial mobile direction, if attempted, should be fully local browse/search/indexation on the phone itself rather than requiring round trips to a PC.

## Workspace model

A workspace is rooted at a user-selected folder and recursively indexes its subtree.

Examples:

- `D:\Archive\2024` can be a workspace.
- `D:\Archive` can also be a workspace and should include/index `2023`, `2024`, etc. while preserving the subfolder structure for later filtering/navigation.

Future support for multiple source roots / global search across multiple workspaces would be useful but is not required initially.

### Portability

Workspace state should be portable with the archive. Preferred layout:

```text
<workspace root>/
  ... user media and folders ...
  .archive-index/
    index.sqlite
    workspace.json        # optional portable/config manifest
    thumbnails/           # optional, rebuildable cache
    logs/
    ... other rebuildable derived data ...
```

Processing may use the laptop SSD as temporary/cache storage for speed, but the resulting index/state must be portable back to the archive. Search/browse must work directly from an external hard drive without copying tens of GB of media to the SSD.

## Canonical data model

Use **SQLite** as the likely canonical index rather than large JSON manifests. JSON may still be useful for portable configuration, debugging, or exports.

Do not make the vector-search index or thumbnail cache the only copy of important state. Derived indexes/caches should be rebuildable.

Open implementation choice: embeddings may be stored as SQLite BLOBs or as separate contiguous matrix/tensor files referenced by SQLite. Do not lock this decision prematurely; benchmark simplicity, portability, read performance, and update behavior.

The database should at minimum track:

- stable asset identity
- current path / filename
- media type
- file size and filesystem timestamps
- cryptographic/content hash where available
- EXIF/media metadata
- source/derived relationships
- indexing component status
- exact model/algorithm/settings/version per component
- embeddings or embedding references
- quality score and component metrics
- grouping / cluster information
- user selection state
- user tags/notes
- compression jobs and provenance
- missing/offline state
- failure logs

## File identity, moves, and missing media

Do not treat a pathname as the permanent identity of an asset.

Suggested behavior on rescan:

1. Check assets still at the same path using cheap metadata first.
2. Detect new/missing paths.
3. Use content hashes to recognize moved or renamed files reliably.
4. Filename matching may be used as a fallback hint, never as definitive identity.
5. If a previously indexed file cannot be found, mark it **offline/missing** rather than immediately deleting its index entry.
6. Allow the user to relink, keep offline metadata/index information, or explicitly remove the missing asset from the workspace.

## Incremental and resumable processing

Incremental indexing is mandatory. `Update index` should process only new/changed/missing assets and reuse completed work when valid.

Every expensive component must have independently persisted progress, e.g.:

```text
metadata       complete
thumbnails     complete
embeddings     3241 / 9100
quality        3180 / 9100
ocr            not requested
faces          not requested
clustering     pending
compression    43 / 838
```

Closing/stopping the app must preserve job state. A disconnected external drive may fail the active operation; automatic hot-reconnect handling is not required initially, but reopening and resuming must be safe and convenient.

For per-file errors, log the failure and continue processing the rest of the batch. Provide a Problems view summarizing e.g. `X files failed in Y step`, with detailed logs and retry actions.

Support both:

- **Update Index** — incremental work.
- **Rebuild Index / Rebuild Component** — regenerate derived state deliberately.

Already-generated indexes must remain browseable/searchable on another machine without the models installed. Models are required for generating/regenerating derived information, not for consuming valid stored results.

## Default indexing pipeline

Tentative lightweight/default image indexing components:

- file/metadata extraction
- semantic embedding
- basic quality metrics
- perceptual hash / duplicate signals
- thumbnail generation if useful for UI performance

Optional/heavier components exposed by presets/advanced mode may include:

- OCR
- face detection / recognition / clustering
- workspace-level semantic clustering
- richer quality models
- denser or higher-quality embeddings
- video sampling/shot analysis
- speech transcription

Users should be able to enable a feature such as OCR even while choosing lightweight settings for other components.

## Search and browse

### Scope and filters

Semantic search should search the entire current workspace by default.

Structured properties should be explicit filters rather than unnecessarily encoded in the natural-language query. Examples:

- year/session/subfolder
- media type
- date range
- tags
- people (later)

A query such as `cat` should primarily represent semantic content; a user who wants 2021 results can explicitly restrict the scope to 2021.

English semantic queries are sufficient initially. Multilingual semantic-query handling can be added later, potentially by translating a query to English before embedding if that proves adequate. Transcript search itself must preserve/search the actual languages spoken in the videos.

### Views

Provide at least two switchable search/browse views eventually:

1. **Gallery** — primary/default view; similarity score visible under results.
2. **Embedding map** — secondary/novel exploratory view showing projected embedding positions and where query/results fall.

Each asset should support `find similar`, using image-to-image embedding retrieval. This is considered important.

The embedding map is initially a secondary feature, but could become useful for selection and exploration if implemented well.

Potential future map/navigation concept:

- full embedding projection (PCA/UMAP-like)
- coarse semantic clusters
- strict photo groups
- individual photos

Treat this as an experimental visualization, not a core requirement for the first working search UI.

## Clustering

Workspace-level clustering is optional and should be disabled in the Lightweight preset and available/enabled in High or Advanced modes.

Its purpose is **visual organization/navigation**, not semantic truth and not a factor that changes search correctness.

Potential approach:

- k-means over semantic embeddings
- choose a usable `K` by optimizing silhouette score over a sensible candidate range
- persist cluster assignments and the exact embedding/model/K/algorithm used
- allow the user to disable clustering or manually request another K later

Potential cluster labels such as `cats`, `food`, `city streets` are navigation aids only. A lightweight VLM or representative-image labeling step may produce approximate labels. Perfect labels are not required.

## Image grouping / near-duplicate selection

A proof of concept already exists conceptually/experimentally using:

- CLIP embeddings
- temporal distance between images
- a general diversity signal for grouping
- focus and exposure metrics for quality ranking

Results were promising but clearly early/simple and JPEG-image-only.

The production grouping system should be **strict/conservative about merges**: placing unrelated photos in the same group is worse than splitting one true burst into multiple groups.

Grouping should be primarily automatic, but manual correction should remain possible.

Do not conflate all similarity levels:

- exact duplicate
- same source / encoded derivative
- near-identical burst/angle
- same event/moment
- merely semantically similar

Use the appropriate signals for each.

Open grouping questions still to resolve:

- whether time proximity is a hard prerequisite for strict groups
- preferred temporal window / adaptive temporal logic
- exact combination of perceptual hash, embeddings, capture time, and possibly GPS
- whether best-scoring image should become the group representative automatically

## Selection philosophy

The application should **not attempt to infer subjective sentimental value** as the primary selection objective.

Automatic selection should focus on relatively objective/operational signals:

- technical image quality
- strict redundancy/grouping
- uniqueness/diversity
- possibly face/person-presence bonuses later

The user makes final subjective decisions, aided by search, grouping, rankings, and UI.

The user's previous personal categories (`family`, `animals`, `manzara`, `diğer`) should not be baked into the generic app. Arbitrary user-created organization/tags should be supported instead. Semantic clustering/search can help users construct whatever categories they want afterward.

Videos are **not part of automatic quality selection initially**. For now videos are indexing/search/compression targets only.

## Automatic preselection

Automatic preselection is desirable and should be conservative:

- In a strict group of near-identical images, usually recommend/select one strong representative.
- Two may be retained/recommended when subtle differences make both plausible.
- Prefer selecting slightly too much rather than silently excluding valuable material, but avoid filling the selection with obviously useless images.
- The core mechanism should remain score/ranking based so users can globally sort later.

Strong categorical labels such as `definitely keep` / `probably keep` can be UI interpretations of underlying scores; do not make those labels the fundamental representation.

Open questions still to resolve:

- whether uniqueness/diversity is a separate displayed score or part of the composite selection score
- singleton behavior
- exact preselection rule per group

## Quality scoring

Quality scores must be **absolute/comparable across the workspace**, not only relative within each near-duplicate group. This enables a global `highest quality / best selected images` view.

Initial cheap metrics should emphasize:

- focus/sharpness
- exposure
- contrast
- noise

Do **not** significantly reward raw resolution: mixed-device archives would otherwise systematically favor newer/higher-resolution cameras.

A single composite score should be the main visible value. Detailed component metrics should be accessible in an image/details popup.

Important limitation: simple focus/exposure/contrast/noise metrics alone have already shown limited agreement with human judgments. Treat them as a baseline, not as a solved definition of photographic quality. Benchmark lightweight learned/no-reference quality models later if useful.

Potential future person/face presence can contribute a small selection bonus as a proxy for potentially valuable content, but must not be treated as a true sentimental-value detector.

## Selection UI ideas

Do not prematurely freeze the UI. Keep several ideas available for iterative testing.

Possible hierarchy:

1. Gallery of strict image groups, one representative thumbnail each.
2. Open group to inspect every member.
3. Optional best-to-worst ranking within a group.
4. Quality score visible; component details available on demand.
5. Conservative automatic preselection, editable by user.
6. Similar-image mini-search from each asset.
7. Optional workspace embedding map.
8. Optional semantic clusters as a layer between the full workspace and strict groups.
9. Time filters, later map/GPS filters.

Useful detailed-comparison features later may include synchronized zoom/pan, face crops, rapid keyboard selection, and undo/history.

Selection decisions should be stored in the index and point to the existing media. Do not physically copy files for every decision. Thumbnail files, if generated, exist solely as rebuildable UI-performance caches.

## User metadata

Useful:

- arbitrary tags
- notes
- optional saved searches/search history

Not a priority:

- star-style ratings/grading of personal media

## Metadata

Preserve and use available metadata where possible, especially:

- timestamps
- GPS
- orientation
- EXIF
- video duration/frame rate/codec
- relevant color/HDR metadata

Some metadata may already have been lost in historical compressed files; do not assume it exists.

Future optional UI can include calendar/timeline and geographic map views where metadata is available.

## Video indexing

Initial video scope:

- file-level semantic indexing/search is acceptable
- transcript search can be added
- compression is supported
- automatic video quality/selection is deferred

Shot/timestamp-level analysis remains a bookmarked future enhancement. Potential future representation:

```text
video
  global representation
  optional sampled frames / shots
    representative frame
    visual embedding
    timestamps
  transcript segments
  OCR segments
  detected faces
```

Do not make shot-level processing mandatory in the first version.

## Speech, OCR, faces

These are modular optional enrichment passes.

### Speech

Eventually support local speech-to-text for videos. Spoken content may be Turkish, English, French, or other languages, so transcript indexing must not assume English-only speech.

### OCR

Optional. Advanced users may choose OCR even under otherwise lightweight settings.

### Faces

Potential future Samsung-Gallery-like flow:

- detect faces
- embed/cluster recurring identities
- show unknown face clusters
- user merges/splits/labels identities
- use known/repeated face presence as an optional selection signal

Keep the face subsystem modular and verify pretrained-model licensing before choosing/distributing checkpoints.

## Compression model

Compression is a separate workflow from indexing.

Default scope is the entire workspace. Supporting selected subfolders/files is useful but secondary.

### Provenance over codec guessing

Do **not** infer that an asset is already processed merely because its extension/codec is JXL/AV1.

The application should regard a compression as known/complete only if:

- the app created it and recorded the provenance, or
- the user explicitly imports/marks an existing file as an accepted compressed derivative.

Historical manual compressions should otherwise be ignored for app-managed compression state, because they may use different settings.

Consistency with historical encoding is not required. Backwards compatibility with old manual compression metadata/settings is not a priority.

### Source/derived relationship

Track app-generated compressed outputs explicitly:

```text
source_asset -> compressed_asset
```

Store codec/encoder/settings/version and validation state with that relationship/job.

### Safety

The application must never delete the source asset.

After successful compression and verification, the UI may report:

```text
423 originals safely compressed
8 failed
423 source files may now be manually removed
```

Expose the list/reveal operation and allow retry of failed jobs.

### Temporary outputs and resume behavior

Never treat a partially encoded output as complete.

Encode to a temporary file/path, verify it, then finalize/rename it. If interrupted halfway through one file, restart that individual encode later. Resume the **job queue**, not the byte position inside a JXL/AV1 encode.

### Failures

Support:

- retry individual failed job
- batch retry all failed jobs
- advanced `retry with different settings`

Do not silently switch encoder settings after failures; archive outputs should not become inconsistent without explicit user action.

### Verification

Compression completion requires lightweight validation at minimum:

- output exists
- output can be decoded/probed successfully
- expected dimensions (images) or duration/dimensions (video)
- no catastrophic size anomaly
- relevant metadata preserved where intended

Expensive perceptual metrics are not required for every normal compression job. They can be used during benchmarking/development and optionally exposed to advanced users.

### Current historical baseline

Previous manual workflow:

- JPEG XL: quality 60, effort 7
- video: HandBrake AV1 `4K Very Fast` preset/workflow

These are baselines for testing, not compatibility constraints. Exact HandBrake encoder/rate-control/audio/color settings still need to be inspected on the PC before implementing canonical defaults.

### Canonical presets

Prefer one canonical image compression preset and one canonical video compression preset for normal users, with full advanced customization available.

Likely output naming:

```text
IMG_1234.jpg -> IMG_1234.jxl
VID_1234.mp4 -> VID_1234.mp4  # AV1 indicated by codec, not extension
```

Open decision: how to handle pre-existing same-stem compressed files such as `IMG_001.jpg` + `IMG_001.jxl` when the JXL is not app-managed. Revisit deduplication/recompression semantics before implementation.

## Existing archive structure

Current rough structure is year-oriented, e.g.:

```text
D:/2018/photos/example.jxl
D:/2018/selection/diğer/example.jpeg
```

The exact filesystem tree will be supplied later. Do not hard-code this structure; the app should eventually handle arbitrary folder structures while preserving them in the index.

Historically, selected originals used mutually exclusive personal categories with rough precedence:

`family -> animals -> manzara -> diğer`

This historical convention is archive context only and should not define the generic application's automated selection logic.

## Checksums and integrity

Cryptographic hashes are useful for:

- stable asset identity across move/rename
- verifying source copies
- duplicate detection
- detecting accidental corruption
- verifying SSD temporary processing copied back correctly
- distinguishing originals from re-encoded derivatives

Hashes are not a replacement for backups.

The user currently does not maintain a complete second backup of the archive. Development/testing must therefore be conservative: use copied test subsets and never perform destructive source operations from the application.

## Candidate technologies — provisional, benchmark before locking in

Likely direction:

- language/backend: Python
- canonical metadata/index: SQLite
- metadata extraction: ExifTool + `ffprobe` or equivalent
- image compression: libjxl / `cjxl`
- video compression: FFmpeg and a chosen AV1 encoder after benchmarking/matching desired quality-speed tradeoff
- semantic image-text embeddings: benchmark CLIP/SigLIP-family lightweight models and alternatives
- image-to-image/instance similarity: benchmark semantic embeddings and/or a dedicated visual embedding model
- vector retrieval: exact brute-force may be sufficient for thousands of assets; FAISS or another local vector index can be added where useful
- speech: local Whisper/faster-whisper-class implementation
- OCR: benchmark local options
- UI: local application with gallery/search/processing views; exact framework not decided

Do not over-engineer for millions of assets. Typical workspaces contain thousands of images. Prefer simple exact methods when their latency is already good enough.

## Development phases

### Phase 0 — inventory and compression benchmark definition

- inspect exact archive structure/filesystem
- inspect exact HandBrake/XL Converter settings if still relevant
- select representative test media
- define preservation/validation rules

### Phase 1 — read-only workspace/index foundation

- workspace creation/opening
- recursive scan
- SQLite schema
- stable asset identity
- metadata extraction
- incremental rescan
- missing/offline handling
- optional thumbnails
- basic gallery/filtering

### Phase 2 — semantic image retrieval

- benchmark lightweight/high embedding candidates
- persist embeddings/provenance
- text-to-image search
- image-to-image search
- gallery result view
- global/subfolder filters

### Phase 3 — strict grouping and image selection

- grouping signals and conservative thresholds
- absolute quality score
- representative images
- ranking
- automatic conservative preselection
- manual correction and persistence

### Phase 4 — compression pipeline

- canonical JXL/AV1 presets
- job queue/progress/retry
- temp outputs
- validation
- source/derived tracking
- explicit safe-to-delete-original list

### Phase 5 — richer browsing/organization

- workspace clustering
- embedding map
- cluster labels
- tags/notes
- richer global ranking/selection views

### Phase 6 — video and semantic enrichment

- better video representations / optional shot indexing
- speech transcription
- OCR
- face clustering/recognition
- timeline/map views

These phases are guidance, not rigid milestones. Build vertical prototypes where that produces faster evidence.

## Still-open questions / future discussion

### Environment / filesystem

- Exact filesystem of the external archive drive (NTFS/exFAT/etc.) still needs to be checked on Windows.
- Exact archive folder tree will be provided later.

### Grouping / selection

- Whether uniqueness/diversity remains separate from quality or contributes to final preselection score.
- Whether time proximity is a hard prerequisite for strict grouping.
- Appropriate temporal window/adaptive strategy.
- Exact grouping combination of time, perceptual similarity, embeddings, GPS, etc.
- Representative-thumbnail rule.
- Singleton automatic-preselection behavior.
- Exact conservative preselection thresholds/rules.
- Better human-aligned absolute quality model beyond basic focus/exposure/contrast/noise.

### Compression

- Exact canonical JXL defaults after benchmarking.
- Exact canonical AV1 encoder/settings after inspecting/testing HandBrake-equivalent candidates.
- Metadata/color/HDR preservation requirements for all observed formats.
- Treatment of unknown pre-existing same-stem derivatives.

### Video

- File-level vs shot/timestamp-level semantic indexing after testing usefulness/cost.
- Frame sampling strategy.
- Transcript/OCR default vs optional behavior.

### UI

- Exact desktop/web/native framework.
- Embedding-map projection method and interaction design.
- Detailed group-comparison UX.
- Global highest-score/selection review UX.

## Coding/agent guidance

- Preserve source media. Never implement automatic deletion of originals.
- Keep file mutations transactional/safe: temporary output -> validation -> finalization.
- Make every expensive pipeline stage idempotent and resumable.
- Store exact provenance for derived data and models.
- Do not use preset names as the sole persisted processing state.
- Prefer modular stages so users can run OCR/faces/clustering/etc. later without redoing embeddings or metadata.
- Keep defaults simple. Put model/algorithm knobs in Advanced settings.
- Optimize for archives with thousands, not millions, of assets unless benchmarks show a need for more complexity.
- Treat clustering and automated selection as assistive tools, never destructive truth.
- Keep arbitrary folder structures and source resolutions/codecs supported.
- Avoid cloud dependencies for core functionality.
- Do not copy proprietary internship/company source code or private artifacts into this project; general techniques and publicly documented approaches may be independently reimplemented.
- Do not commit or push unrelated changes without explicit user permission.
