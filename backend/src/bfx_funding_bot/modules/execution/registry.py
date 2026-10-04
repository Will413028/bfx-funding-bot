"""build_executor factory + per-venue invariant validation.

Env vars:
  BFX_FILL_TRACKER_ENABLED=false (default) | true
  BFX_WS_CLIENT_ENABLED=false (default) | true

The executor is always the Bitfinex live executor: on the ``simulated`` venue it talks to
the simulated venue's own ``httpx`` client, so the code that runs is the code that runs
live. ``BFX_EXECUTOR`` is refused, not ignored.

Venue invariants (CC4):
  - ``bitfinex`` needs the auth WS (``BFX_WS_CLIENT_ENABLED=true``): without it a REST
    submit returns "submitted" but never fills -> stale exposure.
  - ``simulated`` has no WebSocket and no REST fill tracker: both flags must be off, so a
    process cannot be configured to expect a channel the venue does not have.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import httpx

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.core.venue import Venue
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
    fill_tracker_enabled: bool
    ws_client_enabled: bool


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ("true", "1", "yes")


def build_executor(
    *,
    venue: Venue,
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
    fill_tracker_enabled = _env_bool("BFX_FILL_TRACKER_ENABLED", False)
    ws_client_enabled = _env_bool("BFX_WS_CLIENT_ENABLED", False)

    if venue == "bitfinex" and not ws_client_enabled:
        raise ExecutorConfigError(
            "the Bitfinex executor without BFX_WS_CLIENT_ENABLED=true "
            "= stale exposure (REST submit returns 'submitted'; WS foc EXECUTED fills). "
            "Set BFX_WS_CLIENT_ENABLED=true."
        )
    if venue == "simulated" and (ws_client_enabled or fill_tracker_enabled):
        raise ExecutorConfigError(
            "the simulated venue has no WebSocket and no REST fill tracker: unset "
            "BFX_WS_CLIENT_ENABLED and BFX_FILL_TRACKER_ENABLED (fills are found by the "
            "periodic reconcile)."
        )
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
        fill_tracker_enabled=fill_tracker_enabled,
        ws_client_enabled=ws_client_enabled,
    )
