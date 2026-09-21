"""Fills are priced off the market series of the tenor actually posted.

A strategy that observes the a30 aggregate but posts 2-day offers competes in
the 2-day book; crediting it the aggregate rate for a 2-day lock was the tenor
mismatch that inflated a30 cells (2026-09-22 review F2).
"""
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import (
    resolve_market_series_key,
    run_backtest,
)
from bfx_funding_bot.modules.backtest.schemas import LendDecision
from bfx_funding_bot.modules.backtest.strategies.always_market_rate import (
    AlwaysMarketRateStrategy,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle

_HOUR = 3_600_000
_T0 = 1_704_067_200_000  # 2024-01-01T00:00Z
_LINEAR = BacktestConfig(fill_model="linear-baseline")


def _series(period_agg: str, close: str, n: int = 240) -> list[FundingCandle]:
    return [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg=period_agg,
            mts=_T0 + i * _HOUR, close=Decimal(close),
        )
        for i in range(n)
    ]


class _FixedPeriod(Strategy):
    """Always lend at the observed close for a fixed tenor."""

    def __init__(self, period_days: int) -> None:
        self._p = period_days

    @property
    def name(self) -> str:
        return f"fixed_p{self._p}"

    def decide(self, candle: FundingCandle) -> LendDecision | None:
        if candle.close is None:
            return None
        return LendDecision(mts=candle.mts, rate=candle.close, period_days=self._p)


def test_resolve_prefers_exact_tenor_then_aggregate() -> None:
    assert resolve_market_series_key(2, {"p2": [], "a30": []}) == "p2"
    assert resolve_market_series_key(30, {"p2": [], "p30": [], "a30": []}) == "p30"
    assert resolve_market_series_key(14, {"p2": [], "a30": []}) == "a30"
    with pytest.raises(KeyError):
        resolve_market_series_key(30, {"p2": []})


def test_two_day_offer_from_a30_close_is_priced_against_p2() -> None:
    a30 = _series("a30", "0.0002")   # aggregate prints higher
    p2 = _series("p2", "0.0001")     # the 2-day book the offer actually sits in
    legacy = run_backtest(a30, _FixedPeriod(2), _LINEAR)
    aware = run_backtest(a30, _FixedPeriod(2), _LINEAR, market_series_by_agg={"p2": p2, "a30": a30})
    assert legacy.fill_rate == Decimal("1")
    assert aware.fill_rate < legacy.fill_rate
    assert aware.net_monthly_return_pct < legacy.net_monthly_return_pct
    assert aware.pricing_series_used == {"p2": aware.n_trades}


def test_mid_tenor_falls_back_to_aggregate_and_records_it() -> None:
    a30 = _series("a30", "0.0002")
    p2 = _series("p2", "0.0001")
    r = run_backtest(a30, _FixedPeriod(14), _LINEAR, market_series_by_agg={"p2": p2, "a30": a30})
    assert r.pricing_series_used == {"a30": r.n_trades}
    assert r.fill_rate == Decimal("1"), "aggregate prices itself: spread 0"


def test_missing_mts_in_resolved_series_degrades_to_observed_candle() -> None:
    a30 = _series("a30", "0.0002")
    p2_first_hour_only = _series("p2", "0.0001")[:1]
    r = run_backtest(a30, AlwaysMarketRateStrategy(period_days=2), _LINEAR,
                     market_series_by_agg={"p2": p2_first_hour_only, "a30": a30})
    # Trade 1 hits the p2 slot (100% above market -> fill 0); the other four have no p2
    # print at their mts and degrade to the observed candle (spread 0 -> fill 1).
    assert r.n_trades == 5
    assert r.fill_rate == Decimal(4) / Decimal(5)
    assert r.pricing_series_used == {"p2": 5}


def test_market_candles_and_series_by_agg_are_mutually_exclusive() -> None:
    s = _series("p2", "0.0001")
    with pytest.raises(ValueError, match="not both"):
        run_backtest(s, _FixedPeriod(2), _LINEAR, market_candles=s, market_series_by_agg={"p2": s})


def test_truncate_at_window_end_credits_only_in_window_days() -> None:
    s = _series("p2", "0.0001", n=48)  # two days of candles
    full = run_backtest(s, _FixedPeriod(30), _LINEAR)
    truncated = run_backtest(
        s, _FixedPeriod(30), BacktestConfig(fill_model="linear-baseline", truncate_at_window_end=True)
    )
    # One trade at hour 0. Full credits 30 days; truncated credits the 47h left in the window.
    assert full.n_trades == truncated.n_trades == 1
    ratio = truncated.gross_monthly_return_pct / full.gross_monthly_return_pct
    assert abs(ratio - Decimal(47) / Decimal(24) / Decimal(30)) < Decimal("1e-9")


def test_default_config_is_byte_stable_for_existing_callers() -> None:
    s = _series("p2", "0.0001")
    before = run_backtest(s, AlwaysMarketRateStrategy(period_days=2), _LINEAR)
    again = run_backtest(s, AlwaysMarketRateStrategy(period_days=2), _LINEAR)
    assert before == again
    assert before.pricing_series_used is None
