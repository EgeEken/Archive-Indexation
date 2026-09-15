"""Common interface for the two supported local semantic models."""

from __future__ import annotations

from contextlib import nullcontext
from time import perf_counter
from threading import Lock

from PIL import Image

from .models import (
    OPENCLIP_CHECKPOINT,
    OPENCLIP_DIMENSION,
    OPENCLIP_MODEL_ID,
    OPENCLIP_PROVIDER,
    OPENCLIP_VERSION,
    SIGLIP_DIMENSION,
    SIGLIP_MODEL_ID,
    SIGLIP_PROVIDER,
    SIGLIP_VERSION,
    model_cache_dir,
    model_status,
)
from .vector import normalize_vector


class EmbeddingProviderError(RuntimeError):
    """An embedding provider cannot be used for the requested operation."""


class EmbeddingProviderUnavailable(EmbeddingProviderError):
    """The selected embedding runtime or model is not installed."""


class EmbeddingProvider:
    provider_id: str
    model_id: str
    version: str
    dimension: int
    batch_size: int

    def preflight(self) -> None:
        raise NotImplementedError

    def encode_images(self, images: list[Image.Image]):
        raise NotImplementedError

    def prepare_images(self, images: list[Image.Image]):
        return images

    def encode_prepared_images(self, prepared):
        return self.encode_images(prepared)

    def encode_text(self, text: str):
        raise NotImplementedError

    @property
    def settings(self) -> dict[str, object]:
        raise NotImplementedError


class OpenCLIPProvider(EmbeddingProvider):
    provider_id = OPENCLIP_PROVIDER
    model_id = OPENCLIP_MODEL_ID
    version = OPENCLIP_VERSION
    dimension = OPENCLIP_DIMENSION

    def __init__(self, *, batch_size: int = 16, precision: str = "fp32") -> None:
        self.batch_size = batch_size
        self.precision = precision
        self._torch = None
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._device = None
        self.last_timings: dict[str, float] = {}
        self._timing_lock = Lock()

    @property
    def settings(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "checkpoint": OPENCLIP_CHECKPOINT,
            "version": self.version,
            "dimension": self.dimension,
            "normalization": "unit_l2",
            "similarity": "cosine_dot_product",
            "image_preprocessing": "open_clip_default_v1",
            "text_preprocessing": "open_clip_tokenizer_v1",
            "batch_size": self.batch_size,
            "precision": self.precision,
        }

    def preflight(self) -> None:
        self._load()

    def encode_images(self, images: list[Image.Image]):
        return self.encode_prepared_images(self.prepare_images(images))

    def prepare_images(self, images: list[Image.Image]):
        self._load()
        started = perf_counter()
        tensors = [self._preprocess(image) for image in images]
        self._record_timing("embedding.preprocessing", perf_counter() - started)
        return self._torch.stack(tensors)

    def encode_prepared_images(self, prepared):
        self._load()
        return self._encode_tensor_batch(prepared, image=True)

    def encode_text(self, text: str):
        self._load()
        tokens = self._tokenizer([text])
        with self._torch.inference_mode():
            output = self._model.encode_text(tokens.to(self._device))
        return normalize_vector(output.detach().cpu().numpy()[0])

    def _encode_tensor_batch(self, tensors, *, image: bool):
        started = perf_counter()
        torch = self._torch
        autocast = torch.autocast(device_type="cuda", dtype=torch.float16) if self.precision == "fp16" and self._device.type == "cuda" else nullcontext()
        with torch.inference_mode(), autocast:
            output = self._model.encode_image(tensors.to(self._device)) if image else self._model.encode_text(tensors.to(self._device))
        self._record_timing("embedding.inference", perf_counter() - started)
        import numpy as np

        value = output.detach().float().cpu().numpy()
        return value / np.linalg.norm(value, axis=1, keepdims=True)

    def _record_timing(self, name: str, elapsed: float) -> None:
        with self._timing_lock:
            self.last_timings[name] = self.last_timings.get(name, 0.0) + elapsed

    def _load(self) -> None:
        if self._model is not None:
            return
        status = model_status(self.provider_id)
        if not status["installed"]:
            raise EmbeddingProviderUnavailable(
                "OpenCLIP B/16 model is not installed; run `archive-index model install openclip-b16`"
            )
        try:
            import open_clip
            import torch
        except ImportError as error:
            raise EmbeddingProviderUnavailable("install the embeddings extra before using OpenCLIP") from error
        try:
            model, _, preprocess = open_clip.create_model_and_transforms(
                self.model_id,
                pretrained=OPENCLIP_CHECKPOINT,
                cache_dir=str(model_cache_dir(self.provider_id)),
                device="cuda" if torch.cuda.is_available() else "cpu",
            )
            model.eval()
            self._torch = torch
            self._model = model
            self._preprocess = preprocess
            self._tokenizer = open_clip.get_tokenizer(self.model_id)
            self._device = next(model.parameters()).device
        except Exception as error:
            raise EmbeddingProviderUnavailable(f"OpenCLIP could not be loaded: {error}") from error


class SigLIP2Provider(EmbeddingProvider):
    provider_id = SIGLIP_PROVIDER
    model_id = SIGLIP_MODEL_ID
    version = SIGLIP_VERSION
    dimension = SIGLIP_DIMENSION

    def __init__(self, *, batch_size: int = 16, precision: str = "fp32") -> None:
        self.batch_size = batch_size
        self.precision = precision
        self._torch = None
        self._model = None
        self._processor = None
        self._device = None
        self.last_timings: dict[str, float] = {}
        self._timing_lock = Lock()

    @property
    def settings(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "dimension": self.dimension,
            "normalization": "unit_l2",
            "similarity": "cosine_dot_product",
            "image_preprocessing": "siglip2_processor_v1",
            "text_preprocessing": "siglip2_processor_v1",
            "batch_size": self.batch_size,
            "precision": self.precision,
        }

    def preflight(self) -> None:
        self._load()

    def encode_images(self, images: list[Image.Image]):
        return self.encode_prepared_images(self.prepare_images(images))

    def prepare_images(self, images: list[Image.Image]):
        self._load()
        started = perf_counter()
        inputs = self._processor(images=images, return_tensors="pt")
        self._record_timing("embedding.preprocessing", perf_counter() - started)
        return inputs

    def encode_prepared_images(self, prepared):
        self._load()
        return self._encode_inputs(prepared, "image")

    def encode_text(self, text: str):
        self._load()
        inputs = self._processor(text=[text], padding="max_length", return_tensors="pt")
        return normalize_vector(self._encode_inputs(inputs, "text")[0])

    def _encode_inputs(self, inputs, kind: str):
        torch = self._torch
        values = {key: value.to(self._device) for key, value in inputs.items() if hasattr(value, "to")}
        autocast = torch.autocast(device_type="cuda", dtype=torch.float16) if self.precision == "fp16" and self._device.type == "cuda" else nullcontext()
        started = perf_counter()
        with torch.inference_mode(), autocast:
            if kind == "image":
                output = self._model.get_image_features(**values)
            else:
                output = self._model.get_text_features(**values)
        self._record_timing("embedding.inference", perf_counter() - started)
        if hasattr(output, "pooler_output"):
            output = output.pooler_output
        import numpy as np

        value = output.detach().float().cpu().numpy()
        return value / np.linalg.norm(value, axis=1, keepdims=True)

    def _record_timing(self, name: str, elapsed: float) -> None:
        with self._timing_lock:
            self.last_timings[name] = self.last_timings.get(name, 0.0) + elapsed

    def _load(self) -> None:
        if self._model is not None:
            return
        status = model_status(self.provider_id)
        if not status["installed"]:
            raise EmbeddingProviderUnavailable(
                "SigLIP2 Base model is not installed; run `archive-index model install siglip2-base`"
            )
        try:
            import torch
            from transformers import AutoModel, AutoProcessor
        except ImportError as error:
            raise EmbeddingProviderUnavailable("install the embeddings extra before using SigLIP2") from error
        try:
            cache = model_cache_dir(self.provider_id)
            self._processor = AutoProcessor.from_pretrained(cache, local_files_only=True)
            self._model = AutoModel.from_pretrained(cache, local_files_only=True)
            self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            self._model.to(self._device)
            self._model.eval()
            self._torch = torch
        except Exception as error:
            raise EmbeddingProviderUnavailable(f"SigLIP2 could not be loaded: {error}") from error


def create_embedding_provider(provider: str, *, batch_size: int = 16, precision: str = "fp32") -> EmbeddingProvider:
    if provider == OPENCLIP_PROVIDER:
        return OpenCLIPProvider(batch_size=batch_size, precision=precision)
    if provider == SIGLIP_PROVIDER:
        return SigLIP2Provider(batch_size=batch_size, precision=precision)
    raise EmbeddingProviderError(f"unsupported embedding provider: {provider}")
