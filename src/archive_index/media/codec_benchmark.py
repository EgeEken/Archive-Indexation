"""Read-only codec capability and fixture benchmark helpers."""

from __future__ import annotations

from pathlib import Path
from time import perf_counter


def codec_capabilities() -> dict[str, object]:
    from .jpegxl import production_capability

    capability = {**production_capability()}
    capability["available"] = bool(capability.get("decoder_available"))
    try:
        import imagecodecs
    except ImportError:
        return {"jpeg_xl": capability, "avif": {"available": False}}
    return {
        "jpeg_xl": capability,
        "avif": {"available": bool(getattr(imagecodecs, "avif_encode", None)), "production_enabled": False},
    }


def benchmark_jpeg_xl_decode(source: Path, iterations: int = 3) -> dict[str, object]:
    import imagecodecs

    if iterations < 1:
        raise ValueError("iterations must be positive")
    encoded = source.read_bytes()
    durations = []
    for _ in range(iterations):
        started = perf_counter()
        decoded = imagecodecs.jpegxl_decode(encoded)
        durations.append(perf_counter() - started)
        if getattr(decoded, "size", 0) == 0:
            raise ValueError("JPEG XL decoder returned no pixels")
    return {
        "codec": "jpeg-xl",
        "iterations": iterations,
        "bytes": len(encoded),
        "mean_seconds": sum(durations) / len(durations),
        "min_seconds": min(durations),
        "max_seconds": max(durations),
    }
