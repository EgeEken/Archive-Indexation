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
