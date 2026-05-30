from decimal import Decimal

import pytest

from bfx_funding_bot.modules.live_validation.live_attribution import (
    FillRecord,
    FrrPoint,
    cell_period_days,
)


def test_cell_period_days_p2_is_two():
    assert cell_period_days("p2", Decimal("30")) == Decimal("2")


def test_cell_period_days_a30_uses_avg_period():
    assert cell_period_days("a30", Decimal("27.5")) == Decimal("27.5")


def test_cell_period_days_unknown_raises():
    with pytest.raises(ValueError, match="unknown period_agg"):
        cell_period_days("p7", Decimal("30"))


def test_fillrecord_is_frozen():
    f = FillRecord(
        venue_offer_id="1",
        fill_ts_ms=0,
        size_usdt=Decimal("100"),
        rate=Decimal("0.0003"),
        period_days=Decimal("2"),
        release_ts_ms=None,
    )
    with pytest.raises(Exception):
        f.size_usdt = Decimal("200")  # type: ignore[misc]


def test_frrpoint_is_frozen():
    p = FrrPoint(mts=0, frr=Decimal("0.0002"), avg_period=Decimal("30"))
    assert p.frr == Decimal("0.0002")


from bfx_funding_bot.modules.live_validation.live_attribution import (
    weekly_window_bounds,
)

WEEK = 7 * 24 * 60 * 60 * 1000


def test_weekly_window_bounds_exact_two_weeks():
    bounds = weekly_window_bounds(0, 2 * WEEK)
    assert bounds == [(0, WEEK), (WEEK, 2 * WEEK)]


def test_weekly_window_bounds_partial_trailing_week():
    bounds = weekly_window_bounds(0, WEEK + 100)
    assert bounds == [(0, WEEK), (WEEK, WEEK + 100)]


def test_weekly_window_bounds_single_short_span():
    bounds = weekly_window_bounds(1000, 5000)
    assert bounds == [(1000, 5000)]


def test_weekly_window_bounds_empty_when_end_le_start():
    assert weekly_window_bounds(5000, 5000) == []
    assert weekly_window_bounds(5000, 4000) == []


from bfx_funding_bot.modules.live_validation.live_attribution import (
    MS_PER_DAY,
    attribute_passive,
)


def test_attribute_passive_single_full_week():
    pts = [FrrPoint(mts=1000, frr=Decimal("0.0003"), avg_period=Decimal("30"))]
    bounds = [(0, WEEK)]
    out = attribute_passive(pts, window_bounds=bounds)
    assert len(out) == 1
    assert out[0].month_mts == 0
    assert out[0].n_trades == 1
    assert out[0].net_monthly == Decimal("0.0003") * Decimal(WEEK) / MS_PER_DAY * Decimal("100")


def test_attribute_passive_averages_frr_in_window():
    pts = [
        FrrPoint(mts=10, frr=Decimal("0.0002"), avg_period=Decimal("30")),
        FrrPoint(mts=20, frr=Decimal("0.0004"), avg_period=Decimal("30")),
    ]
    out = attribute_passive(pts, window_bounds=[(0, WEEK)])
    expected = Decimal("0.0003") * Decimal(WEEK) / MS_PER_DAY * Decimal("100")
    assert out[0].net_monthly == expected
    assert out[0].fill_rate == Decimal("0.0003")


def test_attribute_passive_zero_frr_points_in_window():
    out = attribute_passive([], window_bounds=[(0, WEEK)])
    assert out[0].net_monthly == Decimal("0")
    assert out[0].n_trades == 0
    assert out[0].fill_rate == Decimal("0")


from bfx_funding_bot.modules.live_validation.live_attribution import (
    attribute_active,
)

C = Decimal("570")


def _fill(ts, size, rate, period, release=None):
    return FillRecord(
        venue_offer_id=str(ts),
        fill_ts_ms=ts,
        size_usdt=Decimal(size),
        rate=Decimal(rate),
        period_days=Decimal(period),
        release_ts_ms=release,
    )


def test_attribute_active_held_to_term():
    fills = [_fill(1000, "570", "0.0003", "2")]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    assert out[0].n_trades == 1
    expected = Decimal("570") * Decimal("0.0003") * Decimal("2") / C * Decimal("100")
    assert out[0].net_monthly == expected
    assert out[0].fill_rate == Decimal("0.0003")


def test_attribute_active_release_caps_duration():
    one_day = 24 * 60 * 60 * 1000
    fills = [_fill(0, "570", "0.0003", "2", release=one_day)]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    expected = Decimal("570") * Decimal("0.0003") * Decimal("1") / C * Decimal("100")
    assert out[0].net_monthly == expected


def test_attribute_active_release_longer_than_period_uses_period():
    five_days = 5 * 24 * 60 * 60 * 1000
    fills = [_fill(0, "570", "0.0003", "2", release=five_days)]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    expected = Decimal("570") * Decimal("0.0003") * Decimal("2") / C * Decimal("100")
    assert out[0].net_monthly == expected


def test_attribute_active_multiple_fills_one_window():
    fills = [_fill(100, "200", "0.0003", "2"), _fill(200, "300", "0.0005", "2")]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    interest = (
        Decimal("200") * Decimal("0.0003") * Decimal("2")
        + Decimal("300") * Decimal("0.0005") * Decimal("2")
    )
    assert out[0].net_monthly == interest / C * Decimal("100")
    assert out[0].n_trades == 2
    assert out[0].fill_rate == (Decimal("0.0003") + Decimal("0.0005")) / Decimal("2")


def test_attribute_active_zero_fill_window_is_idle():
    out = attribute_active([], capital=C, window_bounds=[(0, WEEK)])
    assert out[0].net_monthly == Decimal("0")
    assert out[0].n_trades == 0
    assert out[0].fill_rate == Decimal("0")
