"""build_executor factory + env-driven invariant validation.

Env vars:
  BFX_EXECUTOR=paper (4.2 default) | bitfinex_live (4.4+)
  BFX_FILL_TRACKER_ENABLED=false (default) | true (bitfinex_live only)
  BFX_WS_CLIENT_ENABLED=false (default) | true (required for bitfinex_live)

CC4 invariants:
  - paper + fill_tracker_enabled=true is invalid (paper offers don't exist at
    venue → fill_tracker would emit false cancelled events).
  - paper + ws_client_enabled=true is invalid (WS expects live venue offers;
    paper offers never reach venue).
  - bitfinex_live without BFX_WS_CLIENT_ENABLED=true is invalid (REST submit
    returns "submitted" but never fills → stale exposure).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.protocols import ExecutorPort
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName


class _EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class ExecutorConfigError(RuntimeError):
    """Inconsistent env config — daemon should not start."""


@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    executor: ExecutorPort
    fill_tracker_enabled: bool
    ws_client_enabled: bool
    is_simulated: bool  # paper -> True; bitfinex_live -> False (drives A2 event payload)


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
    cell: str,
    http: httpx.AsyncClient | None = None,
    bus: Any | None = None,  # DomainEventBus typed via Any to avoid circular ref
) -> ExecutorSpec:
    executor_raw = os.environ.get("BFX_EXECUTOR", "paper")
    executor_kind = executor_raw.lower()
    fill_tracker_enabled = _env_bool("BFX_FILL_TRACKER_ENABLED", False)
    ws_client_enabled = _env_bool("BFX_WS_CLIENT_ENABLED", False)

    # Paper invariants
    if executor_kind == "paper":
        if fill_tracker_enabled:
            raise ExecutorConfigError(
                "BFX_EXECUTOR=paper with BFX_FILL_TRACKER_ENABLED=true is invalid "
                "(CC4: paper offer_ids never appear at venue → would emit false "
                "cancelled events). Set both to defaults (paper / false) or wire "
                "BFX_EXECUTOR=bitfinex_live (4.4)."
            )
        if ws_client_enabled:
            raise ExecutorConfigError(
                "BFX_EXECUTOR=paper with BFX_WS_CLIENT_ENABLED=true is invalid "
                "(WS expects live venue offers; paper offers never reach venue)."
            )
        return ExecutorSpec(
            executor=EchoPaperExecutor(
                event_sink=event_sink, phase=phase, strategy=strategy, cell=cell,
            ),
            fill_tracker_enabled=False,
            ws_client_enabled=False,
            is_simulated=True,
        )

    # Live invariants
    if executor_kind == "bitfinex_live":
        if not os.environ.get("BFX_API_KEY") or not os.environ.get("BFX_API_SECRET"):
            raise ExecutorConfigError(
                "BFX_EXECUTOR=bitfinex_live requires BFX_API_KEY and BFX_API_SECRET"
            )
        if not ws_client_enabled:
            raise ExecutorConfigError(
                "BFX_EXECUTOR=bitfinex_live without BFX_WS_CLIENT_ENABLED=true "
                "= stale exposure (REST submit returns 'submitted'; WS fcn fills). "
                "Set BFX_WS_CLIENT_ENABLED=true."
            )
        if http is None or bus is None:
            raise ExecutorConfigError(
                "bitfinex_live requires http + bus deps to build_executor()"
            )
        from bfx_funding_bot.external.bitfinex.live_executor import BitfinexLiveExecutor
        return ExecutorSpec(
            executor=BitfinexLiveExecutor(
                http=http, event_sink=event_sink, bus=bus,
                phase=phase, strategy=strategy, cell=cell,
            ),
            fill_tracker_enabled=fill_tracker_enabled,
            ws_client_enabled=ws_client_enabled,
            is_simulated=False,
        )

    raise ExecutorConfigError(f"unknown BFX_EXECUTOR={executor_raw!r}")
