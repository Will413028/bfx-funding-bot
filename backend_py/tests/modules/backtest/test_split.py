from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.split import compute_train_end_mts
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles(n: int) -> list[FundingCandle]:
    return [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=1704067200000 + i * 3_600_000,
            open=Decimal("0.0001"), close=Decimal("0.0001"),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for i in range(n)
    ]


def test_compute_train_end_mts_70_30_split_on_10_candles_returns_7th_candle_mts() -> None:
    candles = _candles(10)
    # split_idx = int(10 * 0.7) = 7 -> return candles[6].mts (zero-indexed: candle 0..6 = 7 candles in train)
    expected = candles[6].mts
    assert compute_train_end_mts(candles) == expected


def test_compute_train_end_mts_handles_unsorted_input() -> None:
    candles = _candles(10)
    shuffled = [candles[5], candles[0], candles[9], *candles[1:5], *candles[6:9]]
    assert compute_train_end_mts(shuffled) == candles[6].mts


def test_compute_train_end_mts_raises_on_empty() -> None:
    with pytest.raises(ValueError, match="empty"):
        compute_train_end_mts([])


def test_compute_train_end_mts_handles_single_candle() -> None:
    candles = _candles(1)
    # split_idx = int(1 * 0.7) = 0 -> would access candles[-1]; should still return that candle's mts
    assert compute_train_end_mts(candles) == candles[0].mts


def test_compute_train_end_mts_handles_two_candles() -> None:
    candles = _candles(2)
    # split_idx = int(2 * 0.7) = 1 -> return candles[0].mts
    assert compute_train_end_mts(candles) == candles[0].mts
