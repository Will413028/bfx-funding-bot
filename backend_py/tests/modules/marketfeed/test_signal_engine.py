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
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
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

    # Non-divergent path: exactly 2 events emitted (signal + decision)
    assert len(captured) == 2
    for event in captured:
        Envelope.model_validate(event)  # raises if schema violation


async def test_cp3_divergence_path_also_passes_schema():
    """CP3 (divergence variant): when reporter detects divergence, the warn-level
    signal event with divergence_detail must also validate against schema."""
    captured: list[dict] = []
    axiom = MagicMock()
    axiom.emit = AsyncMock(side_effect=lambda e: captured.append(e))

    candles_repo = MagicMock()
    candles_repo.get_up_to = AsyncMock(return_value=_history(8))

    # Mock reporter that always returns a divergence dict
    reporter = MagicMock()
    reporter.check = MagicMock(return_value={
        "live": {"signal_score": 1.0, "signal_direction": "post",
                 "strategy_attributes": {}},
        "replay": {"signal_score": -1.0, "signal_direction": "skip",
                   "strategy_attributes": {}},
        "diff_fields": ["signal_score", "signal_direction"],
    })

    engine = SignalEngine(
        phase=Phase.PAPER, axiom=axiom, candles_repo=candles_repo, reporter=reporter,
    )
    cell = _cell()
    reg = StrategyRegistry()
    strategy = build_strategy(cell)
    for c in _history(7):
        strategy.observe(c)
    reg.put(cell, strategy)

    await engine.process_candle(cell=cell, candle=_history(8)[-1], registry=reg)

    # Divergent path: 3 events (signal info + signal warn + decision)
    assert len(captured) == 3
    types_and_levels = [(e["event_type"], e["level"]) for e in captured]
    assert ("signal", "info") in types_and_levels
    assert ("signal", "warn") in types_and_levels
    assert ("decision", "info") in types_and_levels

    # All must validate
    for event in captured:
        Envelope.model_validate(event)

    # Warn-level signal must carry divergence_detail
    warn_signals = [e for e in captured
                    if e["event_type"] == "signal" and e["level"] == "warn"]
    assert len(warn_signals) == 1
    assert warn_signals[0]["payload"]["divergence_detail"] is not None


def test_resolve_budget_seconds_raises_if_staleness_not_resolved() -> None:
    """Loader invariant: cell.staleness_budget_hours must be set before emit.

    None at emit time = loader bypassed = AssertionError to surface the bug.
    """
    import pytest
    from bfx_funding_bot.modules.marketfeed.signal_engine import _resolve_budget_seconds

    cell = CellConfig(
        strategy="mean_reversion",
        symbol="fUSD",
        period_agg="p30",
        timeframe="1h",
        params={"threshold_sigma": 1.0, "ratio_sigma": 0.5, "ema_alpha": 0.01},
        reference_amount_usdt=150.0,
        staleness_budget_hours=None,  # simulate loader bypass
    )
    with pytest.raises(AssertionError, match="staleness_budget_hours not resolved"):
        _resolve_budget_seconds(cell)


def test_resolve_budget_seconds_correct_value() -> None:
    """Resolved cell returns hours * 3600."""
    from bfx_funding_bot.modules.marketfeed.signal_engine import _resolve_budget_seconds

    cell = CellConfig(
        strategy="rate_percentile",
        symbol="fUSD",
        period_agg="p30",
        timeframe="1h",
        params={"percentile": 75, "lookback_hours": 168},
        reference_amount_usdt=150.0,
        staleness_budget_hours=12,
    )
    assert _resolve_budget_seconds(cell) == 12 * 3600


def test_resolve_staleness_budget_hours_raises_if_not_resolved() -> None:
    """Loader invariant: cell.staleness_budget_hours must be set before processing.

    None at signal_engine.process_candle time = loader bypassed = AssertionError
    (parallel to test_resolve_budget_seconds_raises_if_staleness_not_resolved).
    """
    import pytest
    from bfx_funding_bot.modules.marketfeed.signal_engine import _resolve_staleness_budget_hours

    cell = CellConfig(
        strategy="mean_reversion",
        symbol="fUSD",
        period_agg="p30",
        timeframe="1h",
        params={"threshold_sigma": 1.0, "ratio_sigma": 0.5, "ema_alpha": 0.01},
        reference_amount_usdt=150.0,
        staleness_budget_hours=None,
    )
    with pytest.raises(AssertionError, match="staleness_budget_hours not resolved"):
        _resolve_staleness_budget_hours(cell)


def test_resolve_staleness_budget_hours_correct_value() -> None:
    """Resolved cell returns hours value as-is."""
    from bfx_funding_bot.modules.marketfeed.signal_engine import _resolve_staleness_budget_hours

    cell = CellConfig(
        strategy="rate_percentile",
        symbol="fUSD",
        period_agg="p30",
        timeframe="1h",
        params={"percentile": 75, "lookback_hours": 168},
        reference_amount_usdt=150.0,
        staleness_budget_hours=12,
    )
    assert _resolve_staleness_budget_hours(cell) == 12
