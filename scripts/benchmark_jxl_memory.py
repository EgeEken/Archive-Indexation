"""Measure sequential JPEG XL decode/encode/validation memory use."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

from archive_index.media.jpegxl import encode, load_source, profile_settings, validate


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--width", type=int, default=6000)
    parser.add_argument("--height", type=int, default=4000)
    parser.add_argument("--profile", choices=("High Quality", "Balanced", "High Compression"), default="Balanced")
    args = parser.parse_args()
    settings = {"quality": {"High Quality": 80, "Balanced": 60, "High Compression": 40}[args.profile], "effort": 7}
    profile = {"codec": "jpeg-xl", "settings": profile_settings({"settings": settings})}
    temporary = None
    source = args.source
    if source is None:
        temporary = tempfile.TemporaryDirectory(prefix="archive-index-jxl-memory-")
        source = Path(temporary.name) / "fixture.jpg"
        image = Image.effect_noise((args.width, args.height), 100).convert("RGB")
        try:
            image.save(source, quality=95)
        finally:
            image.close()
    baseline = _rss_bytes()
    started = time.perf_counter()
    with _PeakSampler(baseline) as sampler:
        decode_started = time.perf_counter()
        image, metadata = load_source(source)
        decode_ms = (time.perf_counter() - decode_started) * 1000
        encode_started = time.perf_counter()
        encoded = encode(image, profile)
        encode_ms = (time.perf_counter() - encode_started) * 1000
        validation_started = time.perf_counter()
        validation = validate(encoded, image)
        validation_ms = (time.perf_counter() - validation_started) * 1000
        image.close()
    total_ms = (time.perf_counter() - started) * 1000
    del encoded
    gc.collect()
    output = {
        "dimensions": [args.width, args.height] if args.source is None else [validation["width"], validation["height"]],
        "source": str(source),
        "source_size_bytes": source.stat().st_size,
        "profile": args.profile,
        "settings": profile["settings"],
        "output_size_bytes": validation["size_bytes"],
        "output_sha256": validation["sha256"],
        "compression_ratio": round(source.stat().st_size / validation["size_bytes"], 4),
        "decode_ms": round(decode_ms, 2),
        "encode_ms": round(encode_ms, 2),
        "validation_ms": round(validation_ms, 2),
        "total_ms": round(total_ms, 2),
        "mse": validation["mse"],
        "rss_baseline_bytes": baseline,
        "rss_peak_bytes": sampler.peak,
        "rss_peak_delta_bytes": sampler.peak - baseline,
        "rss_final_bytes": _rss_bytes(),
        "metadata": metadata,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    if temporary is not None:
        temporary.cleanup()


if __name__ == "__main__":
    main()
