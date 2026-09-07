# Archive Indexation Project

## Purpose

Build a local-first application for personal photo/video archives combining:

1. **Browse and search** — semantic retrieval, metadata, tags, and later OCR/transcripts/faces.
2. **Archive management** — incremental indexing, grouping, quality-assisted image selection, compression, and derived-file tracking.
3. **Portability** — an indexed archive must remain browseable/searchable directly from the archive drive without requiring indexing models to be installed.

The first implementation targets Windows PCs but must not assume the primary RTX 3070 Ti development machine. Lower-end systems are important and eventual fully local mobile use is a long-term goal.

## Primary constraints

- Primary development/testing machine: Windows laptop with RTX 3070 Ti.
- Do not hard-code hardware, camera model, image resolution, archive size, or directory layout.
- Typical processing scope is one year/session of roughly 10–30 GB, thousands of photos, and around hundreds of videos, although a workspace may be rooted above multiple years.
- Full archive is over 100 GB.
- Current media is mostly JPEG/JXL images and MP4/MOV videos; compressed video uses AV1 in MP4.
- Selected originals are untouched phone JPEGs rather than RAW/PNG/TIFF exports.
- Core functionality is local-first. Permanent cloud hosting is not a dependency.
- Every expensive task must be resumable and safe to stop.
- The application itself must never delete source/original files. Manual deletion after verified compression remains a user action.

## Product structure

One application should cover both browsing/search and management, but distinct sections are preferable to a forced continuous workflow. Likely areas:

- Home / workspace selection
- Browse / Search
- Selection
- Index / Process
- Compression
- Problems / Logs
- Settings / Advanced

The app should open to workspace selection rather than silently treating the last workspace as the only default.

## Compute tiers

Compute tiers are a first-class feature.

### Presets

- **Lightweight** — default. Keep compute as low as practical and accept reduced model/indexing quality when necessary.
- **High** — improved models/denser processing while remaining usable on ordinary consumer GPUs rather than datacenter hardware.

### Advanced mode

Allow individual components/models/features to be selected manually. A user may, for example, choose lightweight embeddings and quality scoring while explicitly enabling OCR.

Preset names are UI conveniences only. Persist the exact work performed: model/algorithm, settings/version, completion status, and provenance for every derived component. Features omitted in one run must be runnable later without rebuilding unrelated components.

Compression is an independent workflow, not implicitly part of initial indexing.

Long-term mobile support should favor smaller models and reduced quality over making a large desktop model run impractically slowly. An initial mobile port should ideally index/search/browse locally on the device rather than requiring a PC round trip.

## Workspace model

A workspace is rooted at a user-selected folder and recursively indexes its subtree.

Examples:

- `D:\Archive\2024` can be a workspace.
- `D:\Archive` can also be a workspace and index `2023`, `2024`, etc., while retaining the subfolder hierarchy as navigable/filterable metadata.

Future support for multiple roots/global search across workspaces is useful but not required for the first version.

### Portability

Workspace state should be portable with the archive. Preferred conceptual layout:

```text
<workspace root>/
  ... user media and folders ...
  .archive-index/
    index.sqlite
    workspace.json        # optional portable/config manifest
    thumbnails/           # optional rebuildable cache
    logs/
    ... other rebuildable derived data ...
```

Processing may use a local SSD as temporary/cache storage for speed, but the resulting index must be portable back to the archive. Search/browse must also work directly from an external hard drive without copying the media to the SSD.

## Canonical data model

Use **SQLite** as the likely canonical index. JSON remains useful for portable configuration, debugging, manifests, or exports.

Do not make a vector index or thumbnail cache the only copy of important state. Derived indexes/caches should be rebuildable.

Embedding storage remains an implementation choice: SQLite BLOBs versus a separate contiguous matrix/tensor file referenced by SQLite should be benchmarked rather than fixed prematurely.

The database should track at least:

- stable asset identity
- current path / filename
- media type
- file size and filesystem timestamps
- content hash where appropriate
- EXIF/media metadata
- source/derived relationships
- indexing component states
- exact model/algorithm/settings/version per component
- embeddings or embedding references
- absolute quality score and component metrics
- strict image grouping information
- optional semantic cluster information
- user selection state
- arbitrary tags/notes
- compression jobs/provenance
- missing/offline state
- failures/logs

## File identity, moves, and missing media

Do not use the pathname as permanent asset identity.

Recommended rescan behavior:

1. Check unchanged paths cheaply using file metadata.
2. Identify new and missing paths.
3. Use content hashes to recognize moved/renamed files reliably.
4. Filename matching may be a fallback hint but never definitive identity.
5. Mark unresolved indexed assets **offline/missing** rather than deleting their index entry automatically.
6. Allow relink, keep-offline, or explicit removal actions.

## Incremental and resumable processing

Incremental indexing is mandatory. `Update Index` should reuse valid work and process only new/changed/missing assets.

Every expensive component must persist independent progress, e.g.:

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

Stopping/closing must preserve state. If an external drive disappears, the operation may fail; hot reconnect is not required initially, but reopening and resuming must be safe.

A per-file failure must not abort a batch. Continue processing, then expose a Problems view such as `X files failed in Y step`, detailed logs, and retry actions.

Support both:

- **Update Index** — incremental work.
- **Rebuild Index / Rebuild Component** — deliberate regeneration.

Already-generated indexes must remain usable for browsing/search on a machine without the generation models installed.

## Default indexing pipeline

Tentative default/lightweight image stages:

- file/metadata extraction
- semantic embedding
- basic quality metrics
- perceptual hash / cheap similarity signals
- thumbnail generation if useful for UI performance

Optional/heavier stages can include:

- OCR
- face detection/recognition/clustering
- workspace semantic clustering
- richer learned quality models
- larger embeddings
- denser video sampling / shot analysis
- speech transcription

## Search and browse

Semantic search should target the whole current workspace by default. Structured attributes should be explicit filters rather than pushed into the text query when avoidable, e.g.:

- year/session/subfolder
- media type
- date range
- tags
- people later

English semantic queries are sufficient initially. Multilingual query translation can be explored later. Transcript indexing must preserve/search whatever languages were actually spoken.

### Views

Eventually provide at least:

1. **Gallery** — primary/default view; similarity score visible.
2. **Embedding map** — secondary exploratory view showing projected embedding positions and query/result locations.

Each asset should support `Find similar` / image-to-image search. This is considered important.

The embedding map is initially a novelty/secondary feature but could become useful for selection and archive exploration if implemented well. Potential hierarchy:

- full embedding projection (PCA/UMAP-like)
- coarse semantic clusters
- strict photo groups
- individual photos

Do not make this visualization a blocker for the first useful gallery/search UI.

## Workspace semantic clustering

Workspace-level clustering is optional. It should be off in Lightweight and available/enabled in High or Advanced modes.

Its purpose is visual organization/navigation, not search correctness or objective semantic truth.

Potential approach:

- k-means over semantic embeddings
- select a useful `K` by optimizing silhouette score over a sensible candidate range
- persist assignments plus embedding/model/K/algorithm provenance
- allow disabling clustering or manually choosing/recomputing K

Approximate cluster labels such as `cats`, `food`, or `city streets` are navigation aids. A lightweight VLM may label representative images/groups; perfect labels are unnecessary.

## Strict image grouping

The grouping used for selection means **basically the same photo of the same thing from essentially the same angle**, not broad semantic similarity.

A proof of concept used CLIP embeddings, temporal distance, a diversity signal, and simple focus/exposure quality metrics. It worked surprisingly well but is still early and JPEG-only. The implementation from that proof of concept should be inspected once committed and used as evidence rather than recreated from memory.

Production grouping must strongly prefer false splits over false merges: unrelated photos in one strict group are worse than a true burst being split into smaller groups.

### Grouping logic

Current intended behavior:

- Time proximity is a hard/near-hard prerequisite for a strict group.
- A rough initial temporal candidate window is **about 10 seconds**.
- Time is **not sufficient**: grouping should require a strict **AND** between temporal proximity and strong visual similarity.
- The visual comparison need not be a large semantic embedding model; cheap perceptual/visual similarity is preferable if it works reliably.
- Manual group correction should remain possible.
- The best-scoring member should normally become the representative thumbnail.

Do not conflate:

- exact duplicate
- same source / encoded derivative
- near-identical burst/angle (strict selection group)
- same event/moment
- broad semantic similarity

Use separate signals/thresholds where appropriate.

## Quality scoring

Quality must be represented as an **absolute workspace-comparable score** so the user can globally rank images and inspect the best selected images across the workspace.

Initial cheap signals should emphasize:

- focus/sharpness
- exposure
- contrast
- noise

Do **not** significantly reward raw resolution; otherwise mixed-device archives systematically favor newer/higher-resolution cameras.

A single composite quality score should be prominent in the UI. Component values belong in a details popup/expanded view.

Simple focus/exposure/contrast/noise metrics have already shown limited agreement with human quality judgments. Treat them as an inexpensive baseline, not a solved notion of photographic quality. Benchmark lightweight no-reference/learned quality models later if they improve agreement enough for their cost.

**Diversity/uniqueness should not materially alter the quality score.** Quality and diversity are distinct signals. Diversity may influence preselection, but an image must not receive a higher photographic-quality score merely because it is unique.

Potential future detected/repeated-face presence may add a small selection bonus as a proxy for potentially valuable content, but it must not be described as sentimental-value inference.

## Selection philosophy

Do not try to recreate the user's historical subjective category system (`family`, `animals`, `manzara`, `diğer`) as generic automated classification. The app should focus on relatively objective-ish assistance:

- technical quality
- strict redundancy/grouping
- diversity/uniqueness
- optional auxiliary signals such as face presence later

The user makes final subjective decisions using the search/grouping/ranking UI. Arbitrary tags/categories may be created by users afterward.

Videos are excluded from automatic quality selection initially. Their first scope is indexing/search/compression.

### Automatic preselection

Automatic preselection should be conservative but useful:

- Usually select/recommend **one** image from a strict group.
- Rarely select **2–3** if several members are all high quality and meaningfully different even by intra-group standards.
- Prefer selecting slightly too much over silently omitting a valuable candidate, but do not retain obviously useless images.
- Group-level ranking should primarily use quality; diversity can help decide whether a second/third high-quality member adds enough distinct information to keep.
- Singleton images are strong candidates only when their quality is sufficiently good; being unique does not automatically mean `keep`.
- Keep the underlying representation score/ranking based. Labels such as `probably keep` can be UI interpretations rather than the canonical logic.

## Selection UI ideas

Do not freeze the UI prematurely. Candidate ideas include:

1. Gallery of strict image groups with one representative thumbnail each.
2. Clicking a group shows all members.
3. Optional best-to-worst ranking within groups.
4. Quality score visible; component details on demand.
5. Conservative automatic preselection editable by the user.
6. Similar-image mini-search from each asset.
7. Optional workspace embedding map.
8. Optional semantic clusters between full-workspace and strict-group levels.
9. Time filters and later GPS/map filtering.
10. Later comparison aids such as synchronized zoom/pan, face crops, keyboard shortcuts, and undo/history.

Selection state belongs in the index and points to existing media. Do not physically copy media for each selection decision. Thumbnails, if generated, are rebuildable UI caches.

## User metadata

Useful:

- arbitrary tags
- notes
- optional saved searches/search history

Not a priority:

- star/rating systems for personal media

## Metadata

Preserve/use available metadata where possible, especially timestamps, GPS, orientation, EXIF, video duration/frame rate/codec, and relevant color/HDR information. Some historical compressed files may already have lost metadata; do not assume availability.

Optional later views can include timeline/calendar and map browsing.

## Video indexing

Initial scope:

- file-level semantic indexing/search is acceptable
- transcript search can be added
- compression is supported
- automatic video quality/selection is deferred

Shot/timestamp-level indexing remains a bookmarked future enhancement and must not block v1.

Potential future representation:

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

## OCR, speech, faces

Keep these as modular enrichment passes.

### Speech

Eventually support local speech-to-text. Audio may contain Turkish, English, French, or other languages, so transcript indexing must not assume English-only content.

### OCR

Optional and independently selectable, including under otherwise lightweight settings.

### Faces

Potential later Samsung-Gallery-like flow:

- detect faces
- embed/cluster recurring identities
- show unknown face clusters
- user merges/splits/labels identities
- optionally use known/repeated face presence as a selection signal

Keep the subsystem modular and verify pretrained-model licensing before distributing checkpoints.

## Compression

Compression is separate from indexing.

Default scope is the entire workspace, with selected subfolder/file targeting as a secondary feature.

### Provenance over codec guessing

Do not infer app-managed completion merely from `.jxl` or AV1. A compression is known/complete only if:

- the app created it and recorded provenance, or
- the user explicitly imports/marks an existing file as an accepted compressed derivative.

Historical manual compressions otherwise remain outside app-managed compression state; backwards compatibility/consistency with them is not required.

### Relationships and safety

Track:

```text
source_asset -> compressed_asset
```

with codec/encoder/settings/version and validation state.

The application never deletes the source asset. After verified completion, it can list source files that may now be removed manually and show failed jobs.

### Temporary outputs and resume

Encode to a temporary filename/path, verify it, then finalize/rename. If interrupted during an individual JXL/AV1 encode, restart that file later. Resume the **queue**, not a byte position within the encode.

### Failure handling

Support individual retry, batch retry, and explicit `Retry with different settings`. Never silently change compression settings as a fallback.

### Verification

Before marking a compression complete, cheaply verify at minimum:

- output exists
- it probes/decodes successfully
- expected dimensions and, for video, duration
- no catastrophic file-size anomaly
- intended metadata is preserved where applicable

Expensive SSIM/VMAF-style validation is optional/benchmarking-oriented rather than mandatory for every job.

### Historical baseline

Previous manual workflow:

- JPEG XL: quality 60, effort 7
- HandBrake AV1 `4K Very Fast` preset/workflow

These are starting points for benchmarking, not compatibility requirements. Exact HandBrake encoder/rate-control/audio/color settings still need to be inspected before canonical defaults are finalized.

Prefer one canonical image preset and one canonical video preset for normal users, with advanced customization.

Likely naming:

```text
IMG_1234.jpg -> IMG_1234.jxl
VID_1234.mp4 -> VID_1234.mp4
```

Open decision: behavior when an unknown pre-existing same-stem derivative already exists, e.g. `IMG_001.jpg` + `IMG_001.jxl`.

## Existing archive context

Rough historical structure:

```text
D:/2018/photos/example.jxl
D:/2018/selection/diğer/example.jpeg
```

The exact tree still needs to be recorded. Do not hard-code it.

Historically selected JPEG originals used mutually exclusive personal categories with precedence roughly:

`family -> animals -> manzara -> diğer`

This is archive context only and should not define generic automated selection.

## Checksums and integrity

Content hashes are useful for stable identity across moves/renames, copy verification, exact duplicate detection, corruption detection, validating SSD-to-archive transfers, and distinguishing originals from derivatives.

Hashes do not replace backups. The user currently has no complete second backup, so development must use conservative source handling and preferably copied test subsets before any mutation-heavy workflow touches the real archive.

## Candidate technologies — provisional

Likely direction, benchmark before locking in:

- Python backend/core
- SQLite canonical catalog
- ExifTool + `ffprobe` or equivalent for metadata
- libjxl / `cjxl` for image compression
- FFmpeg with a selected AV1 encoder for video compression
- CLIP/SigLIP-family or other lightweight image-text embeddings, benchmarked for Lightweight/High tiers
- cheap perceptual/visual similarity for strict burst grouping where possible
- exact brute-force vector retrieval may be sufficient for thousands of assets; FAISS or equivalent only where useful
- local Whisper/faster-whisper-class speech recognition
- local OCR model to be benchmarked
- local app UI; exact framework undecided

Do not over-engineer for millions of assets. Typical workspaces contain thousands.

## Development phases

### Phase 0 — inspect existing proof of concept and real archive

- inspect the committed proof-of-concept grouping/quality implementation
- record actual external-drive filesystem and representative folder tree
- define source/compressed/selection folder-role behavior
- inspect representative metadata and media formats
- create a safe representative test workspace

### Phase 1 — read-only workspace/index foundation

- workspace creation/opening
- recursive scan
- SQLite schema
- stable identity/content hashes
- metadata extraction
- incremental rescan
- missing/offline behavior
- optional thumbnails
- basic gallery/filtering

### Phase 2 — image retrieval

- benchmark Lightweight/High embedding candidates
- persist embeddings/provenance
- text-to-image search
- image-to-image search
- gallery result view
- workspace/subfolder filters

### Phase 3 — strict grouping and selection

- reuse/benchmark proof-of-concept grouping
- strict time + visual-similarity thresholds
- absolute quality scoring
- group representatives
- global/group rankings
- conservative automatic preselection
- manual corrections and persisted selection

### Phase 4 — compression pipeline

- benchmark/finalize canonical JXL/AV1 settings
- persistent queue/progress/retry
- temporary outputs
- validation
- source/derived relationships
- explicit safe-to-delete-original list

### Phase 5 — richer browsing/organization

- semantic workspace clustering
- embedding map
- approximate cluster labels
- tags/notes
- richer global ranking/selection review

### Phase 6 — video and enrichment

- better video representations / optional shot indexing
- speech transcription
- OCR
- face clustering/recognition
- timeline/map views

These are guidance, not rigid milestones. Prefer vertical prototypes when they provide faster evidence.

## Remaining implementation-relevant unknowns

- External archive drive filesystem (NTFS/exFAT/etc.), free space, and exact representative folder tree.
- Which folders under an arbitrary workspace root should be considered canonical media, existing compressed derivatives, historical selections, or ignored/cache folders.
- Historical-archive behavior where original JPEGs were manually deleted after compression and only JXL plus selected JPEG copies remain.
- Exact metadata present in representative JPEG/JXL/MP4/MOV files and what must be preserved.
- Exact HandBrake video settings/canonical AV1 settings when compression work begins.
- Treatment of unknown pre-existing same-stem compressed derivatives.
- Final Lightweight/High embedding and quality models after benchmarking.
- Exact cheap visual-similarity method/thresholds after inspecting the proof-of-concept implementation.
- Exact UI framework and packaging approach.

## Coding/agent guidance

- Preserve source media. Never implement automatic deletion of originals.
- Keep mutations safe: temporary output -> validation -> finalization.
- Make expensive pipeline stages idempotent and resumable.
- Persist exact provenance rather than only preset names.
- Keep optional stages modular so OCR/faces/clustering/etc. can be added later without unrelated recomputation.
- Keep defaults simple and advanced controls explicit.
- Optimize for thousands rather than millions of assets unless benchmarks show otherwise.
- Treat semantic clustering and automated selection as assistive tools, never destructive truth.
- Strict image grouping must prefer false splits over false merges.
- Quality and diversity are separate concepts.
- Support arbitrary folder structures and media resolutions/codecs.
- Avoid cloud dependencies for core functionality.
- Do not copy proprietary internship/company source code, configs, private weights, client data, or internal artifacts. General techniques and public approaches may be independently reimplemented.
- Do not commit or push unrelated changes without explicit user permission.
