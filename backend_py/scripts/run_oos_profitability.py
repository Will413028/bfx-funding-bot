"""Canary OOS profitability characterization (backlog #4, reframed).

Runs the deployed canary config (MeanReversion x fUST x {a30,p2}) over rolling
1-month OOS windows vs the AlwaysFRR passive benchmark, and writes a research doc
with bootstrap CIs + a selection-bias deflated-Sharpe check.

Spec:  docs/superpowers/specs/2026-05-28-canary-oos-profitability-validation-design.md
Plan:  docs/superpowers/plans/2026-05-28-canary-oos-profitability.md

Usage:
    cd backend_py
    uv run python scripts/run_oos_profitability.py \\
        --output ../docs/research/2026-05-28-canary-oos-profitability.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.oos_profitability import (
    ActiveReturnSummary,
    OosSummary,
    WindowOutcome,
    active_return_summary,
    bootstrap_ci,
    deflated_sharpe,
    percentile,
    sharpe_skew_kurt,
    summarize_oos,
)
from bfx_funding_bot.modules.backtest.strategies.base import Strategy
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.marketfeed.config import CellConfig, load_cells_only
from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy

logger = logging.getLogger("run_oos_profitability")

START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
DEFAULT_N_TRIALS = 9  # MeanReversion grid (6) + RatePercentile (3) per cell in the Phase 3b sweep
CANARY_YAML = Path("configs/cells.canary.yaml")


@dataclass(frozen=True)
class CellReport:
    cell_label: str
    n_windows: int
    strat_summary: OosSummary
    base_summary: OosSummary
    active: ActiveReturnSummary
    median_ci: tuple[Decimal, Decimal]
    deflated_sharpe: Decimal
    n_trials: int


def build_cell_report(
    cell: CellConfig,
    strat_outcomes: list[WindowOutcome],
    base_outcomes: list[WindowOutcome],
    n_trials: int,
) -> CellReport:
    """Pure: turn per-window outcomes into a full CellReport."""
    strat_summary = summarize_oos(strat_outcomes)
    base_summary = summarize_oos(base_outcomes)
    active = active_return_summary(strat_outcomes, base_outcomes)

    monthly = [o.net_monthly for o in strat_outcomes]
    median_ci = bootstrap_ci(
        monthly, lambda vs: percentile(vs, Decimal("0.5")), seed=20260528  # seed = first-run date, fixed for reproducibility
    )

    sr, skew, kurt = sharpe_skew_kurt([m / Decimal("100") for m in monthly])
    dsr = deflated_sharpe(sr, n_trials=n_trials, n_obs=len(monthly), skew=skew, kurtosis=kurt)
    return CellReport(
        cell_label=cell.cell_id,
        n_windows=len(strat_outcomes),
        strat_summary=strat_summary,
        base_summary=base_summary,
        active=active,
        median_ci=median_ci,
        deflated_sharpe=dsr,
        n_trials=n_trials,
    )


def render_markdown(reports: list[CellReport], *, data_window: str) -> str:
    """Pure: render the research doc."""
    lines: list[str] = []
    lines.append("# Canary OOS Profitability — fUST MeanReversion (a30, p2)\n")
    lines.append(f"**Run date**: {datetime.now(UTC).isoformat()}")
    lines.append(f"**Data window**: {data_window}")
    lines.append("**Config**: `configs/cells.canary.yaml` (deployed params, fixed — not re-swept)")
    lines.append("**Fill model**: linear (deterministic) — see methodology.\n")

    lines.append("## TL;DR\n")
    for r in reports:
        s, b = r.strat_summary, r.base_summary
        lines.append(
            f"- **{r.cell_label}** ({r.n_windows} months): strat median "
            f"{s.median_monthly:.4f}%/mo (annualized {s.annualized_pct:.2f}%), "
            f"baseline {b.median_monthly:.4f}%/mo; active median "
            f"{r.active.median_active:.4f}%/mo, IR {r.active.information_ratio}; "
            f"worst-month {s.worst_monthly:.4f}%, idle {s.idle_rate:.2%}; "
            f"deflated-Sharpe {r.deflated_sharpe:.4f}."
        )
    lines.append("")

    lines.append("## OOS honesty caveat\n")
    lines.append(
        "Deployed params were chosen by a sweep over this same 2022-2026 history, so "
        "these per-month returns are **in-sample to the parameter-selection process** — "
        "an **optimistic** estimate, not pristine OOS. The deflated-Sharpe section "
        "quantifies the selection-bias haircut. The only true out-of-sample test is the "
        "live canary itself.\n"
    )

    for r in reports:
        s, b = r.strat_summary, r.base_summary
        lines.append(f"## Cell {r.cell_label}\n")
        lines.append("| Metric | Strategy | Baseline (AlwaysFRR) |")
        lines.append("|---|---|---|")
        lines.append(f"| median monthly % | {s.median_monthly:.4f} | {b.median_monthly:.4f} |")
        lines.append(f"| p25 monthly % | {s.p25_monthly:.4f} | {b.p25_monthly:.4f} |")
        lines.append(f"| worst month % | {s.worst_monthly:.4f} | {b.worst_monthly:.4f} |")
        lines.append(f"| best month % | {s.best_monthly:.4f} | {b.best_monthly:.4f} |")
        lines.append(f"| annualized % | {s.annualized_pct:.2f} | {b.annualized_pct:.2f} |")
        lines.append(f"| Sortino (monthly) | {s.sortino} | {b.sortino} |")
        lines.append(f"| idle rate | {s.idle_rate:.2%} | {b.idle_rate:.2%} |")
        lines.append(f"| mean fill rate | {s.mean_fill_rate:.4f} | {b.mean_fill_rate:.4f} |")
        lines.append("")
        lines.append(
            f"**Strategy median monthly 95% CI (bootstrap):** "
            f"[{r.median_ci[0]:.4f}%, {r.median_ci[1]:.4f}%]\n"
        )
        lines.append("### Active return vs passive\n")
        lines.append(
            f"- median active: {r.active.median_active:.4f}%/mo; "
            f"mean active: {r.active.mean_active:.4f}%/mo\n"
            f"- information ratio: {r.active.information_ratio}\n"
            f"- months strategy > baseline: {r.active.pct_months_outperform:.2%}\n"
        )
        lines.append("### Selection bias\n")
        lines.append(
            f"- configs tried (n_trials): {r.n_trials}; observations: {r.n_windows}\n"
            f"- **deflated Sharpe: {r.deflated_sharpe:.4f}** "
            f"(>0.95 = edge survives selection-bias deflation)\n"
        )

    lines.append("## Non-backtestable risk register\n")
    lines.append(
        "The catastrophic risk for a lending bot is **platform/credit/liquidity tail** "
        "(Bitfinex insolvency, socialized loss, Tether risk). No backtest rigor addresses "
        "it — it is mitigated by the **position cap ($450)** and not lending the full "
        "balance, NOT by this report. Treat these numbers as alpha characterization only.\n"
    )
    return "\n".join(lines)


def _report_to_json(reports: list[CellReport]) -> dict:  # type: ignore[type-arg]
    def summ(s: OosSummary) -> dict:  # type: ignore[type-arg]
        return {k: str(v) for k, v in s.__dict__.items()}
    return {
        "reports": [
            {
                "cell": r.cell_label,
                "n_windows": r.n_windows,
                "strategy": summ(r.strat_summary),
                "baseline": summ(r.base_summary),
                "active": {k: str(v) for k, v in r.active.__dict__.items()},
                "median_ci": [str(r.median_ci[0]), str(r.median_ci[1])],
                "deflated_sharpe": str(r.deflated_sharpe),
                "n_trials": r.n_trials,
            }
            for r in reports
        ]
    }


async def _run_cell(session: AsyncSession, cell: CellConfig, n_trials: int) -> CellReport:
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    candles = await get_candles_in_range(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg, start_mts=START_MTS, end_mts=end_mts,
    )
    if not candles:
        raise SystemExit(f"No candles for {cell.cell_id}; run scripts/backfill_candles.py")
    windows = compute_wfo_windows(candles)
    if not windows:
        raise SystemExit(f"No WFO windows for {cell.cell_id}; candle series too short?")
    def _make_strategy() -> Strategy:
        return build_strategy(cell)  # type: ignore[return-value]

    strat_outcomes, base_outcomes = evaluate_oos_windows(
        candles, windows, make_strategy=_make_strategy
    )
    logger.info("%s: %d windows", cell.cell_id, len(windows))
    return build_cell_report(cell, strat_outcomes, base_outcomes, n_trials)


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="markdown output path (.json sibling auto)")
    parser.add_argument("--n-trials", type=int, default=DEFAULT_N_TRIALS)
    args = parser.parse_args()

    # Bootstrap mirrors scripts/run_phase3b_wfo_matrix.py exactly:
    #   Settings() → make_engine(settings) → make_session_factory(engine)
    #   session_scope(session_factory) per cell; engine.dispose() in finally
    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    reports: list[CellReport] = []
    try:
        for cell in load_cells_only(CANARY_YAML):
            async with session_scope(session_factory) as session:
                reports.append(await _run_cell(session, cell, args.n_trials))
    except Exception:
        logger.exception("OOS run failed")
        return 2
    finally:
        await engine.dispose()

    data_window = f"{datetime.fromtimestamp(START_MTS / 1000, UTC):%Y-%m} .. now"
    md = render_markdown(reports, data_window=data_window)
    out = Path(args.output)
    out.write_text(md)
    out.with_suffix(".json").write_text(json.dumps(_report_to_json(reports), indent=2))
    logger.info("wrote %s and %s", out, out.with_suffix(".json"))
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
