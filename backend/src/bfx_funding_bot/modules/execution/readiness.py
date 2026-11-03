"""Trading business-readiness state, deliberately separate from liveness."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from bfx_funding_bot.modules.execution.contracts import BlockReason

# Reason reported while a dependency's freshness heartbeat is stale
# (core.health.DEPENDENCY_THRESHOLDS); ``dependency`` names which one.
DEPENDENCY_STALE = "dependency_stale"


@dataclass(frozen=True, slots=True)
class ReadinessSnapshot:
    trading_ready: bool
    reason: str | None
    dependency: str | None


class TradingReadiness:
    """One daemon-owned readiness state shared by execution and operator views.

    Two inputs: the latest execution decision (``set_ready`` / ``set_blocked``,
    from the execution gate) and the set of stale dependencies. The health scan
    marks a dependency stale (``set_dependency_stale``); the dependency's own
    successful answer clears it at once (``clear_dependency``). Every dependency
    passed to the constructor starts stale: never seen since boot is not ready.
    Any stale dependency makes the snapshot not ready whatever the last decision
    was; the per-submit guards, not this state, are what block a submit.
    """

    def __init__(
        self, *, on_change: Callable[[bool], None] | None = None,
        dependencies: Iterable[str] = (),
    ) -> None:
        self._on_change = on_change
        self._decision = ReadinessSnapshot(
            trading_ready=False,
            reason="startup_not_ready",
            dependency="startup",
        )
        self._stale_dependencies: set[str] = set(dependencies)
        self._notify()

    def set_ready(self) -> None:
        self._decision = ReadinessSnapshot(True, None, None)
        self._notify()

    def set_blocked(self, reason: BlockReason, dependency: str) -> None:
        self._decision = ReadinessSnapshot(False, reason.value, dependency)
        self._notify()

    def set_dependency_stale(self, dependency: str) -> None:
        self._stale_dependencies.add(dependency)
        self._notify()

    def clear_dependency(self, dependency: str) -> None:
        self._stale_dependencies.discard(dependency)
        self._notify()

    def snapshot(self) -> ReadinessSnapshot:
        if self._stale_dependencies:
            return ReadinessSnapshot(False, DEPENDENCY_STALE, min(self._stale_dependencies))
        return self._decision

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(self.snapshot().trading_ready)
