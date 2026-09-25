"""Reproducible MeanReversion param derivation pipeline (Tier 2) — thin CLI.

--write  (manual, needs Neon): pull candles -> cell_pipeline.write_outputs
         (freeze fixtures + patch params/_provenance into the YAML files).
--check  (CI, offline): cell_pipeline.check_against_fixture — re-derive from the
         committed fixtures and assert the committed YAML matches. Non-zero on drift.

Usage:
    cd backend
    uv run python scripts/derive_cells.py --write   # operator, before deploy
    uv run python scripts/derive_cells.py --check    # CI / pre-commit

Exit codes: 0 = OK; 1 = --check found drift; 2 = --write found no candles for a cell.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

from bfx_funding_bot.modules.backtest.cell_derivation import DerivedCell, derive_cell_params
from bfx_funding_bot.modules.backtest.cell_pipeline import (
    CELLS_YAML,
    DEPLOYED_YAML,
    FIXTURES,
    MR_CELLS,
    CellKey,
    check_against_fixture,
    write_outputs,
)
from bfx_funding_bot.modules.backtest.config import BacktestConfig
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.lending.tracking.model import FillRateModel

logger = logging.getLogger("derive_cells")

START_MTS = int(datetime(2022, 1, 1, tzinfo=UTC).timestamp() * 1000)
RESEARCH_CONFIG = BacktestConfig(fill_model="linear-baseline")
UNUSED_LINEAR_MODEL = FillRateModel.from_rows([], artifact=None)


async def _write_main() -> int:
    from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
    from bfx_funding_bot.core.settings import Settings
    from bfx_funding_bot.modules.candles.repository import get_candles_in_range

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    end_mts = int(datetime.now(UTC).timestamp() * 1000)
    series: dict[CellKey, list[FundingCandle]] = {}
    derived: dict[CellKey, DerivedCell] = {}
    try:
        for symbol, period_agg, _timeframe in MR_CELLS:
            async with session_scope(session_factory) as session:
                candles = await get_candles_in_range(
                    session,
                    symbol=symbol,
                    timeframe="1h",
                    period_agg=period_agg,
                    start_mts=START_MTS,
                    end_mts=end_mts,
                )
            if not candles:
                logger.error("no candles for %s_%s; run backfill", symbol, period_agg)
                return 2
            key: CellKey = ("mean_reversion", symbol, period_agg)
            series[key] = candles
            d = derive_cell_params(
                candles, config=RESEARCH_CONFIG, fill_model=UNUSED_LINEAR_MODEL,
            )
            derived[key] = d
            logger.info(
                "derived %s_%s: ema_span=%d thr=%s ratio=%s mean_active=%s IR=%s",
                symbol,
                period_agg,
                d.ema_span,
                d.threshold_sigma,
                d.ratio_sigma,
                d.mean_active,
                d.information_ratio,
            )
    finally:
        await engine.dispose()

    write_outputs(
        series, derived, FIXTURES, [CELLS_YAML, DEPLOYED_YAML], deployed_path=DEPLOYED_YAML
    )
    logger.info("wrote fixtures + patched %s, %s", CELLS_YAML, DEPLOYED_YAML)
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument(
        "--write", action="store_true", help="pull Neon, freeze, derive, patch YAML"
    )
    g.add_argument(
        "--check",
        action="store_true",
        help="offline: committed YAML == re-derive(fixture)",
    )
    args = parser.parse_args()

    if args.write:
        sys.exit(asyncio.run(_write_main()))
    problems = check_against_fixture(
        FIXTURES,
        [CELLS_YAML, DEPLOYED_YAML],
        deployed_path=DEPLOYED_YAML,
        config=RESEARCH_CONFIG,
        fill_model=UNUSED_LINEAR_MODEL,
    )
    if problems:
        for p in problems:
            logger.error("DRIFT: %s", p)
        sys.exit(1)
    logger.info("derive_cells --check: OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
