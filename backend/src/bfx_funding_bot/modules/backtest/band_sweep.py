"""Pure logic for the AdaptivePeriod band (t1,t2) sweep.

Reuses the OOS engine + stats primitives; adds the two new statistics the
shared build_cell_report does not compute (active-series DSR; paired
band-vs-band difference CI). No engine/oos_eval changes. See
docs/superpowers/specs/2026-06-04-adaptive-period-band-sweep-design.md.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from bfx_funding_bot.modules.backtest.oos_profitability import (
    WindowOutcome,
    active_return_summary,
    bootstrap_ci,
    deflated_sharpe,
    paired_active_returns,
    percentile,
    sharpe_skew_kurt,
)
from bfx_funding_bot.modules.backtest.strategies.adaptive_period import AdaptivePeriodStrategy
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.config import load_cells_only

# ---------------------------------------------------------------------------
# Task 1: grid enumeration
# ---------------------------------------------------------------------------

_T1_VALUES = [Decimal("0.5"), Decimal("1.0"), Decimal("1.5")]
_T2_VALUES = [Decimal("1.5"), Decimal("2.0"), Decimal("2.5")]


def enumerate_bands() -> list[tuple[Decimal, Decimal]]:
    """The 8 (t1, t2) variants, strict t1 < t2 (drops the (1.5,1.5) cell)."""
    return [(t1, t2) for t1 in _T1_VALUES for t2 in _T2_VALUES if t1 < t2]


# ---------------------------------------------------------------------------
# Task 2: period-path simulation
# ---------------------------------------------------------------------------

_GAP_MINUTES_DEFAULT = 30  # matches BacktestConfig.gap_minutes default


def simulate_period_path(
    candles: list[FundingCandle],
    *,
    ema_span: int,
    ratio_sigma: Decimal,
    t1: Decimal,
    t2: Decimal,
    p_mid: int,
    p_long: int,
    gap_minutes: int = _GAP_MINUTES_DEFAULT,
) -> list[int]:
    """Trade-level period sequence, mirroring engine.run_backtest's
    observe -> cooldown-skip -> decide loop (engine.py:104-127) so the
    distribution matches the actual backtest trades. Full series, no record
    window (record window defaults to full span in the engine)."""
    strat = AdaptivePeriodStrategy(
        ema_span=ema_span, ratio_sigma=ratio_sigma, t1=t1, t2=t2,
        p_mid=p_mid, p_long=p_long,
    )
    ordered = sorted(candles, key=lambda c: c.mts)
    gap_candles = math.ceil(gap_minutes / 60)
    cooldown_until_idx = -1
    periods: list[int] = []
    for i, candle in enumerate(ordered):
        strat.observe(candle)
        if i <= cooldown_until_idx:
            continue
        decision = strat.decide(candle)
        if decision is None:
            continue
        periods.append(decision.period_days)
        cooldown_until_idx = i + decision.period_days * 24 + gap_candles
    return periods


def period_profile(periods: list[int], *, p_long: int) -> dict[str, Decimal]:
    """avg_period (trade-mean) + p14_share (time-weighted: fraction of total
    locked-days spent in p_long locks — the §3 tail-risk axis)."""
    if not periods:
        return {"avg_period": Decimal("0"), "p14_share": Decimal("0")}
    total_days = Decimal(sum(periods))
    long_days = Decimal(sum(p for p in periods if p == p_long))
    return {
        "avg_period": Decimal(sum(periods)) / Decimal(len(periods)),
        "p14_share": (long_days / total_days) if total_days > 0 else Decimal("0"),
    }


# ---------------------------------------------------------------------------
# Task 3: active-series deflated Sharpe
# ---------------------------------------------------------------------------


def active_deflated_sharpe(
    strat: list[WindowOutcome],
    base: list[WindowOutcome],
    *,
    n_trials: int,
) -> Decimal | None:
    """Deflated Sharpe on the per-window ACTIVE series (strat - always-2d) —
    the statistic band selection optimizes. None if < 3 windows or the active
    series has zero variance (tight band ≈ baseline; DSR undefined)."""
    paired = paired_active_returns(strat, base)
    if len(paired) < 3:
        return None
    mean = sum(paired, Decimal("0")) / Decimal(len(paired))
    if all(p == mean for p in paired):  # zero variance -> Sharpe undefined
        return None
    sr, skew, kurt = sharpe_skew_kurt([p / Decimal("100") for p in paired])
    return deflated_sharpe(sr, n_trials=n_trials, n_obs=len(paired), skew=skew, kurtosis=kurt)


# ---------------------------------------------------------------------------
# Task 4: paired band-vs-band difference CI
# ---------------------------------------------------------------------------

_PAIRED_DIFF_SEED = 20260604  # run date, fixed for reproducibility


def paired_difference_ci(
    a: list[WindowOutcome],
    b: list[WindowOutcome],
    *,
    seed: int = _PAIRED_DIFF_SEED,
) -> tuple[Decimal, Decimal]:
    """Bootstrap CI of the mean per-shared-month (A.net_monthly - B.net_monthly).
    Aligned by month_mts. Empty/degenerate -> (0, 0)."""
    b_by_mts = {o.month_mts: o.net_monthly for o in b}
    diffs = [o.net_monthly - b_by_mts[o.month_mts] for o in a if o.month_mts in b_by_mts]
    if len(diffs) < 2:
        return (Decimal("0"), Decimal("0"))
    return bootstrap_ci(diffs, lambda vs: sum(vs, Decimal("0")) / Decimal(len(vs)), seed=seed)


def is_tied(ci: tuple[Decimal, Decimal]) -> bool:
    """A pair is statistically indistinguishable iff its difference CI straddles 0."""
    return ci[0] <= Decimal("0") <= ci[1]


def pairwise_tie_matrix(
    labeled: list[tuple[tuple[Decimal, Decimal], list[WindowOutcome]]],
) -> list[dict[str, object]]:
    """For every band pair, the paired-difference CI of (A_strat − B_strat) and
    whether it straddles 0 (tied). Baseline cancels (A_active − B_active =
    A_strat − B_strat), so we difference the strat outcomes directly. Feeds the
    §7 step-3 null/tie decision over survivors."""
    rows: list[dict[str, object]] = []
    for i in range(len(labeled)):
        for j in range(i + 1, len(labeled)):
            (a_band, a_out), (b_band, b_out) = labeled[i], labeled[j]
            ci = paired_difference_ci(a_out, b_out)
            rows.append({
                "band_a": f"({a_band[0]},{a_band[1]})",
                "band_b": f"({b_band[0]},{b_band[1]})",
                "ci_lo": ci[0], "ci_hi": ci[1], "tied": is_tied(ci),
            })
    return rows


# ---------------------------------------------------------------------------
# Task 5: disjoint-window split
# ---------------------------------------------------------------------------

SPLIT_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)


def split_disjoint(
    outcomes: list[WindowOutcome],
    *,
    split_mts: int = SPLIT_MTS,
) -> tuple[list[WindowOutcome], list[WindowOutcome]]:
    """Partition outcomes into (early = test month < 2022, recent = >= 2022).
    Non-overlapping by construction — the corrected G2 robustness gate."""
    early = [o for o in outcomes if o.month_mts < split_mts]
    recent = [o for o in outcomes if o.month_mts >= split_mts]
    return early, recent


# ---------------------------------------------------------------------------
# Task 6: per-cell band report assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BandResult:
    t1: Decimal
    t2: Decimal
    n_windows: int
    median_active: Decimal
    mean_active: Decimal
    mean_over_median: Decimal      # fat-tail proxy; 0 if median == 0
    best_month_active: Decimal
    win_rate: Decimal              # pct_months_outperform
    avg_period: Decimal
    p14_share: Decimal
    active_dsr: Decimal | None     # None = undefined (near-baseline / <3 windows)
    early_median_active: Decimal   # disjoint pre-2022 half
    recent_median_active: Decimal  # disjoint 2022+ half


def _median_active(strat: list[WindowOutcome], base: list[WindowOutcome]) -> Decimal:
    # strat/base halves stay aligned because the full lists are contract-aligned
    # and split_disjoint applies an identical predicate; callers must pass
    # contract-aligned lists, else paired_active_returns raises.
    if not strat or not base:
        return Decimal("0")
    paired = paired_active_returns(strat, base)
    return percentile(paired, Decimal("0.5")) if paired else Decimal("0")


def build_band_result(
    *,
    t1: Decimal,
    t2: Decimal,
    strat_outcomes: list[WindowOutcome],
    base_outcomes: list[WindowOutcome],
    periods: list[int],
    p_long: int,
    n_trials: int,
) -> BandResult:
    active = active_return_summary(strat_outcomes, base_outcomes)
    paired = paired_active_returns(strat_outcomes, base_outcomes)
    profile = period_profile(periods, p_long=p_long)
    mean_over_median = (
        active.mean_active / active.median_active if active.median_active != 0 else Decimal("0")
    )
    # disjoint halves
    s_early, s_recent = split_disjoint(strat_outcomes)
    b_early, b_recent = split_disjoint(base_outcomes)
    return BandResult(
        t1=t1,
        t2=t2,
        n_windows=len(strat_outcomes),
        median_active=active.median_active,
        mean_active=active.mean_active,
        mean_over_median=mean_over_median,
        best_month_active=max(paired) if paired else Decimal("0"),
        win_rate=active.pct_months_outperform,
        avg_period=profile["avg_period"],
        p14_share=profile["p14_share"],
        active_dsr=active_deflated_sharpe(strat_outcomes, base_outcomes, n_trials=n_trials),
        early_median_active=_median_active(s_early, b_early),
        recent_median_active=_median_active(s_recent, b_recent),
    )


# ---------------------------------------------------------------------------
# Task 7: render markdown report
# ---------------------------------------------------------------------------


def _fmt(d: Decimal | None, places: str = "0.0001") -> str:
    return "n/a" if d is None else str(d.quantize(Decimal(places)))


def render_cell_section(cell_label: str, results: list[BandResult]) -> str:
    """One per-cell markdown section: band comparison table + disjoint ranks."""
    lines: list[str] = [f"## Cell {cell_label}\n"]
    lines.append(
        "| (t1,t2) | median active | mean active | mean÷median | best-month | "
        "win-rate | avg_period | p14_share | active-DSR |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in results:
        lines.append(
            f"| ({r.t1},{r.t2}) | {_fmt(r.median_active)} | {_fmt(r.mean_active)} | "
            f"{_fmt(r.mean_over_median, '0.01')} | {_fmt(r.best_month_active)} | "
            f"{_fmt(r.win_rate, '0.01')} | {_fmt(r.avg_period, '0.01')} | "
            f"{_fmt(r.p14_share, '0.001')} | {_fmt(r.active_dsr, '0.0001')} |"
        )
    lines.append("")
    # disjoint-window rank table (G2): rank by median active in each half
    lines.append("### Disjoint-window rank (median active; lower rank = better)\n")
    early_rank = _rank_labels(results, key=lambda r: r.early_median_active)
    recent_rank = _rank_labels(results, key=lambda r: r.recent_median_active)
    lines.append("| (t1,t2) | rank pre-2022 | rank 2022+ |")
    lines.append("|---|---|---|")
    for r in results:
        label = f"({r.t1},{r.t2})"
        lines.append(f"| {label} | {early_rank[label]} | {recent_rank[label]} |")
    lines.append("")
    return "\n".join(lines)


def _rank_labels(results: list[BandResult], *, key: Callable[[BandResult], Decimal]) -> dict[str, int]:
    ordered = sorted(results, key=key, reverse=True)  # higher active = rank 1
    return {f"({r.t1},{r.t2})": i + 1 for i, r in enumerate(ordered)}


def render_report(sections: dict[str, list[BandResult]], *, data_window: str) -> str:
    """Full report: header + caveats + per-cell sections."""
    lines: list[str] = ["# Adaptive-Period Band (t1,t2) Sweep\n"]
    lines.append(f"**Data window**: {data_window}")
    lines.append("**Fill model**: linear, mean fill = 1.0 (the 100%-fill optimism caveat).")
    lines.append(
        "**Status**: characterization only — locked-but-not-armed, same as p_long=14. "
        "Not in cells.live.yaml; zero live impact.\n"
    )
    lines.append("## Standing caveats\n")
    lines.append(
        "- 100%-fill optimism (mean fill = 1.0); EDA ratio_sigma from 2022-2026; "
        "tiers chosen-not-gated. The chosen band is a relative, backtest-internal pick, "
        "NOT a claim it reproduces live or resolves live≈0.\n"
        "- Robustness gate = disjoint pre-2022 vs 2022+ ranks (not nested). "
        "DSR is on the ACTIVE series (n_trials=8); the bot-vs-idle level DSR is non-gating.\n"
        "- Null result is valid: if survivors' paired difference CIs straddle 0, band is "
        "not a meaningful lever — keep the highest-t2 (least p14-tail) band.\n"
    )
    for cell_label, results in sections.items():
        lines.append(render_cell_section(cell_label, results))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Task 8: load_cell_ratio_sigmas (pure helper; driver script is separate)
# ---------------------------------------------------------------------------


def load_cell_ratio_sigmas(p14_yaml: Path) -> dict[str, Decimal]:
    """Per-cell ratio_sigma from the p14 experimental config — the single
    source of truth (strategy-independent EDA). Keyed by cell_id."""
    return {
        cell.cell_id: Decimal(str(cell.params["ratio_sigma"]))
        for cell in load_cells_only(p14_yaml)
    }
