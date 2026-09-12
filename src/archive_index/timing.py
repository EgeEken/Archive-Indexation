"""Small internal timing recorder for indexing diagnostics."""

from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from time import perf_counter
from typing import Iterator


@dataclass
class TimingRecorder:
    seconds: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @contextmanager
    def measure(self, name: str, count: int = 0) -> Iterator[None]:
        started = perf_counter()
        try:
            yield
        finally:
            self.seconds[name] += perf_counter() - started
            if count:
                self.counts[name] += count

    def add(self, name: str, seconds: float, count: int = 0) -> None:
        self.seconds[name] += seconds
        if count:
            self.counts[name] += count

    def summary(self) -> dict[str, object]:
        return {
            "seconds": dict(self.seconds),
            "counts": dict(self.counts),
        }


def timed(recorder: TimingRecorder | None, name: str, count: int = 0):
    return recorder.measure(name, count) if recorder is not None else nullcontext()
