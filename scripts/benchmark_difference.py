"""Benchmark the vectorized Difference computation at a representative camera resolution."""

from __future__ import annotations

import json
from io import BytesIO
from time import perf_counter

import numpy as np
from PIL import Image

from archive_index.api.comparison import _difference_map


def main() -> None:
    width, height = 4240, 2832
    left = Image.fromarray(np.zeros((height, width, 3), dtype=np.uint8), mode="RGB")
    right_pixels = np.zeros((height, width, 3), dtype=np.uint8)
    right_pixels[::17, ::19] = (24, 12, 6)
    right = Image.fromarray(right_pixels, mode="RGB")
    try:
        compute_started = perf_counter()
        difference, mse, maximum = _difference_map(left, right)
        compute_ms = (perf_counter() - compute_started) * 1000
        try:
            encode_started = perf_counter()
            output = BytesIO()
            difference.save(output, format="PNG", optimize=True)
            encoded_bytes = len(output.getvalue())
            encode_ms = (perf_counter() - encode_started) * 1000
        finally:
            difference.close()
        print(json.dumps({
            "resolution": f"{width}x{height}",
            "global_mse": round(mse, 6),
            "max_pixel_mse": round(maximum, 6),
            "difference_compute_ms": round(compute_ms, 3),
            "difference_encode_ms": round(encode_ms, 3),
            "total_ms": round(compute_ms + encode_ms, 3),
            "encoded_bytes": encoded_bytes,
        }, sort_keys=True))
    finally:
        left.close()
        right.close()


if __name__ == "__main__":
    main()
