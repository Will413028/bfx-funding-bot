from decimal import Decimal

from bfx_funding_bot.modules.backtest.frr_series import FrrSeries
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

H = 3_600_000  # 1h in ms


def _series() -> FrrSeries:
    # stored frr is the raw funding_stats value (per-day rate / 365)
    return FrrSeries([(0, Decimal("1e-6")), (2 * H, Decimal("2e-6"))])


def test_at_converts_stored_frr_to_per_day_via_365() -> None:
    s = _series()
    assert s.at(0) == Decimal("1e-6") * Decimal("365")


def test_at_returns_latest_point_at_or_before_mts() -> None:
    s = _series()
    assert s.at(H) == Decimal("1e-6") * Decimal("365")  # as-of: 0 <= H < 2H
    assert s.at(2 * H) == Decimal("2e-6") * Decimal("365")
    assert s.at(3 * H) == Decimal("2e-6") * Decimal("365")


def test_at_returns_none_before_first_point() -> None:
    assert _series().at(-1) is None


def test_at_returns_none_when_stale_beyond_cap() -> None:
    s = FrrSeries([(0, Decimal("1e-6"))], max_staleness_ms=24 * H)
    assert s.at(24 * H) == Decimal("1e-6") * Decimal("365")  # exactly at cap: ok
    assert s.at(24 * H + 1) is None


def test_from_stats_skips_null_frr_and_sorts() -> None:
    stats = [
        FundingStat(symbol="fUST", mts=2 * H, frr=Decimal("2e-6")),
        FundingStat(symbol="fUST", mts=H, frr=None),
        FundingStat(symbol="fUST", mts=0, frr=Decimal("1e-6")),
    ]
    s = FrrSeries.from_stats(stats)
    assert s.at(0) == Decimal("1e-6") * Decimal("365")
    assert s.at(H) == Decimal("1e-6") * Decimal("365")  # null row is skipped
    assert s.at(2 * H) == Decimal("2e-6") * Decimal("365")


def test_len_counts_points() -> None:
    assert len(_series()) == 2
