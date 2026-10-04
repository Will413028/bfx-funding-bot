"""build_executor factory + env-driven invariant validation.

Env vars:
  BFX_FILL_TRACKER_ENABLED=false (default) | true
  BFX_WS_CLIENT_ENABLED=false (default) | true (required)

The executor is always the Bitfinex live executor; there is no other venue adapter
here. A ``BFX_EXECUTOR`` env value is not read.

CC4 invariant:
  - Without BFX_WS_CLIENT_ENABLED=true the executor is invalid (REST submit returns
    "submitted" but never fills -> stale exposure).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
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
    fill_tracker_enabled: bool
    ws_client_enabled: bool


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ("true", "1", "yes")


def build_executor(
    *,
    event_sink: _EventSink,
    phase: Phase,
    strategy: StrategyName,
    configured_symbols: frozenset[str] | None = None,
    cell: str,
    http: httpx.AsyncClient | None = None,
    bus: Any | None = None,  # DomainEventBus typed via Any to avoid circular ref
    auth_gate: AuthRequestGate | None = None,
) -> ExecutorSpec:
    fill_tracker_enabled = _env_bool("BFX_FILL_TRACKER_ENABLED", False)
    ws_client_enabled = _env_bool("BFX_WS_CLIENT_ENABLED", False)

    if not ws_client_enabled:
        raise ExecutorConfigError(
            "the Bitfinex executor without BFX_WS_CLIENT_ENABLED=true "
            "= stale exposure (REST submit returns 'submitted'; WS foc EXECUTED fills). "
            "Set BFX_WS_CLIENT_ENABLED=true."
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
        ),
        fill_tracker_enabled=fill_tracker_enabled,
        ws_client_enabled=ws_client_enabled,
    )
