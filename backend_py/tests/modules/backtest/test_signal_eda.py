# tests/modules/backtest/test_signal_eda.py
from decimal import Decimal

import numpy as np
import pandas as pd

from bfx_funding_bot.modules.backtest.signal_eda import (
    SIGNALS,
    SPLIT_MTS,
    CellRegimeIC,
    SignalVerdict,
    add_forward_rate_change,
    amount_pctile,
    annualize_daily_spread_pp,
    bh_fdr,
    block_bootstrap_ic,
    build_signal_frame,
    decide_signal,
    frr_curvature,
    frr_trend,
    quintile_spread,
    render_report,
    spearman_ic,
    spike_pctile,
    spike_z,
    split_regime,
    utilization_pctile,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat

_DAY = 86_400_000


def _candles(closes: list[str], *, start: int = 1_500_000_000_000, step: int = _DAY):
    return [
        FundingCandle(symbol="fUST", timeframe="1D", period_agg="p2",
                      mts=start + i * step, open=Decimal(c), high=Decimal(c),
                      low=Decimal(c), close=Decimal(c))
        for i, c in enumerate(closes)
    ]


def _stats(rows: list[dict], *, start: int = 1_500_000_000_000, step: int = _DAY):
    return [
        FundingStat(symbol="fUST", mts=start + i * step,
                    frr=Decimal(str(r["frr"])),
                    funding_amount=Decimal(str(r.get("amt", 1000))),
                    funding_amount_used=Decimal(str(r.get("used", 500))))
        for i, r in enumerate(rows)
    ]


def test_build_signal_frame_aligns_stats_onto_candle_grid() -> None:
    candles = _candles(["0.0001", "0.0002", "0.0003"])
    stats = _stats([{"frr": 1e-6}, {"frr": 2e-6}, {"frr": 3e-6}])
    df = build_signal_frame(candles, stats)
    assert list(df["close"]) == [0.0001, 0.0002, 0.0003]
    assert df["frr"].tolist() == [1e-6, 2e-6, 3e-6]
    assert df["funding_amount"].tolist() == [1000.0, 1000.0, 1000.0]


def test_build_signal_frame_asof_uses_latest_prior_stat() -> None:
    # stat at t0 only; candles at t0, t1 -> t1 should carry t0's frr (backward asof)
    candles = _candles(["0.0001", "0.0002"])
    stats = _stats([{"frr": 5e-6}])  # single stat at t0
    df = build_signal_frame(candles, stats)
    assert df["frr"].tolist() == [5e-6, 5e-6]


def test_add_forward_rate_change_is_time_based_mean_minus_spot() -> None:
    # closes 1,2,3,4,5 on a daily grid; H=2 -> mean(next 2 closes) - spot
    df = build_signal_frame(_candles(["1", "2", "3", "4", "5"]), _stats([{"frr": 1e-6}] * 5))
    out = add_forward_rate_change(df, [2])
    # row0: mean(2,3) - 1 = 1.5 ; row1: mean(3,4) - 2 = 1.5 ; ...
    assert out["fwd_d2"].iloc[0] == 1.5
    assert out["fwd_d2"].iloc[1] == 1.5
    # last row has no forward candles -> NaN
    assert np.isnan(out["fwd_d2"].iloc[-1])


def test_split_regime_is_disjoint() -> None:
    early_frame = build_signal_frame(
        _candles(["1"], start=SPLIT_MTS - 2 * _DAY),
        _stats([{"frr": 1e-6}], start=SPLIT_MTS - 2 * _DAY),
    )
    late_frame = build_signal_frame(
        _candles(["1"], start=SPLIT_MTS + _DAY),
        _stats([{"frr": 1e-6}], start=SPLIT_MTS + _DAY),
    )
    combined = pd.concat([early_frame, late_frame], ignore_index=True)
    e, late_df = split_regime(combined)
    assert (e["mts"] < SPLIT_MTS).all()
    assert (late_df["mts"] >= SPLIT_MTS).all()
    assert set(e["mts"]).isdisjoint(set(late_df["mts"]))


def test_build_signal_frame_stats_after_all_candles_yield_nan() -> None:
    # stats start one day AFTER the single candle -> backward asof finds nothing -> NaN
    candles = _candles(["0.0001"])  # one candle at t0
    stats = _stats([{"frr": 9e-6}], start=1_500_000_000_000 + _DAY)
    df = build_signal_frame(candles, stats)
    assert np.isnan(df["frr"].iloc[0])
    assert np.isnan(df["funding_amount"].iloc[0])


def test_spike_z_is_zero_on_flat_then_positive_on_spike() -> None:
    frr = [1e-6] * 30 + [1e-5]  # flat then 10x spike
    df = build_signal_frame(_candles(["1"] * 31), _stats([{"frr": v} for v in frr]))
    z = spike_z(df, w=14)
    assert z.iloc[-1] > 3.0  # spike is many sigma above rolling baseline
    assert abs(z.iloc[20]) < 1e-9  # flat region -> z ~ 0


def test_frr_trend_sign_tracks_direction() -> None:
    rising = [float(i) * 1e-6 for i in range(1, 41)]
    df = build_signal_frame(_candles(["1"] * 40), _stats([{"frr": v} for v in rising]))
    t = frr_trend(df, k=3)
    assert t.dropna().iloc[-1] > 0  # monotone rising -> positive trend


def test_frr_curvature_zero_on_linear_trend() -> None:
    # constant slope -> first difference is constant -> second difference is 0
    linear = [float(i) * 1e-6 for i in range(1, 61)]
    df = build_signal_frame(_candles(["1"] * 60), _stats([{"frr": v} for v in linear]))
    c = frr_curvature(df, k=3)
    assert abs(c.dropna().iloc[-1]) < 1e-9


def test_frr_curvature_positive_on_accelerating_rise() -> None:
    # quadratic growth -> first difference itself rising -> positive curvature
    accel = [float(i**2) * 1e-6 for i in range(1, 61)]
    df = build_signal_frame(_candles(["1"] * 60), _stats([{"frr": v} for v in accel]))
    c = frr_curvature(df, k=3)
    assert c.dropna().iloc[-1] > 0


def test_spike_pctile_is_in_unit_interval_and_high_at_spike() -> None:
    frr = [1e-6] * 30 + [1e-5]  # flat then 10x spike
    df = build_signal_frame(_candles(["1"] * 31), _stats([{"frr": v} for v in frr]))
    p = spike_pctile(df, w=14)
    assert (p.dropna() >= 0).all() and (p.dropna() <= 1).all()
    assert p.iloc[-1] == 1.0  # spike is the max within its trailing window


def test_pctile_is_in_unit_interval_and_high_at_max() -> None:
    amt = list(range(1, 41))
    df = build_signal_frame(_candles(["1"] * 40), _stats([{"frr": 1e-6, "amt": a} for a in amt]))
    p = amount_pctile(df, w=20)
    assert (p.dropna() >= 0).all() and (p.dropna() <= 1).all()
    assert p.iloc[-1] == 1.0  # latest is the max of its window


def test_utilization_uses_ratio_not_absolute() -> None:
    rows = [{"frr": 1e-6, "amt": 1000, "used": u} for u in range(100, 140)]
    df = build_signal_frame(_candles(["1"] * 40), _stats(rows))
    u = utilization_pctile(df, w=20)
    assert u.iloc[-1] == 1.0  # rising utilization -> latest at top percentile


def test_signals_registry_has_four_relative_signals() -> None:
    # GUARD: every signal must be a rolling/relative transform, never raw frr/amount.
    assert set(SIGNALS) == {"frr_trend", "spike_detect", "funding_supply", "utilization"}


def test_spearman_ic_recovers_planted_positive_correlation() -> None:
    rng = np.random.default_rng(0)
    x = pd.Series(rng.normal(size=500))
    y = x + pd.Series(rng.normal(size=500)) * 0.3  # strong positive monotone link
    ic = spearman_ic(x, y)
    assert ic > 0.7


def test_spearman_ic_near_zero_on_independent_series() -> None:
    rng = np.random.default_rng(1)
    x = pd.Series(rng.normal(size=500))
    y = pd.Series(rng.normal(size=500))
    assert abs(spearman_ic(x, y)) < 0.15


def test_spearman_ic_nan_when_too_few_pairs() -> None:
    assert np.isnan(spearman_ic(pd.Series([1.0, 2.0]), pd.Series([1.0, np.nan])))


def test_quintile_spread_monotone_for_linear_link() -> None:
    x = pd.Series(np.linspace(0, 1, 200))
    y = x * 2.0
    spread, monotonic = quintile_spread(x, y)
    assert spread > 0
    assert monotonic is True


def test_quintile_spread_returns_nan_on_constant_signal() -> None:
    x = pd.Series([1.0] * 100)
    y = pd.Series(np.random.default_rng(0).normal(size=100))
    spread, monotonic = quintile_spread(x, y)
    assert np.isnan(spread)
    assert monotonic is False


def test_bootstrap_ci_excludes_zero_for_strong_link() -> None:
    rng = np.random.default_rng(2)
    x = pd.Series(rng.normal(size=400))
    y = x + pd.Series(rng.normal(size=400)) * 0.3
    res = block_bootstrap_ic(x, y)
    assert res.ci_lo > 0  # CI strictly above 0
    assert res.p_value < 0.05


def test_bootstrap_ci_includes_zero_for_noise() -> None:
    rng = np.random.default_rng(3)
    x = pd.Series(rng.normal(size=400))
    y = pd.Series(rng.normal(size=400))
    res = block_bootstrap_ic(x, y)
    assert res.ci_lo < 0 < res.ci_hi  # CI straddles 0
    assert res.p_value > 0.05


def test_bootstrap_is_reproducible_with_fixed_seed() -> None:
    rng = np.random.default_rng(4)
    x = pd.Series(rng.normal(size=200))
    y = x * 0.5 + pd.Series(rng.normal(size=200))
    a = block_bootstrap_ic(x, y)
    b = block_bootstrap_ic(x, y)
    assert a.ci_lo == b.ci_lo and a.ci_hi == b.ci_hi and a.p_value == b.p_value


def test_bootstrap_nan_when_too_few() -> None:
    res = block_bootstrap_ic(pd.Series([1.0] * 5), pd.Series([1.0] * 5))
    assert np.isnan(res.ci_lo)


def test_bootstrap_handles_constant_resample_without_warning_error() -> None:
    # A near-constant series can produce zero-variance resample blocks (rank corr = nan).
    # Under strict warning filters this must NOT raise; nan ics are dropped, not fatal.
    import warnings
    rng = np.random.default_rng(7)
    x = pd.Series([1.0] * 60 + list(rng.normal(size=40)))
    y = pd.Series([1.0] * 60 + list(rng.normal(size=40)))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        res = block_bootstrap_ic(x, y)
    assert hasattr(res, "ci_lo")  # completed without raising


def test_bootstrap_p_value_zero_is_valid_floor_for_monotone() -> None:
    # A perfectly monotone link -> every non-NaN resample IC is positive ->
    # frac_neg=0 -> p_value=0.0. This is a valid floor, not an error.
    x = pd.Series(np.linspace(0, 1, 300))
    y = x * 2.0
    res = block_bootstrap_ic(x, y)
    assert res.p_value == 0.0
    assert res.ci_lo > 0


# ---------------------------------------------------------------------------
# Task 5: Benjamini-Hochberg FDR correction
# ---------------------------------------------------------------------------


def test_bh_fdr_rejects_clearly_small_p() -> None:
    rejected = bh_fdr([0.001, 0.002, 0.9, 0.8], alpha=0.05)
    assert rejected == [True, True, False, False]


def test_bh_fdr_marginal_p_passes_raw_but_fails_adjusted() -> None:
    # GUARD: a raw-p of 0.04 (passes un-adjusted 0.05) must be rejected by BH
    # when buried among many large p-values.
    pvals = [0.04] + [0.6] * 19  # 20 tests, only one smallish
    rejected = bh_fdr(pvals, alpha=0.05)
    assert rejected[0] is False  # BH threshold for rank 1 = (1/20)*0.05 = 0.0025
    assert not any(rejected)


def test_bh_fdr_empty() -> None:
    assert bh_fdr([], alpha=0.05) == []


def test_bh_fdr_treats_nan_as_not_rejected() -> None:
    rejected = bh_fdr([0.001, float("nan"), 0.002], alpha=0.05)
    assert rejected == [True, False, True]


# ---------------------------------------------------------------------------
# Task 6: GO/KILL decision
# ---------------------------------------------------------------------------


def _ic(cell: str, regime: str, ic: float, sig: bool = True) -> CellRegimeIC:
    return CellRegimeIC(cell=cell, regime=regime, horizon=7, ic=ic, fdr_significant=sig)


def test_go_when_robust_across_cells_and_regimes() -> None:
    obs = [
        _ic("fUST_a30", "early", 0.06), _ic("fUST_a30", "late", 0.05),
        _ic("fUST_p2", "early", 0.05), _ic("fUST_p2", "late", 0.04),
        _ic("fUSD_a30", "early", 0.05), _ic("fUSD_a30", "late", 0.04),
        _ic("fUSD_p2", "early", -0.01, sig=False), _ic("fUSD_p2", "late", 0.00, sig=False),
    ]
    v = decide_signal("frr_trend", obs)
    assert v.verdict == "GO"


def test_kill_when_sign_flips_across_regimes() -> None:
    obs = [
        _ic("fUST_a30", "early", 0.06), _ic("fUST_a30", "late", -0.06),
        _ic("fUST_p2", "early", 0.05), _ic("fUST_p2", "late", -0.05),
        _ic("fUSD_a30", "early", 0.05), _ic("fUSD_a30", "late", -0.05),
        _ic("fUSD_p2", "early", 0.05), _ic("fUSD_p2", "late", -0.05),
    ]
    v = decide_signal("spike_detect", obs)
    assert v.verdict == "KILL"
    assert "regime" in v.reason.lower()


def test_kill_when_only_one_cell_significant() -> None:
    obs = [
        _ic("fUST_a30", "early", 0.06), _ic("fUST_a30", "late", 0.05),
        _ic("fUST_p2", "early", 0.01, sig=False), _ic("fUST_p2", "late", 0.00, sig=False),
        _ic("fUSD_a30", "early", 0.00, sig=False), _ic("fUSD_a30", "late", 0.01, sig=False),
        _ic("fUSD_p2", "early", 0.00, sig=False), _ic("fUSD_p2", "late", 0.00, sig=False),
    ]
    v = decide_signal("utilization", obs)
    assert v.verdict == "KILL"


def test_kill_when_median_ic_below_floor() -> None:
    obs = [
        _ic("fUST_a30", "early", 0.02), _ic("fUST_a30", "late", 0.02),
        _ic("fUST_p2", "early", 0.02), _ic("fUST_p2", "late", 0.02),
        _ic("fUSD_a30", "early", 0.02), _ic("fUSD_a30", "late", 0.02),
        _ic("fUSD_p2", "early", 0.02), _ic("fUSD_p2", "late", 0.02),
    ]
    v = decide_signal("funding_supply", obs)
    assert v.verdict == "KILL"
    assert "floor" in v.reason.lower()


def test_kill_when_robust_cells_disagree_in_sign() -> None:
    obs = [
        _ic("fUST_a30", "early", 0.06), _ic("fUST_a30", "late", 0.05),
        _ic("fUST_p2", "early", 0.05), _ic("fUST_p2", "late", 0.04),
        _ic("fUSD_a30", "early", 0.05), _ic("fUSD_a30", "late", 0.04),
        _ic("fUSD_p2", "early", -0.05), _ic("fUSD_p2", "late", -0.06),
    ]
    v = decide_signal("frr_trend", obs)
    assert v.verdict == "KILL"
    assert "disagree" in v.reason.lower()


# ---------------------------------------------------------------------------
# Task 7: render_report
# ---------------------------------------------------------------------------


def test_render_report_contains_verdict_and_table_headers() -> None:
    verdicts = [
        SignalVerdict("frr_trend", "GO", "3/4 cells robust, median IC 0.050", 0.05),
        SignalVerdict("utilization", "KILL", "no FDR-significant IC in any cell/regime", 0.00),
    ]
    md = render_report(verdicts, data_window="2016-2026, fUST 86 / fUSD 115 windows")
    assert "# Signal EDA Funnel" in md
    assert "frr_trend" in md and "GO" in md
    assert "utilization" in md and "KILL" in md
    assert "0.050" in md
    assert "2016-2026" in md


def test_render_report_null_result_when_all_kill() -> None:
    verdicts = [
        SignalVerdict("frr_trend", "KILL", "regime-fragile", 0.01),
        SignalVerdict("spike_detect", "KILL", "below floor", 0.02),
    ]
    md = render_report(verdicts, data_window="test window")
    assert "Null result" in md
    assert "falsified" in md.lower()


def test_resample_daily_collapses_subdaily_to_last_of_day() -> None:
    from bfx_funding_bot.modules.backtest.signal_eda import resample_daily

    half = 43_200_000  # 12h -> 2 rows per day over 3 days
    candles = _candles(["1", "2", "3", "4", "5", "6"], step=half)
    stats = _stats([{"frr": 1e-6}] * 6, step=half)
    daily = resample_daily(build_signal_frame(candles, stats))
    assert len(daily) == 3
    assert list(daily["close"]) == [2.0, 4.0, 6.0]  # last-of-day kept


def test_resample_daily_empty() -> None:
    from bfx_funding_bot.modules.backtest.signal_eda import resample_daily

    assert resample_daily(pd.DataFrame()).empty


# ---------------------------------------------------------------------------
# Gate 5: economic hurdle (2026-07-19 methodology audit) — statistical GO must
# also clear a fee+friction-scale economic effect, else it's noise vs the
# execution-layer levers (E1/E2 moved ~1-2 pp APR; a signal worth < 0.5 pp
# gross can never survive 15% fee + fill friction).
# ---------------------------------------------------------------------------


def _all_pass_obs() -> list[CellRegimeIC]:
    return [
        _ic("fUST_a30", "early", 0.06), _ic("fUST_a30", "late", 0.05),
        _ic("fUST_p2", "early", 0.05), _ic("fUST_p2", "late", 0.04),
        _ic("fUSD_a30", "early", 0.05), _ic("fUSD_a30", "late", 0.04),
        _ic("fUSD_p2", "early", 0.05), _ic("fUSD_p2", "late", 0.04),
    ]


def test_annualize_daily_spread_pp() -> None:
    assert abs(annualize_daily_spread_pp(0.0001) - 3.65) < 1e-9


def test_gate5_kills_statistically_significant_but_economically_trivial() -> None:
    # 0.000005/day → 0.18 pp APR: passes all 4 statistical gates, fails hurdle.
    v = decide_signal("s", _all_pass_obs(), median_quintile_spread=0.000005)
    assert v.verdict == "KILL"
    assert "economically insignificant" in v.reason


def test_gate5_passes_above_hurdle() -> None:
    # 0.00002/day → 0.73 pp APR ≥ 0.5 hurdle.
    v = decide_signal("s", _all_pass_obs(), median_quintile_spread=0.00002)
    assert v.verdict == "GO"


def test_gate5_negative_spread_uses_magnitude() -> None:
    v = decide_signal("s", _all_pass_obs(), median_quintile_spread=-0.00002)
    assert v.verdict == "GO"


def test_gate5_absent_spread_keeps_legacy_behavior() -> None:
    # Backward compat: callers not passing spread get the original 4-gate verdict.
    v = decide_signal("s", _all_pass_obs())
    assert v.verdict == "GO"
