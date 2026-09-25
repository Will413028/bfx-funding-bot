"""MR-with-FRR-floor backtest — SKIP-FRR-parking contradiction probe (research-gated).

Answers the 2026-07-06 profit review §3 open question: WFO attributed +15-19%
edge to MR's skip behavior, while live G3 diagnosed MR-timing alpha ~= 0
(INSUFFICIENT_DATA). This runs four arms per symbol over the same weekly-OOS
(calendar WFO, train 3mo / test 1mo / step 1mo) methodology as
run_oos_profitability.py:

  - mr:                 deployed MeanReversion params (skip -> idle, earns 0)
  - mr_frr_floor:       same params, skip -> park at FRR (period 2d)
  - always_market_rate: passive close-rate baseline
  - always_frr:         park at FRR every candle (the free auto-renew alternative)

FRR per-day rate = funding_stats.frr x 365 (FRR_ANNUALIZATION SSOT in
live_attribution.py; unit re-verified over full 2016-2026 history 2026-07-19 —
see the in-report FRR unit audit, which hard-gates the run).

Usage (one-shot container on the VM; DB read-only):
    cd backend
    uv run python -m scripts.run_frr_floor_backtest \\
        --output ~/bfx/reports/2026-07-19-frr-floor-backtest.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from statistics import median

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.frr_series import FrrSeries
from bfx_funding_bot.modules.backtest.oos_profitability import (
    ActiveReturnSummary,
    OosSummary,
    WindowOutcome,
    active_return_summary,
    bootstrap_ci,
    paired_active_returns,
    summarize_oos,
)
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFrrStrategy
from bfx_funding_bot.modules.backtest.strategies.always_market_rate import (
    AlwaysMarketRateStrategy,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.strategies.mean_reversion import (
    MeanReversionStrategy,
)
from bfx_funding_bot.modules.backtest.strategies.mean_reversion_frr_floor import (
    MeanReversionFrrFloorStrategy,
)
from bfx_funding_bot.modules.backtest.wfo import WfoWindow, compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.funding_stats.repository import get_in_range
from bfx_funding_bot.modules.live_validation.live_attribution import (
    FRR_ANNUALIZATION,
    assert_market_rate_band,
)
from bfx_funding_bot.modules.marketfeed.config import CellConfig, load_cells_only

logger = logging.getLogger("run_frr_floor_backtest")

ARM_MR = "mr"
ARM_FLOOR = "mr_frr_floor"
ARM_AMR = "always_market_rate"
ARM_FRR = "always_frr"
ARMS = (ARM_MR, ARM_FLOOR, ARM_AMR, ARM_FRR)

PAIRS: tuple[tuple[str, str, str], ...] = (
    ("mr_frr_floor_vs_mr", ARM_FLOOR, ARM_MR),
    ("mr_frr_floor_vs_always_frr", ARM_FLOOR, ARM_FRR),
    ("mr_frr_floor_vs_always_market_rate", ARM_FLOOR, ARM_AMR),
    ("mr_vs_always_market_rate", ARM_MR, ARM_AMR),
    ("always_frr_vs_always_market_rate", ARM_FRR, ARM_AMR),
)

DEFAULT_SYMBOLS = ["fUST", "fUSD"]
DEFAULT_CELLS_YAML = Path("configs/cells.live.yaml")
BOOTSTRAP_SEED = 20260719  # first-run date, fixed for reproducibility

# Order-of-magnitude gate for the per-year median close/(frr*365) ratio. The
# 2016-2026 audit observed [0.33, 0.97]; a wrong unit is off by ~365x either way.
_RATIO_GATE_LO = Decimal("0.1")
_RATIO_GATE_HI = Decimal("10")


# ---------------------------------------------------------------- unit audit


@dataclass(frozen=True)
class YearAudit:
    year: int
    n: int
    med_frr_raw: Decimal  # stored funding_stats.frr scale (~1e-6)
    med_close: Decimal
    med_ratio: Decimal  # median close / (frr*365)


@dataclass(frozen=True)
class FrrUnitAudit:
    per_year: list[YearAudit]
    ok: bool


def audit_frr_unit(candles: list[FundingCandle], series: FrrSeries) -> FrrUnitAudit:
    """Empirical unit check: per-year median close/(frr*365) must be O(1).

    frr*365 and candle close are the same quantity family (per-day funding
    rate); the ratio drifts with market structure (FRR premium) but a unit
    error is off by ~365x. Gate: every year's median in [0.1, 10].
    """
    by_year: dict[int, list[tuple[Decimal, Decimal]]] = {}
    for c in candles:
        if c.close is None or c.close <= 0:
            continue
        frr = series.at(c.mts)
        if frr is None or frr <= 0:
            continue
        year = datetime.fromtimestamp(c.mts / 1000, UTC).year
        by_year.setdefault(year, []).append((c.close, frr))

    per_year: list[YearAudit] = []
    ok = bool(by_year)
    for year in sorted(by_year):
        rows = by_year[year]
        med_close = median(close for close, _ in rows)
        med_frr_raw = median(frr / FRR_ANNUALIZATION for _, frr in rows)
        med_ratio = median(close / frr for close, frr in rows)
        per_year.append(
            YearAudit(
                year=year, n=len(rows), med_frr_raw=med_frr_raw,
                med_close=med_close, med_ratio=med_ratio,
            )
        )
        if not (_RATIO_GATE_LO <= med_ratio <= _RATIO_GATE_HI):
            ok = False
    return FrrUnitAudit(per_year=per_year, ok=ok)


# ------------------------------------------------------------------- arms


def deployed_mr_params(
    cells: list[CellConfig], *, symbol: str, period_agg: str
) -> dict[str, object]:
    """Extract deployed MeanReversion params for (symbol, period_agg) from cells."""
    for cell in cells:
        if (
            cell.strategy.value == "mean_reversion"
            and cell.symbol == symbol
            and cell.period_agg == period_agg
        ):
            p = cell.params
            return {
                "ema_span": int(p["ema_span"]),
                "threshold_sigma": Decimal(str(p["threshold_sigma"])),
                "ratio_sigma": Decimal(str(p["ratio_sigma"])),
            }
    raise SystemExit(f"no mean_reversion cell for {symbol}_{period_agg} in cells yaml")


def arm_factories(
    *,
    ema_span: int,
    threshold_sigma: Decimal,
    ratio_sigma: Decimal,
    frr_at: Callable[[int], Decimal | None],
) -> dict[str, Callable[[], Strategy]]:
    """Fresh-per-window strategy factories for the four arms."""
    return {
        ARM_MR: lambda: MeanReversionStrategy(
            ema_span=ema_span, threshold_sigma=threshold_sigma, ratio_sigma=ratio_sigma
        ),
        ARM_FLOOR: lambda: MeanReversionFrrFloorStrategy(
            ema_span=ema_span, threshold_sigma=threshold_sigma,
            ratio_sigma=ratio_sigma, frr_at=frr_at,
        ),
        ARM_AMR: lambda: AlwaysMarketRateStrategy(period_days=2),
        ARM_FRR: lambda: AlwaysFrrStrategy(frr_at=frr_at, period_days=2),
    }


def evaluate_arms(
    candles: list[FundingCandle],
    windows: list[WfoWindow],
    factories: dict[str, Callable[[], Strategy]],
    config: BacktestConfig,
) -> dict[str, list[WindowOutcome]]:
    """evaluate_oos_windows generalized to N arms — same slicing/recording rules."""
    outcomes: dict[str, list[WindowOutcome]] = {name: [] for name in factories}
    for w in windows:
        sliced = [c for c in candles if w.train_start_mts <= c.mts <= w.test_end_mts]
        for name, make in factories.items():
            r = run_backtest(
                sliced, make(), config, w.test_start_mts, w.test_end_mts
            )
            outcomes[name].append(
                WindowOutcome(
                    month_mts=w.test_start_mts,
                    net_monthly=r.net_monthly_return_pct,
                    n_trades=r.n_trades,
                    fill_rate=r.fill_rate,
                )
            )
    return outcomes


# ----------------------------------------------------------------- report


@dataclass(frozen=True)
class SymbolReport:
    symbol: str
    n_windows: int
    data_window: str
    arm_summaries: dict[str, OosSummary]
    pair_summaries: dict[str, ActiveReturnSummary]
    pair_mean_ci: dict[str, tuple[Decimal, Decimal]]
    per_year_mean_monthly: dict[str, dict[int, Decimal]]
    unit_audit: FrrUnitAudit


def build_symbol_report(
    *,
    symbol: str,
    outcomes: dict[str, list[WindowOutcome]],
    unit_audit: FrrUnitAudit,
    data_window: str,
) -> SymbolReport:
    """Pure: per-arm summaries + paired diffs (+bootstrap mean CI) + per-year means."""
    arm_summaries = {name: summarize_oos(outcomes[name]) for name in ARMS}

    pair_summaries: dict[str, ActiveReturnSummary] = {}
    pair_mean_ci: dict[str, tuple[Decimal, Decimal]] = {}
    for label, a, b in PAIRS:
        pair_summaries[label] = active_return_summary(outcomes[a], outcomes[b])
        diffs = paired_active_returns(outcomes[a], outcomes[b])
        pair_mean_ci[label] = bootstrap_ci(
            diffs,
            lambda vs: sum(vs, Decimal("0")) / Decimal(len(vs)),
            seed=BOOTSTRAP_SEED,
        )

    per_year_mean_monthly: dict[str, dict[int, Decimal]] = {}
    for name in ARMS:
        by_year: dict[int, list[Decimal]] = {}
        for o in outcomes[name]:
            year = datetime.fromtimestamp(o.month_mts / 1000, UTC).year
            by_year.setdefault(year, []).append(o.net_monthly)
        per_year_mean_monthly[name] = {
            year: sum(vals, Decimal("0")) / Decimal(len(vals))
            for year, vals in sorted(by_year.items())
        }

    return SymbolReport(
        symbol=symbol,
        n_windows=len(outcomes[ARM_MR]),
        data_window=data_window,
        arm_summaries=arm_summaries,
        pair_summaries=pair_summaries,
        pair_mean_ci=pair_mean_ci,
        per_year_mean_monthly=per_year_mean_monthly,
        unit_audit=unit_audit,
    )


def render_markdown(reports: list[SymbolReport], *, data_window: str) -> str:
    lines: list[str] = []
    lines.append("# MR-with-FRR-floor backtest — SKIP FRR parking probe\n")
    lines.append(f"**Run date**: {datetime.now(UTC).isoformat()}")
    lines.append(f"**Data window**: {data_window}")
    lines.append(
        "**Methodology**: calendar WFO (train 3mo warmup / test 1mo / step 1mo), "
        "linear fill model, deployed canary MR params (fixed, not re-swept). "
        "Same window methodology as run_oos_profitability.py."
    )
    lines.append(
        "**Question** (profit review 2026-07-06 §3): does replacing MR's SKIP-idle "
        "with FRR parking dominate — i.e. reconcile 'WFO says skip is +15-19% edge' "
        "with 'live G3 says MR alpha ~= 0'?\n"
    )

    lines.append("## FRR unit audit (gate: passed for every symbol below)\n")
    lines.append(
        "- `funding_stats.frr` is stored as (per-day FRR)/365 (~1e-6 scale; the "
        "2026-05-10 sample 1.12e-06 is this normal scale, not a corrupt value). "
        "Per-day rate = `frr x 365` (FRR_ANNUALIZATION SSOT, live-verified "
        "2026-07-06 vs ticker FRR, <0.5% error)."
    )
    lines.append(
        "- Gate: per-year median close/(frr*365) must lie in [0.1, 10] — a unit "
        "error would be ~365x off. Observed drift 0.93 -> 0.33 across 2016-2026 "
        "is market structure (FRR premium vs p2 close grew), not units.\n"
    )
    for r in reports:
        lines.append(f"### {r.symbol}\n")
        lines.append("| year | n | median frr (stored) | median close | median close/(frr*365) |")
        lines.append("|---|---|---|---|---|")
        for y in r.unit_audit.per_year:
            lines.append(
                f"| {y.year} | {y.n} | {y.med_frr_raw:.3E} | {y.med_close:.3E} "
                f"| {y.med_ratio:.4f} |"
            )
        lines.append("")

    for r in reports:
        lines.append(f"## {r.symbol} — four arms over {r.n_windows} monthly OOS windows\n")
        lines.append(f"Data window: {r.data_window}\n")
        lines.append("| metric | " + " | ".join(ARMS) + " |")
        lines.append("|---|" + "---|" * len(ARMS))

        def row(label: str, fmt: Callable[[OosSummary], str], rep: SymbolReport = r) -> str:
            return f"| {label} | " + " | ".join(
                fmt(rep.arm_summaries[a]) for a in ARMS
            ) + " |"

        lines.append(row("annualized net %", lambda s: f"{s.annualized_pct:.2f}"))
        lines.append(row("median monthly %", lambda s: f"{s.median_monthly:.4f}"))
        lines.append(row("mean monthly %", lambda s: f"{s.mean_monthly:.4f}"))
        lines.append(row("p25 monthly %", lambda s: f"{s.p25_monthly:.4f}"))
        lines.append(row("worst month %", lambda s: f"{s.worst_monthly:.4f}"))
        lines.append(row("best month %", lambda s: f"{s.best_monthly:.4f}"))
        lines.append(row("idle rate", lambda s: f"{s.idle_rate:.2%}"))
        lines.append(row("mean fill rate", lambda s: f"{s.mean_fill_rate:.4f}"))
        lines.append(row("sortino", lambda s: f"{s.sortino}"))
        lines.append("")

        lines.append("### Paired monthly differences (a - b, % per month)\n")
        lines.append(
            "| pair | median | mean [95% CI] | IR | months a>b (win rate) |"
        )
        lines.append("|---|---|---|---|---|")
        for label, _a, _b in PAIRS:
            p = r.pair_summaries[label]
            lo, hi = r.pair_mean_ci[label]
            lines.append(
                f"| {label} | {p.median_active:.4f} | {p.mean_active:.4f} "
                f"[{lo:.4f}, {hi:.4f}] | {p.information_ratio} "
                f"| {p.pct_months_outperform:.2%} |"
            )
        lines.append("")

        lines.append("### Per-year mean net monthly % (regime drift check)\n")
        years = sorted({y for arm in r.per_year_mean_monthly.values() for y in arm})
        lines.append("| year | " + " | ".join(ARMS) + " |")
        lines.append("|---|" + "---|" * len(ARMS))
        for year in years:
            cells = []
            for a in ARMS:
                v = r.per_year_mean_monthly[a].get(year)
                cells.append(f"{v:.4f}" if v is not None else "—")
            lines.append(f"| {year} | " + " | ".join(cells) + " |")
        lines.append("")

    lines.append("## Honesty caveats\n")
    lines.append(
        "- **Fill model is the linear proxy** (alpha=5): offers priced above the "
        "candle close get linearly discounted fill, hitting 0 fill at +20% above "
        "close. FRR frequently sits far above the p2 close (median close/(frr*365) "
        "~0.33-0.6 since 2022), so the FRR arms' offers are often modeled as 0-fill "
        "months — the real FRR auto-renew queue would eventually fill at FRR. This "
        "systematically *understates* always_frr and mr_frr_floor. There is no "
        "validated fill model for FRR-pegged offers; treat the FRR arms under "
        "alpha=5 as a lower bound, and the --fill-alpha 1e-9 sensitivity run "
        "('FRR offers always fill at FRR') as an upper bound."
    )
    lines.append(
        "- **Immediate-execution assumption**: a decision fills (probabilistically) "
        "at the decision candle, then capital locks for period 2d + 30min gap. No "
        "queue latency, no partial-period returns."
    )
    lines.append(
        "- **15% Bitfinex fee applied uniformly** to all arms (net figures); real "
        "FRR auto-renew pays the same fee, so pairwise comparisons are fee-neutral."
    )
    lines.append(
        "- **Deployed MR params were selected on this same history** (Phase 3b "
        "sweep) — MR/floor arms are in-sample to parameter selection; the passive "
        "arms are not. Optimistic for MR-family arms."
    )
    lines.append(
        "- **funding_stats.frr is hourly, LOCF as-of lookup** (staleness cap 24h; "
        "max observed gap 9.2h). FRR before the funding_stats series start is "
        "unavailable -> those candles are idle for FRR arms (start is clamped to "
        "joint coverage, so this only affects edges)."
    )
    lines.append(
        "- **Full-equity compounding**: each trade deploys the whole budget; no "
        "per-cell allocation, no order-book depth, no chunking. Same simplification "
        "for all arms."
    )
    lines.append(
        "- **p2 cells only** (deployed canary uses a30+p2; a30's FRR-anchored "
        "period semantics are not modeled here)."
    )
    return "\n".join(lines)


def _report_to_json(reports: list[SymbolReport]) -> dict:  # type: ignore[type-arg]
    def summ(s: OosSummary) -> dict:  # type: ignore[type-arg]
        return {k: str(v) for k, v in s.__dict__.items()}

    return {
        "reports": [
            {
                "symbol": r.symbol,
                "n_windows": r.n_windows,
                "data_window": r.data_window,
                "arms": {a: summ(r.arm_summaries[a]) for a in ARMS},
                "pairs": {
                    label: {k: str(v) for k, v in r.pair_summaries[label].__dict__.items()}
                    for label, _a, _b in PAIRS
                },
                "pair_mean_ci": {
                    label: [str(lo), str(hi)]
                    for label, (lo, hi) in r.pair_mean_ci.items()
                },
                "per_year_mean_monthly": {
                    a: {str(y): str(v) for y, v in ys.items()}
                    for a, ys in r.per_year_mean_monthly.items()
                },
                "unit_audit": {
                    "ok": r.unit_audit.ok,
                    "per_year": [
                        {k: str(v) for k, v in y.__dict__.items()}
                        for y in r.unit_audit.per_year
                    ],
                },
            }
            for r in reports
        ]
    }


# ------------------------------------------------------------------- main


async def _run_symbol(
    session: AsyncSession,
    *,
    symbol: str,
    period_agg: str,
    cells: list[CellConfig],
    fill_model: str,
    fill_alpha: Decimal,
) -> SymbolReport:
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    candles = await get_candles_in_range(
        session, symbol=symbol, timeframe="1h", period_agg=period_agg,
        start_mts=0, end_mts=end_mts,
    )
    if not candles:
        raise SystemExit(f"no candles for {symbol} 1h {period_agg}")
    stats = await get_in_range(session, symbol=symbol, start_mts=0, end_mts=end_mts)
    series = FrrSeries.from_stats(stats)
    if len(series) == 0:
        raise SystemExit(f"no funding_stats frr rows for {symbol}")

    # Clamp to joint coverage so all four arms see the same months.
    frr_start = min(s.mts for s in stats if s.frr is not None)
    candles = [c for c in candles if c.mts >= frr_start]

    audit = audit_frr_unit(candles, series)
    if not audit.ok:
        raise SystemExit(
            f"FRR unit audit FAILED for {symbol}: per-year median close/(frr*365) "
            f"outside [0.1, 10] — do not trust the FRR arms. "
            f"{[(y.year, str(y.med_ratio)) for y in audit.per_year]}"
        )
    # Double insurance (same guard as the live G3 loader): frr*365 must sit in
    # the plausible per-day band on average.
    frr_rates = [s.frr * FRR_ANNUALIZATION for s in stats if s.frr is not None]
    assert_market_rate_band(frr_rates)

    windows = compute_wfo_windows(candles)
    if not windows:
        raise SystemExit(f"no WFO windows for {symbol}")

    params = deployed_mr_params(cells, symbol=symbol, period_agg=period_agg)
    factories = arm_factories(
        ema_span=params["ema_span"],  # type: ignore[arg-type]
        threshold_sigma=params["threshold_sigma"],  # type: ignore[arg-type]
        ratio_sigma=params["ratio_sigma"],  # type: ignore[arg-type]
        frr_at=series.at,
    )
    config = BacktestConfig(fill_model=fill_model, fill_alpha=fill_alpha)  # type: ignore[arg-type]
    outcomes = evaluate_arms(candles, windows, factories, config)
    logger.info("%s: %d windows, %d candles", symbol, len(windows), len(candles))

    first = datetime.fromtimestamp(candles[0].mts / 1000, UTC)
    last = datetime.fromtimestamp(candles[-1].mts / 1000, UTC)
    return build_symbol_report(
        symbol=symbol,
        outcomes=outcomes,
        unit_audit=audit,
        data_window=f"{first:%Y-%m-%d} .. {last:%Y-%m-%d}",
    )


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="markdown output path (.json sibling auto)")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--period-agg", default="p2")
    parser.add_argument(
        "--cells", default=str(DEFAULT_CELLS_YAML),
        help="cells yaml with deployed MR params (default: configs/cells.live.yaml)",
    )
    parser.add_argument(
        "--fill-model", default="linear-baseline", choices=["linear-baseline"],
        help="linear-baseline (default) matches run_oos_profitability determinism",
    )
    parser.add_argument(
        "--fill-alpha", default="5.0",
        help=(
            "linear fill decay slope (default 5.0). Only FRR-parking decisions "
            "ever price above close, so a tiny value (e.g. 1e-9) is the "
            "'FRR offers always fill' sensitivity — MR/AMR arms are unaffected."
        ),
    )
    args = parser.parse_args()

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    cells = load_cells_only(Path(args.cells))

    reports: list[SymbolReport] = []
    try:
        for symbol in [s.strip() for s in args.symbols.split(",") if s.strip()]:
            async with session_scope(session_factory) as session:
                reports.append(
                    await _run_symbol(
                        session, symbol=symbol, period_agg=args.period_agg,
                        cells=cells, fill_model=args.fill_model,
                        fill_alpha=Decimal(args.fill_alpha),
                    )
                )
    except Exception:
        logger.exception("FRR-floor backtest failed")
        return 2
    finally:
        await engine.dispose()

    data_window = " / ".join(f"{r.symbol}: {r.data_window}" for r in reports)
    md = render_markdown(reports, data_window=data_window)
    out = Path(args.output).expanduser()
    out.write_text(md)
    out.with_suffix(".json").write_text(json.dumps(_report_to_json(reports), indent=2))
    logger.info("wrote %s and %s", out, out.with_suffix(".json"))
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
