from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.sortino import (
    compute_sortino,
    month_end_timestamps_within,
    monthly_returns_from_equity_curve,
)


def test_monthly_returns_two_month_endpoints_returns_one_return() -> None:
    # equity_curve = [(mts1, equity1), (mts2, equity2)]
    # mts1 = 2024-01-31 23:00 UTC; mts2 = 2024-02-29 23:00 UTC
    curve = [
        (1706742000000, Decimal("1.00")),  # 2024-01-31 23:00 UTC
        (1709247600000, Decimal("1.01")),  # 2024-02-29 23:00 UTC
    ]
    rets = monthly_returns_from_equity_curve(curve)
    assert len(rets) == 1
    assert abs(rets[0] - Decimal("0.01")) < Decimal("0.0001")


def test_monthly_returns_empty_curve_returns_empty() -> None:
    assert monthly_returns_from_equity_curve([]) == []


def test_monthly_returns_single_point_returns_empty() -> None:
    curve = [(1706742000000, Decimal("1.00"))]
    assert monthly_returns_from_equity_curve(curve) == []


def test_compute_sortino_with_mixed_returns() -> None:
    # 3 positives + 1 negative: mean = (0.02 + 0.03 + 0.04 - 0.01)/4 = 0.02
    # downside std = std([-0.01]) = 0 → undefined, but per spec single-downside-obs std=0 → return +inf
    returns = [Decimal("0.02"), Decimal("0.03"), Decimal("0.04"), Decimal("-0.01")]
    sortino = compute_sortino(returns)
    # With only 1 downside obs, std=0 → +inf path
    assert sortino == Decimal("Infinity")


def test_compute_sortino_with_no_downside_returns_positive_infinity() -> None:
    returns = [Decimal("0.01"), Decimal("0.02"), Decimal("0.03")]
    sortino = compute_sortino(returns)
    assert sortino == Decimal("Infinity")


def test_compute_sortino_with_fewer_than_three_returns_returns_zero() -> None:
    assert compute_sortino([Decimal("0.01"), Decimal("0.02")]) == Decimal("0")
    assert compute_sortino([Decimal("0.01")]) == Decimal("0")
    assert compute_sortino([]) == Decimal("0")


def test_compute_sortino_with_multiple_downside_returns_finite_value() -> None:
    # returns: 0.05, 0.03, -0.02, -0.04
    # mean = 0.005
    # downside = [-0.02, -0.04]; mean_downside = -0.03; var = ((0.01)^2 + (0.01)^2)/2 = 1e-4; std = 0.01
    # sortino = 0.005 / 0.01 = 0.5
    returns = [Decimal("0.05"), Decimal("0.03"), Decimal("-0.02"), Decimal("-0.04")]
    sortino = compute_sortino(returns)
    assert abs(sortino - Decimal("0.5")) < Decimal("0.01")


def test_month_end_timestamps_within_jan_to_march_2024() -> None:
    # 2024-01-15 00:00 UTC → 2024-03-15 00:00 UTC
    start = int(datetime(2024, 1, 15, tzinfo=UTC).timestamp() * 1000)
    end = int(datetime(2024, 3, 15, tzinfo=UTC).timestamp() * 1000)
    ts = month_end_timestamps_within(start, end)
    assert len(ts) == 2  # Jan-end + Feb-end (March-end > end)
    # Jan 31, 23:59:59 UTC
    jan_end = int(datetime(2024, 1, 31, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
    feb_end = int(datetime(2024, 2, 29, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
    assert ts[0] == jan_end
    assert ts[1] == feb_end


def test_month_end_timestamps_within_empty_when_start_after_end() -> None:
    assert month_end_timestamps_within(2000, 1000) == []


def test_compute_sortino_with_multiple_equal_downside_returns_infinity() -> None:
    """Two identical downside obs → variance still 0 → +inf via the std==0 guard."""
    returns = [Decimal("0.05"), Decimal("-0.01"), Decimal("-0.01")]
    sortino = compute_sortino(returns)
    assert sortino == Decimal("Infinity")


def test_monthly_returns_handles_unsorted_curve() -> None:
    """Code sorts internally — passing reversed input must give same result."""
    sorted_curve = [
        (1706742000000, Decimal("1.00")),  # 2024-01-31 23:00
        (1709247600000, Decimal("1.02")),  # 2024-02-29 23:00
        (1711925999000, Decimal("1.05")),  # 2024-03-31 23:59:59
    ]
    reversed_curve = list(reversed(sorted_curve))
    assert monthly_returns_from_equity_curve(reversed_curve) == monthly_returns_from_equity_curve(sorted_curve)


def test_monthly_returns_raises_on_zero_equity() -> None:
    curve = [
        (1706742000000, Decimal("1.00")),
        (1709247600000, Decimal("0")),
        (1711925999000, Decimal("0.5")),
    ]
    with pytest.raises(ValueError, match="zero equity"):
        monthly_returns_from_equity_curve(curve)
