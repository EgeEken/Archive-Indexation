"""Explicit model-cache management for the supported embedding providers."""

from __future__ import annotations

import json
import os
from pathlib import Path

OPENCLIP_PROVIDER = "openclip-b16-datacomp-xl"
OPENCLIP_MODEL_ID = "ViT-B-16"
OPENCLIP_CHECKPOINT = "datacomp_xl_s13b_b90k"
OPENCLIP_VERSION = "openclip-datacomp-xl-v1"
OPENCLIP_DIMENSION = 512
OPENCLIP_EXPECTED_DOWNLOAD_BYTES = 350 * 1024 * 1024

SIGLIP_PROVIDER = "siglip2-base-patch16-224"
SIGLIP_MODEL_ID = "google/siglip2-base-patch16-224"
SIGLIP_VERSION = "siglip2-base-patch16-224-v1"
SIGLIP_DIMENSION = 768
SIGLIP_EXPECTED_DOWNLOAD_BYTES = 1_000 * 1024 * 1024

EMBEDDING_MODEL_BUDGET_BYTES = 5 * 1024 * 1024 * 1024
MODEL_CACHE_MARKER = "archive-index-model.json"


def model_spec(provider: str) -> dict[str, object]:
    if provider == OPENCLIP_PROVIDER:
        return {
            "provider": provider,
            "model_id": OPENCLIP_MODEL_ID,
            "checkpoint": OPENCLIP_CHECKPOINT,
            "version": OPENCLIP_VERSION,
            "dimension": OPENCLIP_DIMENSION,
            "expected_download_bytes": OPENCLIP_EXPECTED_DOWNLOAD_BYTES,
            "cache_name": "openclip-vit-b16-datacomp-xl",
        }
    if provider == SIGLIP_PROVIDER:
        return {
            "provider": provider,
            "model_id": SIGLIP_MODEL_ID,
            "version": SIGLIP_VERSION,
            "dimension": SIGLIP_DIMENSION,
            "expected_download_bytes": SIGLIP_EXPECTED_DOWNLOAD_BYTES,
            "cache_name": "siglip2-base-patch16-224",
        }
    raise ValueError(f"unsupported embedding provider: {provider}")


def model_cache_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return root / "Archive Indexation" / "models" / "embeddings"


def model_cache_dir(provider: str) -> Path:
    return model_cache_root() / str(model_spec(provider)["cache_name"])


def marker_path(provider: str) -> Path:
    return model_cache_dir(provider) / MODEL_CACHE_MARKER


def model_cache_size(provider: str) -> int:
    directory = model_cache_dir(provider)
    if not directory.is_dir():
        return 0
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def model_status(provider: str) -> dict[str, object]:
    spec = model_spec(provider)
    marker = marker_path(provider)
    installed = False
    marker_data: dict[str, object] = {}
    if marker.is_file():
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            marker_data = value if isinstance(value, dict) else {}
            files = marker_data.get("files", [])
            installed = bool(marker_data.get("complete")) and isinstance(files, list) and all(
                (model_cache_dir(provider) / str(name)).is_file() for name in files
            )
        except (OSError, json.JSONDecodeError):
            installed = False
    return {
        **spec,
        "installed": installed,
        "cache_bytes": model_cache_size(provider),
        "cache_path": str(model_cache_dir(provider)),
        "marker": marker_data,
    }


def install_model(provider: str) -> dict[str, object]:
    spec = model_spec(provider)
    existing = sum(model_cache_size(name) for name in (OPENCLIP_PROVIDER, SIGLIP_PROVIDER) if name != provider)
    expected = int(spec["expected_download_bytes"])
    if existing + expected > EMBEDDING_MODEL_BUDGET_BYTES:
        raise RuntimeError("the expected embedding model download would exceed the 5 GB model budget")
    directory = model_cache_dir(provider)
    directory.mkdir(parents=True, exist_ok=True)
    if provider == OPENCLIP_PROVIDER:
        _install_openclip(directory)
    else:
        _install_siglip(directory)
    files = [path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file() and path.name != MODEL_CACHE_MARKER]
    marker = {
        "complete": True,
        "provider": provider,
        "model_id": spec["model_id"],
        "version": spec["version"],
        "files": sorted(files),
    }
    marker_path(provider).write_text(json.dumps(marker, indent=2), encoding="utf-8")
    return model_status(provider)


def _install_openclip(directory: Path) -> None:
    try:
        import open_clip
    except ImportError as error:
        raise RuntimeError("install the embeddings extra before installing OpenCLIP") from error
    try:
        model, _, _ = open_clip.create_model_and_transforms(
            OPENCLIP_MODEL_ID,
            pretrained=OPENCLIP_CHECKPOINT,
            cache_dir=str(directory),
            device="cpu",
        )
        del model
    except Exception as error:
        raise RuntimeError(f"OpenCLIP model installation failed: {error}") from error


def _install_siglip(directory: Path) -> None:
    try:
        from huggingface_hub import snapshot_download
        from transformers import AutoModel, AutoProcessor
    except ImportError as error:
        raise RuntimeError("install the embeddings extra before installing SigLIP2") from error
    try:
        snapshot_download(
            SIGLIP_MODEL_ID,
            local_dir=str(directory),
            local_dir_use_symlinks=False,
            allow_patterns=("*.json", "*.safetensors", "tokenizer*", "preprocessor*", "*.txt"),
            ignore_patterns=("*.bin", "*.msgpack", "*.h5", "*.onnx"),
        )
        AutoProcessor.from_pretrained(directory, local_files_only=True)
        model = AutoModel.from_pretrained(directory, local_files_only=True)
        del model
    except Exception as error:
        raise RuntimeError(f"SigLIP2 model installation failed: {error}") from error
