"""Bounded, allowlisted stage timing for isolated restore diagnostics."""

from __future__ import annotations

import math
from collections.abc import Callable

_STAGES = frozenset(
    {
        "resource_setup",
        "physical_and_wal_recovery",
        "isolation_bootstrap",
        "verification",
        "cleanup",
    }
)


class StageTiming:
    """Measure non-overlapping restore stages with an injected monotonic clock."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._active_name: str | None = None
        self._active_started: float | None = None
        self._last_sample: float | None = None
        self._durations: dict[str, float] = {}

    def _validate_name(self, name: str) -> None:
        if name not in _STAGES:
            raise ValueError("stage_unknown")

    def _sample(self) -> float:
        try:
            sample = float(self._clock())
        except Exception as exc:
            raise ValueError("clock_invalid") from exc
        if not math.isfinite(sample):
            raise ValueError("clock_invalid")
        if self._last_sample is not None and sample < self._last_sample:
            raise ValueError("clock_backwards")
        self._last_sample = sample
        return sample

    def begin(self, name: str) -> None:
        self._validate_name(name)
        if name == self._active_name or name in self._durations:
            raise ValueError("stage_duplicate")
        if self._active_name is not None:
            raise ValueError("stage_overlap")
        started = self._sample()
        self._active_name = name
        self._active_started = started

    def end(self, name: str) -> None:
        self._validate_name(name)
        if self._active_name != name or self._active_started is None:
            raise ValueError("stage_not_active")
        ended = self._sample()
        self._durations[name] = ended - self._active_started
        self._active_name = None
        self._active_started = None

    def render(self) -> dict[str, float]:
        if self._active_name is not None:
            raise ValueError("stage_unclosed")
        return dict(self._durations)
