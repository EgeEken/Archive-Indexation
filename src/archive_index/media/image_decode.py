"""Canonical pixel loading for rendered application images."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .jxl import decode as decode_jxl

DECODER_GAP_EXTENSIONS = frozenset(
    {".arw", ".cr2", ".cr3", ".dng", ".heic", ".heif", ".jxl", ".nef", ".raf", ".rw2"}
)


def load_full_image(source: Path, prepared_image: Image.Image | None = None) -> Image.Image:
    if prepared_image is not None:
        return prepared_image.copy()
    if source.suffix.casefold() == ".jxl":
        image = decode_jxl(source)
        return image.convert("RGB") if image.mode != "RGB" else image
    try:
        with Image.open(source) as image:
            oriented = ImageOps.exif_transpose(image)
            if oriented.mode != "RGB":
                oriented = oriented.convert("RGB")
            return oriented.copy()
    except UnidentifiedImageError as error:
        if source.suffix.casefold() in DECODER_GAP_EXTENSIONS:
            raise RuntimeError(f"no image decoder is configured for {source.suffix.casefold()}") from error
        raise


def load_reduced_image(
    source: Path,
    size: tuple[int, int],
    prepared_image: Image.Image | None = None,
) -> Image.Image:
    if prepared_image is None and source.suffix.casefold() != ".jxl":
        try:
            with Image.open(source) as image:
                if image.format in {"JPEG", "MPO"}:
                    image.draft("RGB", size)
                oriented = ImageOps.exif_transpose(image)
                oriented.thumbnail(size, Image.Resampling.LANCZOS)
                if oriented.mode != "RGB":
                    oriented = oriented.convert("RGB")
                return oriented.copy()
        except UnidentifiedImageError as error:
            if source.suffix.casefold() in DECODER_GAP_EXTENSIONS:
                raise RuntimeError(f"no image decoder is configured for {source.suffix.casefold()}") from error
            raise
    image = load_full_image(source, prepared_image)
    image.thumbnail(size, Image.Resampling.LANCZOS)
    if image.mode != "RGB":
        converted = image.convert("RGB")
        image.close()
        return converted
    return image
