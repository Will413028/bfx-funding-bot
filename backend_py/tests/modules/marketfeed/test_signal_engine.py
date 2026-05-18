from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.config import CellConfig
from bfx_funding_bot.modules.marketfeed.schemas import Envelope, Phase
from bfx_funding_bot.modules.marketfeed.signal_engine import SignalEngine
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    StrategyRegistry,
    build_strategy,
)


def _cell() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},
        "reference_amount_usdt": 150.0,
    })


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


async def test_signal_engine_emits_signal_and_decision():
    captured: list[dict] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    candles_repo = MagicMock()
    candles_repo.get_up_to = AsyncMock(return_value=_history(8))

    engine = SignalEngine(phase=Phase.PAPER, axiom=axiom, candles_repo=candles_repo)
    cell = _cell()
    reg = StrategyRegistry()
    strategy = build_strategy(cell)
    for c in _history(7):
        strategy.observe(c)
    reg.put(cell, strategy)

    await engine.process_candle(cell=cell, candle=_history(8)[-1], registry=reg)

    types = [e["event_type"] for e in captured]
    assert "signal" in types
    assert "decision" in types


async def test_cp3_every_emit_passes_schema_validation():
    """CP3: every emitted event must validate against Envelope + payload model."""
    captured: list[dict] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    candles_repo = MagicMock()
    candles_repo.get_up_to = AsyncMock(return_value=_history(8))

    engine = SignalEngine(phase=Phase.PAPER, axiom=axiom, candles_repo=candles_repo)
    cell = _cell()
    reg = StrategyRegistry()
    strategy = build_strategy(cell)
    for c in _history(7):
        strategy.observe(c)
    reg.put(cell, strategy)

    await engine.process_candle(cell=cell, candle=_history(8)[-1], registry=reg)

    for event in captured:
        Envelope.model_validate(event)  # raises if schema violation
