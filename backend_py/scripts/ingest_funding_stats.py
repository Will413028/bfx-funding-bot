"""Forward-fill funding_stats to current from the latest stored row.

Brings funding_stats current after a gap (e.g. after backtest-only backfill
that stopped at 2026-05-10 and live fills have happened since). Idempotent
(upsert). Safe to run against the live DB — does NOT touch the running daemon.

Usage:
    cd backend_py
    uv run python -m scripts.ingest_funding_stats
    uv run python -m scripts.ingest_funding_stats --symbols fUST,fUSD
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

import httpx

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.funding_stats.service import (
    backfill_funding_stats_to_latest,
)

logger = logging.getLogger("ingest_funding_stats")

DEFAULT_SYMBOLS = ["fUST", "fUSD"]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--symbols",
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated list of symbols to forward-fill (default: fUST,fUSD)",
    )
    return p.parse_args()


async def _amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    try:
        async with httpx.AsyncClient() as http:
            client = BitfinexREST(
                http=http,
                base_url=settings.bitfinex_api_base_url,
                limiter=FundingRateLimiter(),
            )
            for symbol in symbols:
                logger.info("→ forward-filling funding_stats for %s", symbol)
                async with session_scope(session_factory) as session:
                    stats = await backfill_funding_stats_to_latest(
                        client=client,
                        session=session,
                        symbol=symbol,
                    )
                latest_dt = (
                    datetime.fromtimestamp(stats.earliest_mts / 1000, UTC).isoformat()
                    if stats.earliest_mts
                    else "N/A"
                )
                print(
                    f"[{symbol}] pages={stats.pages} rows={stats.rows} "
                    f"latest_mts={stats.earliest_mts} ({latest_dt})"
                )
    except Exception:
        logger.exception("ingest_funding_stats failed")
        return 2
    finally:
        await engine.dispose()

    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
