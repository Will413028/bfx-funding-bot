from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.strategy_registry import (
    StrategyRegistry,
    build_strategy,
    build_strategy_at_boundary,
)
from bfx_funding_bot.modules.strategy import (
    CellConfig,
    MeanReversionStrategy,
    RatePercentileStrategy,
    StrategyName,
)


def _cell_mr() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "mean_reversion", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"threshold_sigma": 1.5, "ratio_sigma": 0.0042, "ema_span": 100},
        "reference_amount_usdt": 150.0,
    })


def _cell_rp() -> CellConfig:
    return CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 168},
        "reference_amount_usdt": 150.0,
    })


def test_build_strategy_mean_reversion():
    s = build_strategy(_cell_mr())
    assert isinstance(s, MeanReversionStrategy)


def test_build_strategy_rate_percentile():
    s = build_strategy(_cell_rp())
    assert isinstance(s, RatePercentileStrategy)


def test_registry_put_get():
    cell = _cell_mr()
    reg = StrategyRegistry()
    s = build_strategy(cell)
    reg.put(cell, s)
    assert reg.get(cell) is s


def test_registry_missing_raises():
    reg = StrategyRegistry()
    with pytest.raises(KeyError):
        reg.get(_cell_mr())


def test_build_strategy_at_boundary_is_deterministic():
    """Phase 4.3 LOCF SoT — given the same (history, ref_mts, budget_hours)
    the function produces deterministically equal Strategy state. This is
    the invariant warmup and divergence_reporter rely on for CP1
    byte-equivalence."""
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 10},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })
    base_mts = 1747584000000
    history = [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="a30",
            mts=base_mts + i * 3600_000,
            open=Decimal(f"0.0001{i}"), close=Decimal(f"0.0001{i}"),
            high=Decimal(f"0.0001{i}"), low=Decimal(f"0.0001{i}"),
            volume=Decimal("100"),
        )
        for i in range(8)
    ]
    ref_mts = base_mts + 8 * 3600_000

    r1 = build_strategy_at_boundary(
        cell=cell, history=history, ref_mts=ref_mts, budget_hours=2,
    )
    r2 = build_strategy_at_boundary(
        cell=cell, history=history, ref_mts=ref_mts, budget_hours=2,
    )

    # observed_count identical
    assert r1.observed_count == r2.observed_count
    # Strategy internal state identical (compare _window deque for RP).
    assert list(r1.strategy._window) == list(r2.strategy._window)  # type: ignore[attr-defined]


def test_build_strategy_at_boundary_sparse_locf_symmetry():
    """Phase 4.3 LOCF SoT — sparse history (candles only at slots 0/3/6)
    yields LOCF-filled state with 7 observations of the 3 source candles
    (carry-forward), with the boundary slot dropped. This is the contract
    warmup.warmup_cell and DivergenceReporter.check both depend on.
    """
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "p30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 10},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 12,
    })
    base_mts = 1747584000000
    sparse_history = [
        FundingCandle(
            symbol="fUSD", timeframe="1h", period_agg="p30",
            mts=base_mts + i * 3600_000,
            open=Decimal(f"0.001{i}"), close=Decimal(f"0.001{i}"),
            high=Decimal(f"0.001{i}"), low=Decimal(f"0.001{i}"),
            volume=Decimal("100"),
        )
        for i in (0, 3, 6)
    ]
    ref_mts = base_mts + 7 * 3600_000  # 8 slots (0..7), drop slot 7 boundary

    result = build_strategy_at_boundary(
        cell=cell, history=sparse_history, ref_mts=ref_mts, budget_hours=12,
    )

    # filled slots: 0..7 (8 total). Drop slot 7 (boundary). Slots 0..6 = 7 slots.
    # Each within budget → all non-None → 7 observations.
    assert result.observed_count == 7
    # _window content traces LOCF carry-forward:
    # slots 0,1,2: candle at slot 0 (0.0010)
    # slots 3,4,5: candle at slot 3 (0.0013)
    # slot 6: candle at slot 6 (0.0016)
    expected = [
        Decimal("0.0010"), Decimal("0.0010"), Decimal("0.0010"),
        Decimal("0.0013"), Decimal("0.0013"), Decimal("0.0013"),
        Decimal("0.0016"),
    ]
    assert list(result.strategy._window) == expected  # type: ignore[attr-defined]


def test_build_strategy_at_boundary_empty_history():
    """No DB candles → empty strategy state, observed_count=0."""
    cell = CellConfig.model_validate({
        "strategy": "rate_percentile", "symbol": "fUSD", "period_agg": "a30",
        "timeframe": "1h",
        "params": {"percentile": 75, "lookback_hours": 5},
        "reference_amount_usdt": 150.0,
        "staleness_budget_hours": 2,
    })
    result = build_strategy_at_boundary(
        cell=cell, history=[], ref_mts=1747584000000, budget_hours=2,
    )
    assert result.observed_count == 0
    assert isinstance(result.strategy, RatePercentileStrategy)
    assert list(result.strategy._window) == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ema_span schema tests (Tier 2 param pipeline: drop ema_alpha round-trip)
# ---------------------------------------------------------------------------


def test_build_mean_reversion_reads_ema_span():
    cell = CellConfig(
        strategy=StrategyName.MEAN_REVERSION, symbol="fUST", period_agg="a30",
        params={"ema_span": 24, "threshold_sigma": 0.5, "ratio_sigma": 0.9915},
    )
    strat = build_strategy(cell)
    # MeanReversionStrategy stores _ema_span directly; no alpha round-trip.
    assert strat._ema_span == 24  # type: ignore[attr-defined]
    assert strat._threshold_sigma == Decimal("0.5")  # type: ignore[attr-defined]


def test_cell_config_rejects_legacy_ema_alpha():
    with pytest.raises(ValidationError):
        CellConfig(
            strategy=StrategyName.MEAN_REVERSION, symbol="fUST", period_agg="a30",
            params={"ema_alpha": 0.01183, "threshold_sigma": 1.0, "ratio_sigma": 0.99},
        )
