"""Benchmark the vectorized Difference computation at a representative camera resolution."""

from __future__ import annotations

import json
from io import BytesIO
from time import perf_counter

import numpy as np
from PIL import Image

from archive_index.api.comparison import _difference_map


def encode(difference: Image.Image, **options: object) -> tuple[float, int]:
    started = perf_counter()
    output = BytesIO()
    difference.save(output, format="PNG", **options)
    return (perf_counter() - started) * 1000, len(output.getvalue())


def main() -> None:
    width, height = 4240, 2832
    generator = np.random.default_rng(20260925)
    left_pixels = generator.integers(0, 256, (height, width, 3), dtype=np.uint8)
    noise = generator.integers(-24, 25, (height, width, 3), dtype=np.int16)
    right_pixels = np.clip(left_pixels.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    left = Image.fromarray(left_pixels, mode="RGB")
    right = Image.fromarray(right_pixels, mode="RGB")
    try:
        compute_started = perf_counter()
        difference, mse, maximum = _difference_map(left, right)
        compute_ms = (perf_counter() - compute_started) * 1000
        try:
            encodings = {}
            for name, options in (
                ("png_optimize_true", {"optimize": True}),
                ("png_default", {}),
                ("png_compress_level_1", {"compress_level": 1}),
                ("png_compress_level_0", {"compress_level": 0}),
            ):
                elapsed, size = encode(difference, **options)
                encodings[name] = {"encode_ms": round(elapsed, 3), "encoded_bytes": size}
            chosen = encodings["png_compress_level_1"]
        finally:
            difference.close()
        print(json.dumps({
            "resolution": f"{width}x{height}",
            "input": "high-entropy compression-noise synthetic pair",
            "global_mse": round(mse, 6),
            "max_pixel_mse": round(maximum, 6),
            "difference_compute_ms": round(compute_ms, 3),
            "difference_encode_ms": chosen["encode_ms"],
            "total_ms": round(compute_ms + chosen["encode_ms"], 3),
            "encoded_bytes": chosen["encoded_bytes"],
            "chosen_encoding": "PNG compress_level=1",
            "encodings": encodings,
        }, sort_keys=True))
    finally:
        left.close()
        right.close()


if __name__ == "__main__":
    main()
