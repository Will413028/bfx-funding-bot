"""L4 — are the OOS conclusions fragile under the candle distortion live suffered?

Backtests read funding_candles as it stands today: the settled value. Before the
2026-07-27 immutability fix, the live daemon observed whatever the venue had
pushed at scheduler-read time, and the venue kept revising it afterwards. Every
historical result therefore assumed a price the bot could not have had.

The true point-in-time series is unrecoverable (in-place upsert, no timestamps),
so this replays the OOS evaluation with the settled closes perturbed by the
distortion distribution we did measure, many times, and reports how far the
headline numbers move.

WHAT THIS CAN AND CANNOT ANSWER
  can:    "do the conclusions survive plausible distortion" (robustness)
  cannot: "what would live actually have earned" (needs the lost series)

HOW THE DISTORTION IS APPLIED (v2, 2026-07-27)
The strategy observes the PERTURBED series; fills and P&L price off the TRUE
series. That is what live suffered: it quoted from a value the venue later
revised, while the market settled at the true rate, so the quote sat at the wrong
distance from the market and fills changed.

The first run (2026-07-26, N=500) fed one perturbed series to both sides. That
collapses spread_pct to 0, cancels the error between the decision side and the
payoff side, and makes the experiment nearly incapable of failing — its clean
result was an artifact, not resilience. Superseded by this version.

Sample and biases: ADR 2026-07-27 candle immutability, D2.

Usage (VM — the laptop's .env reaches the VM only through an SSH tunnel):
    cd backend
    uv run python scripts/run_distortion_sensitivity.py --runs 1 --output /tmp/x.md
    # --runs 1 first to time a single pass, then size the real N off that.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.distortion import perturb_candles
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.marketfeed.config import CellConfig, load_cells_only
from bfx_funding_bot.modules.marketfeed.strategy_registry import build_strategy
from bfx_funding_bot.modules.strategy import Strategy

logger = logging.getLogger("distortion_sensitivity")

START_MTS = 1451606400000  # 2016-01-01, matches run_oos_profitability
DEFAULT_CELLS_YAML = Path("configs/cells.live.yaml")

# Empirical (final - live)/live * 100, from the only surviving sample. See the
# research doc for the five biases — notably 0.174 is a FLOOR, since the sample
# only contains distortions large enough to have tripped a divergence warning.
OBSERVED_PCT_SAMPLES: list[float] = [
    -0.068, -33.924, -1.024, 13.682, -0.191,
    -22.241, 0.111, -4.547, -35.330, 2.602,
    0.508, 0.862, 5.532, 0.032, -0.673,
    0.481, 0.021, 3.671, -9.792, 1.443,
]
OBSERVED_DISTORTION_RATE = 23 / 132


@dataclass(frozen=True)
class ArmResult:
    """Headline numbers for one evaluation pass."""

    median_monthly: float
    positive_month_ratio: float
    n_windows: int


def _summarize(outcomes: list[WindowOutcome]) -> ArmResult:
    monthly = [float(o.net_monthly) for o in outcomes]
    if not monthly:
        return ArmResult(0.0, 0.0, 0)
    return ArmResult(
        median_monthly=statistics.median(monthly),
        positive_month_ratio=sum(1 for m in monthly if m > 0) / len(monthly),
        n_windows=len(monthly),
    )


def _evaluate(
    cell: CellConfig,
    observed: list[FundingCandle],
    market: list[FundingCandle] | None = None,
) -> ArmResult:
    """`observed` drives the strategy; `market` prices fills (None = same series)."""
    # Windows come from the true series so both arms share identical boundaries.
    windows = compute_wfo_windows(market if market is not None else observed)
    if not windows:
        raise SystemExit(f"No WFO windows for {cell.cell_id}; series too short?")

    def _make_strategy() -> Strategy:
        return build_strategy(cell)  # type: ignore[return-value]

    strat_outcomes, _ = evaluate_oos_windows(
        observed,
        windows,
        make_strategy=_make_strategy,
        config=BacktestConfig(fill_model="linear-baseline"),
        fill_model=None,
        market_candles=market,
    )
    return _summarize(strat_outcomes)


async def _run_cell(
    session: AsyncSession, cell: CellConfig, runs: int, distortion_rate: float
) -> dict[str, Any]:
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    candles = await get_candles_in_range(
        session, symbol=cell.symbol, timeframe=cell.timeframe,
        period_agg=cell.period_agg, start_mts=START_MTS, end_mts=end_mts,
    )
    if not candles:
        raise SystemExit(f"No candles for {cell.cell_id}")

    t0 = time.monotonic()
    baseline = _evaluate(cell, candles)
    baseline_secs = time.monotonic() - t0
    logger.info(
        "%s baseline: median=%.4f%% positive=%.1f%% windows=%d (%.1fs/pass)",
        cell.cell_id, baseline.median_monthly,
        baseline.positive_month_ratio * 100, baseline.n_windows, baseline_secs,
    )

    medians: list[float] = []
    positives: list[float] = []
    for i in range(runs):
        perturbed = perturb_candles(
            candles,
            distortion_rate=distortion_rate,
            pct_samples=OBSERVED_PCT_SAMPLES,
            seed=i,
        )
        r = _evaluate(cell, perturbed, market=candles)
        medians.append(r.median_monthly)
        positives.append(r.positive_month_ratio)
        if (i + 1) % 10 == 0 or i == 0:
            logger.info("%s run %d/%d", cell.cell_id, i + 1, runs)

    return {
        "cell": cell.cell_id,
        "baseline_secs_per_pass": round(baseline_secs, 2),
        "baseline": baseline.__dict__,
        "runs": runs,
        "distortion_rate": distortion_rate,
        "perturbed_median_monthly": _dist(medians),
        "perturbed_positive_ratio": _dist(positives),
        # The decision-relevant question: does distortion push the headline number
        # below the baseline often enough to matter?
        "share_of_runs_below_baseline_median": (
            sum(1 for m in medians if m < baseline.median_monthly) / len(medians)
            if medians else None
        ),
    }


def _dist(xs: list[float]) -> dict[str, float] | None:
    if not xs:
        return None
    s = sorted(xs)
    return {
        "min": s[0],
        "p05": s[max(0, int(0.05 * (len(s) - 1)))],
        "p50": statistics.median(s),
        "p95": s[min(len(s) - 1, int(0.95 * (len(s) - 1)))],
        "max": s[-1],
        "mean": statistics.fmean(s),
    }


def _render(payload: dict[str, Any]) -> str:
    lines = [
        "# L4 — Candle distortion sensitivity",
        "",
        f"Generated: {payload['generated_at']}",
        "",
        "Replays the OOS evaluation with settled closes perturbed by the measured",
        "distortion distribution. Answers whether the conclusions are fragile — NOT",
        "what live would have earned (that series is unrecoverable).",
        "",
        f"- distortion rate: {payload['distortion_rate']} (a floor — see research doc)",
        f"- runs per cell: {payload['runs']}",
        f"- sample: n={len(OBSERVED_PCT_SAMPLES)}, single series, 7-day window",
        "",
        "| cell | baseline median | perturbed p05 | p50 | p95 | baseline positive% | perturbed positive p05 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in cast("list[dict[str, Any]]", payload["cells"]):
        b, pm, pp = c["baseline"], c["perturbed_median_monthly"], c["perturbed_positive_ratio"]
        lines.append(
            f"| {c['cell']} | {b['median_monthly']:.4f}% | {pm['p05']:.4f}% | "
            f"{pm['p50']:.4f}% | {pm['p95']:.4f}% | {b['positive_month_ratio']*100:.1f}% | "
            f"{pp['p05']*100:.1f}% |"
        )
    lines += ["", "## Raw", "", "```json", json.dumps(payload, indent=2, default=str), "```"]
    return "\n".join(lines) + "\n"


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--runs", type=int, default=1, help="Monte Carlo passes per cell")
    p.add_argument("--distortion-rate", type=float, default=OBSERVED_DISTORTION_RATE)
    p.add_argument("--cells", default=str(DEFAULT_CELLS_YAML))
    args = p.parse_args()

    cells = load_cells_only(Path(args.cells))
    engine = make_engine(Settings())
    sf = make_session_factory(engine)
    results = []
    try:
        async with session_scope(sf) as session:
            for cell in cells:
                results.append(
                    await _run_cell(session, cell, args.runs, args.distortion_rate)
                )
    finally:
        await engine.dispose()

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "runs": args.runs,
        "distortion_rate": args.distortion_rate,
        "cells": results,
    }
    out = Path(args.output)
    out.write_text(_render(payload))
    out.with_suffix(".json").write_text(json.dumps(payload, indent=2, default=str))
    logger.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
