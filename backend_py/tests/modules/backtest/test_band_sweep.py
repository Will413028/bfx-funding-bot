# tests/modules/backtest/test_band_sweep.py
import math
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.band_sweep import enumerate_bands
from bfx_funding_bot.modules.backtest.band_sweep import simulate_period_path, period_profile
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.strategies.adaptive_period import AdaptivePeriodStrategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles(closes: list[str], symbol: str = "fUST") -> list[FundingCandle]:
    # hourly candles starting at an arbitrary epoch; only close/mts matter here
    base = 1_500_000_000_000
    return [
        FundingCandle(symbol=symbol, timeframe="1h", period_agg="a30",
                      mts=base + i * 3_600_000, open=Decimal(c), high=Decimal(c),
                      low=Decimal(c), close=Decimal(c))
        for i, c in enumerate(closes)
    ]


def test_enumerate_bands_is_8_strict_pairs() -> None:
    bands = enumerate_bands()
    assert len(bands) == 8
    # strict t1 < t2, no (1.5, 1.5)
    assert all(t1 < t2 for t1, t2 in bands)
    assert (Decimal("1.5"), Decimal("1.5")) not in bands
    # the deployed candidate is in-grid
    assert (Decimal("0.5"), Decimal("1.5")) in bands
    # exact set
    assert set(bands) == {
        (Decimal("0.5"), Decimal("1.5")), (Decimal("0.5"), Decimal("2.0")), (Decimal("0.5"), Decimal("2.5")),
        (Decimal("1.0"), Decimal("1.5")), (Decimal("1.0"), Decimal("2.0")), (Decimal("1.0"), Decimal("2.5")),
        (Decimal("1.5"), Decimal("2.0")), (Decimal("1.5"), Decimal("2.5")),
    }


def test_simulate_period_path_matches_engine_trade_count() -> None:
    # A long, mildly varying series so several trades + cooldowns fire.
    closes = [str(Decimal("0.0003") + Decimal("0.0001") * Decimal((i % 7))) for i in range(400)]
    candles = _candles(closes)
    params = dict(ema_span=24, ratio_sigma=Decimal("0.40"), t1=Decimal("0.5"),
                  t2=Decimal("1.5"), p_mid=7, p_long=14)

    periods = simulate_period_path(candles, **params)

    strat = AdaptivePeriodStrategy(**params)
    rb = run_backtest(candles, strat, BacktestConfig(fill_model="linear"))
    assert len(periods) == rb.n_trades  # the cooldown loop is mirrored exactly
    assert all(p in (2, 7, 14) for p in periods)


def test_period_profile_avg_and_p14_share() -> None:
    profile = period_profile([14, 14, 2, 2], p_long=14)
    # avg = (14+14+2+2)/4 = 8 ; time-weighted p14 share = 28 / 32
    assert profile["avg_period"] == Decimal("8")
    assert profile["p14_share"] == (Decimal("28") / Decimal("32"))


def test_period_profile_empty() -> None:
    profile = period_profile([], p_long=14)
    assert profile["avg_period"] == Decimal("0")
    assert profile["p14_share"] == Decimal("0")
