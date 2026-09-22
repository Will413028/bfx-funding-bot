"""Period-structure backtest (Proposal D, 2026-09-22 review §6) — pure, no DB.

The lever no deployed strategy uses is the lock tenor: every live strategy posts
2-day offers, while FRR (what auto-renew earns) has traded at 1.7-3x the p2 close
since 2022. This module scores arms that differ *only* in tenor, pricing each
fill against the market series of the tenor actually posted:

- `always_2d`    AlwaysMarketRate on p2, 2-day locks.
- `always_30d`   AlwaysMarketRate on p30 (LOCF onto the hourly grid), 30-day locks.
- `adaptive_period`  deployed AdaptivePeriod params on a30, locks 2/7/14 priced
  p2 / a30 / a30 (a30 is the stated approximation for 7 and 14).
- `mr_a30`       deployed MeanReversion on a30 posting 2-day offers, priced vs p2 —
  the tenor-mismatch fix.
- `mr_a30_legacy`  same strategy priced vs a30 (old engine behaviour) — diagnostic
  that measures how much the mismatch inflated a30 cells.
- `mr_p2`        deployed MeanReversion on p2 (unchanged by tenor pricing).
- `always_frr`   FRR arm when funding_stats are available (DB runs only).

Windows are the calendar WFO used everywhere else; `truncate_at_window_end` is
on so a long lock opened late in a test month is credited only up to month end.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import BacktestIncomplete, run_backtest
from bfx_funding_bot.modules.backtest.oos_profitability import (
    ActiveReturnSummary,
    OosSummary,
    WindowOutcome,
    active_return_summary,
    bootstrap_ci,
    paired_active_returns,
    summarize_oos,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.wfo import WfoWindow
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import reindex_and_ffill
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel

_MS_PER_HOUR = 3_600_000

ARM_ALWAYS_2D = "always_2d"
ARM_ALWAYS_30D = "always_30d"
ARM_ADAPTIVE = "adaptive_period"
ARM_MR_A30 = "mr_a30"
ARM_MR_A30_LEGACY = "mr_a30_legacy"
ARM_MR_P2 = "mr_p2"
ARM_ALWAYS_FRR = "always_frr"

# (label, a, b): paired per-window diff a - b. Only pairs whose arms both ran are reported.
DEFAULT_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("always_30d_vs_always_2d", ARM_ALWAYS_30D, ARM_ALWAYS_2D),
    ("adaptive_period_vs_always_2d", ARM_ADAPTIVE, ARM_ALWAYS_2D),
    ("mr_a30_vs_always_2d", ARM_MR_A30, ARM_ALWAYS_2D),
    ("mr_a30_legacy_vs_mr_a30", ARM_MR_A30_LEGACY, ARM_MR_A30),
    ("mr_p2_vs_always_2d", ARM_MR_P2, ARM_ALWAYS_2D),
    ("always_frr_vs_always_2d", ARM_ALWAYS_FRR, ARM_ALWAYS_2D),
    ("always_30d_vs_always_frr", ARM_ALWAYS_30D, ARM_ALWAYS_FRR),
)


def to_hourly_grid(
    candles: Sequence[FundingCandle], *, max_gap_hours: int
) -> list[FundingCandle]:
    """LOCF a sparse series onto the hourly grid, keeping wall-clock cooldowns honest.

    The engine counts cooldown in candle indices and assumes 1h spacing; a sparse
    p30 series fed raw would stretch a 30-day lock over far more than 30 days.
    Slots within `max_gap_hours` of the last print carry that close at the slot's
    mts; slots beyond the budget become `close=None` candles (no decision, index
    still advances).
    """
    if not candles:
        return []
    ordered = sorted(candles, key=lambda c: c.mts)
    start, ref = ordered[0].mts, ordered[-1].mts
    filled = reindex_and_ffill(ordered, ref_mts=ref, max_gap_hours=max_gap_hours)
    template = ordered[0]
    out: list[FundingCandle] = []
    for i, slot in enumerate(filled):
        slot_mts = start + i * _MS_PER_HOUR
        if slot.candle is None:
            out.append(
                FundingCandle(
                    symbol=template.symbol, timeframe=template.timeframe,
                    period_agg=template.period_agg, mts=slot_mts, close=None,
                )
            )
        elif slot.candle.mts == slot_mts:
            out.append(slot.candle)
        else:
            out.append(slot.candle.model_copy(update={"mts": slot_mts}))
    return out


def clamp_to_joint_coverage(
    series: Mapping[str, list[FundingCandle]],
) -> dict[str, list[FundingCandle]]:
    """Trim every series to [max(first mts), min(last mts)] so all arms see the same months."""
    non_empty = {k: v for k, v in series.items() if v}
    if not non_empty:
        return {}
    lo = max(min(c.mts for c in v) for v in non_empty.values())
    hi = min(max(c.mts for c in v) for v in non_empty.values())
    return {k: [c for c in v if lo <= c.mts <= hi] for k, v in non_empty.items()}


@dataclass(frozen=True)
class ArmSpec:
    name: str
    observe_key: str  # which series drives observe()/decide()
    make_strategy: Callable[[], Strategy]
    period_aware: bool = True  # False = legacy single-series pricing (diagnostic)


@dataclass(frozen=True)
class ArmRun:
    outcomes: list[WindowOutcome]
    series_used: dict[str, int] = field(default_factory=dict)
    # Set when the fill evidence could not score this arm (e.g. no book model for the
    # series its tiers are priced on). Such arms are reported, never silently scored.
    incomplete_reason: str | None = None


def _outcome(month_mts: int, result_net: Decimal, n_trades: int, fill_rate: Decimal) -> WindowOutcome:
    return WindowOutcome(
        month_mts=month_mts, net_monthly=result_net, n_trades=n_trades, fill_rate=fill_rate
    )


def evaluate_period_arms(
    series: Mapping[str, list[FundingCandle]],
    windows: Sequence[WfoWindow],
    arms: Sequence[ArmSpec],
    config: BacktestConfig,
    *,
    fill_models_by_agg: Mapping[str, FillRateModel] | None = None,
) -> dict[str, ArmRun]:
    """Fresh strategy per window per arm; period-aware arms price fills by tenor.

    With `fill_models_by_agg` (empirical mode) each trade is scored by the model of
    the series that priced it; an arm whose trades hit a series without a model is
    returned with `incomplete_reason` and no outcomes rather than scored linearly.
    """
    runs: dict[str, ArmRun] = {arm.name: ArmRun(outcomes=[]) for arm in arms}
    for w in windows:
        sliced = {
            key: [c for c in cs if w.train_start_mts <= c.mts <= w.test_end_mts]
            for key, cs in series.items()
        }
        for arm in arms:
            if runs[arm.name].incomplete_reason is not None:
                continue
            observed = sliced[arm.observe_key]
            try:
                if arm.period_aware:
                    r = run_backtest(
                        observed, arm.make_strategy(), config,
                        w.test_start_mts, w.test_end_mts,
                        market_series_by_agg=sliced, fill_models_by_agg=fill_models_by_agg,
                    )
                else:
                    r = run_backtest(
                        observed, arm.make_strategy(), config,
                        w.test_start_mts, w.test_end_mts,
                        fill_model=(
                            fill_models_by_agg.get(arm.observe_key)
                            if fill_models_by_agg is not None else None
                        ),
                    )
            except BacktestIncomplete as error:
                runs[arm.name] = ArmRun(outcomes=[], incomplete_reason=error.reason)
                continue
            run = runs[arm.name]
            run.outcomes.append(
                _outcome(w.test_start_mts, r.net_monthly_return_pct, r.n_trades, r.fill_rate)
            )
            for key, n in (r.pricing_series_used or {}).items():
                run.series_used[key] = run.series_used.get(key, 0) + n
    return runs


@dataclass(frozen=True)
class PeriodStructureReport:
    symbol: str
    n_windows: int
    data_window: str
    arm_summaries: dict[str, OosSummary]
    series_used: dict[str, dict[str, int]]
    pair_summaries: dict[str, ActiveReturnSummary]
    pair_mean_ci: dict[str, tuple[Decimal, Decimal]]
    per_year_mean_monthly: dict[str, dict[int, Decimal]]
    notes: tuple[str, ...]
    # arm -> [(month_mts, net_monthly)] chronological; feeds the weekly drift rule.
    windows: dict[str, list[tuple[int, Decimal]]] = field(default_factory=dict)


def build_report(
    *,
    symbol: str,
    runs: Mapping[str, ArmRun],
    data_window: str,
    pairs: Sequence[tuple[str, str, str]] = DEFAULT_PAIRS,
    notes: Sequence[str] = (),
    bootstrap_seed: int = 20260922,
) -> PeriodStructureReport:
    incomplete = {name: run.incomplete_reason for name, run in runs.items()
                  if run.incomplete_reason is not None}
    runs = {name: run for name, run in runs.items() if run.incomplete_reason is None}
    notes = [*notes, *(f"{name} not scored: {reason}" for name, reason in incomplete.items())]
    arm_summaries = {name: summarize_oos(run.outcomes) for name, run in runs.items()}
    pair_summaries: dict[str, ActiveReturnSummary] = {}
    pair_mean_ci: dict[str, tuple[Decimal, Decimal]] = {}
    for label, a, b in pairs:
        if a not in runs or b not in runs:
            continue
        pair_summaries[label] = active_return_summary(runs[a].outcomes, runs[b].outcomes)
        diffs = paired_active_returns(runs[a].outcomes, runs[b].outcomes)
        pair_mean_ci[label] = bootstrap_ci(
            diffs, lambda vs: sum(vs, Decimal("0")) / Decimal(len(vs)), seed=bootstrap_seed
        )
    per_year: dict[str, dict[int, Decimal]] = {}
    for name, run in runs.items():
        by_year: dict[int, list[Decimal]] = {}
        for o in run.outcomes:
            by_year.setdefault(datetime.fromtimestamp(o.month_mts / 1000, UTC).year, []).append(o.net_monthly)
        per_year[name] = {
            y: sum(v, Decimal("0")) / Decimal(len(v)) for y, v in sorted(by_year.items())
        }
    n_windows = len(next(iter(runs.values())).outcomes) if runs else 0
    return PeriodStructureReport(
        symbol=symbol, n_windows=n_windows, data_window=data_window,
        arm_summaries=arm_summaries,
        series_used={name: dict(run.series_used) for name, run in runs.items()},
        pair_summaries=pair_summaries, pair_mean_ci=pair_mean_ci,
        per_year_mean_monthly=per_year, notes=tuple(notes),
        windows={name: [(o.month_mts, o.net_monthly) for o in run.outcomes]
                 for name, run in runs.items()},
    )


def _f(x: Decimal, nd: int = 4) -> str:
    return "inf" if not x.is_finite() else f"{x:.{nd}f}"


def render_markdown(
    reports: Sequence[PeriodStructureReport], *, fill_alpha: Decimal, caveats: Sequence[str]
) -> str:
    lines = [
        "# Period-structure four-arm backtest (Proposal D)",
        "",
        f"**Run date**: {datetime.now(UTC).isoformat()}",
        f"**Fill model**: linear-baseline, fill_alpha={fill_alpha} (above-market quotes decay linearly; "
        "run again with a tiny alpha for the always-fill bound).",
        "**Windows**: calendar WFO (train 3mo warmup / test 1mo / step 1mo), fresh state per window, "
        "locks truncated at window end.",
        "",
        "## Standing caveats",
        "",
    ]
    lines += [f"- {c}" for c in caveats]
    for r in reports:
        lines += ["", f"## {r.symbol} — {r.n_windows} monthly OOS windows ({r.data_window})", ""]
        lines += [f"- {n}" for n in r.notes]
        lines += [
            "",
            "| arm | annualized net % | median monthly % | p25 | worst | best | mean | idle rate | mean fill | priced by |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for name, s in r.arm_summaries.items():
            used = ", ".join(f"{k}:{v}" for k, v in sorted(r.series_used.get(name, {}).items())) or "observed"
            lines.append(
                f"| {name} | {_f(s.annualized_pct, 2)} | {_f(s.median_monthly)} | {_f(s.p25_monthly)} | "
                f"{_f(s.worst_monthly)} | {_f(s.best_monthly)} | {_f(s.mean_monthly)} | "
                f"{_f(s.idle_rate, 2)} | {_f(s.mean_fill_rate)} | {used} |"
            )
        lines += [
            "",
            "### Paired monthly differences (a − b, % per month)",
            "",
            "| pair | median | mean [95% CI] | IR | months a>b |",
            "|---|---|---|---|---|",
        ]
        for label, ps in r.pair_summaries.items():
            lo, hi = r.pair_mean_ci[label]
            lines.append(
                f"| {label} | {_f(ps.median_active)} | {_f(ps.mean_active)} [{_f(lo)}, {_f(hi)}] | "
                f"{_f(ps.information_ratio, 2)} | {_f(ps.pct_months_outperform * 100, 1)}% |"
            )
        years = sorted({y for d in r.per_year_mean_monthly.values() for y in d})
        arms = list(r.per_year_mean_monthly)
        lines += ["", "### Per-year mean net monthly %", "", "| year | " + " | ".join(arms) + " |",
                  "|---|" + "---|" * len(arms)]
        for y in years:
            lines.append(
                f"| {y} | " + " | ".join(
                    _f(r.per_year_mean_monthly[a][y]) if y in r.per_year_mean_monthly[a] else "-"
                    for a in arms
                ) + " |"
            )
    return "\n".join(lines) + "\n"


def report_to_json(reports: Sequence[PeriodStructureReport], *, fill_alpha: Decimal) -> dict[str, object]:
    def _summary(s: OosSummary) -> dict[str, str | int]:
        return {
            "n_windows": s.n_windows, "median_monthly": str(s.median_monthly),
            "p25_monthly": str(s.p25_monthly), "worst_monthly": str(s.worst_monthly),
            "best_monthly": str(s.best_monthly), "mean_monthly": str(s.mean_monthly),
            "annualized_pct": str(s.annualized_pct), "idle_rate": str(s.idle_rate),
            "mean_fill_rate": str(s.mean_fill_rate), "sortino": str(s.sortino),
        }

    return {
        "fill_alpha": str(fill_alpha),
        "symbols": [
            {
                "symbol": r.symbol, "n_windows": r.n_windows, "data_window": r.data_window,
                "notes": list(r.notes),
                "arms": {n: _summary(s) for n, s in r.arm_summaries.items()},
                "series_used": r.series_used,
                "pairs": {
                    label: {
                        "median_active": str(ps.median_active), "mean_active": str(ps.mean_active),
                        "information_ratio": str(ps.information_ratio),
                        "pct_months_outperform": str(ps.pct_months_outperform),
                        "mean_ci": [str(r.pair_mean_ci[label][0]), str(r.pair_mean_ci[label][1])],
                    }
                    for label, ps in r.pair_summaries.items()
                },
                "per_year_mean_monthly": {
                    a: {str(y): str(v) for y, v in d.items()} for a, d in r.per_year_mean_monthly.items()
                },
                "windows": {
                    a: [[m, str(v)] for m, v in ws] for a, ws in r.windows.items()
                },
            }
            for r in reports
        ],
    }
