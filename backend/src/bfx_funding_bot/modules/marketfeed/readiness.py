"""Trading business-readiness state, deliberately separate from liveness."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from bfx_funding_bot.modules.execution.contracts import BlockReason


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    trading_ready: bool
    reason: str | None
    dependency: str | None


class TradingReadiness:
    """One daemon-owned readiness state shared by execution and operator views."""

    def __init__(self, *, on_change: Callable[[bool], None] | None = None) -> None:
        self._on_change = on_change
        self._snapshot = ReadinessSnapshot(
            trading_ready=False,
            reason="startup_not_ready",
            dependency="startup",
        )
        self._notify()

    def set_ready(self) -> None:
        self._snapshot = ReadinessSnapshot(True, None, None)
        self._notify()

    def set_blocked(self, reason: BlockReason, dependency: str) -> None:
        self._snapshot = ReadinessSnapshot(False, reason.value, dependency)
        self._notify()

    def snapshot(self) -> ReadinessSnapshot:
        return self._snapshot

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(self._snapshot.trading_ready)
