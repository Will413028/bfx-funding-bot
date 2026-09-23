from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.live_validation.live_attribution import (
    FRR_ANNUALIZATION,
    MS_PER_DAY,
    ClampDiagnostic,
    CreditCloseRecord,
    DeploymentAnchorResult,
    FillRecord,
    FrrBenchmark,
    G3Verdict,
    MarketRatePoint,
    NavAnchorResult,
    VerdictState,
    apply_credit_closes,
    assert_market_rate_band,
    attribute_active,
    attribute_idle,
    attribute_passive,
    cell_period_days,
    check_deployment_anchor,
    check_nav_anchor,
    clamp_active_window,
    decide_verdict,
    fill_duration_days,
    frr_points_from_stats,
    open_principal_at,
    weekly_window_bounds,
)

WEEK = 7 * 24 * 60 * 60 * 1000
C = Decimal("570")

# ---------------------------------------------------------------------------
# Task 1: scaffold — FillRecord / MarketRatePoint / cell_period_days
# ---------------------------------------------------------------------------


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
    with pytest.raises(FrozenInstanceError):
        f.size_usdt = Decimal("200")  # type: ignore[misc]


def test_marketratepoint_is_frozen():
    p = MarketRatePoint(mts=0, rate=Decimal("0.0002"))
    with pytest.raises(FrozenInstanceError):
        p.rate = Decimal("0.9")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Task 2: weekly_window_bounds
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task 3: attribute_passive (AlwaysMarketRate arm — lends at funding_candles.close)
# ---------------------------------------------------------------------------


def test_attribute_passive_single_full_week():
    pts = [MarketRatePoint(mts=1000, rate=Decimal("0.0003"))]
    bounds = [(0, WEEK)]
    out = attribute_passive(pts, window_bounds=bounds)
    assert len(out) == 1
    assert out[0].month_mts == 0
    assert out[0].n_trades == 1
    assert out[0].net_monthly == Decimal("0.0003") * Decimal(WEEK) / MS_PER_DAY * Decimal("100")


def test_attribute_passive_averages_rate_in_window():
    pts = [
        MarketRatePoint(mts=10, rate=Decimal("0.0002")),
        MarketRatePoint(mts=20, rate=Decimal("0.0004")),
    ]
    out = attribute_passive(pts, window_bounds=[(0, WEEK)])
    expected = Decimal("0.0003") * Decimal(WEEK) / MS_PER_DAY * Decimal("100")
    assert out[0].net_monthly == expected
    assert out[0].fill_rate == Decimal("0.0003")


def test_attribute_passive_zero_points_in_window():
    out = attribute_passive([], window_bounds=[(0, WEEK)])
    assert out[0].net_monthly == Decimal("0")
    assert out[0].n_trades == 0
    assert out[0].fill_rate == Decimal("0")


# ---------------------------------------------------------------------------
# C1 regression: passive baseline must be the per-day MARKET rate
# (funding_candles.close ~1e-4), never funding_stats.frr (~1e-6, ~185x too small).
# assert_market_rate_band guards the loader against re-introducing that unit bug.
# ---------------------------------------------------------------------------


def test_assert_market_rate_band_accepts_candle_close_scale():
    # Real fUST/p2 candle-close daily rates live around 1e-4..1e-3.
    assert_market_rate_band([Decimal("0.0002"), Decimal("0.0003"), Decimal("0.00015")])


def test_assert_market_rate_band_rejects_frr_scale():
    # The exact bug: funding_stats.frr sample (fUSD, 2026-05-10) = 1.12e-06.
    with pytest.raises(ValueError, match="outside plausible per-day band"):
        assert_market_rate_band([Decimal("1.12e-06"), Decimal("1.0e-06")])


def test_assert_market_rate_band_rejects_above_band():
    # A percentage-vs-fraction mixup (0.0003 stored as 0.03 etc.) must also trip.
    with pytest.raises(ValueError, match="outside plausible per-day band"):
        assert_market_rate_band([Decimal("0.5")])


def test_assert_market_rate_band_empty_is_noop():
    # No coverage in window → nothing to assert; the loader's coverage guard handles it.
    assert assert_market_rate_band([]) is None


def test_assert_market_rate_band_boundaries_are_inclusive():
    # Pin the exact constants: both edges (1e-5, 0.05) must NOT raise (<=, <=).
    assert assert_market_rate_band([Decimal("1e-5")]) is None
    assert assert_market_rate_band([Decimal("0.05")]) is None


def test_assert_market_rate_band_just_outside_boundaries_trip():
    # Just below the floor and just above the ceiling must raise — pins the band width.
    with pytest.raises(ValueError, match="outside plausible per-day band"):
        assert_market_rate_band([Decimal("9.9e-6")])
    with pytest.raises(ValueError, match="outside plausible per-day band"):
        assert_market_rate_band([Decimal("0.0501")])


# ---------------------------------------------------------------------------
# Task 4: attribute_active
# ---------------------------------------------------------------------------


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


def test_attribute_active_zero_capital_raises():
    with pytest.raises(ValueError, match="capital must be positive"):
        attribute_active([], capital=Decimal("0"), window_bounds=[(0, WEEK)])


def test_attribute_active_routes_fill_to_correct_window():
    f1 = _fill(100, "570", "0.0003", "2")            # window 1
    f2 = _fill(WEEK + 100, "570", "0.0003", "2")     # window 2
    out = attribute_active([f1, f2], capital=C, window_bounds=[(0, WEEK), (WEEK, 2 * WEEK)])
    assert out[0].n_trades == 1
    assert out[1].n_trades == 1


def test_attribute_active_fill_outside_all_windows_ignored():
    f = _fill(3 * WEEK, "570", "0.0003", "2")
    out = attribute_active([f], capital=C, window_bounds=[(0, WEEK), (WEEK, 2 * WEEK)])
    assert all(o.n_trades == 0 for o in out)


def test_attribute_active_clamps_overlap_over_cap():
    # 4 fills, 250 each = 1000 concurrent > 570 cap, same window, same span.
    fills = [_fill(0, "250", "0.0003", "2") for _ in range(4)]
    out = attribute_active(fills, capital=C, window_bounds=[(0, WEEK)])
    raw = Decimal("4") * (Decimal("250") * Decimal("0.0003") * Decimal("2"))
    clamped = raw * (C / Decimal("1000"))
    assert out[0].net_monthly == clamped / C * Decimal("100")
    assert out[0].n_trades == 4  # n_trades unchanged: count by fill_ts


# ---------------------------------------------------------------------------
# attribute_idle (AlwaysIdle arm — capital sits idle, earns 0 by construction)
# ---------------------------------------------------------------------------


def test_attribute_idle_zero_return_per_window():
    out = attribute_idle(window_bounds=[(0, WEEK), (WEEK, 2 * WEEK)])
    assert len(out) == 2
    assert [o.month_mts for o in out] == [0, WEEK]
    assert all(o.net_monthly == Decimal("0") for o in out)
    assert all(o.n_trades == 0 for o in out)
    assert all(o.fill_rate == Decimal("0") for o in out)


def test_attribute_idle_aligns_with_active_month_mts():
    # bot-vs-idle relies on attribute_idle aligning 1:1 by month_mts with
    # attribute_active so paired_active_returns can subtract arm-by-arm.
    bounds = [(0, WEEK), (WEEK, 2 * WEEK)]
    active = attribute_active([], capital=C, window_bounds=bounds)
    idle = attribute_idle(window_bounds=bounds)
    assert [o.month_mts for o in active] == [o.month_mts for o in idle]


def test_attribute_idle_empty_bounds():
    assert attribute_idle(window_bounds=[]) == []


# ---------------------------------------------------------------------------
# Task 5: deployment anchor
# ---------------------------------------------------------------------------


def test_deployment_anchor_within_tolerance():
    r = check_deployment_anchor(
        attributed_deployed=Decimal("300"),
        observed_realized=Decimal("310"),
        tol=Decimal("0.05"),
    )
    assert isinstance(r, DeploymentAnchorResult)
    assert r.within_tolerance is True
    assert r.relative_divergence == abs(Decimal("300") - Decimal("310")) / Decimal("310")


def test_deployment_anchor_beyond_tolerance():
    r = check_deployment_anchor(
        attributed_deployed=Decimal("300"),
        observed_realized=Decimal("100"),
        tol=Decimal("0.05"),
    )
    assert r.within_tolerance is False


def test_deployment_anchor_zero_observed_is_within_when_attributed_zero():
    r = check_deployment_anchor(
        attributed_deployed=Decimal("0"),
        observed_realized=Decimal("0"),
        tol=Decimal("0.05"),
    )
    assert r.within_tolerance is True
    assert r.relative_divergence == Decimal("0")


def test_deployment_anchor_zero_observed_nonzero_attributed_diverges():
    r = check_deployment_anchor(
        attributed_deployed=Decimal("50"),
        observed_realized=Decimal("0"),
        tol=Decimal("0.05"),
    )
    assert r.within_tolerance is False


# ---------------------------------------------------------------------------
# Task 6: NAV anchor
# ---------------------------------------------------------------------------


def test_nav_anchor_unavailable():
    r = check_nav_anchor(
        nav_delta=None, attributed_interest=Decimal("1.0"), tol=Decimal("0.1")
    )
    assert isinstance(r, NavAnchorResult)
    assert r.available is False
    assert r.within_tolerance is True


def test_nav_anchor_within_tolerance():
    r = check_nav_anchor(
        nav_delta=Decimal("1.05"), attributed_interest=Decimal("1.0"), tol=Decimal("0.1")
    )
    assert r.available is True
    assert r.within_tolerance is True


def test_nav_anchor_beyond_tolerance():
    r = check_nav_anchor(
        nav_delta=Decimal("2.0"), attributed_interest=Decimal("1.0"), tol=Decimal("0.1")
    )
    assert r.available is True
    assert r.within_tolerance is False


def test_nav_anchor_zero_attributed_zero_delta_within():
    r = check_nav_anchor(
        nav_delta=Decimal("0"), attributed_interest=Decimal("0"), tol=Decimal("0.1")
    )
    assert r.available is True
    assert r.within_tolerance is True


# ---------------------------------------------------------------------------
# Task 7: G3Verdict / decide_verdict
# ---------------------------------------------------------------------------


def _kw(**over):
    base = {
        "headline_bot_vs_idle": Decimal("0.06"),
        "n_windows": 10,
        "total_capital_days": Decimal("4000"),
        "ci_lo": Decimal("0.01"),
        "ci_hi": Decimal("0.10"),
        "deployment_anchor": check_deployment_anchor(
            attributed_deployed=Decimal("300"),
            observed_realized=Decimal("300"),
            tol=Decimal("0.05"),
        ),
        "nav_anchor": check_nav_anchor(
            nav_delta=None, attributed_interest=Decimal("0"), tol=Decimal("0.1")
        ),
        "min_windows": 8,
        "min_capital_days": Decimal("3990"),
        "mr_alpha_spread": Decimal("0.0"),
        "mr_alpha_ci_lo": Decimal("-0.01"),
        "mr_alpha_ci_hi": Decimal("0.02"),
        "mr_alpha_available": True,
    }
    base.update(over)
    return base


def test_verdict_pass():
    v = decide_verdict(**_kw())
    assert isinstance(v, G3Verdict)
    assert v.state is VerdictState.PASS


def test_verdict_fail_when_ci_hi_negative():
    v = decide_verdict(**_kw(ci_lo=Decimal("-0.10"), ci_hi=Decimal("-0.01")))
    assert v.state is VerdictState.FAIL


def test_verdict_insufficient_when_few_windows():
    v = decide_verdict(**_kw(n_windows=7))
    assert v.state is VerdictState.INSUFFICIENT_DATA


def test_verdict_insufficient_when_low_capital_days():
    v = decide_verdict(**_kw(total_capital_days=Decimal("100")))
    assert v.state is VerdictState.INSUFFICIENT_DATA


def test_verdict_insufficient_when_ci_straddles_zero():
    v = decide_verdict(**_kw(ci_lo=Decimal("-0.02"), ci_hi=Decimal("0.05")))
    assert v.state is VerdictState.INSUFFICIENT_DATA
    assert "straddles" in " ".join(v.reasons).lower()


def test_verdict_unreliable_when_deployment_anchor_diverges():
    bad = check_deployment_anchor(
        attributed_deployed=Decimal("300"),
        observed_realized=Decimal("50"),
        tol=Decimal("0.05"),
    )
    v = decide_verdict(**_kw(deployment_anchor=bad))
    assert v.state is VerdictState.UNRELIABLE


def test_verdict_unreliable_when_nav_anchor_diverges():
    bad = check_nav_anchor(
        nav_delta=Decimal("5"), attributed_interest=Decimal("1"), tol=Decimal("0.1")
    )
    v = decide_verdict(**_kw(nav_anchor=bad))
    assert v.state is VerdictState.UNRELIABLE


def test_verdict_unreliable_takes_priority_over_insufficient():
    bad = check_deployment_anchor(
        attributed_deployed=Decimal("300"),
        observed_realized=Decimal("50"),
        tol=Decimal("0.05"),
    )
    v = decide_verdict(**_kw(n_windows=2, deployment_anchor=bad))
    assert v.state is VerdictState.UNRELIABLE


def test_verdict_pass_at_exactly_min_windows():
    v = decide_verdict(**_kw(n_windows=8))
    assert v.state is VerdictState.PASS


def test_verdict_carries_mr_alpha_without_gating():
    # A negative MR-alpha CI (MR loses to AlwaysMarketRate) must NOT change a
    # bot-vs-idle PASS — alpha is diagnostic only.
    v = decide_verdict(
        **_kw(mr_alpha_spread=Decimal("-0.5"), mr_alpha_ci_lo=Decimal("-0.9"), mr_alpha_ci_hi=Decimal("-0.1"))
    )
    assert v.state is VerdictState.PASS
    assert v.mr_alpha_spread == Decimal("-0.5")
    assert v.mr_alpha_ci_lo == Decimal("-0.9")
    assert v.mr_alpha_ci_hi == Decimal("-0.1")
    assert v.mr_alpha_available is True


def test_verdict_exposes_headline_bot_vs_idle():
    v = decide_verdict(**_kw(headline_bot_vs_idle=Decimal("1.23")))
    assert v.headline_bot_vs_idle == Decimal("1.23")


def test_verdict_mr_alpha_unavailable_flag_carried():
    v = decide_verdict(**_kw(mr_alpha_available=False))
    assert v.state is VerdictState.PASS  # unavailability does not gate
    assert v.mr_alpha_available is False


# ---------------------------------------------------------------------------
# open_principal_at: point-in-time open principal
# ---------------------------------------------------------------------------

DAY_MS = 24 * 60 * 60 * 1000


def test_open_principal_at_fill_held_to_term_counted():
    """Fill started before as_of with period extending past as_of → counted."""
    # fill at t=0, period=2 days, effective_end = 2*DAY_MS
    f = _fill(0, "300", "0.0003", "2")  # release=None
    as_of = DAY_MS  # 1 day in — still open (end = 2*DAY_MS > as_of)
    assert open_principal_at([f], as_of) == Decimal("300")


def test_open_principal_at_matured_fill_not_counted():
    """Fill whose fill_ts + period is at/before as_of (matured) → NOT counted."""
    # fill at t=0, period=2 days, effective_end = 2*DAY_MS
    f = _fill(0, "300", "0.0003", "2")
    as_of = 2 * DAY_MS  # exact end — effective_end is NOT strictly after as_of
    assert open_principal_at([f], as_of) == Decimal("0")


def test_open_principal_at_released_fill_before_as_of_not_counted():
    """Released fill where release_ts <= as_of → closed, NOT counted."""
    # release at 1 day, as_of at 1.5 days
    f = _fill(0, "200", "0.0003", "2", release=DAY_MS)
    as_of = DAY_MS + DAY_MS // 2
    assert open_principal_at([f], as_of) == Decimal("0")


def test_open_principal_at_released_fill_after_as_of_counted():
    """Released fill where release_ts > as_of → still open at as_of → counted."""
    # release at 1.5 days, as_of at 1 day
    f = _fill(0, "200", "0.0003", "2", release=DAY_MS + DAY_MS // 2)
    as_of = DAY_MS
    assert open_principal_at([f], as_of) == Decimal("200")


def test_open_principal_at_future_fill_not_counted():
    """Fill with fill_ts > as_of → NOT counted (not yet filled)."""
    f = _fill(2 * DAY_MS, "500", "0.0003", "2")
    as_of = DAY_MS
    assert open_principal_at([f], as_of) == Decimal("0")


def test_open_principal_at_empty_list_returns_zero():
    """Empty list → Decimal('0')."""
    assert open_principal_at([], 0) == Decimal("0")


def test_open_principal_at_multiple_fills_sums_only_open():
    """Multiple fills: only open ones contribute; matured/future ones don't."""
    # open: fill at 0, period=3 days, as_of = 2*DAY_MS → open (end=3*DAY_MS > as_of)
    f_open = _fill(0, "400", "0.0003", "3")
    # matured: fill at 0, period=1 day, as_of = 2*DAY_MS → end=DAY_MS <= as_of
    f_matured = _fill(0, "100", "0.0003", "1")
    # future: fill at 3*DAY_MS → after as_of
    f_future = _fill(3 * DAY_MS, "250", "0.0003", "2")
    as_of = 2 * DAY_MS
    result = open_principal_at([f_open, f_matured, f_future], as_of)
    assert result == Decimal("400")


# ---------------------------------------------------------------------------
# clamp_active_window: concurrency-clamped interest + capital-days
# ---------------------------------------------------------------------------

DAY = 24 * 60 * 60 * 1000


def test_clamp_single_fill_equals_legacy_formula():
    # No overlap → bit-exact with Σ size·rate·duration.
    f = _fill(0, "570", "0.0003", "2")
    cw = clamp_active_window([f], cap=C)
    assert cw.interest == Decimal("570") * Decimal("0.0003") * Decimal("2")
    assert cw.raw_interest == cw.interest
    assert cw.capital_days == Decimal("570") * Decimal("2")
    assert cw.peak_concurrent == Decimal("570")


def test_clamp_two_overlapping_under_cap_no_scaling():
    # 200 + 300 = 500 < 570 → no clamp, full held-to-term interest each.
    f1 = _fill(100, "200", "0.0003", "2")
    f2 = _fill(200, "300", "0.0005", "2")
    cw = clamp_active_window([f1, f2], cap=C)
    expected = (
        Decimal("200") * Decimal("0.0003") * Decimal("2")
        + Decimal("300") * Decimal("0.0005") * Decimal("2")
    )
    assert cw.interest == expected
    assert cw.raw_interest == expected
    assert cw.peak_concurrent == Decimal("500")


def test_clamp_overlap_over_cap_scales_proportionally():
    # Two simultaneous fills 400 + 400 = 800 > 570, same window, identical span.
    # While both open, scale = 570/800; interest is clamped, raw is not.
    f1 = _fill(0, "400", "0.0003", "2")
    f2 = _fill(0, "400", "0.0003", "2")
    cw = clamp_active_window([f1, f2], cap=C)
    raw = Decimal("2") * (Decimal("400") * Decimal("0.0003") * Decimal("2"))
    assert cw.raw_interest == raw
    # both fully overlap for the whole 2 days → uniform scale 570/800
    assert cw.interest == raw * (C / Decimal("800"))
    assert cw.capital_days == C * Decimal("2")  # min(800, 570) for 2 days
    assert cw.peak_concurrent == Decimal("800")


def test_clamp_partial_overlap_only_clamps_overlap_region():
    # f1 [0, 2d) size 400; f2 [1d, 3d) size 400. Overlap [1d,2d): 800>570 clamp.
    # Non-overlap regions ([0,1d) f1 only, [2d,3d) f2 only) stay full.
    f1 = _fill(0, "400", "0.0003", "2")
    f2 = _fill(DAY, "400", "0.0003", "2")
    cw = clamp_active_window([f1, f2], cap=C)
    rate = Decimal("0.0003")
    scale = C / Decimal("800")
    # f1: [0,1d) full 400 + [1d,2d) scaled 400*570/800
    # f2: [1d,2d) scaled 400*570/800 + [2d,3d) full 400
    f1_int = Decimal("400") * rate * Decimal("1") + Decimal("400") * scale * rate * Decimal("1")
    f2_int = Decimal("400") * scale * rate * Decimal("1") + Decimal("400") * rate * Decimal("1")
    assert cw.interest == f1_int + f2_int
    assert cw.peak_concurrent == Decimal("800")


def test_clamp_release_caps_duration():
    # release at 1 day → 1-day interest, like _fill_duration_days.
    f = _fill(0, "570", "0.0003", "2", release=DAY)
    cw = clamp_active_window([f], cap=C)
    assert cw.interest == Decimal("570") * Decimal("0.0003") * Decimal("1")
    assert cw.capital_days == Decimal("570") * Decimal("1")


def test_clamp_empty_is_zero():
    cw = clamp_active_window([], cap=C)
    assert cw.interest == Decimal("0")
    assert cw.capital_days == Decimal("0")
    assert cw.raw_interest == Decimal("0")
    assert cw.peak_concurrent == Decimal("0")


def test_clamp_zero_cap_raises():
    with pytest.raises(ValueError, match="cap must be positive"):
        clamp_active_window([_fill(0, "100", "0.0003", "2")], cap=Decimal("0"))


def test_attribute_active_per_window_bucketing_no_cross_window_join():
    # Per-window bucketing (by fill_ts), NOT a span-clipped sweep: a fill filled
    # late in window 1 keeps its FULL held-to-term interest in window 1, and
    # window 2 clamps only its own fills. The two 400-fills are concurrent in
    # time but live in different buckets, so their 800 sum is NOT jointly clamped.
    # This pins the implemented behavior (full-span headline + capital_days use a
    # single bucket and ARE jointly clamped; only the per-window CI buckets split).
    f1 = _fill(WEEK - 1, "400", "0.0003", "2")  # spans into window 2
    f2 = _fill(WEEK + 1, "400", "0.0003", "2")
    out = attribute_active([f1, f2], capital=C, window_bounds=[(0, WEEK), (WEEK, 2 * WEEK)])
    full = Decimal("400") * Decimal("0.0003") * Decimal("2") / C * Decimal("100")
    assert out[0].n_trades == 1
    assert out[1].n_trades == 1
    assert out[0].net_monthly == full  # un-clamped: 400 < cap alone
    assert out[1].net_monthly == full


# ---------------------------------------------------------------------------
# ClampDiagnostic derived properties (over_deploy detection + report figures)
# ---------------------------------------------------------------------------


def test_clamp_diagnostic_excess_return_pct_formula():
    d = ClampDiagnostic(
        cap=Decimal("1000"),
        peak_concurrent=Decimal("1200"),
        raw_interest=Decimal("100"),
        clamped_interest=Decimal("80"),
    )
    # (100 - 80) / 1000 * 100 = 2.0
    assert d.excess_return_pct == Decimal("2")
    assert d.over_deployed is True


def test_clamp_diagnostic_over_deploy_factor_formula():
    d = ClampDiagnostic(
        cap=Decimal("570"),
        peak_concurrent=Decimal("855"),
        raw_interest=Decimal("0.5"),
        clamped_interest=Decimal("0.33"),
    )
    assert d.over_deploy_factor == Decimal("1.5")  # 855 / 570


def test_clamp_diagnostic_at_cap_is_not_over_deployed():
    d = ClampDiagnostic(
        cap=Decimal("570"),
        peak_concurrent=Decimal("570"),
        raw_interest=Decimal("0.2"),
        clamped_interest=Decimal("0.2"),
    )
    assert d.over_deployed is False  # peak == cap is within budget (strict >)
    assert d.excess_return_pct == Decimal("0")


def test_clamp_diagnostic_zero_cap_guards_are_zero():
    d = ClampDiagnostic(
        cap=Decimal("0"),
        peak_concurrent=Decimal("0"),
        raw_interest=Decimal("0"),
        clamped_interest=Decimal("0"),
    )
    assert d.over_deploy_factor == Decimal("0")
    assert d.excess_return_pct == Decimal("0")


# ---- E3: AlwaysFRR arm 素材（import 已在檔頭，見上）----
def _stat(mts: int, frr: str | None) -> FundingStat:
    return FundingStat(
        symbol="fUST", mts=mts,
        frr=Decimal(frr) if frr is not None else None,
        avg_period=Decimal("95"),
    )


def test_frr_points_annualize_by_365():
    pts = frr_points_from_stats([_stat(0, "0.00000102")])
    assert pts[0].rate == Decimal("0.00000102") * FRR_ANNUALIZATION
    # ×365 後落在 assert_market_rate_band 的 [1e-5, 0.05] 內
    assert Decimal("0.00001") <= pts[0].rate <= Decimal("0.05")


def test_frr_points_skip_null_frr():
    assert frr_points_from_stats([_stat(0, None)]) == []


def test_frr_points_preserve_mts_order():
    pts = frr_points_from_stats([_stat(100, "1e-6"), _stat(200, "2e-6")])
    assert [p.mts for p in pts] == [100, 200]


def test_frr_benchmark_dataclass_shape():
    b = FrrBenchmark(
        available=False, spread=Decimal("0"), ci_lo=Decimal("0"),
        ci_hi=Decimal("0"), reason="funding_stats empty",
    )
    assert b.available is False and b.reason == "funding_stats empty"


def test_fill_duration_days_public_alias():
    # 公開版與既有語意一致：無 release = held-to-term
    assert fill_duration_days(_fill(0, "100", "0.0002", "2")) == Decimal("2")
    # release 提早 → 取實際存續
    one_day_ms = 24 * 60 * 60 * 1000
    assert fill_duration_days(
        _fill(0, "100", "0.0002", "2", release=one_day_ms)
    ) == Decimal("1")


# ---------------------------------------------------------------------------
# apply_credit_closes — venue close truth joined onto fills (fcc → CreditClosed).
# Regression root: 2026-07-19 anchor divergence — borrower returned 1338.03
# after 40 min, bot re-lent it; held-to-term double-counted the principal.
# ---------------------------------------------------------------------------


def _close(credit_id, amount, mts_create, close_ts):
    return CreditCloseRecord(
        credit_id=credit_id, amount=Decimal(amount),
        mts_create=mts_create, close_ts_ms=close_ts,
    )


def test_apply_credit_closes_regression_1338_early_return():
    h = 3600 * 1000
    f1 = _fill(0, "1338.03", "0.0003", "2")            # 08:30 fill → returned early
    f2 = _fill(1 * h, "1338.03", "0.0003", "2")        # 09:10 re-lend, still open
    close = _close(555, "1338.03", mts_create=10, close_ts=h - 300_000)
    out = apply_credit_closes([f1, f2], [close])
    assert out[0].release_ts_ms == h - 300_000         # f1 closed by venue truth
    assert out[1].release_ts_ms is None                # f2 untouched
    # open principal at "now" no longer double-counts the returned principal
    assert open_principal_at(out, 2 * h) == Decimal("1338.03")


def test_apply_credit_closes_matches_nearest_preceding_fill():
    f_old = _fill(0, "100", "0.0003", "2")
    f_near = _fill(5_000, "100", "0.0003", "2")
    close = _close(1, "100", mts_create=6_000, close_ts=50_000)
    out = apply_credit_closes([f_old, f_near], [close])
    assert out[0].release_ts_ms is None
    assert out[1].release_ts_ms == 50_000


def test_apply_credit_closes_ignores_amount_mismatch_and_future_fills():
    f_future = _fill(400_000, "100", "0.0003", "2")    # beyond mts_create+slack(5m)
    f_other = _fill(0, "99", "0.0003", "2")            # different amount
    close = _close(1, "100", mts_create=6_000, close_ts=500_000)
    out = apply_credit_closes([f_future, f_other], [close])
    assert [f.release_ts_ms for f in out] == [None, None]


def test_apply_credit_closes_keeps_earlier_existing_release():
    f = _fill(0, "100", "0.0003", "2", release=10_000)  # RESERVATION_RELEASED earlier
    close = _close(1, "100", mts_create=100, close_ts=50_000)
    out = apply_credit_closes([f], [close])
    assert out[0].release_ts_ms == 10_000


def test_apply_credit_closes_dedupes_by_credit_id():
    f1 = _fill(0, "100", "0.0003", "2")
    f2 = _fill(1_000, "100", "0.0003", "2")
    dupes = [
        _close(7, "100", mts_create=1_500, close_ts=60_000),
        _close(7, "100", mts_create=1_500, close_ts=70_000),  # redelivery
    ]
    out = apply_credit_closes([f1, f2], dupes)
    # one credit → exactly one fill released (the nearest), not two
    assert sorted(f.release_ts_ms is not None for f in out) == [False, True]
