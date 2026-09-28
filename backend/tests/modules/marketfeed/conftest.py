"""Shared fixtures for marketfeed signal_engine tests."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.core.telemetry import Phase
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.execution.deployment.standing_quote import (
    StandingQuoteStore,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    StrategyRegistry,
    build_strategy,
)


class _EventCapture:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)


class _StubCandlesRepo:
    """Returns a small history so DivergenceReporter has enough data to run.

    Returns _history() so divergence reporter has >= 2 candles; this exercises
    the full process_candle code path the way the live daemon does.
    """
    async def get_up_to(self, **kw: Any) -> list[FundingCandle]:
        return _history(8)


def _cell() -> CellConfig:
    """Validated CellConfig matching the existing test_signal_engine pattern."""
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile",
        "symbol": "fUSD",
        "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},
        "reference_amount_usdt": 150.0,
    })
    cell.staleness_budget_hours = 2  # simulate load_config() resolution
    return cell


def _history(n: int) -> list[FundingCandle]:
    return [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=1747584000000 + i * 3600_000,
            open=Decimal(f"0.000{i+1}"), close=Decimal(f"0.000{i+1}"),
            high=Decimal(f"0.000{i+1}"), low=Decimal(f"0.000{i+1}"),
            volume=Decimal("100"),
        )
        for i in range(n)
    ]


def _candle() -> FundingCandle:
    """The boundary candle is history[7] (the 8th) — strategy already observed
    indices 0..6 so this is the new tick. Matches existing test pattern."""
    return _history(8)[-1]


def _make_registry(cell: CellConfig) -> StrategyRegistry:
    """Build a registry with the strategy pre-warmed on history[:-1]."""
    reg = StrategyRegistry()
    strategy = build_strategy(cell)
    for c in _history(7):
        strategy.observe(c)
    reg.put(cell, strategy)
    return reg


@pytest.fixture
def capture_engine() -> tuple:
    """Engine with a StandingQuoteStore; produces POST decisions (rate_percentile
    strategy on ascending-rate history resolves to POST)."""
    axiom = _EventCapture()
    diagnostics = _EventCapture()
    store = StandingQuoteStore(ttl_ms=3_900_000)
    cell = _cell()
    engine = SignalEngine(
        phase=Phase.PAPER, event_sink=axiom, diagnostics=diagnostics,
        candles_repo=_StubCandlesRepo(),
        quote_store=store,
        clock=lambda: 5_000,
    )
    registry = _make_registry(cell)
    return engine, axiom, diagnostics, store, cell, _candle(), registry


@pytest.fixture
def capture_engine_blocked() -> tuple:
    """Engine with a StandingQuoteStore; strategy on high-rate history resolves
    to SKIP because the boundary candle rate is well below the 75th-percentile
    threshold of the warm-up window.

    Window filled with 0.0009 (lookback_hours=5); boundary candle = 0.0001,
    so threshold = 0.0009 and decide() returns None → SKIP/below_threshold.

    Note: safety-block behavior (POST→SKIP via safety_chain) has moved to
    DeploymentReconciler and is tested in the deployment reconciler test suite.
    This fixture simulates a genuine strategy-level SKIP (below_threshold).
    """
    axiom = _EventCapture()
    diagnostics = _EventCapture()
    store = StandingQuoteStore(ttl_ms=3_900_000)
    cell = _cell()
    engine = SignalEngine(
        phase=Phase.PAPER, event_sink=axiom, diagnostics=diagnostics,
        candles_repo=_StubCandlesRepo(),
        quote_store=store,
        clock=lambda: 5_000,
    )
    # Build a registry where the strategy window is filled with HIGH rates so
    # the 75th-percentile threshold is high, then the boundary candle has a LOW
    # rate that is clearly below threshold → strategy emits None → SKIP/below_threshold.
    reg = StrategyRegistry()
    strategy = build_strategy(cell)
    # 7 warm-up candles at high rate (last 5 fill the lookback_hours=5 window
    # with 0.0009); boundary candle at 0.0001 → close(0.0001) < threshold(0.0009)
    # → decide() returns None → SKIP/below_threshold.
    high_candles = [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=1747584000000 + i * 3600_000,
            open=Decimal("0.0009"), close=Decimal("0.0009"),
            high=Decimal("0.0009"), low=Decimal("0.0009"),
            volume=Decimal("100"),
        )
        for i in range(7)
    ]
    boundary_candle = FundingCandle(
        symbol="fUSD", timeframe="1h", period_agg="a30",
        mts=1747584000000 + 7 * 3600_000,
        open=Decimal("0.0001"), close=Decimal("0.0001"),
        high=Decimal("0.0001"), low=Decimal("0.0001"),
        volume=Decimal("100"),
    )
    for c in high_candles:
        strategy.observe(c)
    reg.put(cell, strategy)
    return engine, axiom, diagnostics, store, cell, boundary_candle, reg
