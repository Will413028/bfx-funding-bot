from decimal import Decimal

import pytest

from bfx_funding_bot.modules.backtest.oos_profitability import (
    WindowOutcome,
    _percentile,
    summarize_oos,
)


def _w(month_mts: int, net_monthly: str, n_trades: int = 5, fill_rate: str = "0.8") -> WindowOutcome:
    return WindowOutcome(
        month_mts=month_mts,
        net_monthly=Decimal(net_monthly),
        n_trades=n_trades,
        fill_rate=Decimal(fill_rate),
    )


def test_percentile_linear_interpolation():
    vals = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
    assert _percentile(vals, Decimal("0.5")) == Decimal("2.5")
    assert _percentile(vals, Decimal("0")) == Decimal("1")
    assert _percentile(vals, Decimal("1")) == Decimal("4")
    # q=0.25 over 4 points: pos = 0.25*3 = 0.75 -> 1 + 0.75*(2-1) = 1.75
    assert _percentile(vals, Decimal("0.25")) == Decimal("1.75")


def test_percentile_single_value():
    assert _percentile([Decimal("7")], Decimal("0.9")) == Decimal("7")


def test_percentile_empty_raises():
    with pytest.raises(ValueError):
        _percentile([], Decimal("0.5"))


def test_summarize_basic_distribution():
    outcomes = [
        _w(0, "1.0"),
        _w(1, "2.0"),
        _w(2, "3.0"),
        _w(3, "4.0"),
    ]
    s = summarize_oos(outcomes)
    assert s.n_windows == 4
    assert s.median_monthly == Decimal("2.5")
    assert s.p25_monthly == Decimal("1.75")
    assert s.worst_monthly == Decimal("1.0")
    assert s.best_monthly == Decimal("4.0")
    assert s.mean_monthly == Decimal("2.5")


def test_summarize_annualized_is_geometric():
    # 12 windows each +1% -> annualized = (1.01^12)^(12/12) - 1 = 1.01^12 - 1
    outcomes = [_w(i, "1.0") for i in range(12)]
    s = summarize_oos(outcomes)
    expected = (Decimal("1.01") ** 12 - Decimal("1")) * Decimal("100")
    assert abs(s.annualized_pct - expected) < Decimal("0.0001")


def test_summarize_idle_rate_and_fill():
    outcomes = [
        _w(0, "2.0", n_trades=10, fill_rate="0.9"),
        _w(1, "0.0", n_trades=0, fill_rate="0"),  # idle month
        _w(2, "1.0", n_trades=4, fill_rate="0.5"),
    ]
    s = summarize_oos(outcomes)
    assert s.idle_rate == Decimal("1") / Decimal("3")
    # mean_fill_rate only over windows with trades: (0.9 + 0.5)/2 = 0.7
    assert s.mean_fill_rate == Decimal("0.7")


def test_summarize_all_idle_fill_zero():
    outcomes = [_w(0, "0.0", n_trades=0, fill_rate="0"), _w(1, "0.0", n_trades=0, fill_rate="0")]
    s = summarize_oos(outcomes)
    assert s.idle_rate == Decimal("1")
    assert s.mean_fill_rate == Decimal("0")


def test_summarize_sortino_small_sample_is_zero():
    # <3 windows -> compute_sortino returns 0 (untrustworthy)
    s = summarize_oos([_w(0, "1.0"), _w(1, "2.0")])
    assert s.sortino == Decimal("0")


def test_summarize_empty_raises():
    with pytest.raises(ValueError):
        summarize_oos([])
