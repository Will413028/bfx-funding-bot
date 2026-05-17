from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.wfo import WfoWindow, compute_wfo_windows
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles_hourly(start_year: int, start_month: int, n_hours: int) -> list[FundingCandle]:
    """n_hours of hourly candles starting at YYYY-MM-01 00:00 UTC."""
    start = int(datetime(start_year, start_month, 1, tzinfo=UTC).timestamp() * 1000)
    return [
        FundingCandle(
            symbol="fUST", timeframe="1h", period_agg="p2",
            mts=start + i * 3_600_000,
            open=Decimal("0.0001"), close=Decimal("0.0001"),
            high=Decimal("0.0001"), low=Decimal("0.0001"),
            volume=Decimal("100"),
        )
        for i in range(n_hours)
    ]


def test_compute_wfo_windows_six_month_input_produces_at_least_two_windows() -> None:
    candles = _candles_hourly(2024, 1, 6 * 30 * 24)  # Jan 1 - Jun 29 approx
    windows = compute_wfo_windows(candles, train_months=3, test_months=1, step_months=1)
    assert len(windows) >= 2
    assert isinstance(windows[0], WfoWindow)
    jan_1 = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
    apr_1 = int(datetime(2024, 4, 1, tzinfo=UTC).timestamp() * 1000)
    may_1 = int(datetime(2024, 5, 1, tzinfo=UTC).timestamp() * 1000)
    assert windows[0].train_start_mts == jan_1
    assert windows[0].train_end_mts == apr_1 - 1
    assert windows[0].test_start_mts == apr_1
    assert windows[0].test_end_mts == may_1 - 1


def test_compute_wfo_windows_12_month_input_produces_8_to_10_windows() -> None:
    candles = _candles_hourly(2024, 1, 12 * 30 * 24)
    windows = compute_wfo_windows(candles, train_months=3, test_months=1, step_months=1)
    assert 8 <= len(windows) <= 10


def test_compute_wfo_windows_skips_windows_below_min_candles() -> None:
    candles = _candles_hourly(2024, 1, 100)
    windows = compute_wfo_windows(candles, train_months=3, test_months=1, step_months=1, min_candles_per_segment=200)
    assert windows == []


def test_compute_wfo_windows_raises_on_empty_input() -> None:
    with pytest.raises(ValueError, match="empty"):
        compute_wfo_windows([], train_months=3, test_months=1, step_months=1)


def test_compute_wfo_windows_walk_step_two_months() -> None:
    candles = _candles_hourly(2024, 1, 12 * 30 * 24)
    windows = compute_wfo_windows(candles, train_months=3, test_months=1, step_months=2)
    assert 4 <= len(windows) <= 6
    if len(windows) >= 2:
        jan_1 = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
        mar_1 = int(datetime(2024, 3, 1, tzinfo=UTC).timestamp() * 1000)
        assert windows[0].train_start_mts == jan_1
        assert windows[1].train_start_mts == mar_1


def test_compute_wfo_windows_returns_immutable_dataclass() -> None:
    from dataclasses import FrozenInstanceError

    candles = _candles_hourly(2024, 1, 6 * 30 * 24)
    windows = compute_wfo_windows(candles)
    with pytest.raises(FrozenInstanceError):
        windows[0].train_start_mts = 0  # type: ignore[misc]
