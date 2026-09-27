"""Pure G3 attribution: arms, deployment check and the four-state verdict.

The active arm reads venue credits (credit_attribution.CreditLifetime). Real
numbers from the live account (2026-09-25..27): credit 466642176, 150.76884612
fUST at 0.00019999/day, repaid after 842 s (14 min); expired credit 466451710
at 0.0001482, held 2 days (09-22 18:08:30Z .. 09-24 18:08:31Z).
"""
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.live_validation.credit_attribution import CreditLifetime
from bfx_funding_bot.modules.live_validation.live_attribution import (
    FRR_ANNUALIZATION,
    MS_PER_DAY,
    DeploymentCheck,
    FrrBenchmark,
    G3Verdict,
    MarketRatePoint,
    VerdictState,
    assert_market_rate_band,
    attribute_active,
    attribute_idle,
    attribute_passive,
    credit_capital_days,
    decide_verdict,
    frr_points_from_stats,
    peak_open_principal,
)

WEEK = 7 * 24 * 60 * 60 * 1000
DAY = 24 * 60 * 60 * 1000
C = Decimal("570")

AMOUNT = Decimal("150.76884612")
RATE = Decimal("0.00019999")
CREATED = 1790350246000        # 2026-09-25 15:30:46Z
REPAID = 1790351088000         # + 842 s
EXPIRED_OPEN = 1790100510000   # 466451710: 2026-09-22 18:08:30Z
EXPIRED_CLOSE = 1790273311000  # last payout 09-24 18:08:31Z (its expiry)
EXPIRED_RATE = Decimal("0.0001482")
MON = 1789948800000            # 2026-09-21, the calendar week both fall in


def _credit(credit_id: str, opened: int, closed: int | None, *, amount: Decimal = AMOUNT,
            rate: Decimal = RATE) -> CreditLifetime:
    return CreditLifetime(credit_id=credit_id, symbol="fUST", amount=amount, rate=rate,
                          period_days=2, mts_create=opened, opened_ms=opened, closed_ms=closed)


EARLY = _credit("466642176", CREATED, REPAID)
EXPIRED = _credit("466451710", EXPIRED_OPEN, EXPIRED_CLOSE, rate=EXPIRED_RATE)


def test_marketratepoint_is_frozen():
    p = MarketRatePoint(mts=0, rate=Decimal("0.0002"))
    with pytest.raises(FrozenInstanceError):
        p.rate = Decimal("0.9")  # type: ignore[misc]


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
# attribute_active: venue credits × actual held time, clipped per window
# ---------------------------------------------------------------------------


def test_attribute_active_early_repaid_credit_earns_its_fourteen_minutes():
    """Regression: the fill model booked 466642176 as 2 days held-to-term; the
    venue says it was lent 842 s."""
    out = attribute_active([EARLY], capital=C, window_bounds=[(MON, MON + WEEK)],
                           now_ms=MON + 2 * WEEK)
    fourteen_min = AMOUNT * RATE * Decimal(842_000) / MS_PER_DAY
    assert out[0].net_monthly == fourteen_min / C * Decimal("100")
    held_to_term = AMOUNT * RATE * Decimal("2")
    assert out[0].net_monthly < held_to_term / C * Decimal("100") / Decimal("200")
    assert out[0].n_trades == 1
    assert out[0].fill_rate == RATE


def test_attribute_active_expired_credit_earns_its_two_days():
    out = attribute_active([EXPIRED], capital=C, window_bounds=[(MON, MON + WEEK)],
                           now_ms=MON + 2 * WEEK)
    held = Decimal(EXPIRED_CLOSE - EXPIRED_OPEN) / MS_PER_DAY   # 2 days + 1 s
    assert out[0].net_monthly == AMOUNT * EXPIRED_RATE * held / C * Decimal("100")


def test_attribute_active_splits_a_credit_across_windows():
    """A credit is booked where it was held, not whole to the window it opened in."""
    mid = EXPIRED_OPEN + DAY
    out = attribute_active([EXPIRED], capital=C,
                           window_bounds=[(MON, mid), (mid, MON + WEEK)], now_ms=MON + WEEK)
    first = AMOUNT * EXPIRED_RATE * Decimal(DAY) / MS_PER_DAY
    second = AMOUNT * EXPIRED_RATE * Decimal(EXPIRED_CLOSE - mid) / MS_PER_DAY
    assert [o.net_monthly for o in out] == [first / C * 100, second / C * 100]
    assert [o.n_trades for o in out] == [1, 1]


def test_attribute_active_open_credit_accrues_until_now():
    open_credit = _credit("9", CREATED, None)
    now = CREATED + DAY // 2
    out = attribute_active([open_credit], capital=C, window_bounds=[(MON, MON + WEEK)],
                           now_ms=now)
    assert out[0].net_monthly == AMOUNT * RATE * Decimal("0.5") / C * Decimal("100")


def test_attribute_active_fill_rate_is_capital_weighted():
    out = attribute_active([EARLY, EXPIRED], capital=C, window_bounds=[(MON, MON + WEEK)],
                           now_ms=MON + WEEK)
    days = credit_capital_days([EARLY, EXPIRED], MON, MON + WEEK, now_ms=MON + WEEK)
    interest = out[0].net_monthly * C / Decimal("100")
    assert out[0].fill_rate == interest / days
    assert EXPIRED_RATE < out[0].fill_rate < RATE


def test_attribute_active_per_window_capital():
    basis = {(0, WEEK): Decimal("100"), (WEEK, 2 * WEEK): Decimal("200")}
    credit = _credit("1", 0, 2 * WEEK, amount=Decimal("100"), rate=Decimal("0.001"))
    out = attribute_active([credit], capital=lambda lo, hi: basis[(lo, hi)],
                           window_bounds=list(basis), now_ms=2 * WEEK)
    assert out[0].net_monthly == Decimal("0.7") * 100 / 100
    assert out[1].net_monthly == Decimal("0.7") * 100 / 200


def test_attribute_active_no_credit_window_is_idle():
    out = attribute_active([], capital=C, window_bounds=[(0, WEEK)], now_ms=WEEK)
    assert (out[0].net_monthly, out[0].n_trades, out[0].fill_rate) == (0, 0, 0)


def test_attribute_active_zero_capital_raises():
    with pytest.raises(ValueError, match="capital must be positive"):
        attribute_active([], capital=Decimal("0"), window_bounds=[(0, WEEK)], now_ms=WEEK)


def test_attribute_active_credit_outside_all_windows_ignored():
    out = attribute_active([EARLY], capital=C, window_bounds=[(0, WEEK)], now_ms=REPAID)
    assert out[0].n_trades == 0 and out[0].net_monthly == 0


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
    active = attribute_active([], capital=C, window_bounds=bounds, now_ms=2 * WEEK)
    idle = attribute_idle(window_bounds=bounds)
    assert [o.month_mts for o in active] == [o.month_mts for o in idle]


def test_attribute_idle_empty_bounds():
    assert attribute_idle(window_bounds=[]) == []


# ---------------------------------------------------------------------------
# G3Verdict / decide_verdict
# ---------------------------------------------------------------------------


def _kw(**over):
    base = {
        "headline_bot_vs_idle": Decimal("0.06"),
        "n_windows": 10,
        "total_capital_days": Decimal("4000"),
        "ci_lo": Decimal("0.01"),
        "ci_hi": Decimal("0.10"),
        "ledger_divergence": [],
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


def test_verdict_unreliable_when_ledger_diverges():
    v = decide_verdict(**_kw(ledger_divergence=["week 2026-09-21 credits net 1 vs ledger 2"]))
    assert v.state is VerdictState.UNRELIABLE
    assert "week 2026-09-21" in v.reasons[0]


def test_verdict_unreliable_takes_priority_over_insufficient():
    v = decide_verdict(**_kw(n_windows=2, ledger_divergence=["week x"]))
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
# peak_open_principal / DeploymentCheck (replaces the over-deploy clamp)
# ---------------------------------------------------------------------------


def test_peak_open_principal_counts_concurrent_credits_only():
    a = _credit("a", 0, 2 * DAY, amount=Decimal("100"))
    b = _credit("b", DAY, 3 * DAY, amount=Decimal("50"))
    c = _credit("c", 3 * DAY, 4 * DAY, amount=Decimal("120"))   # opens as b closes
    assert peak_open_principal([a, b, c], now_ms=5 * DAY) == Decimal("150")
    assert peak_open_principal([], now_ms=DAY) == 0


def test_peak_open_principal_open_credit_counts_until_now():
    assert peak_open_principal([_credit("x", CREATED, None)], now_ms=CREATED + DAY) == AMOUNT


def test_re_lent_principal_is_not_double_counted():
    """The clamp's reason (re-lent principal double counted by held-to-term) is
    gone: 466642176 closed after 842 s, so re-lending it does not overlap."""
    relent = _credit("466642177", REPAID, REPAID + 2 * DAY)
    assert peak_open_principal([EARLY, relent], now_ms=REPAID + 3 * DAY) == AMOUNT


def test_deployment_check_flags_only_above_cap():
    assert DeploymentCheck(Decimal("570"), Decimal("570")).over_deployed is False
    over = DeploymentCheck(Decimal("570"), Decimal("855"))
    assert over.over_deployed is True
    assert over.over_deploy_factor == Decimal("1.5")
    assert DeploymentCheck(Decimal("0"), Decimal("0")).over_deploy_factor == 0


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
