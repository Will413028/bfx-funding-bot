"""L2 calibrated guards.

Threshold values come from 4.3 G2 calibration; all disabled in 4.2 default
config. Enabled paths are still tested (Coverage gate prevents dead-code rot).

Each guard receives an injected `source` that provides the metric it needs;
4.4 wiring will pass real implementations (PnL ledger, divergence reporter).
4.2 only ships the guard logic + protocol — actual sources are stubbed in
build_daemon and never reached because enabled=false short-circuits.
"""
from __future__ import annotations

from typing import Protocol

from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    GuardResult,
)
from bfx_funding_bot.modules.marketfeed.schemas import DecisionPayload


class _PnLSourceProtocol(Protocol):
    def realized_loss_pct_24h(self) -> float: ...
    def drawdown_pct(self) -> float: ...


class _DivergenceSourceProtocol(Protocol):
    def divergence_rate_pct(self, window_minutes: int) -> float: ...


class RealizedLossGuard:
    name = "realized_loss_24h"
    is_calibrated = True

    def __init__(
        self, *, enabled: bool, threshold_pct: float | None,
        source: _PnLSourceProtocol,
    ) -> None:
        if enabled and threshold_pct is None:
            raise ValueError("enabled=True requires threshold_pct")
        self.enabled = enabled
        self.threshold_pct = threshold_pct
        self.source = source

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        if not self.enabled:
            return GuardResult(allowed=True, guard_name=self.name)
        assert self.threshold_pct is not None  # narrow for mypy
        loss_pct = self.source.realized_loss_pct_24h()
        if loss_pct > self.threshold_pct:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=f"realized_loss_pct_24h={loss_pct} > threshold_pct={self.threshold_pct}",
            )
        return GuardResult(allowed=True, guard_name=self.name)


class DrawdownGuard:
    name = "drawdown_from_peak"
    is_calibrated = True

    def __init__(
        self, *, enabled: bool, threshold_pct: float | None,
        source: _PnLSourceProtocol,
    ) -> None:
        if enabled and threshold_pct is None:
            raise ValueError("enabled=True requires threshold_pct")
        self.enabled = enabled
        self.threshold_pct = threshold_pct
        self.source = source

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        if not self.enabled:
            return GuardResult(allowed=True, guard_name=self.name)
        assert self.threshold_pct is not None
        dd = self.source.drawdown_pct()
        if dd > self.threshold_pct:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=f"drawdown={dd:.4f} > threshold={self.threshold_pct}",
            )
        return GuardResult(allowed=True, guard_name=self.name)


class DivergenceRateGuard:
    name = "divergence_rate"
    is_calibrated = True

    def __init__(
        self, *, enabled: bool, threshold_pct: float | None,
        window_minutes: int | None, source: _DivergenceSourceProtocol,
    ) -> None:
        if enabled and (threshold_pct is None or window_minutes is None):
            raise ValueError("enabled=True requires threshold_pct + window_minutes")
        self.enabled = enabled
        self.threshold_pct = threshold_pct
        self.window_minutes = window_minutes
        self.source = source

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult:
        if not self.enabled:
            return GuardResult(allowed=True, guard_name=self.name)
        assert self.threshold_pct is not None
        assert self.window_minutes is not None
        rate = self.source.divergence_rate_pct(self.window_minutes)
        if rate > self.threshold_pct:
            return GuardResult(
                allowed=False, guard_name=self.name,
                reason=f"divergence_rate={rate:.4f} > threshold={self.threshold_pct}",
            )
        return GuardResult(allowed=True, guard_name=self.name)
