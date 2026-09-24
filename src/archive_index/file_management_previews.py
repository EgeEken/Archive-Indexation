"""Bundled and app-owned compression profile previews."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

from .file_management import quality_to_distance
from .media.image_decode import load_full_image
from .workspace import Workspace

PREVIEW_VERSION = "compression-preview-v1"
REFERENCE_RESOURCE = "assets/compression-preview/reference.jpg"
BUILTIN_PREVIEW_MANIFEST = "assets/compression-preview/manifest.json"


def bundled_preview_manifest() -> dict[str, object]:
    resource = Path(__file__).resolve().parent / "web" / "assets" / "compression-preview" / "manifest.json"
    return json.loads(resource.read_text(encoding="utf-8"))


def custom_profile_preview(workspace: Workspace, profile: dict[str, object]) -> dict[str, object]:
    if profile.get("codec") not in {"jpeg-xl", "avif"}:
        raise ValueError("preview is available only for image compression profiles")
    settings = profile.get("settings") or {}
    reference_path = Path(__file__).resolve().parent / "web" / "assets" / "compression-preview" / "reference.jpg"
    reference_bytes = reference_path.read_bytes()
    key = hashlib.sha256(
        json.dumps(
            {"version": PREVIEW_VERSION, "reference": hashlib.sha256(reference_bytes).hexdigest(), "codec": profile["codec"], "settings": settings},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cache_dir = workspace.index_directory / "compression-previews"
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / f"{key}.json"
    if manifest_path.is_file():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    reference = load_full_image(reference_path)
    decoded = None
    try:
        encoded = _encode(reference, profile)
        decoded = _decode(encoded, profile["codec"])
        output_name = f"{key}.webp"
        output_path = cache_dir / output_name
        _save_lossless_webp(decoded, output_path)
        metrics = _metrics(reference, decoded, len(reference_bytes), len(encoded))
    finally:
        reference.close()
        if decoded is not None:
            decoded.close()
    result = {
        "version": PREVIEW_VERSION,
        "profile_id": profile["id"],
        "profile_name": profile["name"],
        "codec": profile["codec"],
        "settings": settings,
        "reference": {"filename": reference_path.name, "size_bytes": len(reference_bytes), "url": f"/assets/compression-preview/reference.jpg"},
        "compressed": {"filename": f"{profile['name']}.{profile['container']}", "size_bytes": len(encoded), "url": f"/api/file-management/profile-preview/{key}"},
        "metrics": metrics,
    }
    manifest_path.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    cached = sorted(cache_dir.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in cached[16:]:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".webp").unlink(missing_ok=True)
    return result


def cached_preview_file(workspace: Workspace, key: str) -> Path:
    path = workspace.index_directory / "compression-previews" / f"{key}.webp"
    if not path.is_file():
        raise FileNotFoundError(key)
    return path


def _encode(image: Image.Image, profile: dict[str, object]) -> bytes:
    import numpy as np

    array = np.asarray(image.convert("RGB"))
    if profile["codec"] == "jpeg-xl":
        import imagecodecs

        settings = profile.get("settings") or {}
        return imagecodecs.jpegxl_encode(
            array,
            distance=quality_to_distance(float(settings.get("quality", 60))),
            effort=int(settings.get("effort", 7)),
        )
    try:
        import imagecodecs

        settings = profile.get("settings") or {}
        return imagecodecs.avif_encode(array, speed=int(settings.get("effort", 7)))
    except (AttributeError, ImportError, RuntimeError, ValueError) as error:
        raise RuntimeError("AVIF encoder is not available in this runtime.") from error


def _decode(encoded: bytes, codec: str) -> Image.Image:
    import numpy as np

    import imagecodecs

    try:
        array = imagecodecs.jpegxl_decode(encoded) if codec == "jpeg-xl" else imagecodecs.avif_decode(encoded)
    except (AttributeError, ImportError, RuntimeError, ValueError) as error:
        raise RuntimeError("AVIF encoder is not available in this runtime.") from error
    return Image.fromarray(np.asarray(array)).convert("RGB")


def _save_lossless_webp(image: Image.Image, path: Path) -> None:
    output = BytesIO()
    image.save(output, format="WEBP", lossless=True, method=6)
    path.write_bytes(output.getvalue())


def _metrics(reference: Image.Image, decoded: Image.Image, reference_bytes: int, compressed_bytes: int) -> dict[str, object]:
    mse = _mse(reference, decoded) if reference.size == decoded.size else None
    return {
        "reference_bytes": reference_bytes,
        "compressed_bytes": compressed_bytes,
        "compression_ratio": round(reference_bytes / compressed_bytes, 3) if compressed_bytes else None,
        "compressed_percent": round(compressed_bytes / reference_bytes * 100, 2) if reference_bytes else None,
        "mse": round(mse, 6) if mse is not None else None,
    }


def _mse(left: Image.Image, right: Image.Image) -> float:
    difference = ImageChops.difference(left.convert("RGB"), right.convert("RGB"))
    try:
        width, height = difference.size
        return sum(ImageStat.Stat(difference).sum2) / (width * height * 3)
    finally:
        difference.close()
