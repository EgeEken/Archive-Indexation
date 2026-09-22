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

The production foundation and current Phase 0–9.4 work are implemented outside `Early Testing/`:

1. Phase 0: production package and `uv` setup;
2. Phase 1: workspace lifecycle and SQLite persistence;
3. Phase 2: incremental, source-safe scanner;
4. Phase 3: metadata, thumbnails, and persistent jobs;
5. Phase 4: localhost workspace/gallery UI;
6. Phase 5: fixed technical-quality baseline;
7. subsequent UI/UX improvements, image viewer, workspace registry/picker, video center-frame thumbnails, and the current selection/review workflow;
8. Phase 7.5 learned image-quality provider integration: official LAR-IQA inference is available through mutually exclusive `quality-lar-cpu` and `quality-lar-cuda` extras and is the default for new and migrated workspaces; explicit quality-off remains available.
9. Phase 8A workspace planning, read-only folder analysis, persisted scope configuration, and selective reconfiguration are implemented;
10. Phase 8B exact-duplicate consolidation, conservative RAW/JPEG reconciliation, preferred-representation failover, and multi-representation review are implemented;
11. Phase 8C uniform video-frame sampling and learned video technical-quality scoring are implemented in frozen commit `d92e8cd` (`Add sampled video quality`); its focused 8C.1 extraction optimization and decode audit are complete with a deterministic short/medium single-process path and a long-video seek fallback;
12. Phase 8D separate indexing scope/quality configuration, optional RAW embedded-preview thumbnails/quality, and independent persistent enrichment jobs are implemented;
13. Phase 9 semantic embeddings/search is implemented with OpenCLIP ViT-B/16 DataComp XL as the default and Google SigLIP2 Base Patch16 224 as the alternative. New workspace setup enables semantic search by default; existing workspaces retain their stored choice. Model installation is explicit into the global user cache, and indexing/search never downloads a model. Image vectors use the preferred rendered representation or a validated RAW embedded preview; video vectors use shared sampled frames. The workspace UI supports semantic text search, similarity chips, and Find similar through the existing gallery/viewer paths. Provider switching/cache reuse, model management, persistent embedding jobs, and schema migration 16 remain implemented. Full-archive embedding generation remains opt-in.
14. Phase 9.4 archive visualizations are implemented: local Geo Map, Canvas Timeline, and persisted PCA Vector Cloud, all using the shared browser filter state and existing Details/viewer workflow.

The real Kadıköy validation workspace contains 1,369 images. Its production indexing and thumbnail performance checkpoint was completed and accepted; the current application uses reduced JPEG decoding and shared image processing where applicable. Phase 6 strict near-identical grouping is implemented with versioned Pillow-only dHash, 16×16 normalized luminance, and 32×32 RGB histograms. The current grouping rule is a special adjacent ≤0.5-second burst-chain path with a loose catastrophic-difference veto, plus the conservative ≤10-second complete-linkage path for slower relationships. Candidate diagnostics are available through `archive-index group-diagnostics` and app-owned JSON under `.archive-index/diagnostics/`. Phase 7 uses at most one automatic recommendation per strict group: the persisted representative is recommended only when its fixed technical-quality score is at least 0.70. Recommended implies Representative, but a representative is not necessarily recommended. Manual selected/rejected/undecided decisions are persisted separately and visually override, never erase, machine state. Selection is a workflow inside Gallery and Groups rather than a separate top-level view; Gallery and Groups share the same filtering, sorting, paging, effective-state tags, and Select/Reject actions. Recommendation provenance records its source grouping run and stale results are not presented after grouping changes. Existing workspaces open without an automatic re-index; Re-index is an explicit workspace action, while new folders opened through the home flow start indexing. The home has a recent-workspace registry, reliable Windows folder picker helper, safe recent-entry removal, and optional deletion of only the app-owned `.archive-index/` after confirmation. The viewer supports reusable Details side-panel inspection, Grouping deep links, cursor-centered image zoom, drag panning, and a Smooth pixel-rendering toggle; video playback keeps separate codec/thumbnail limitations and safe single-range serving. HTTP Range support remains a deferred video-viewer concern, not a grouping requirement. Compression, OCR, faces, RAW development decoding, and semantic clustering remain later phases.

Index storage now uses a curated, normalized metadata policy: source media remains canonical for full EXIF, while SQLite stores only useful application fields and compact standard GPS scalars. Arbitrary binary EXIF, MakerNote, PrintImageMatching, and unknown vendor blobs must not be persisted or hex-encoded. Image and video thumbnail caches use 320px JPEG output at quality 50 with independently versioned provenance. SQLite compaction is an explicit maintenance operation (`archive-index compact <workspace>`), not part of every Re-index. The schema is version 23 and configuration version 5; metadata component version 5, strict visual-feature version 4, and reconciliation version 3 invalidate only their own derived state. Thumbnail versions remain `pillow-jpeg-v2` / `ffmpeg-center-frame-jpeg-v2`. Visual-feature JSON remains unchanged for now; large numeric embeddings use float16 BLOB storage rather than JSON text, persisted semantic projections retain source-run provenance, and transparent ZIP/unZIP of inactive indexes remains deferred.

Phase 8A is implemented. Workspace setup performs a read-only filesystem analysis before creating an index, presents a persisted configuration plan, and applies configuration only after explicit confirmation. Folder rules use exact direct-folder semantics; legacy ancestor rules are materialized to explicit physical folders during configuration migration. Supported formats are category-level configuration rather than per-extension UI. Excluded files remain indexed rows with `in_scope = 0`, are not treated as offline while they remain observed, and are omitted from gallery, media processing, grouping, recommendations, folders, and safe media serving. A later scan may still mark an excluded row offline when the source is absent; re-including it preserves that missing state until the source returns. The planner never hashes, decodes, reads EXIF, invokes ffmpeg, or creates `.archive-index`; reparse points and the app-owned state directory are skipped. New-folder Apply & index starts the existing persistent job pipeline, while indexed workspaces can be reconfigured without restarting the server. Phase 8A does not add fake future controls or a second job/configuration system.

Phase 8B is implemented. Reconciliation uses a versioned exact-byte SHA-256 relationship and a conservative same-stem RAW/JPEG relationship with timestamp/camera corroboration and explicit ambiguity conflicts. It merges only safe exact duplicate logical assets, preserves manual decisions, and keeps RAW plus rendered files as separate physical representations of one logical asset when the conservative pairing rule succeeds. The active reconciliation run is persisted and independently rebuildable; grouping and recommendation provenance are invalidated when logical assets merge. Preferred representation and component-aware failover are used for gallery thumbnails, details, and issue presentation. Filyos 3 validation consolidated 1,183 unambiguous ARW/JPEG pairs from 2,385 physical files into 1,202 logical assets, recorded 1,183 RAW/JPEG relationships, found no exact-duplicate families or ambiguity conflicts, and rebuilt cached grouping/recommendation state without source decoding. Grouping remained 356 groups (331 multi-image, 25 singleton; largest 22), and the reconciliation, cached feature/group, and recommendation jobs each completed in about one second or less at the displayed timestamp resolution. Exact duplicate and RAW/JPEG relationships remain organizational metadata, not strict-group membership or recommendation logic. RAW development decoding and HTTP Range support for large video seeking remain deferred.

Phase 7.5 quality is an optional provider choice stored per workspace. The active learned provider is the official LAR-IQA two-branch MobileNetV3-Large + KAN checkpoint (`lar-iqa-2branch-kan`, `AIM_Training_2branche_KAN-Head.pt`, SHA-256 `70c243d7324c76df43df8ab6a44eb535ee9f4f3acb928e5dfe9deb2bb3b7b0ab`) with the upstream RGB resize-384 authentic branch, center-crop-1280 synthetic branch, ImageNet normalization, and merged scalar output. Its versioned raw output and canonical [0,1] score are persisted without the old handcrafted component scores; this image-quality component remains image-only. The checkpoint is installed explicitly with `archive-index model install lar-iqa` into the user model cache, never into a repository or workspace, and inference dependencies are optional (`uv sync --extra quality-lar-cpu` or `uv sync --extra quality-lar-cuda`). The two learned-quality extras use explicit official PyTorch indexes and are mutually exclusive; the CUDA extra pins the official Windows CUDA 12.8 wheels. Quality changes invalidate only quality state; recommendation/grouping state and manual decisions follow their existing provenance rules.

Phase 8C adds a separate video-quality component. In-scope online videos are sampled at uniform bin centers using `N = clamp(ceil(duration_seconds * target_fps), min_frames, max_frames)`, defaulting to 2.0 samples/s with bounds 2 and 32. Frames are extracted transiently through ffmpeg, downscaled to at most 1920px, oriented, scored through the existing LAR-IQA provider, and aggregated as the arithmetic mean of the highest-scoring 25% of successful frames. A result is complete/partial only when at least 75% of requested samples succeed; otherwise the video component is failed without an active result. Video sampling, aggregate, provider, checkpoint, duration, and input fingerprint provenance are versioned independently from image quality, visual features, grouping, and recommendations. Videos never enter image strict groups; current Phase 9.3A video singleton decisions may still be recommendation candidates when video quality is enabled. The initial Filyos 3 validation processed 19 videos and 451 sampled frames with no failures in 854.2 seconds. Phase 8C.1 changes sampler provenance to version 3 and uses one bounded ffmpeg session with transient PNG files for videos up to 30 seconds; videos longer than 30 seconds use cancellable per-frame input seeking because full sequential decode was slower on the measured 50.5-second clip. The one-session path measured 25.8 seconds for 32 frames versus 53.2 seconds with per-frame extraction on a 15.5-second clip, and 9.0 versus 17.5 seconds for 10 frames on a 4.5-second clip; the long 50.5-second clip measured 73.0 seconds one-session versus 53.8 seconds per-frame, motivating the threshold. The full v3 validation completed all 19 Filyos videos and 451 samples in about 511 seconds with 451 successful samples; 355 samples retained absolute ffmpeg timestamps and the 96 long-video input-seek samples deliberately retained no absolute timestamp. Mean absolute score drift against the earlier sampler was about 0.000011. The decode audit found all 19 Filyos clips are HEVC Rext, 3840×2160, `yuv422p10le`, approximately 119.88 fps, 280.6–286.8 Mbps, with approximately 1.001-second keyframe spacing. The installed FFmpeg exposes CUDA/NVDEC and `hevc_cuvid`, but the exact 4:2:2 10-bit stream fails forced CUVID decoding with `CUDA_ERROR_NOT_SUPPORTED`; generic CUDA mode falls back to native decoding. A raw RGB pipe was only 12–13% faster on the short test clips and about 3% faster on the 50.5-second clip, and differed from the established PNG/Pillow conversion by up to 4/255, so no raw-pipe or hardware-decoding change was retained. Cached completed video runs continue to skip ffmpeg and inference. HTTP Range support for large video playback/seeking remains a deferred viewer concern.

Phase 7.6/7.7 indexing performance keeps the public Re-index workflow but runs metadata/thumbnails and quality as independent jobs. Learned quality is lazy on cached runs, uses an automatically selected batch size of 16 on CUDA or 4 on CPU, uses two CPU preparation workers by default and eight on CUDA, and keeps one bounded preparation/model session for the quality stage. Component-state preparation is bulk-loaded and quality, media, and visual-feature results are persisted in bounded batches without weakening WAL, FULL synchronous mode, or foreign-key enforcement. Metadata/thumbnails and visual features use bounded twelve-worker CPU pools by default, with CLI overrides; serial and parallel feature rows and group membership are identical in validation. An internal timing recorder reports discovery, hashing, state preparation, media decode/encode/verify/finalization, quality preparation/inference, feature, grouping, and recommendation stages. The final 128-image Kadıköy decode benchmark measured median 5.80/5.27/5.06 s at 8/12/16 workers; twelve workers was retained because sixteen added only a noise-sized improvement with higher memory. Shared 320px decode for thumbnails and 160px grouping features was measured but rejected because it changed cached features, despite unchanged membership on the tested subset. Provider Off preserves valid cached quality and now skips the quality state pass when no transition is required. The current 0.70 recommendation threshold produced 297 recommendations across 456 Kadıköy groups and 49 across 70 Filyos groups. CUDA quality has since been validated on the RTX 3070 Ti Laptop GPU with `torch 2.11.0+cu128`, `torchvision 0.26.0+cu128`, CUDA 12.8, and the expected device available. An operational run on a 2,385-asset workspace, including about 1,170 images requiring quality processing, completed in just under four minutes with the quality stage averaging about 14 images/s; this was not a controlled repeated benchmark. The CPU/CUDA extras are mutually exclusive environment choices: switching them changes the PyTorch wheels in the project environment, while the checkpoint remains in the persistent user model cache and is not downloaded on each launch.

Kadıköy storage validation on 1,369 images measured 124.62 MiB SQLite, 31,902 pages, 2,821 freelist pages, 102.47 MiB metadata JSON, 17.03 MiB thumbnails, and 146.10 MiB total `.archive-index` before cleanup. After the metadata/thumbnail rewrite, metadata was 0.88 MiB (675-byte average, 690-byte maximum), thumbnails were 8.34 MiB (6,388-byte average, 6,423-byte median, 14,601-byte maximum), SQLite was still 124.62 MiB with 28,799 freelist pages, and the total index was 137.44 MiB. After explicit compaction, SQLite was 11.88 MiB with 3,042 pages and zero freelist pages; the total `.archive-index` was 24.71 MiB and `PRAGMA integrity_check` returned `ok`.

The initial LAR-IQA validation on the real workspaces produced scores for all 1,369 Kadıköy images and all 171 Filyos images with no failures. Kadıköy: min/median/mean/max `0.497/0.707/0.711/0.890`, p10/p25/p75/p90 `0.624/0.656/0.766/0.804`, and Spearman correlation with the former Pillow baseline `0.458`. Filyos: `0.500/0.716/0.706/0.798`, p10/p25/p75/p90 `0.633/0.677/0.745/0.762`, correlation `0.388`. CPU quality-only processing with cached features used the shared persistent job path and measured about 686 s Kadıköy and 89 s Filyos for the first run, then 6.6 s and 1.2 s cached reruns that reopened no source pixels; the official checkpoint was not silently downloaded.

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

## Phase 7.5 — optional learned image-quality provider

This phase is implemented. New and migrated workspaces default to LAR-IQA quality scoring, while the application remains usable without the neural dependencies when quality is explicitly turned off or unavailable. The mutually exclusive `quality-lar-cpu` and `quality-lar-cuda` extras run the official LAR-IQA two-branch MobileNetV3-Large + KAN inference path with fixed upstream preprocessing and a versioned checkpoint hash. Quality persists raw model output and a canonical [0,1] score; old Pillow component scores are retained only as a benchmark/migration helper. Videos have no quality score. Provider/version changes invalidate only quality state, and any downstream recommendation refresh preserves manual decisions.

The checkpoint is installed explicitly with `archive-index model install lar-iqa` into the user model cache. It is not downloaded during ordinary indexing and is never copied into a workspace or repository. A future quality pass may evaluate calibration, mobile deployment, or a smaller model; Phase 8 must not assume LAR-IQA is the final model.

---

## Phase 8A — workspace planning, configuration, and scoped indexing

This phase is implemented as the current production setup flow. The home screen can analyze a user-selected folder without creating state, show a read-only plan with supported file counts, approximate size, and a rough hardware-dependent estimate, then Apply & index the selected configuration. Existing workspaces expose the same flow through Configure. Versioned configuration is server-validated and persisted separately from the canonical media rows; changing scope updates `physical_file.in_scope` transactionally and preserves source files, cached rows, manual decisions, and unrelated component state. Normal indexing, gallery queries, grouping, recommendations, folder filters, and media routes consume only in-scope files. Duplicate/RAW-JPEG reconciliation is implemented in Phase 8B; semantic embeddings/search is implemented in Phase 9.

## Phase 8B — exact duplicates, RAW/JPEG reconciliation, and representation failover

This phase is implemented. Exact duplicate families are identified only by equal media bytes and are merged when manual selected/rejected decisions do not conflict; the physical files and an `exact_duplicate` relationship remain available. RAW/JPEG pairing is limited to one RAW and one rendered image with a casefolded same stem, compatible timestamp semantics within five seconds or a unique same-stem fallback when no timing exists, and no conflicting camera identities. Ambiguous candidates become persisted conflicts instead of being merged. Videos are excluded. Reconciliation is transactional, independently rebuildable, cancellable, and versioned; the previous active result remains in place until a successful rebuild is ready. Physical representation details, relationship labels, preferred-file selection, and component-aware failover are available through the existing detail/gallery API. No source media is modified. HTTP Range support for large video playback/seeking is a deferred video-viewer concern, not a Phase 8B blocker.

---

## Phase 8C — video frame sampling and learned video quality

This phase is implemented. Video quality is a separate, optional enrichment path built on the existing persistent job engine and LAR-IQA provider. It uses bounded uniform frame sampling, transient ffmpeg extraction, per-frame normalized quality rows, top-quartile mean aggregation, independent provenance/invalidation, resumable sample runs, partial results only at the documented 75% success threshold, and no image-grouping or recommendation side effects. Setup persists the sampling controls and Details reports requested/successful samples. Phase 8C.1 uses one ffmpeg process per short/medium video and a cancellable per-frame seek fallback for long videos; sampler version 3 identifies this choice and actual timestamps are recorded only when absolute source timestamps are available. HTTP Range support for large video playback/seeking remains a deferred viewer concern.

## Phase 8D — separate indexing scope and quality controls

This phase is implemented. Workspace configuration now distinguishes rendered-image scope, RAW scope, video scope, rendered-image quality provider, RAW-only quality provider, and the video-quality enabled toggle. Existing configuration aliases remain readable for migration, but new UI/API state uses the separate fields. Rendered-image LAR-IQA quality remains independent from optional RAW-only quality: RAW-only assets use an optional `rawpy` embedded preview when available, while paired RAW representations remain `not_requested` because the rendered representation is the quality source. RAW preview thumbnails use their own versioned provenance and do not imply full RAW development support. Missing RAW preview capability is reported as unsupported per file and does not abort the rest of indexing.

The existing persistent job engine now exposes separate raw-quality and video-quality jobs alongside media, reconciliation, feature, grouping, recommendation, and embedding jobs. Provider-off state transitions preserve valid cached scores and do not present unsupported files as pending work. Configuration migration is schema version 16; changing scope or one quality control invalidates only the affected component/downstream state. The optional `raw-preview` and `embeddings` dependencies are locked separately from the base environment, while the LAR CPU/CUDA extras remain mutually exclusive. Full RAW decoding/development, automatic archive compression, OCR, faces, and semantic clustering remain deferred.

Phase 8D real-file validation found 171/171 Filyos ARWs with 4240×2832 embedded previews. A 100-pair diagnostic used 8.669 seconds for preview extraction (11.54 images/s), 7.569 seconds for RAW-preview LAR scoring (13.21 images/s), and 5.954 seconds for paired JPEG scoring (16.80 images/s); RAW-preview versus JPEG scores had mean absolute difference 0.022232, median difference 0.023634, and Spearman correlation 0.939310. No duplicate RAW scores were persisted for paired assets.

## Phase 9 — semantic image embeddings and search

This phase is implemented. The current product supports only OpenCLIP ViT-B/16 DataComp XL and Google SigLIP2 Base Patch16 224 through the optional `embeddings` extra. Models are explicitly installed in the global user cache; indexing and search never download them. Search is off by default, image vectors are one per logical asset, video vectors are per shared sampled frame with MAX-frame retrieval, and exact cosine search uses normalized float16 SQLite BLOBs. The model/version/run and provider-specific component state are independently persisted and rebuildable. The setup UI, semantic text search, and image `Find similar` action use the existing workspace-scoped gallery/search API. No ANN index, embedding clustering, or automatic selection behavior uses semantic vectors.

### Phase 9.1 throughput pass

Image embedding preparation now uses a bounded thread-pool producer with a maximum four prepared batches buffered ahead of a single persistent provider/model consumer. CPU source decode and the provider's unchanged preprocessing overlap batched GPU inference; the main thread remains responsible for inference, normalization, float16 conversion, and SQLite persistence. The selected default is 8 preparation workers and batch size 16 after a real Kadıköy sweep: 1/4/8/12 workers measured approximately 4.1/11.0/11.7/11.7 images/s on a fixed 128-image subset after excluding model initialization from the parallel runs. The 8-worker 500-image run measured 42.6 seconds processing (11.75 images/s) for OpenCLIP and 46.7 seconds (10.70 images/s) for SigLIP2, versus the accepted serialized baselines of 108.3 and 112.5 seconds. Preparation work remains CPU/disk bound; summed decode and preprocessing timings exceed wall time because they overlap. The rough semantic-index ETA was updated to 0.10 seconds per vector. Cached runs still skip model initialization, source decode, preprocessing, and inference. A serialized-versus-pipelined comparison on 128 real images produced cosine >=0.99999988 after stored float16 round-trip and identical top-10 results for five representative queries for both providers. Failure-isolated preparation, cancellation, partial-run reuse, and RAW-preview embedding paths remain covered by tests. No preprocessing semantics changed. Video decoding/sampling overlap remains explicitly deferred.

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

## Phase 9.2 — Workspace browser UX/search polish

This phase is implemented. The accepted production interface remains a standard-library localhost server with vanilla HTML/CSS/JavaScript. Do not start Phase 10 or replace the current search/model architecture without evidence. The following decisions describe the accepted browser shell and search behavior; later Phase 9.3A Configure/runtime changes are recorded below.

### Workspace shell

- Keep the fixed top app bar visible while scrolling.
- The left side contains `Archive Indexation` (return to the workspace/home menu), the current workspace name, and a small Explorer action for the workspace root.
- The center navigation is `Gallery | Groupings | Geo Map | Timeline | Vector Cloud`.
- The right side contains `Configure` and `Re-index`.
- Remove the top-level Problems button and move diagnostics into Configure.
- Remove the separate workspace asset-count/path line below the app bar.
- Opening or switching workspaces must not require restarting the server.

### Sidebar and unified search

The left filter sidebar is fixed during Gallery, Groupings, Geo Map, Timeline, and Vector Cloud browsing, collapsible with a small reopen control, and preserves its state and each view's scroll position across view changes, sidebar toggles, and viewer/details dialogs.

Use one unified search field for filename and semantic search. Filename matching updates immediately; semantic search starts after roughly 500 ms debounce. Filename matches rank ahead of semantic-only matches and ignore the semantic threshold. Show a semantic score when one exists, but never invent one. Typing switches sorting to Search descending; clearing the field returns to Time descending. Filename search continues to work when embeddings or the model are unavailable. The helper below the field should report states such as ready/provider, searching, result counts, re-index required, model missing, or failure with compact success/active/error colors.

The semantic threshold is a persisted UI setting from 0.0 to 1.0, defaulting to 0.20, and applies only to semantic results. Sort uses buttons for Time, Quality, and Search plus one ascending/descending arrow. Search descending orders filename matches before higher similarity; ascending reverses that ordering. Empty search behaves as Time sort.

Folders are represented by a compact summary row. Clicking it opens a workspace folder-tree modal with checked folders, nested selection, intuitive parent/child and indeterminate behavior, and bottom-right Select all/Deselect all actions. The full tree does not remain permanently in the sidebar. Also provide compact filters for Auto Review (`All`, `Representative`, `Recommended`), Manual Review (`All`, `Undecided`, `Selected`, `Rejected`), Type (`All`, `Images`, `Videos`), and Layout (`All`, `Horizontal`, `Vertical`), plus one Clear filters action.

The recommendation threshold is a persisted 0.0–1.0 control with the current baseline at 0.70. Adjusting it recomputes derived recommendations immediately without re-indexing. It remains at most one recommendation per image group and still requires the representative to meet the threshold. Manual decisions are independent and must never be changed by thresholds, filters, search, or automatic processing.

### Gallery and review actions

Replace explicit pagination and page-size controls with continuous/infinite scrolling that loads ahead of the viewport while keeping the rendered DOM bounded/windowed. Use square thumbnails and always show a compact capture timestamp. Search metadata and tags appear below thumbnails, never over the image. Do not show JPEG/format tags. Representative, Recommended, Filename match, and Similarity metadata are conditional; no active search means no search tags.

Every card always has `Select | Reject` actions. Undecided actions are muted; Selected and Rejected use saturated green/red active styling, desaturate the opposite action, and toggle back to Undecided when clicked again. Clicking the opposite action changes the decision directly. Manual Selected/Rejected state is not represented as a separate gallery metadata tag.

Contextual empty states should be centered and explain the active condition, such as no media matching filters, no results above the semantic threshold, embeddings not indexed, or no selected videos in the chosen folders.

### Viewer, Find Similar, and details

The fullscreen image/video viewer keeps consistent controls with centered Select/Reject actions. Image actions are `Find similar`, `See group`, `Show in Explorer`, `Info`, and `Close`; existing zoom, Smooth, keyboard navigation, outside-click, and safe media-serving behavior must remain intact.

Find Similar must not replace the normal Gallery. It opens a contextual similar-images section from the viewer, initially showing six highest-ranked results and adding six per Load more. Show raw similarity below each result, exclude the source, preserve filters, and use a separate internal image-similarity messaging threshold rather than the text-search threshold. The first six ranked results are shown regardless of that messaging threshold.

Details remains a modal. Its thumbnail opens the viewer; keep the path text plain and use the fullscreen toolbar or compact per-representation Explorer actions. Order sections as Overview, Technical details, Overall technical quality, and compact Representations. Do not show `Source: rendered image`, raw JSON, or alarming representation-level RAW warnings when a preferred JPEG is healthy. Representation rows should show media type, filename, size, preferred status, Explorer action, and muted paths.

Technical details order is Camera, Lens, Focal length, Aperture, Shutter speed, ISO. Measurement tracks are visual and non-interactive, with ticks, markers, helper labels, and logarithmic context. Focal-length display context ends around 600 mm; values above it clamp visually with a red overflow marker while retaining the accurate numeric value.

### Groups and Configure

Groupings keep singleton groups visible. Videos behave as singleton groups; do not display an unsupported-video-grouping warning or add multi-video grouping. With Type = All, image groups and singleton video groups coexist under the selected sort/filter state; Type = Videos shows one singleton group per video.

Configure is the home for diagnostics and semantic model management. The normal Configure workflow exposes only the active OpenCLIP ViT-B/16 DataComp XL provider with installed/not-installed state, model size, explicit install, and embedding readiness. Legacy SigLIP2 runs remain readable and reusable internally, but SigLIP2 is not a normal Configure choice. Installation remains explicit with no silent download. If embeddings are absent or stale after installation, say `Re-index required` and provide an obvious re-index path.

### Search performance and progress

Phase 9.3A provides the shared provider/model session for semantic queries and embedding indexing. Phase 9.3B adds a bounded active embedding-matrix cache keyed by workspace, provider, active run, dimension, and browser-visible generation; it stores normalized fp32 matrices and keeps exact cosine/MAX-frame ranking. The cache is bounded to two workspace entries and is released when the semantic provider session is cleared or replaced.

Long-running jobs should expose the current sub-stage, current/total work, percentage, rate, elapsed time, and meaningful ETA. Embedding progress counts vectors: one JPEG is one vector and a sampled video contributes one unit per frame. When video quality and semantic video embeddings are both pending, Phase 9.3B shares one bounded transient decoded-frame pass while retaining independent jobs, runs, component states, caching, resumability, and cancellation.

Phase 9.3B scaling validation used synthetic 10k/50k/100k production-schema fixtures. The browser cold request improved from the pre-optimization 4.01/98.90/505.68 seconds to 1.38/6.99/14.36 seconds; warm requests were 0.15/0.78/1.45 seconds. Semantic cold/warm queries were 0.38/0.11, 2.11/0.43, and 4.35/0.94 seconds. The cached semantic allocation was 21.8/108.4/216.6 MB current at 10k/50k/100k, with 56.8/284.8/569.3 MB traced peaks; the 100k fp32 matrix itself is about 205 MB. Browser catalog memory remains about 53/266/531 MB peak. These are local comparison measurements, not CI thresholds. The remaining scaling limits are the bounded Python catalog/matrix memory and exact full-matrix ranking; no ANN index or external cache service is used.

The real Sony `ODA8_7563.MP4` validation clip is 3840×2160 HEVC `yuv422p10le` at approximately 119.88 fps. With 19 configured samples, two CPU FFmpeg decode passes took 28.425 seconds and one shared pass took 14.294 seconds. With installed LAR-IQA and OpenCLIP inference on the RTX 3070 Ti Laptop GPU, the shared run measured 14.536 seconds decode, 1.368 seconds quality inference, and 0.446 seconds embedding inference, 16.350 seconds total; the equivalent separate-pass total was about 30.239 seconds. No sampled-frame directory is persisted. GPS Details now exposes one valid logical-asset coordinate pair and a tiny external Google Maps link without reverse geocoding or map embedding.

### Deferred backlog

Record but do not implement in Phase 9.2: Phase 10 compressed representations in the Representations section; synchronized original-vs-compressed zoom comparison; lightweight RAW-preview exposure adjustment; and deeper hardware/decode adaptability work.

---

## Phase 9.2 correction contract (2026-09-16)

Phase 9.2 is implemented in the working tree. Its fixed shell, shared sidebar, unified filename/semantic search, exact cosine ranking, windowed square Gallery, independent manual review, contextual Find Similar (up to twelve strong results initially), and singleton video groups remain the accepted architecture. Phase 9.4 adds the three read-only visualization views described below. Phase 10 has not started.

- Search distinguishes installed/available from loaded/ready. A single background semantic worker initializes only the active provider and handles query encoding/ranking. Opening a workspace starts non-blocking preload of its configured active provider, including before embeddings are indexed; query requests share that initialization. Missing models are never downloaded automatically. Loading, Searching, results and failures are visible states. Replaced browser requests are aborted and stale responses do not overwrite newer input. A 40 ms response budget returns fast warm queries directly; longer work is polled without holding an HTTP request through model initialization.
- The active semantic provider remains resident for the app/server session without idle eviction. Changing provider or disabling semantic search queues release of the old model and unused CUDA memory on the same worker before loading any replacement. An in-flight initialization finishes before release; it is never duplicated. Provider selection is app-wide, following the opened/configured workspace. Ranking and text-vector caches remain bounded. Process exit releases the remaining model. Readiness remains truthful while preparing, searching or failed.
- Browser catalog summaries and filter rankings are cached separately. A locate target is not part of the filter signature. Previously rendered Gallery/Groupings windows are retained when filters, window and data revision match. Decisions, threshold changes and observed database revision changes invalidate their display keys. Per-view scroll restoration remains exact; See group preserves filters/query and uses immediate target positioning.
- Folder selection stores real workspace-relative folder paths only, including the empty path for files directly in the workspace root, with sorted query serialization. Every folder checkbox is an independent exact-folder filter; the visible tree is for readability only. Selecting all normalizes to the all-folders state; equivalent click histories must give the same selection and singular/plural summary. All/Deselect all are the only global operations. Folder changes apply live; X, backdrop and Escape close the overlay.
- Clear filters explicitly resets both thresholds (semantic 0.20, persisted recommendation 0.70), search, sorting and every sidebar filter. It never writes manual decisions.
- Gallery Details is a central modal. Fullscreen Info is a right-side drawer using the same detail renderer; it keeps the image/video visible and remains usable while navigating. Opening/closing Info must not replace, pause or restart the media element.
- Before replacing or closing fullscreen media, pause every current video, remove its source and reset it. Native close, Escape, media navigation and page teardown must silence old media. A detached old media error must not replace newer content.
- Technical scales are non-interactive, horizontal, logarithmic/contextual tracks with sparse non-wrapping labels separated from ticks. Actual values remain in the value column; overflow clamps the marker and colors it red. Validate both the central modal and narrow fullscreen drawer.
- Representation rows are compact and padded, with outlined Explorer buttons. Healthy rows have no diagnostic disclosure. Expected unsupported RAW metadata/thumbnail decoder states stay compact and hidden; representation-specific warnings retain an expandable status affordance for genuine requested failures.
- Semantic planning reports total image candidates, video-frame units and vectors separately from pending/reusable vectors. Storage/time estimates describe pending work. Reuse requires the matching provider/version/settings, source fingerprint, component completion and stored vectors. The Configure plan endpoint must include these fields. Before metadata/reconciliation, image counts are provisional and unknown video durations are explicitly reported rather than invented.
- Reconciliation v2 treats an already-unified RAW/JPEG family as reconciled, preserves its relationship and does not emit an ambiguity. The old bug recorded same-asset ambiguities with a misleading manual-conflict message. Current diagnostics exclude these resolved/self records and manual conflicts whose two current explicit decisions no longer conflict. Real ambiguous candidate families and Selected-versus-Rejected conflicts stay actionable; never overwrite decisions to remove a warning. Existing invalid records can remain historical until an explicit reconciliation replaces the run.
- Model installation checks the installed provider against the compatible active embedding run and current source state. Existing compatible vectors show Ready; absent/stale/inactive vectors offer Re-index. Installation never starts indexing.
- Native browser observation: original Filyos 6 `ODA8_7605.MP4` played in the Windows in-app browser on 2026-09-16. ffprobe identifies HEVC Rext, `yuv422p10le` (4:2:2, 10 bit), 3840×2160, 120000/1001 fps (~119.88). This is original Sony camera footage, not an edited H.264 export. It does not establish universal browser support or change the earlier FFmpeg/NVDEC decoding findings. No transcoder/codec dependency was added.
- Browser search keeps a fixed status/count slot: semantic preparation and search state never replace the media count, and filename matching remains available immediately when semantic search is disabled or still warming. The count is visible media over total in-scope workspace media; filename-match wording is shown only for an active query.
- Recommendation-threshold changes re-filter the cached browser catalog from stored representative and quality data. Sidebar collapse/expand only changes layout and reflows the existing bounded gallery window; it does not fetch or rebuild the catalog.
- Show similar images stays in the fullscreen viewer's vertical scroll, reports all matches at the 0.90 strong-similarity cutoff, starts with up to twelve highest-ranked strong results, and provides Load more for lower-ranked alternatives before appending automatic pages without evicting earlier results. Similarity SQL filters are chunked below SQLite's variable limit, and a completed indexing job must not leave metadata, thumbnail, or quality component rows in `pending` or `running`; persistence failures become explicit component failures and remain recoverable on re-index.

---

## Phase 9.3A — Configure workflow, shared embedding runtime, and paired RAW processing

Phase 9.3A is implemented in the working tree. Configure is a plain-language workflow with exactly four setup decisions: Index these folders; Automatically assess media quality; Let me search by semantic content; and conditional Video participation. All supported media in selected folders is indexed automatically; per-extension and media-category indexing controls are not user-facing. Diagnostics is a separate troubleshooting footer. Configure hides the workspace visualization navigation while open, keeps matching Index/Re-index actions at the top and bottom, and shows planner-backed selected/reusable/pending counts plus feature ETAs. Folder rules apply to direct files in each folder, with legacy inherited selections materialized during migration. The normal Gallery folder popup uses indexed direct-media counts and does not traverse the filesystem when opened. Details omit intentionally disabled quality blocks; current diagnostics filter resolved component failures while retaining historical records internally. Quality and semantic model cards share one readiness/install presentation, and setup Cancel has restrained destructive styling.

The persisted configuration is version 5 with additive schema migrations 20–23. New workspaces select all supported media, enable media quality and semantic search by default, and use OpenCLIP B/16 DataComp XL by default; model installation remains explicit and never occurs during indexing or search. Legacy per-extension/category fields remain readable internally but normalize to all supported categories enabled; folder rules are the only indexing-scope decision. Configure exposes one global quality decision, one global semantic-search decision, one conditional video-participation decision, sampling, and exact-folder choices. Global quality covers rendered images, RAW-only embedded previews, and selected video quality; paired secondary RAW work is not duplicated. Video sampling controls are visible only when video participation is enabled. LAR-IQA readiness shows provider/model, installed state, checkpoint size, workspace ETA, and an explicit install action. Legacy SigLIP2 vectors remain untouched and reusable but are not selectable in normal Configure.

`embeddings.runtime.EmbeddingRuntime` owns the active provider lifetime for both semantic search and embedding indexing. Only the selected provider is resident, concurrent initialization is shared, inference is serialized through one runtime lock, provider switches release the old provider before loading the new one, disabling semantic search releases it, and a failed load can be retried after explicit installation. Stored vectors for either provider are not deleted. Focused tests verify one provider construction/preflight across prewarm, search, and indexing.

Metadata and RAW/JPEG reconciliation now complete before final representation-specific thumbnails. A healthy paired RAW uses the rendered representation for Gallery thumbnails, image embeddings, normal image quality, and strict-group visual features; its secondary RAW derived work is marked `not_requested` when intentionally skipped. RAW-only assets use their embedded preview for thumbnails, quality, embeddings, and strict-group visual features. Mixed local/absolute capture-time evidence is accepted only with matching camera evidence and matching wall-clock time. A structured RAW lens is normalized to its model/name, never an object representation. If the rendered member is unavailable, the RAW member is selected for required derived work. Valid cached derivatives are preserved, and metadata needed for reconciliation is still collected. Videos remain singleton representatives and can be recommended when requested video quality meets the current threshold. Details omit processing-only video sample counts and representation type prefixes; the independent fullscreen quality overlay is transparent.

Phase 9.4 now provides `Gallery | Groupings | Geo Map | Timeline | Vector Cloud`. Future Groupings should offer `Strict | Broad`, retaining current conservative near-identical grouping as Strict and reserving looser clustering-oriented organization for Broad. The later Similar-viewer navigation redesign and comprehensive responsive-layout, viewport, browser-zoom, and eventual mobile pass remain follow-up UX work; they are not part of Phase 9.4.

### Deferred follow-up notes

- Phase 10 image compression must first validate an actual `libjxl` runtime for decoding and eventual encoding. JXL files need explicit app-owned provenance and source/derivative relationships; filename or extension inference is not sufficient. External JXL files and their reconciliation behavior require a separate investigation before implementation.
- ETA estimates should later be calibrated from machine-local completed-job throughput and compute characteristics rather than treated as universal constants.
- Preserve future `Strict | Broad` grouping, the Similar-viewer navigation redesign, and comprehensive responsive/viewport/browser-zoom/mobile work as deferred roadmap items.

## Phase 9.3C — browser regression safety net, low-cost CI, and modularization

Phase 9.3C is complete. `tests/e2e/test_browser_e2e.py` contains thirteen Playwright Chromium tests using temporary isolated workspaces and registries, tiny Pillow fixtures, controlled SQLite semantic state, and the real localhost server. The suite does not use `Early Testing/`, external services, native folder pickers, GPU, ffmpeg, or model downloads. Failures preserve screenshots, browser logs, and traces under ignored `test-results/e2e`.

GitHub Actions is defined in `.github/workflows/ci.yml`. It uses one standard `ubuntu-latest` job with `contents: read`, a bounded timeout, concurrency cancellation, Python/uv caching, test-only dependencies, Node only for syntax checks, and Chromium only. It has no secrets, cron, paid or premium runners, Windows runners, GPU, model/checkpoint downloads, or external services. Browser artifacts upload only on failure. Windows-native folder-picker and Ctrl+C/`cmd.exe` behavior remain local/manual smoke coverage.

Backend boundaries are:

- `api/server.py`: localhost HTTP wiring, route dispatch, resource serving, and orchestration adapters.
- `api/workspaces.py`: registry, Home, setup/planning, workspace removal, and offline helpers.
- `api/browser.py`: browser-facing asset summaries, physical representations, and details.
- `api/search.py`: search readiness, semantic query, Similar, and filter response services.
- `api/jobs.py`: indexing, re-index, reconciliation, embedding, grouping, and recommendation job orchestration.

Frontend uses ordered native browser scripts without a framework or bundler:

- `app-shared.js`: state, API, shared formatting, review, and card utilities.
- `app-setup.js`: Home and Configure/setup workflow.
- `app-browser.js`: workspace bootstrap, search/gallery data, and virtual gallery.
- `app-viewer.js`: fullscreen viewer and Similar mode.
- `app-details.js`: details, metadata, and quality rendering.
- `app-maintenance.js`: jobs, diagnostics, offline cleanup, and indexing.
- `app-groups.js`: filters, folders, view switching, and Groupings.
- `app-visualizations.js`: local Geo Map, Timeline, Vector Cloud, Canvas interaction, representative thumbnails, and direct viewer opening.
- `app-bootstrap.js`: event wiring and startup.

`app.js` remains a compatibility resource. `app.css` was intentionally left unsplit because it remains manageable and a split would add risk without a clear benefit. Responsive-layout overhaul, broader grouping, and Phase 10 remain deferred.

## Phase 9.4 — archive visualizations

Phase 9.4 is complete. The top-level workspace navigation is exactly `Gallery | Groupings | Geo Map | Timeline | Vector Cloud`. Geo Map, Timeline, and Vector Cloud are read-only views of existing indexed state and use the same shared browser filter service as Gallery and Groupings. The central `loadCurrentView()` dispatcher handles switching, filter changes, semantic-search polling, and post-index refresh. View URLs are `?view=geo`, `?view=timeline`, and `?view=vector`; `gallery` and `groups` remain unchanged. Canvas renderers preserve per-view pan/zoom state while the browser revision and filter signature remain current.

Geo Map uses valid GPS from the preferred active physical representation, then active fallbacks including RAW metadata. Latitude and longitude must be finite and within their normal geographic ranges. It has no reverse geocoding, location names, map tiles, CDN scripts, or runtime map-network calls. The basemap is a 251,749-byte app-owned GeoJSON reduction of Natural Earth 1:110m Admin 0 country boundaries (public domain; source: https://github.com/nvkelso/natural-earth-vector). Geo and Vector use the camera `screen = (world - center) * scale + viewport/2`; pointer zoom solves the center from the world coordinate under the pointer and panning changes the center in world units. Geo uses Web-Mercator coordinates, stable world cell keys, deep-zoom subdivision through LOD 24, screen-space marker thumbnails, and a local metric grid whose reference latitude is fixed on fit/load so panning cannot change meters-per-cell. Exact same-coordinate groups use the existing fullscreen viewer sequence, ordered by capture time then asset ID, and start at the quality-first representative; ordinary spatial cluster badges retain zoom behavior. Representative thumbnails use highest finite quality with asset-ID tie-breaking.

Timeline has Capture time and File created time modes. Capture mode positions assets using deterministic UTC numeric coordinates built from the stored displayed date/time components. It intentionally does not reinterpret an EXIF-local-unknown timestamp as UTC, and explicit offsets do not shift the visible position. File created mode uses the persisted file-created time of the preferred active physical representation and uses the same displayed-component coordinate rule. It uses a one-dimensional `centerTime`/`visibleSpan` camera, adaptive second-to-year/calendar intervals targeting approximately 92 pixels per density bucket, sparse globally anchored occupied maps, visible-range bins with kernel overscan, bounded LOD caches, Gaussian smoothing over adjacent logical bins, and one representative thumbnail per nonempty bucket positioned at its actual time. Thin stems connect representatives to the axis; vertical position has no semantic meaning. Thumbnail footprint is included in the camera bounds so edge points remain visible. Pointer zoom preserves the exact time under the pointer; horizontal drag is clamped in time units. Neighboring coarse/fine intervals crossfade with smoothstep while preserving representatives by asset ID. Each time mode keeps its own transform, and point selection opens the existing fullscreen viewer directly; Viewer Info reuses Details. Count badges are shown only for grouped representatives and refine on click.

Vector Cloud uses persisted schema-23 projection provenance: `semantic_projection_run`, `semantic_projection_point`, and `workspace_semantic_projection`. The active run records source embedding run, `pca-2d-v1`, version, settings, asset count, asset-set fingerprint, status, and activation time. Projection builds are atomic and failure-isolated. Images use one stored logical-asset embedding. Videos use active frame embeddings, L2-normalize each frame, arithmetic-mean the frames, and L2-normalize the mean to create one visualization vector; existing MAX-frame semantic search is unchanged. PCA centers float32 vectors, fits on all assets or a deterministic SHA-256 asset-ID sample capped at 10,000, stabilizes component signs by the largest loading, transforms all assets, and stores finite coordinates only. Missing or stale projections explain that Re-index rebuilds them from stored embeddings; opening Vector Cloud never loads a model or decodes source media.

Vector Cloud uses the same world camera and a cached approximately 512×512 world-anchored Gaussian density texture keyed by filtered data revision, with sqrt normalization and no semantic axis names or fake clusters. The density raster is not rebuilt during pan or zoom; representative thumbnails are drawn at their own PCA coordinates, not cell centroids. Stable integer x/y/LOD cells are cached by data/filter/revision, visible cells are selected by world-range iteration, neighboring levels crossfade with smoothstep, and LOD derives from rendered thumbnail footprint rather than viewport size. Final display grouping is two-stage: a globally anchored candidate cell is selected about 0.75 thumbnail long-edge wide, then candidate rectangles are consolidated when their actual aspect-aware rectangles overlap at least 55% of the smaller rectangle. A screen-space hash with bounded neighboring buckets avoids O(N²); each group compares candidates only to its stable anchor, preventing transitive chains. Group counts sum candidate counts, representatives use highest finite quality then asset ID, and grouping is pan-invariant and cached by browser revision/filter signature plus scale/LOD band. Image/video fallback colors remain restrained, grouped count badges refine on click, and visualization points open the existing fullscreen viewer directly. Timeline grouped buckets pass sorted member IDs to that viewer, which requests additional asset details only when navigating. Viewer Info reuses the existing Details side panel; there is no visualization-specific selection panel. All visualization thumbnails use one natural-aspect long-edge rectangle for draw, border, hover, badge anchor, and exact reverse-painter-order hit testing. Render order is stable by finite quality ascending then asset ID, and hover adds only a final border/glow overlay. Geo local metric grids iterate both visible directions from world-zero anchored indices, with one-cell metric scale bars and a fixed fit/load reference latitude. Zoom LOD uses a 420ms absolute last-zoom idle window and a 240ms settle crossfade; panning never restarts or blurs that transition. The Timeline canvas fills the remaining visualization workspace and its lower area remains wheel/pan interactive. The opt-in synthetic benchmarks cover PCA and Timeline/Vector/Geo preparation at 10k, 50k, and 100k assets; the latest local run measured PCA 0.023/0.098/0.189s, Timeline 0.019/0.074/0.118s, Vector LOD 0.004/0.010/0.004s plus display grouping 0.013/0.003/0.004s, and Geo 0.006/0.012/0.029s. No browser paint benchmark was recorded; all three renderers remain Canvas-based with no per-asset DOM nodes. Geo uses a bundled 251,749-byte Natural Earth 1:110m reduction, fades to a local GPS-relative coordinate field with graticule, north indicator, and metric scale bar at deep zoom, and makes no map-network calls. Spatial views are contained in the desktop viewport through a flex containment chain; the filter sidebar scrolls internally and the Timeline fills the remaining canvas surface. Search input uses a 250ms latest-query debounce, immediate clear/Enter handling, generation-checked aborts, staged 275/500ms polling, and full-ranking cache reuse across allowed filters while preserving video MAX-frame semantics. RAF thumbnail invalidations are coalesced per active visualization. JPEG XL remains recognized by indexing and the supported-extension UI but is explicitly in the decoder-gap set: no libjxl, djxl, imagecodecs, Pillow plugin, or native decoder is bundled, so `.jxl` decode remains deferred to Phase 10. Groupings expose stable `Group N` labels for real multi-image groups. The final targeted Phase 9.4 correction pass is implemented and its regression verification is green; Broad grouping, responsive/mobile overhaul, and Phase 10 remain deferred.

## Phase 10 — compression pipeline

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

## Phase 10B — richer organization and exploratory navigation

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

## Phase 11B — video semantic enrichment

Progressively add:

1. video thumbnails/proxies;
2. simple file-level visual representation using sampled frames;
3. semantic search over videos;
4. speech transcription;
5. transcript text search;
6. optional OCR;
7. optional shot detection/timestamp-level results;
8. faces later.

Do not attempt automatic video selection until there is a separate validated design for it. Video quality itself is implemented in Phase 8C and does not currently feed selection or recommendations.

---

## Phase 12B — packaging, lower-end optimization, and mobile exploration

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

- calibration of the LAR-IQA output for downstream subjective usefulness;
- whether a smaller/mobile learned model should supplement or replace LAR-IQA later.

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
