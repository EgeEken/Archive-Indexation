"""Benchmark the official metadata-preserving JPEG XL production path."""

from __future__ import annotations

import argparse
import gc
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

from archive_index.media.jpegxl import profile_settings
from archive_index.media.jpegxl_tools import encode, validate


def _rss_bytes() -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("page_fault_count", wintypes.DWORD),
                ("peak_working_set_size", ctypes.c_size_t), ("working_set_size", ctypes.c_size_t),
                ("quota_peak_paged_pool_usage", ctypes.c_size_t), ("quota_paged_pool_usage", ctypes.c_size_t),
                ("quota_peak_non_paged_pool_usage", ctypes.c_size_t), ("quota_non_paged_pool_usage", ctypes.c_size_t),
                ("pagefile_usage", ctypes.c_size_t), ("peak_pagefile_usage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        function = ctypes.windll.psapi.GetProcessMemoryInfo
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        function.restype = wintypes.BOOL
        if not function(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.working_set_size)
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)


class _PeakSampler:
    def __init__(self, baseline: int):
        self.peak = baseline
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.wait(0.01):
            self.peak = max(self.peak, _rss_bytes())

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        self.peak = max(self.peak, _rss_bytes())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--width", type=int, default=6000)
    parser.add_argument("--height", type=int, default=4000)
    args = parser.parse_args()
    temporary = tempfile.TemporaryDirectory(prefix="archive-index-jxl-benchmark-")
    root = Path(temporary.name)
    source = args.source
    if source is None:
        source = root / "fixture.jpg"
        reference = Path(__file__).resolve().parents[1] / "src/archive_index/web/assets/compression-preview/reference.jpg"
        with Image.open(reference) as image:
            image.convert("RGB").resize((args.width, args.height), Image.Resampling.LANCZOS).save(source, quality=95)
    results = []
    for effort in (5, 6, 7):
        output = root / f"effort-{effort}.jxl"
        profile = {"codec": "jpeg-xl", "settings": profile_settings({"settings": {"quality": 60, "effort": effort}})}
        baseline = _rss_bytes()
        started = time.perf_counter()
        with _PeakSampler(baseline) as sampler:
            encode_started = time.perf_counter()
            encoded = encode(source, output, profile, root)
            encode_ms = (time.perf_counter() - encode_started) * 1000
            validation_started = time.perf_counter()
            validation = validate(source, output, encoded)
            validation_ms = (time.perf_counter() - validation_started) * 1000
        results.append({
            "dimensions": [args.width, args.height] if args.source is None else [validation["width"], validation["height"]],
            "effort": effort,
            "quality": 60,
            "distance": profile["settings"]["distance"],
            "source_size_bytes": source.stat().st_size,
            "output_size_bytes": validation["size_bytes"],
            "compression_ratio": round(source.stat().st_size / validation["size_bytes"], 4),
            "encode_ms": round(encode_ms, 2),
            "validation_ms": round(validation_ms, 2),
            "total_ms": round((time.perf_counter() - started) * 1000, 2),
            "mse": validation.get("mse"),
            "rss_baseline_bytes": baseline,
            "rss_peak_bytes": sampler.peak,
            "rss_peak_delta_bytes": sampler.peak - baseline,
            "rss_final_bytes": _rss_bytes(),
            "encoder_version": encoded["encoder_version"],
        })
        output.unlink(missing_ok=True)
        gc.collect()
    print(json.dumps({"source": str(source), "results": results}, indent=2, sort_keys=True))
    temporary.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
