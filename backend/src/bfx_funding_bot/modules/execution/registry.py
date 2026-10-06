"""build_executor factory.

The executor is always the Bitfinex live executor: on the ``simulated`` venue it talks to
the simulated venue's own ``httpx`` client, so the code that runs is the code that runs
live. ``BFX_EXECUTOR`` is refused, not ignored.

Whether the authenticated WebSocket runs is not the executor's to decide: the composition
root reads it from the venue wiring's ``VenueCapabilities`` (CC4), never a flag or a name.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import httpx

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
from bfx_funding_bot.external.bitfinex.nonce import AuthRequestGate
from bfx_funding_bot.modules.execution.protocols import ExecutorPort
from bfx_funding_bot.modules.strategy import StrategyName


class _EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class ExecutorConfigError(RuntimeError):
    """Inconsistent env config — daemon should not start."""


@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    executor: ExecutorPort


def build_executor(
    *,
    event_sink: _EventSink,
    phase: Phase,
    strategy: StrategyName,
    cell: str,
    clock: Callable[[], int],
    date_provider: Callable[[], date],
    configured_symbols: frozenset[str] | None = None,
    http: httpx.AsyncClient | None = None,
    bus: Any | None = None,  # DomainEventBus typed via Any to avoid circular ref
    auth_gate: AuthRequestGate | None = None,
) -> ExecutorSpec:
    if "BFX_EXECUTOR" in os.environ:
        raise ExecutorConfigError("BFX_EXECUTOR is removed: the venue follows BFX_PHASE")
    if http is None or bus is None:
        raise ExecutorConfigError("build_executor() requires http + bus deps")
    return ExecutorSpec(
        executor=BitfinexLiveExecutor(
            http=http, event_sink=event_sink, bus=bus,
            phase=phase, strategy=strategy,
            configured_symbols=configured_symbols or frozenset(),
            cell=cell,
            auth_gate=auth_gate,
            clock=clock,
            date_provider=date_provider,
        ),
    )
