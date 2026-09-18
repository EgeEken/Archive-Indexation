"""Shared lifetime and inference ownership for the active embedding model."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from threading import RLock

from .providers import EmbeddingProvider, create_embedding_provider


class EmbeddingRuntime:
    def __init__(self) -> None:
        self.providers: dict[tuple[str, str], EmbeddingProvider] = {}
        self.loads: dict[str, Future] = {}
        self._active_provider: str | None = None
        self._lock = RLock()
        self._inference_lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedding-runtime")

    def prepare(
        self,
        provider_id: str,
        *,
        batch_size: int = 16,
        precision: str = "fp32",
        factory=create_embedding_provider,
    ) -> Future:
        with self._lock:
            if self._active_provider != provider_id:
                old_provider = self._active_provider
                self._active_provider = provider_id
                self.loads.clear()
                if old_provider is not None:
                    self._executor.submit(self._release_provider_id, old_provider)
            current = self.loads.get(provider_id)
            if current is not None and current.done() and current.exception() is not None:
                self.loads.pop(provider_id, None)
                current = None
            if current is not None:
                return current
            current = self._loaded_future(provider_id)
            if current is not None:
                self.loads[provider_id] = current
                return current
            future = self._executor.submit(
                self._initialize,
                provider_id,
                batch_size,
                precision,
                factory,
            )
            self.loads[provider_id] = future
            return future

    def select(
        self,
        provider_id: str | None,
        *,
        factory=create_embedding_provider,
    ) -> Future | None:
        if provider_id is None:
            with self._lock:
                old_provider = self._active_provider
                self._active_provider = None
                self.loads.clear()
                if old_provider is not None:
                    return self._executor.submit(self._release_provider_id, old_provider)
            return None
        return self.prepare(provider_id, factory=factory)

    def state(self, provider_id: str) -> str:
        with self._lock:
            future = self.loads.get(provider_id)
            if future is None:
                return "available"
            if not future.done():
                return "loading"
            return "failed" if future.exception() else "ready"

    def provider(
        self,
        provider_id: str,
        *,
        batch_size: int = 16,
        precision: str = "fp32",
        factory=create_embedding_provider,
    ) -> EmbeddingProvider:
        future = self.prepare(
            provider_id,
            batch_size=batch_size,
            precision=precision,
            factory=factory,
        )
        future.result()
        with self._lock:
            for (loaded_id, _), provider in self.providers.items():
                if loaded_id == provider_id:
                    return provider
        raise RuntimeError(f"embedding provider is not loaded: {provider_id}")

    def loaded_provider(self, provider_id: str, version: str | None = None) -> EmbeddingProvider:
        with self._lock:
            for (loaded_id, loaded_version), provider in self.providers.items():
                if loaded_id == provider_id and (version is None or loaded_version == version):
                    return provider
        raise RuntimeError(f"embedding provider is not loaded: {provider_id}")

    def run(self, provider_id: str, operation, *, factory=create_embedding_provider):
        self.provider(provider_id, factory=factory)
        with self._inference_lock:
            with self._lock:
                if self._active_provider != provider_id:
                    raise RuntimeError(f"embedding provider was replaced: {provider_id}")
                provider = next(
                    provider
                    for (loaded_id, _), provider in self.providers.items()
                    if loaded_id == provider_id
                )
            return operation(provider)

    def release_all(self) -> Future:
        with self._lock:
            old_provider = self._active_provider
            self._active_provider = None
            self.loads.clear()
            if old_provider is None:
                return _completed_future()
            return self._executor.submit(self._release_provider_id, old_provider)

    def _initialize(self, provider_id, batch_size, precision, factory) -> None:
        candidate = factory(provider_id, batch_size=batch_size, precision=precision)
        with self._inference_lock:
            candidate.preflight()
        with self._lock:
            if self._active_provider != provider_id:
                self._release(candidate)
                return
            self.providers[(provider_id, candidate.version)] = candidate

    def _loaded_future(self, provider_id: str) -> Future | None:
        for (loaded_id, _), provider in self.providers.items():
            if loaded_id == provider_id:
                future = _completed_future()
                return future
        return None

    def _release_provider_id(self, provider_id: str) -> None:
        with self._inference_lock:
            with self._lock:
                providers = [
                    provider
                    for (loaded_id, _), provider in self.providers.items()
                    if loaded_id == provider_id
                ]
                for key in list(self.providers):
                    if key[0] == provider_id:
                        self.providers.pop(key, None)
            for provider in providers:
                self._release(provider)

    @staticmethod
    def _release(provider: EmbeddingProvider) -> None:
        torch = getattr(provider, "_torch", None)
        if torch is not None and torch.cuda.is_available():
            torch.cuda.empty_cache()
        for name in ("_model", "_preprocess", "_tokenizer", "_processor", "_device", "_torch"):
            if hasattr(provider, name):
                setattr(provider, name, None)


def _completed_future() -> Future:
    future = Future()
    future.set_result(None)
    return future


embedding_runtime = EmbeddingRuntime()
