"""Period-threshold research simulator (pure, no DB)."""
import pytest

from bfx_funding_bot.modules.backtest.period_threshold import (
    ALWAYS_2D,
    HOURS_PER_YEAR,
    RepaymentScenario,
    ThresholdRule,
    hourly_grid,
    limit_fill_grid,
    monthly_deltas,
    premium_by_bucket,
    simulate,
    trailing_percentile,
)

FULL = RepaymentScenario("full", 1.0, relend_delay_hours=0)
NO_FEE = 0.0


def _apr(daily: float) -> float:
    return daily * 365


def test_always_2d_on_flat_rate_earns_the_rate() -> None:
    r2 = [0.0002] * 480
    res = simulate(r2, [None] * 480, ALWAYS_2D, FULL, fee=NO_FEE)
    assert res.gross_apr == pytest.approx(_apr(0.0002))
    assert (res.n_short, res.n_long, res.idle_share) == (10, 0, 0.0)


def test_fee_and_relend_delay_reduce_apr() -> None:
    r2 = [0.0002] * 490
    res = simulate(r2, [None] * 490, ALWAYS_2D, RepaymentScenario("d", 1.0, 1), fee=0.15)
    # 10 credits of 48 h + 1 idle h each -> 480 of 490 hours lent.
    assert res.net_apr == pytest.approx(0.0002 * 480 / 24 / (490 / HOURS_PER_YEAR) * 0.85)
    assert res.idle_share == pytest.approx(10 / 490)


def test_threshold_locks_the_spike_for_the_full_long_term() -> None:
    # Rate spikes to 0.001 for one hour, then falls back to 0.0001 for a week.
    n = 7 * 24
    r2 = [0.001] + [0.0001] * (n - 1)
    rlong = list(r2)
    rule = ThresholdRule("7d>=0.0005", long_period_days=7, min_long_rate=0.0005)
    locked = simulate(r2, rlong, rule, FULL, fee=NO_FEE)
    rolling = simulate(r2, rlong, ALWAYS_2D, FULL, fee=NO_FEE)
    assert locked.n_long == 1
    assert locked.gross_apr == pytest.approx(_apr(0.001))
    assert locked.gross_apr > rolling.gross_apr


def test_refinance_on_drop_returns_the_lock_and_removes_the_upside() -> None:
    n = 7 * 24
    r2 = [0.001] + [0.0001] * (n - 1)
    rule = ThresholdRule("7d>=0.0005", long_period_days=7, min_long_rate=0.0005)
    adverse = RepaymentScenario("refi", 1.0, relend_delay_hours=0, refinance_on_drop=True)
    locked = simulate(r2, list(r2), rule, adverse, fee=NO_FEE)
    rolling = simulate(r2, list(r2), ALWAYS_2D, adverse, fee=NO_FEE)
    # Returned after one hour, then everything re-lends at the 2d market.
    assert locked.gross_apr == pytest.approx(rolling.gross_apr)


def test_refinance_margin_ignores_small_dips() -> None:
    r2 = [0.001] + [0.00095] * 47
    s = RepaymentScenario("refi", 1.0, relend_delay_hours=0, refinance_on_drop=True)
    res = simulate(r2, [None] * 48, ALWAYS_2D, s, fee=NO_FEE)
    assert res.n_short == 1  # a 5 % dip is inside the 20 % margin


def test_held_fraction_shortens_every_credit() -> None:
    r2 = [0.0002] * 48
    half = RepaymentScenario("half", 0.5, relend_delay_hours=0)
    res = simulate(r2, [None] * 48, ALWAYS_2D, half, fee=NO_FEE)
    assert res.n_short == 2


def test_long_needs_a_price_reference_and_at_least_the_2d_rate() -> None:
    r2 = [0.0005] * 48
    rule = ThresholdRule("7d>=0.0002", long_period_days=7, min_long_rate=0.0002)
    missing = simulate(r2, [None] * 48, rule, FULL, fee=NO_FEE)
    cheaper = simulate(r2, [0.0003] * 48, rule, FULL, fee=NO_FEE)
    assert missing.n_long == 0 and cheaper.n_long == 0


def test_trailing_percentile_has_no_lookahead() -> None:
    values = [1.0, 2.0, 3.0, 100.0]
    out = trailing_percentile(values, 0.5, lookback=2, min_obs=1)
    assert out == [None, 1.0, 1.0, 2.0]  # slot 3 sees only [2, 3]


def test_limit_fill_prices_off_previous_hour_and_needs_a_trade_through() -> None:
    close = [0.0002, 0.07, 0.0002, 0.0003]
    high = [0.0002, 0.07, 0.0003, 0.0003]
    # hour1: offer at 0.0002, high 0.07 -> fill at 0.0002 (not the spike close)
    # hour2: offer at 0.07, high 0.0003 -> no fill; hour3: offer 0.0002 fills
    assert limit_fill_grid(close, high) == [None, 0.0002, None, 0.0002]
    assert limit_fill_grid([0.01, 0.01], [0.01, 0.01], cap=0.002) == [None, 0.002]


def test_monthly_deltas_split_by_calendar_month() -> None:
    start = 1_704_067_200_000  # 2024-01-01T00:00Z
    n = (31 + 29) * 24
    r2 = [0.0002] * n
    m = monthly_deltas(start, r2, [None] * n, ALWAYS_2D, FULL)
    assert (m.months, m.median_delta, m.win_share) == (2, 0.0, 0.0)


def test_hourly_grid_and_premium_by_year() -> None:
    start = 1_704_067_200_000
    grid = hourly_grid([(start, 0.0002), (start + 3_600_000, 0.0004)], start, start + 3 * 3_600_000)
    assert grid == [0.0002, 0.0004, None]
    rows = premium_by_bucket(start, [0.0002, 0.0002, 0.0002], [0.0003, None, None])
    assert len(rows) == 1
    assert rows[0].median_premium == pytest.approx(0.5)
    assert rows[0].long_available_share == pytest.approx(1 / 3)


def test_rule_validation() -> None:
    with pytest.raises(ValueError):
        ThresholdRule("x", 7, min_long_rate=0.0002, r2_percentile=0.9)
    with pytest.raises(ValueError):
        RepaymentScenario("x", 0.0)
