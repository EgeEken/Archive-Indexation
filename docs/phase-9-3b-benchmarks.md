# Phase 9.3B scaling benchmarks

`benchmarks/phase_93b_scaling.py` creates temporary production-schema SQLite workspaces with synthetic assets and measures the current browser and exact semantic-search paths. It does not create repository artifacts. The semantic fixture uses normalized 512-dimensional fp16 vectors and the same SQLite tables used by the application.

Run with the bundled environment:

```text
.venv\Scripts\python.exe benchmarks\phase_93b_scaling.py
```

The first result records the pre-optimization baseline. Later entries will use the same harness and conditions after each scaling change. Wall-clock values are machine- and filesystem-dependent; they are comparison data, not CI thresholds.

## Baseline before Phase 9.3B scaling changes

Measured on 2026-09-18 with the repository environment, temporary local SQLite workspaces, 512-dimensional normalized fp16 embeddings, and allocation tracing enabled during the browser/search calls:

| Logical assets | Browser cold | Browser warm | Folder filter | Filename sort | Quality sort | Semantic cold | Semantic repeated | Browser peak traced | Semantic peak traced |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 4.01 s | 0.17 s | 0.22 s | 0.17 s | 0.16 s | 0.39 s | 0.43 s | 53.1 MB | 58.8 MB |
| 50,000 | 98.90 s | 0.78 s | 1.04 s | 0.79 s | 0.90 s | 2.22 s | 2.28 s | 265.5 MB | 294.8 MB |
| 100,000 | 505.68 s | 1.48 s | 2.14 s | 1.50 s | 1.47 s | 4.76 s | 4.66 s | 530.8 MB | 589.3 MB |

The cold browser request includes full catalog construction and hydration. The warm and filtered/sorted requests reuse the current catalog but still perform the current query/filter work. The repeated semantic query has no embedding matrix cache in this baseline, so it remains close to cold hydration time. The 100k run used about 100 MB of raw fp16 vector storage before Python/SQLite overhead.

## After browser/catalog scaling unit

The logical browser generation and one-pass catalog hydration changed only browser construction and invalidation. Semantic ranking is unchanged until the embedding-matrix unit.

| Logical assets | Browser cold | Browser warm | Folder filter | Filename sort | Quality sort | Browser peak traced |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 1.57 s | 0.17 s | 0.22 s | 0.21 s | 0.26 s | 53.3 MB |
| 50,000 | 10.44 s | 1.02 s | 1.43 s | 1.16 s | 1.23 s | 265.5 MB |
| 100,000 | 20.19 s | 1.64 s | 2.29 s | 1.62 s | 1.91 s | 530.8 MB |

The 100k cold browser request fell from 505.68 s to 20.19 s under the same allocation-tracing conditions. The remaining cold cost is the bounded in-memory catalog itself; later requests reuse it until the browser-visible generation changes.

## After semantic embedding-matrix cache

The cache stores one active workspace/run matrix in normalized fp32 form, allows at most two workspace entries, and releases entries when the semantic provider session is cleared or replaced. The reported current allocation includes the cached matrix; the 100k matrix itself is approximately 205 MB before Python/container overhead.

| Logical assets | Semantic cold | Semantic repeated | Cached allocation current | Cached allocation peak |
| ---: | ---: | ---: | ---: | ---: |
| 10,000 | 0.58 s | 0.12 s | 21.8 MB | 56.8 MB |
| 50,000 | 2.76 s | 0.72 s | 108.4 MB | 284.8 MB |
| 100,000 | 5.43 s | 1.30 s | 216.6 MB | 569.3 MB |

The repeated query improved from 0.43/2.28/4.66 s to 0.12/0.72/1.30 s at 10k/50k/100k. Exact cosine ranking, video MAX-frame collapse, filtering, and run/provider/workspace provenance are unchanged.

## Final repeat after all Phase 9.3B units

A final rerun of the same harness on 2026-09-18 produced the following comparison values. The difference from the earlier after-unit measurements is normal local filesystem and allocation-tracing variance.

| Logical assets | Browser cold | Browser warm | Folder filter | Filename sort | Quality sort | Semantic cold | Semantic warm |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 1.38 s | 0.15 s | 0.22 s | 0.17 s | 0.17 s | 0.38 s | 0.11 s |
| 50,000 | 6.99 s | 0.78 s | 1.08 s | 0.79 s | 0.78 s | 2.11 s | 0.43 s |
| 100,000 | 14.36 s | 1.45 s | 2.14 s | 1.54 s | 1.83 s | 4.35 s | 0.94 s |

The final semantic allocation tracing remained 21.8/108.4/216.6 MB current and 56.8/284.8/569.3 MB peak at 10k/50k/100k. The browser peak remained about 53.3/265.8/530.8 MB.

## Shared video decode validation

The real Sony `ODA8_7563.MP4` clip is 3840×2160 HEVC `yuv422p10le` at approximately 119.88 fps. The configured 9.009-second sample set contains 19 frames. On the CPU FFmpeg path, two sequential decode passes took 28.425 s and one shared pass took 14.294 s. With installed LAR-IQA and OpenCLIP inference on the RTX 3070 Ti Laptop GPU, the shared run measured 14.536 s decode, 1.368 s quality inference, and 0.446 s embedding inference, for 16.350 s total. The equivalent separate-pass total was approximately 30.239 s using the two-pass decode measurement and the same inference measurements. No sampled-frame directory is persisted; frames remain transient and are released after both consumers finish.

The remaining scaling limits are the bounded in-memory browser catalog and exact full-matrix semantic ranking. The active matrix cache is intentionally bounded to two workspace entries; an ANN index, persistent sample-frame cache, and external cache service remain out of scope.
