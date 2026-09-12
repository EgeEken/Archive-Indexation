"""Optional, versioned image-quality providers."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

from PIL import Image, ImageOps

QUALITY_PROVIDER_OFF = "off"
QUALITY_PROVIDER_LAR_IQA = "lar-iqa"
LAR_IQA_ALGORITHM = "lar-iqa"
LAR_IQA_VERSION = "2"
LAR_IQA_MODEL_ID = "lar-iqa-2branch-kan"
LAR_IQA_MODEL_FILENAME = "AIM_Training_2branche_KAN-Head.pt"
LAR_IQA_OFFICIAL_FILE_ID = "1Q3jQiqzYBslOyIwI0R-7UKFCTqv91QxC"
LAR_IQA_CHECKPOINT_SHA256 = "70c243d7324c76df43df8ab6a44eb535ee9f4f3acb928e5dfe9deb2bb3b7b0ab"
LAR_IQA_PREPROCESSING_VERSION = "official-inference-rgb-v1"
IMAGE_NET_MEAN = [0.485, 0.456, 0.406]
IMAGE_NET_STD = [0.229, 0.224, 0.225]


class QualityProviderError(RuntimeError):
    """A quality provider cannot be used for this operation."""


class QualityProviderUnavailable(QualityProviderError):
    """The selected provider is not installed or configured."""


@dataclass(frozen=True)
class ProviderResult:
    raw: dict[str, object]
    score: float
    components: dict[str, float] | None = None


class QualityProvider(Protocol):
    algorithm: str
    version: str
    enabled: bool

    def preflight(self) -> None: ...

    def score_paths(self, paths: Sequence[Path]) -> list[ProviderResult]: ...

    def score_images(self, images: Sequence[Image.Image]) -> list[ProviderResult]: ...

    def score_prepared_images(self, images: Sequence[Image.Image]) -> list[ProviderResult]: ...


class OffQualityProvider:
    algorithm = "quality-off"
    version = "1"
    enabled = False

    def preflight(self) -> None:
        return None

    def score_paths(self, paths: Sequence[Path]) -> list[ProviderResult]:
        raise QualityProviderError("quality scoring is disabled")

    def score_images(self, images: Sequence[Image.Image]) -> list[ProviderResult]:
        raise QualityProviderError("quality scoring is disabled")

    def score_prepared_images(self, images: Sequence[Image.Image]) -> list[ProviderResult]:
        raise QualityProviderError("quality scoring is disabled")


class LegacyPillowProvider:
    """Compatibility provider used only for old benchmarks and migration tests."""

    enabled = True

    def __init__(self) -> None:
        from .quality import QUALITY_ALGORITHM, QUALITY_SCORE_VERSION

        self.algorithm = QUALITY_ALGORITHM
        self.version = QUALITY_SCORE_VERSION

    def preflight(self) -> None:
        return None

    def score_paths(self, paths: Sequence[Path]) -> list[ProviderResult]:
        from .quality import measure_quality

        results = []
        for path in paths:
            with Image.open(path) as image:
                result = measure_quality(image)
            results.append(ProviderResult(result.raw, result.score, result.components))
        return results

    def score_images(self, images: Sequence[Image.Image]) -> list[ProviderResult]:
        from .quality import measure_quality

        return [
            ProviderResult(
                result.raw,
                result.score,
                result.components,
            )
            for result in (measure_quality(image) for image in images)
        ]

    def score_prepared_images(self, images: Sequence[Image.Image]) -> list[ProviderResult]:
        return self.score_images(images)


class LARIQAProvider:
    algorithm = LAR_IQA_ALGORITHM
    version = LAR_IQA_VERSION
    enabled = True

    def __init__(
        self,
        model_path: Path | None = None,
        *,
        batch_size: int = 1,
        precision: str = "fp32",
        preparation_workers: int = 8,
    ) -> None:
        if batch_size < 1:
            raise ValueError("quality batch size must be positive")
        if precision not in {"fp32", "fp16"}:
            raise ValueError("quality precision must be fp32 or fp16")
        if preparation_workers < 1:
            raise ValueError("quality preparation worker count must be positive")
        self.model_path = model_path or default_model_path()
        self.batch_size = batch_size
        self.precision = precision
        self.preparation_workers = preparation_workers
        self._torch = None
        self._transforms = None
        self._model = None
        self._device = None
        self.checkpoint_sha256: str | None = None

    @property
    def settings(self) -> dict[str, object]:
        return {
            "model_id": LAR_IQA_MODEL_ID,
            "checkpoint_filename": LAR_IQA_MODEL_FILENAME,
            "checkpoint_sha256": self.checkpoint_sha256,
            "architecture": "MobileNetV3-Large dual branch + KAN heads",
            "color_space": "RGB",
            "authentic_input": [384, 384],
            "synthetic_input": [1280, 1280],
            "normalization": {"mean": IMAGE_NET_MEAN, "std": IMAGE_NET_STD},
            "preprocessing_version": LAR_IQA_PREPROCESSING_VERSION,
            "batch_size": self.batch_size,
            "precision": self.precision,
        }

    def preflight(self) -> None:
        self._load()

    def score_paths(self, paths: Sequence[Path]) -> list[ProviderResult]:
        self._load()
        if not paths:
            return []

        batches = [paths[start : start + self.batch_size] for start in range(0, len(paths), self.batch_size)]
        results: list[ProviderResult] = []
        with ThreadPoolExecutor(max_workers=self.preparation_workers) as preparation_pool:
            with ThreadPoolExecutor(max_workers=1) as prefetch_pool:
                future = prefetch_pool.submit(self._prepare_batch, preparation_pool, batches[0])
                for index, batch_paths in enumerate(batches):
                    prepared = future.result()
                    if index + 1 < len(batches):
                        future = prefetch_pool.submit(
                            self._prepare_batch,
                            preparation_pool,
                            batches[index + 1],
                        )
                    authentic = [item[0] for item in prepared]
                    synthetic = [item[1] for item in prepared]
                    results.extend(self._score_tensors(authentic, synthetic))
        return results

    def score_images(self, images: Sequence[Image.Image]) -> list[ProviderResult]:
        self._load()
        prepared: list[Image.Image] = []
        for image in images:
            oriented = ImageOps.exif_transpose(image).convert("RGB")
            prepared.append(oriented)
        try:
            return self.score_prepared_images(prepared)
        finally:
            for image in prepared:
                image.close()

    def score_prepared_images(self, images: Sequence[Image.Image]) -> list[ProviderResult]:
        self._load()
        torch = self._torch
        transforms = self._transforms
        authentic = []
        synthetic = []
        for image in images:
            authentic.append(transforms.authentic(image))
            synthetic.append(transforms.synthetic(image))
        return self._score_tensors(authentic, synthetic)

    def _prepare_batch(self, preparation_pool, paths):
        return list(preparation_pool.map(self._prepare_path, paths))

    def _prepare_path(self, path: Path):
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        try:
            return self._transforms.authentic(image), self._transforms.synthetic(image)
        finally:
            image.close()

    def _score_tensors(self, authentic, synthetic) -> list[ProviderResult]:
        torch = self._torch
        authentic_batch = torch.stack(authentic).to(self._device)
        synthetic_batch = torch.stack(synthetic).to(self._device)
        autocast = (
            torch.autocast(device_type="cuda", dtype=torch.float16)
            if self.precision == "fp16" and self._device.type == "cuda"
            else nullcontext()
        )
        with torch.inference_mode(), autocast:
            output = self._model(authentic_batch, synthetic_batch).reshape(-1).detach().cpu().tolist()
        results = []
        for value in output:
            if not isinstance(value, (int, float)) or value != value or value in {float("inf"), float("-inf")}:
                raise QualityProviderError("LAR-IQA returned a non-finite score")
            score = max(0.0, min(1.0, float(value)))
            results.append(
                ProviderResult(
                    raw={"provider": self.algorithm, "model_output": float(value), "output_scale": "0-1"},
                    score=score,
                    components=None,
                )
            )
        return results

    def _load(self) -> None:
        if self._model is not None:
            return
        if not self.model_path.is_file():
            raise QualityProviderUnavailable(
                "LAR-IQA checkpoint is not installed; run "
                "`archive-index model install lar-iqa` or place "
                f"{LAR_IQA_MODEL_FILENAME} at {self.model_path}"
            )
        self.checkpoint_sha256 = _sha256(self.model_path)
        if self.checkpoint_sha256 != LAR_IQA_CHECKPOINT_SHA256:
            raise QualityProviderUnavailable(
                "LAR-IQA checkpoint SHA-256 does not match the published model"
            )
        try:
            import torch
            import timm
            from torchvision import transforms
            from efficient_kan import KAN
        except ImportError as error:
            raise QualityProviderUnavailable(
                "LAR-IQA requires one learned-quality extra; run "
                "`uv sync --extra quality-lar-cpu` or "
                "`uv sync --extra quality-lar-cuda`"
            ) from error

        class MobileNetMergedWithKAN(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.authentic = timm.create_model("mobilenetv3_large_100.ra_in1k", pretrained=False)
                self.syntetic = timm.create_model("mobilenetv3_large_100.ra_in1k", pretrained=False)
                self.aut_dw = KAN([1000, 512])
                self.syn_dw = KAN([1000, 512])
                self.head = KAN([1024, 1])

            def forward(self, inp, inp2):
                authentic = self.aut_dw(self.authentic(inp))
                synthetic = self.syn_dw(self.syntetic(inp2))
                return self.head(torch.cat([authentic, synthetic], dim=1))

        self._torch = torch
        self._transforms = type(
            "LARIQATransforms",
            (),
            {
                "authentic": transforms.Compose([
                    transforms.Resize((384, 384)),
                    transforms.ToTensor(),
                    transforms.Normalize(IMAGE_NET_MEAN, IMAGE_NET_STD),
                ]),
                "synthetic": transforms.Compose([
                    transforms.CenterCrop((1280, 1280)),
                    transforms.ToTensor(),
                    transforms.Normalize(IMAGE_NET_MEAN, IMAGE_NET_STD),
                ]),
            },
        )()
        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model = MobileNetMergedWithKAN()
        checkpoint = torch.load(self.model_path, map_location="cpu", weights_only=False)
        if isinstance(checkpoint, dict) and "state_dict" in checkpoint:
            checkpoint = checkpoint["state_dict"]
        model.load_state_dict(checkpoint, strict=True)
        model.to(self._device)
        model.eval()
        self._model = model


def default_model_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return root / "Archive Indexation" / "models" / LAR_IQA_MODEL_ID / LAR_IQA_MODEL_FILENAME


def create_quality_provider(
    selection: str | QualityProvider | None,
    *,
    batch_size: int = 1,
    preparation_workers: int = 8,
) -> QualityProvider:
    if not isinstance(selection, str) and selection is not None:
        return selection
    if selection in {None, QUALITY_PROVIDER_OFF}:
        return OffQualityProvider()
    if selection == QUALITY_PROVIDER_LAR_IQA:
        return LARIQAProvider(batch_size=batch_size, preparation_workers=preparation_workers)
    if selection == "legacy-pillow":
        return LegacyPillowProvider()
    raise QualityProviderError(f"unknown quality provider: {selection}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
