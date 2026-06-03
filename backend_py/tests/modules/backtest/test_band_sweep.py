# tests/modules/backtest/test_band_sweep.py
import math
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.band_sweep import enumerate_bands
from bfx_funding_bot.modules.backtest.band_sweep import simulate_period_path, period_profile
from bfx_funding_bot.modules.backtest.band_sweep import active_deflated_sharpe
from bfx_funding_bot.modules.backtest.band_sweep import paired_difference_ci, is_tied
from bfx_funding_bot.modules.backtest.band_sweep import split_disjoint, SPLIT_MTS
from bfx_funding_bot.modules.backtest.band_sweep import BandResult, build_band_result
from bfx_funding_bot.modules.backtest.band_sweep import render_cell_section
from bfx_funding_bot.modules.backtest.band_sweep import load_cell_ratio_sigmas
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.strategies.adaptive_period import AdaptivePeriodStrategy
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.candles.schemas import FundingCandle


def _candles(closes: list[str], symbol: str = "fUST") -> list[FundingCandle]:
    # hourly candles starting at an arbitrary epoch; only close/mts matter here
    base = 1_500_000_000_000
    return [
        FundingCandle(symbol=symbol, timeframe="1h", period_agg="a30",
                      mts=base + i * 3_600_000, open=Decimal(c), high=Decimal(c),
                      low=Decimal(c), close=Decimal(c))
        for i, c in enumerate(closes)
    ]


def test_enumerate_bands_is_8_strict_pairs() -> None:
    bands = enumerate_bands()
    assert len(bands) == 8
    # strict t1 < t2, no (1.5, 1.5)
    assert all(t1 < t2 for t1, t2 in bands)
    assert (Decimal("1.5"), Decimal("1.5")) not in bands
    # the deployed candidate is in-grid
    assert (Decimal("0.5"), Decimal("1.5")) in bands
    # exact set
    assert set(bands) == {
        (Decimal("0.5"), Decimal("1.5")), (Decimal("0.5"), Decimal("2.0")), (Decimal("0.5"), Decimal("2.5")),
        (Decimal("1.0"), Decimal("1.5")), (Decimal("1.0"), Decimal("2.0")), (Decimal("1.0"), Decimal("2.5")),
        (Decimal("1.5"), Decimal("2.0")), (Decimal("1.5"), Decimal("2.5")),
    }


def test_simulate_period_path_matches_engine_trade_count() -> None:
    # A long, mildly varying series so several trades + cooldowns fire.
    closes = [str(Decimal("0.0003") + Decimal("0.0001") * Decimal((i % 7))) for i in range(400)]
    candles = _candles(closes)
    params = dict(ema_span=24, ratio_sigma=Decimal("0.40"), t1=Decimal("0.5"),
                  t2=Decimal("1.5"), p_mid=7, p_long=14)

    periods = simulate_period_path(candles, **params)

    strat = AdaptivePeriodStrategy(**params)
    rb = run_backtest(candles, strat, BacktestConfig(fill_model="linear"))
    assert len(periods) == rb.n_trades  # the cooldown loop is mirrored exactly
    assert all(p in (2, 7, 14) for p in periods)


def test_period_profile_avg_and_p14_share() -> None:
    profile = period_profile([14, 14, 2, 2], p_long=14)
    # avg = (14+14+2+2)/4 = 8 ; time-weighted p14 share = 28 / 32
    assert profile["avg_period"] == Decimal("8")
    assert profile["p14_share"] == (Decimal("28") / Decimal("32"))


def test_period_profile_empty() -> None:
    profile = period_profile([], p_long=14)
    assert profile["avg_period"] == Decimal("0")
    assert profile["p14_share"] == Decimal("0")


# --- Task 3: active_deflated_sharpe ---


def _wo(month_mts: int, net: str) -> WindowOutcome:
    return WindowOutcome(month_mts=month_mts, net_monthly=Decimal(net), n_trades=1, fill_rate=Decimal("1"))


def test_active_dsr_strong_edge_is_high() -> None:
    # strat beats base by a steady ~0.5%/mo over 12 windows -> high DSR
    strat = [_wo(i, str(Decimal("1.0") + Decimal("0.01") * Decimal(i % 3))) for i in range(12)]
    base = [_wo(i, "0.5") for i in range(12)]
    dsr = active_deflated_sharpe(strat, base, n_trials=8)
    assert dsr is not None
    assert dsr > Decimal("0.5")


def test_active_dsr_zero_variance_is_none() -> None:
    # strat == base every window -> active series all 0 -> undefined
    strat = [_wo(i, "0.5") for i in range(12)]
    base = [_wo(i, "0.5") for i in range(12)]
    assert active_deflated_sharpe(strat, base, n_trials=8) is None


def test_active_dsr_too_few_windows_is_none() -> None:
    strat = [_wo(0, "1.0"), _wo(1, "1.0")]
    base = [_wo(0, "0.5"), _wo(1, "0.5")]
    assert active_deflated_sharpe(strat, base, n_trials=8) is None


# --- Task 4: paired_difference_ci + is_tied + pairwise_tie_matrix ---


def test_paired_diff_strict_dominance_excludes_zero() -> None:
    a = [_wo(i, "1.0") for i in range(12)]   # A always +0.5 over B
    b = [_wo(i, "0.5") for i in range(12)]
    ci = paired_difference_ci(a, b)
    assert ci[0] > Decimal("0")              # whole CI above 0
    assert not is_tied(ci)


def test_paired_diff_identical_straddles_zero() -> None:
    a = [_wo(i, "0.7") for i in range(12)]
    b = [_wo(i, "0.7") for i in range(12)]
    ci = paired_difference_ci(a, b)
    assert is_tied(ci)                        # CI brackets 0


def test_paired_diff_aligns_by_month_mts() -> None:
    # b is shuffled / partially overlapping; only shared months count
    a = [_wo(0, "1.0"), _wo(1, "1.0"), _wo(2, "1.0")]
    b = [_wo(2, "0.5"), _wo(0, "0.5"), _wo(99, "0.0")]  # months 0,2 shared
    ci = paired_difference_ci(a, b)
    assert ci[0] > Decimal("0")               # 0.5 diff on the 2 shared months


def test_pairwise_tie_matrix_flags_winners_and_ties() -> None:
    from bfx_funding_bot.modules.backtest.band_sweep import pairwise_tie_matrix
    strong = [_wo(i, "1.0") for i in range(12)]
    weak = [_wo(i, "0.5") for i in range(12)]
    tie = [_wo(i, "1.0") for i in range(12)]   # identical to strong
    labeled = [
        ((Decimal("0.5"), Decimal("1.5")), strong),
        ((Decimal("1.0"), Decimal("2.0")), weak),
        ((Decimal("1.5"), Decimal("2.0")), tie),
    ]
    rows = pairwise_tie_matrix(labeled)
    assert len(rows) == 3                       # C(3,2)
    by_pair = {(r["band_a"], r["band_b"]): r for r in rows}
    assert by_pair[("(0.5,1.5)", "(1.0,2.0)")]["tied"] is False   # strong beats weak
    assert by_pair[("(0.5,1.5)", "(1.5,2.0)")]["tied"] is True    # strong == tie


# --- Task 5: split_disjoint ---


def test_split_disjoint_partitions_by_2022() -> None:
    before = SPLIT_MTS - 86_400_000        # one day before boundary
    after = SPLIT_MTS + 86_400_000
    outcomes = [_wo(before, "1.0"), _wo(after, "2.0"), _wo(SPLIT_MTS, "3.0")]
    early, recent = split_disjoint(outcomes)
    assert [o.net_monthly for o in early] == [Decimal("1.0")]            # strictly before 2022
    assert {o.net_monthly for o in recent} == {Decimal("2.0"), Decimal("3.0")}  # >= 2022
    # non-overlapping + total preserved
    assert len(early) + len(recent) == len(outcomes)


# --- Task 6: build_band_result ---


def test_build_band_result_fields() -> None:
    strat = [_wo(SPLIT_MTS - 86_400_000, "1.2"), _wo(SPLIT_MTS + 86_400_000, "0.8")]
    base = [_wo(SPLIT_MTS - 86_400_000, "0.5"), _wo(SPLIT_MTS + 86_400_000, "0.5")]
    r = build_band_result(
        t1=Decimal("0.5"), t2=Decimal("1.5"),
        strat_outcomes=strat, base_outcomes=base,
        periods=[14, 2, 2, 7], p_long=14, n_trials=8,
    )
    assert isinstance(r, BandResult)
    assert r.t1 == Decimal("0.5") and r.t2 == Decimal("1.5")
    assert r.n_windows == 2
    # active = strat - base = [0.7, 0.3] ; median 0.5, mean 0.5
    assert r.median_active == Decimal("0.5")
    assert r.avg_period == (Decimal("25") / Decimal("4"))   # (14+2+2+7)/4
    assert r.p14_share == (Decimal("14") / Decimal("25"))
    # early half has the pre-2022 window only
    assert r.early_median_active == Decimal("0.7")
    assert r.recent_median_active == Decimal("0.3")


# --- Task 7: render_cell_section ---


def test_render_cell_section_has_columns_and_caveats() -> None:
    r = build_band_result(
        t1=Decimal("0.5"), t2=Decimal("1.5"),
        strat_outcomes=[_wo(SPLIT_MTS + 1, "0.8")], base_outcomes=[_wo(SPLIT_MTS + 1, "0.5")],
        periods=[2, 14], p_long=14, n_trials=8,
    )
    md = render_cell_section("fUST_a30", [r])
    assert "fUST_a30" in md
    assert "avg_period" in md and "p14_share" in md          # both axes surfaced (§5)
    assert "0.5" in md and "1.5" in md                       # the band row
    assert "median active" in md.lower()


# --- Task 8: load_cell_ratio_sigmas ---


def test_load_cell_ratio_sigmas_matches_p14_config() -> None:
    sigmas = load_cell_ratio_sigmas(Path("configs/cells.experimental-p14.yaml"))
    # 4 cells, sigma is strategy-independent EDA — the single source of truth (R5)
    assert set(sigmas) == {"fUST_a30", "fUST_p2", "fUSD_a30", "fUSD_p2"}
    assert sigmas["fUST_a30"] == Decimal("0.42049266874194213")
    assert sigmas["fUSD_p2"] == Decimal("0.3450137927640065")
