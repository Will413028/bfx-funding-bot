"""Period-structure arms: hourly grid, per-window evaluation, report assembly."""
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.period_structure import (
    ArmSpec,
    build_report,
    clamp_to_joint_coverage,
    evaluate_period_arms,
    render_markdown,
    report_to_json,
    to_hourly_grid,
)
from bfx_funding_bot.modules.backtest.strategies.always_market_rate import (
    AlwaysMarketRateStrategy,
)
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.schemas import FundingCandle

_HOUR = 3_600_000
_T0 = 1_704_067_200_000  # 2024-01-01T00:00Z
_CONFIG = BacktestConfig(fill_model="linear-baseline", truncate_at_window_end=True)


def _series(period_agg: str, close: str, n: int, *, every: int = 1, start: int = _T0) -> list[FundingCandle]:
    return [
        FundingCandle(symbol="fUST", timeframe="1h", period_agg=period_agg,
                      mts=start + i * _HOUR, close=Decimal(close))
        for i in range(n) if i % every == 0
    ]


def test_hourly_grid_fills_within_budget_and_blanks_beyond() -> None:
    sparse = _series("p30", "0.0003", n=10, every=3)  # prints at h0, h3, h6, h9
    grid = to_hourly_grid(sparse, max_gap_hours=2)
    assert [c.mts for c in grid] == [_T0 + i * _HOUR for i in range(10)]
    assert [c.close is not None for c in grid] == [True, True, True] * 3 + [True]
    grid_tight = to_hourly_grid(sparse, max_gap_hours=1)
    assert [c.close is not None for c in grid_tight] == [True, True, False] * 3 + [True]
    assert grid_tight[1].period_agg == "p30" and grid_tight[1].close == Decimal("0.0003")


def test_hourly_grid_is_identity_on_dense_input() -> None:
    dense = _series("p2", "0.0001", n=5)
    assert to_hourly_grid(dense, max_gap_hours=2) == dense
    assert to_hourly_grid([], max_gap_hours=2) == []


def test_clamp_to_joint_coverage_trims_to_overlap() -> None:
    p2 = _series("p2", "0.0001", n=10)
    p30 = _series("p30", "0.0003", n=6, start=_T0 + 2 * _HOUR)
    clamped = clamp_to_joint_coverage({"p2": p2, "p30": p30, "a30": []})
    assert set(clamped) == {"p2", "p30"}
    assert [c.mts for c in clamped["p2"]] == [_T0 + i * _HOUR for i in range(2, 8)]
    assert clamp_to_joint_coverage({"p2": []}) == {}


def _five_months() -> dict[str, list[FundingCandle]]:
    n = 24 * 31 * 5
    return {
        "p2": _series("p2", "0.0001", n),
        "p30": _series("p30", "0.0003", n),
        "a30": _series("a30", "0.0002", n),
    }


def test_evaluate_period_arms_prices_by_tenor_and_aligns_windows() -> None:
    series = _five_months()
    windows = compute_wfo_windows(series["p2"])
    arms = [
        ArmSpec("always_2d", "p2", lambda: AlwaysMarketRateStrategy(period_days=2)),
        ArmSpec("always_30d", "p30", lambda: AlwaysMarketRateStrategy(period_days=30)),
        ArmSpec("a30_posts_2d", "a30", lambda: AlwaysMarketRateStrategy(period_days=2)),
        ArmSpec("a30_legacy", "a30", lambda: AlwaysMarketRateStrategy(period_days=2), period_aware=False),
    ]
    runs = evaluate_period_arms(series, windows, arms, _CONFIG)
    assert {len(r.outcomes) for r in runs.values()} == {len(windows)}
    assert [o.month_mts for o in runs["always_2d"].outcomes] == [w.test_start_mts for w in windows]
    assert runs["always_30d"].series_used == {"p30": sum(o.n_trades for o in runs["always_30d"].outcomes)}
    assert runs["a30_legacy"].series_used == {}
    # a30 close (0.0002) posted as a 2-day offer competes with the p2 book at 0.0001 -> fills decay.
    assert all(o.fill_rate < Decimal("1") for o in runs["a30_posts_2d"].outcomes)
    assert all(o.fill_rate == Decimal("1") for o in runs["a30_legacy"].outcomes)
    # 30-day locks at 3x the 2-day rate earn more per month than 2-day rolls.
    assert all(
        a.net_monthly > b.net_monthly
        for a, b in zip(runs["always_30d"].outcomes, runs["always_2d"].outcomes, strict=True)
    )


def test_build_report_pairs_only_arms_that_ran_and_renders() -> None:
    series = _five_months()
    windows = compute_wfo_windows(series["p2"])
    arms = [
        ArmSpec("always_2d", "p2", lambda: AlwaysMarketRateStrategy(period_days=2)),
        ArmSpec("always_30d", "p30", lambda: AlwaysMarketRateStrategy(period_days=30)),
    ]
    runs = evaluate_period_arms(series, windows, arms, _CONFIG)
    report = build_report(symbol="fUST", runs=runs, data_window="x..y", notes=("always_frr skipped",))
    assert set(report.pair_summaries) == {"always_30d_vs_always_2d"}
    lo, hi = report.pair_mean_ci["always_30d_vs_always_2d"]
    assert lo <= report.pair_summaries["always_30d_vs_always_2d"].mean_active <= hi
    assert set(report.per_year_mean_monthly["always_2d"]) == {2024}
    md = render_markdown([report], fill_alpha=Decimal("5.0"), caveats=("c1",))
    assert "| always_30d |" in md and "always_30d_vs_always_2d" in md and "- c1" in md
    js = report_to_json([report], fill_alpha=Decimal("5.0"))
    assert js["symbols"][0]["arms"]["always_30d"]["n_windows"] == len(windows)  # type: ignore[index]
