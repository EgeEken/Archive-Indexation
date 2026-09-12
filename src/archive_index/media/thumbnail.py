"""Transactional image thumbnail generation."""

from __future__ import annotations

import os
import subprocess
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from .metadata import DECODER_GAP_EXTENSIONS, MetadataExtractionError, UnsupportedDecoderError

THUMBNAIL_SIZE = (320, 320)
IMAGE_THUMBNAIL_ALGORITHM = "pillow-reduced-jpeg"
IMAGE_THUMBNAIL_VERSION = "pillow-jpeg-v2"
VIDEO_THUMBNAIL_ALGORITHM = "ffmpeg-center-frame-jpeg"
VIDEO_THUMBNAIL_VERSION = "ffmpeg-center-frame-jpeg-v2"
THUMBNAIL_JPEG_QUALITY = 50
THUMBNAIL_VERSION = IMAGE_THUMBNAIL_VERSION


def thumbnail_provenance(media_type: str) -> tuple[str, str, dict[str, object]]:
    if media_type == "video":
        return (
            VIDEO_THUMBNAIL_ALGORITHM,
            VIDEO_THUMBNAIL_VERSION,
            {
                "pipeline": "ffprobe-duration+ffmpeg-center-frame",
                "selection": "center_frame",
                "size": THUMBNAIL_SIZE,
                "jpeg_quality": THUMBNAIL_JPEG_QUALITY,
            },
        )
    return (
        IMAGE_THUMBNAIL_ALGORITHM,
        IMAGE_THUMBNAIL_VERSION,
        {
            "pipeline": "pillow-reduced-decode",
            "size": THUMBNAIL_SIZE,
            "jpeg_quality": THUMBNAIL_JPEG_QUALITY,
        },
    )


def generate_thumbnail(
    source: Path,
    destination: Path,
    size: tuple[int, int] = THUMBNAIL_SIZE,
    prepared_image: Image.Image | None = None,
    media_type: str = "image",
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        image = (
            prepared_image.copy()
            if prepared_image is not None
            else load_video_frame(source, size)
            if media_type == "video"
            else load_reduced_image(source, size)
        )
        try:
            image.thumbnail(size, Image.Resampling.LANCZOS)
            image.save(temporary, format="JPEG", quality=THUMBNAIL_JPEG_QUALITY, optimize=True)
        finally:
            image.close()
        with Image.open(temporary) as validation:
            validation.verify()
        os.replace(temporary, destination)
    except UnidentifiedImageError as error:
        if source.suffix.casefold() in DECODER_GAP_EXTENSIONS:
            raise UnsupportedDecoderError(
                f"no image decoder is configured for {source.suffix.casefold()}"
            ) from error
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def load_reduced_image(source: Path, size: tuple[int, int] = THUMBNAIL_SIZE) -> Image.Image:
    try:
        with Image.open(source) as image:
            if image.format in {"JPEG", "MPO"}:
                image.draft("RGB", size)
            image = ImageOps.exif_transpose(image)
            image.thumbnail(size, Image.Resampling.LANCZOS)
            if image.mode != "RGB":
                image = image.convert("RGB")
            return image.copy()
    except UnidentifiedImageError as error:
        if source.suffix.casefold() in DECODER_GAP_EXTENSIONS:
            raise UnsupportedDecoderError(
                f"no image decoder is configured for {source.suffix.casefold()}"
            ) from error
        raise


def load_full_image(source: Path) -> Image.Image:
    try:
        with Image.open(source) as image:
            oriented = ImageOps.exif_transpose(image)
            if oriented.mode != "RGB":
                oriented = oriented.convert("RGB")
            return oriented.copy()
    except UnidentifiedImageError as error:
        if source.suffix.casefold() in DECODER_GAP_EXTENSIONS:
            raise UnsupportedDecoderError(
                f"no image decoder is configured for {source.suffix.casefold()}"
            ) from error
        raise


def load_video_frame(source: Path, size: tuple[int, int] = THUMBNAIL_SIZE) -> Image.Image:
    duration_command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    try:
        duration_result = subprocess.run(
            duration_command,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise UnsupportedDecoderError("ffmpeg/ffprobe is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise MetadataExtractionError(f"video probing timed out: {source}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or "").strip()
        raise MetadataExtractionError(detail or f"video probing failed: {source}") from error

    try:
        duration = max(float(duration_result.stdout.strip() or "0"), 0.0)
    except ValueError as error:
        raise MetadataExtractionError(f"video duration is invalid: {source}") from error

    frame_command = [
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        str(duration / 2),
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale={size[0]}:{size[1]}:force_original_aspect_ratio=decrease",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        frame_result = subprocess.run(
            frame_command,
            check=True,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError as error:
        raise UnsupportedDecoderError("ffmpeg is not installed") from error
    except subprocess.TimeoutExpired as error:
        raise MetadataExtractionError(f"video thumbnail timed out: {source}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode(errors="replace").strip()
        raise MetadataExtractionError(detail or f"video thumbnail failed: {source}") from error

    try:
        with Image.open(BytesIO(frame_result.stdout)) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail(size, Image.Resampling.LANCZOS)
            if image.mode != "RGB":
                image = image.convert("RGB")
            return image.copy()
    except UnidentifiedImageError as error:
        raise MetadataExtractionError(f"ffmpeg returned an invalid video frame: {source}") from error
