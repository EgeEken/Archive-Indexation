"""JPEG XL decoding through the optional native imagecodecs wrapper."""

from __future__ import annotations

from pathlib import Path

from PIL import Image


def _imagecodecs():
    try:
        import imagecodecs
    except ImportError as error:
        raise RuntimeError(
            "JPEG XL decoding requires the imagecodecs package"
        ) from error
    return imagecodecs


def decoder_version() -> str:
    value = _imagecodecs().jpegxl_version()
    return str(value)


def decode(source: Path) -> Image.Image:
    import numpy as np

    try:
        array = _imagecodecs().jpegxl_decode(source.read_bytes())
        value = np.asarray(array)
    except Exception as error:
        raise ValueError(f"could not decode JPEG XL image: {source}") from error
    if value.ndim not in {2, 3} or value.size == 0:
        raise ValueError(f"JPEG XL decoder returned an invalid image: {source}")
    if value.dtype.kind == "f":
        value = np.clip(value, 0, 1 if value.max(initial=0) <= 1 else 255)
        if value.max(initial=0) <= 1:
            value = value * 255
        value = value.astype("uint8")
    elif value.dtype != np.uint8:
        value = np.clip(value, 0, 255).astype("uint8")
    if value.ndim == 3 and value.shape[2] not in {1, 3, 4}:
        raise ValueError(f"JPEG XL decoder returned unsupported channels: {source}")
    image = Image.fromarray(value)
    if image.mode == "L":
        return image.convert("RGB")
    if image.mode in {"RGBA", "RGB"}:
        return image
    return image.convert("RGB")
