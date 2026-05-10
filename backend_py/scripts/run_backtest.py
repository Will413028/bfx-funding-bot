"""Day-4/5 Checkpoint 2 driver: load candles from Neon → run baseline strategy
→ print monthly return + drawdown.

Usage:
    cd backend_py
    uv run python scripts/run_backtest.py \\
        --symbol fUST --timeframe 1h --period-agg p2 \\
        --days-back 30 \\
        --period-days 2

Exit codes:
    0 — checkpoint pass: backtest produced a number
    1 — checkpoint fail: no candles in DB for the requested range
    2 — operational error
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.modules.backtest.engine import run_backtest
from bfx_funding_bot.modules.backtest.strategies.always_frr import AlwaysFRRStrategy
from bfx_funding_bot.modules.candles.repository import get_candles_in_range

logger = logging.getLogger("run_backtest")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="fUST")
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--period-agg", default="p2")
    p.add_argument("--days-back", type=int, default=30)
    p.add_argument("--period-days", type=int, default=2)
    return p.parse_args()


async def _amain() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days_back * 24 * 60 * 60 * 1000

    try:
        async with session_scope(session_factory) as session:
            candles = await get_candles_in_range(
                session,
                symbol=args.symbol,
                timeframe=args.timeframe,
                period_agg=args.period_agg,
                start_mts=start_ms,
                end_mts=end_ms,
            )
        logger.info("loaded %d candles from Neon", len(candles))
        if not candles:
            logger.error(
                "No candles in DB for %s %s %s [%s, %s]. "
                "Run scripts/backfill_candles.py first.",
                args.symbol, args.timeframe, args.period_agg, start_ms, end_ms,
            )
            return 1

        strategy = AlwaysFRRStrategy(period_days=args.period_days)
        result = run_backtest(candles, strategy)

        logger.info("=" * 60)
        logger.info("✅ Checkpoint 2 backtest result")
        logger.info("  Strategy:           %s", result.strategy_name)
        logger.info("  Symbol:             %s", result.symbol)
        logger.info("  Candles processed:  %d", result.n_candles)
        logger.info("  Trades simulated:   %d", result.n_trades)
        logger.info("  Monthly return:     %s%%", result.monthly_return_pct)
        logger.info("  Max drawdown:       %s%%", result.max_drawdown_pct)
        logger.info("=" * 60)
        return 0

    except Exception:
        logger.exception("Operational error during backtest")
        return 2
    finally:
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
