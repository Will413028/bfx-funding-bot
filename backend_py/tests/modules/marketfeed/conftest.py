"""Shared fixtures for marketfeed phase 4.2 tests."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    Credentials,
    GuardResult,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    Phase,
)
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    StrategyRegistry,
    build_strategy,
)


class _CaptureAxiom:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def emit(self, ev: dict[str, Any]) -> None:
        self.events.append(ev)


class _AllowChain:
    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        return GuardResult(allowed=True, guard_name="ok")


class _BlockChain:
    async def evaluate(self, d: DecisionPayload, c: AccountContext) -> GuardResult:
        return GuardResult(allowed=False, guard_name="cap", reason="over_cap")


class _SpyExecutor:
    def __init__(self) -> None:
        self.calls: list[DecisionPayload] = []

    async def submit(self, d: DecisionPayload, c: AccountContext) -> SubmittedOrder:
        self.calls.append(d)
        return SubmittedOrder(
            cid=1, venue_offer_id="paper_x", status="filled", raw_response=None,
        )


class _StubCandlesRepo:
    """Returns a small history so DivergenceReporter has enough data to run.

    Returns _history() so divergence reporter has >= 2 candles; this exercises
    the full process_candle code path the way the live daemon does.
    """
    async def get_up_to(self, **kw: Any) -> list[FundingCandle]:
        return _history(8)


def _ctx() -> AccountContext:
    return AccountContext("default", Credentials("k", "s"), Decimal("500"))


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
    """Engine wired with _AllowChain — safety eval passes, executor.submit called."""
    axiom = _CaptureAxiom()
    chain = _AllowChain()
    executor = _SpyExecutor()
    cell = _cell()
    engine = SignalEngine(
        phase=Phase.PAPER, axiom=axiom, candles_repo=_StubCandlesRepo(),
        safety_chain=chain, executor=executor, account_ctx=_ctx(),
    )
    engine._test_registry = _make_registry(cell)  # type: ignore[attr-defined]
    return engine, axiom, executor, chain, cell, _candle()


@pytest.fixture
def capture_engine_blocked() -> tuple:
    """Engine wired with _BlockChain — safety eval blocks, executor NOT called."""
    axiom = _CaptureAxiom()
    chain = _BlockChain()
    executor = _SpyExecutor()
    cell = _cell()
    engine = SignalEngine(
        phase=Phase.PAPER, axiom=axiom, candles_repo=_StubCandlesRepo(),
        safety_chain=chain, executor=executor, account_ctx=_ctx(),
    )
    engine._test_registry = _make_registry(cell)  # type: ignore[attr-defined]
    return engine, axiom, executor, chain, cell, _candle()
