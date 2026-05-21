"""build_executor factory + env-driven invariant validation.

Env vars:
  BFX_EXECUTOR=paper (4.2 default) | bitfinex_live (4.4 only — raises 4.2)
  BFX_FILL_TRACKER_ENABLED=false (4.2 default) | true (4.4 only with bitfinex_live)

CC4 invariant: paper + fill_tracker_enabled=true is invalid (paper offers
don't exist at venue → fill_tracker would emit false cancelled events).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Protocol

from bfx_funding_bot.modules.execution.paper import EchoPaperExecutor
from bfx_funding_bot.modules.execution.protocols import ExecutorPort
from bfx_funding_bot.modules.marketfeed.schemas import Phase, StrategyName


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


class ExecutorConfigError(RuntimeError):
    """Inconsistent env config — daemon should not start."""


@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    executor: ExecutorPort
    fill_tracker_enabled: bool


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.lower() in ("true", "1", "yes")


def build_executor(
    *,
    axiom: _AxiomProtocol,
    phase: Phase,
    strategy: StrategyName,
    cell: str,
) -> ExecutorSpec:
    executor_raw = os.environ.get("BFX_EXECUTOR", "paper")
    executor_kind = executor_raw.lower()
    fill_tracker_enabled = _env_bool("BFX_FILL_TRACKER_ENABLED", False)

    if executor_kind == "paper" and fill_tracker_enabled:
        raise ExecutorConfigError(
            "BFX_EXECUTOR=paper with BFX_FILL_TRACKER_ENABLED=true is invalid "
            "(CC4: paper offer_ids never appear at venue → would emit false "
            "cancelled events). Set both to defaults (paper / false) or wire "
            "BFX_EXECUTOR=bitfinex_live (4.4)."
        )

    if executor_kind == "paper":
        return ExecutorSpec(
            executor=EchoPaperExecutor(
                axiom=axiom, phase=phase, strategy=strategy, cell=cell,
            ),
            fill_tracker_enabled=False,
        )

    if executor_kind == "bitfinex_live":
        raise ExecutorConfigError(
            "BFX_EXECUTOR=bitfinex_live not wired in 4.2 — Phase 4.4 ships "
            "BitfinexLiveExecutor. Until then use BFX_EXECUTOR=paper."
        )

    raise ExecutorConfigError(f"unknown BFX_EXECUTOR={executor_raw!r}")
