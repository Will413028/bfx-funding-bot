"""Adaptive-period band (t1,t2) sweep driver.

Evaluates the 8 (t1,t2) bands × 4 cells over a single full-history candle
fetch, splitting outcomes into disjoint pre-2022 / 2022+ halves post-hoc.
Writes a research report. Characterization only (locked-but-not-armed).

Usage:
    cd backend
    uv run python scripts/run_adaptive_band_sweep.py \\
        --output /tmp/2026-06-04-adaptive-period-band-sweep.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.apps.config import load_cells_only
from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.band_sweep import (
    BandResult,
    build_band_result,
    enumerate_bands,
    load_cell_ratio_sigmas,
    pairwise_tie_matrix,
    render_report,
    simulate_period_path,
)
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.backtest.oos_eval import evaluate_oos_windows
from bfx_funding_bot.modules.backtest.oos_profitability import WindowOutcome
from bfx_funding_bot.modules.backtest.wfo import compute_wfo_windows
from bfx_funding_bot.modules.candles.repository import get_candles_in_range
from bfx_funding_bot.modules.strategy import AdaptivePeriodStrategy, Strategy

logger = logging.getLogger("run_adaptive_band_sweep")

P14_CONFIG = Path("configs/cells.experimental-p14.yaml")
DEFAULT_START_MTS = int(datetime(2016, 1, 1, tzinfo=UTC).timestamp() * 1000)
P_MID, P_LONG, EMA_SPAN, N_TRIALS = 7, 14, 24, 8
CELLS = [("fUST", "a30"), ("fUST", "p2"), ("fUSD", "a30"), ("fUSD", "p2")]


async def _run_cell(
    session: AsyncSession, symbol: str, period_agg: str,
    ratio_sigma: Decimal, start_mts: int,
) -> tuple[list[BandResult], list[dict[str, object]]]:
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    candles = await get_candles_in_range(
        session, symbol=symbol, timeframe="1h",
        period_agg=period_agg, start_mts=start_mts, end_mts=end_mts,
    )
    if not candles:
        raise SystemExit(f"No candles for {symbol}_{period_agg}; run backfill_candles.py")
    windows = compute_wfo_windows(candles)
    results: list[BandResult] = []
    labeled: list[tuple[tuple[Decimal, Decimal], list[WindowOutcome]]] = []
    for t1, t2 in enumerate_bands():
        params: dict[str, object] = {
            "ema_span": EMA_SPAN, "ratio_sigma": ratio_sigma,
            "t1": t1, "t2": t2, "p_mid": P_MID, "p_long": P_LONG,
        }

        def _make(p: dict[str, object] = params) -> Strategy:  # default-bind per iteration
            return AdaptivePeriodStrategy(**p)  # type: ignore[arg-type]

        strat_outcomes, base_outcomes = evaluate_oos_windows(
            candles,
            windows,
            make_strategy=_make,
            config=BacktestConfig(fill_model="linear-baseline"),
            fill_model=None,
        )
        periods = simulate_period_path(candles, **params)  # type: ignore[arg-type]
        results.append(build_band_result(
            t1=t1, t2=t2, strat_outcomes=strat_outcomes, base_outcomes=base_outcomes,
            periods=periods, p_long=P_LONG, n_trials=N_TRIALS,
        ))
        labeled.append(((t1, t2), strat_outcomes))
    logger.info("%s_%s: %d windows × 8 bands", symbol, period_agg, len(windows))
    return results, pairwise_tie_matrix(labeled)


def _to_json(
    sections: dict[str, list[BandResult]],
    matrices: dict[str, list[dict[str, object]]],
) -> dict:  # type: ignore[type-arg]
    return {
        cell: {
            "bands": [{k: str(v) for k, v in r.__dict__.items()} for r in results],
            "pairwise_tie_matrix": [
                {k: str(v) for k, v in row.items()} for row in matrices[cell]
            ],
        }
        for cell, results in sections.items()
    }


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="markdown path (.json sibling auto)")
    parser.add_argument("--start-mts", type=int, default=DEFAULT_START_MTS,
                        help="candle fetch start (default 2016-01-01)")
    args = parser.parse_args()

    sigmas = load_cell_ratio_sigmas(tuple(load_cells_only(P14_CONFIG)))
    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    sections: dict[str, list[BandResult]] = {}
    matrices: dict[str, list[dict[str, object]]] = {}
    try:
        for symbol, period_agg in CELLS:
            cell_id = f"{symbol}_{period_agg}"
            async with session_scope(session_factory) as session:
                results, matrix = await _run_cell(
                    session, symbol, period_agg, sigmas[cell_id], args.start_mts,
                )
                sections[cell_id] = results
                matrices[cell_id] = matrix
    except Exception:
        logger.exception("band sweep failed")
        return 2
    finally:
        await engine.dispose()

    data_window = f"{datetime.fromtimestamp(args.start_mts / 1000, UTC):%Y-%m} .. now (split at 2022-01)"
    out = Path(args.output)
    out.write_text(render_report(sections, data_window=data_window))
    out.with_suffix(".json").write_text(json.dumps(_to_json(sections, matrices), indent=2))
    logger.info("wrote %s and %s", out, out.with_suffix(".json"))
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
