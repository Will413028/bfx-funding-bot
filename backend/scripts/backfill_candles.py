"""Day-3 Checkpoint 1 driver: fetch → upsert → read-back → exact-match check.

Usage:
    cd backend
    uv run python scripts/backfill_candles.py \\
        --symbol fUST --timeframe 1h --period-agg p2 \\
        --hours-back 24

Exit codes:
    0 — checkpoint pass: round-trip matches exactly
    1 — checkpoint fail: drift detected, see stderr for which candle
    2 — checkpoint fail: Bitfinex returned no candles
    3 — operational error (network, DB connection, etc.)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from decimal import Decimal

import httpx

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.service import backfill_candles

logger = logging.getLogger("backfill_candles")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default="fUST", help="e.g. fUST, fUSD, fBTC")
    p.add_argument("--timeframe", default="1h", help="1m / 1h / 1D / etc.")
    p.add_argument("--period-agg", default="p2", help="aggregation key, e.g. p2, a30")
    p.add_argument(
        "--hours-back", type=int, default=24, help="hours of history to backfill"
    )
    return p.parse_args()


def _candles_match_exactly(
    fetched: list[FundingCandle], stored: list[FundingCandle]
) -> tuple[bool, str]:
    if len(fetched) != len(stored):
        return False, f"count mismatch: fetched={len(fetched)} stored={len(stored)}"

    by_mts_fetched = {c.mts: c for c in fetched}
    by_mts_stored = {c.mts: c for c in stored}

    if set(by_mts_fetched.keys()) != set(by_mts_stored.keys()):
        missing = set(by_mts_fetched.keys()) - set(by_mts_stored.keys())
        extra = set(by_mts_stored.keys()) - set(by_mts_fetched.keys())
        return False, f"mts set mismatch: missing={missing} extra={extra}"

    for mts, f in by_mts_fetched.items():
        s = by_mts_stored[mts]
        for field in ("open", "close", "high", "low", "volume"):
            fv = getattr(f, field)
            sv = getattr(s, field)
            if fv is None and sv is None:
                continue
            if fv is None or sv is None:
                return False, f"mts={mts} field={field}: nullness mismatch f={fv} s={sv}"
            if abs(fv - sv) > Decimal("1e-15"):
                return False, f"mts={mts} field={field}: f={fv} != s={sv}"
    return True, "match"


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
    start_ms = end_ms - args.hours_back * 60 * 60 * 1000

    try:
        async with httpx.AsyncClient() as http:
            client = BitfinexREST(
                http=http,
                base_url=settings.bitfinex_api_base_url,
                limiter=FundingRateLimiter(),
            )
            logger.info(
                "fetching %s %s %s [%s, %s]",
                args.symbol, args.timeframe, args.period_agg, start_ms, end_ms,
            )
            async with session_scope(session_factory) as session:
                fetched, stored = await backfill_candles(
                    bitfinex=client,
                    session=session,
                    symbol=args.symbol,
                    timeframe=args.timeframe,
                    period_agg=args.period_agg,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    limit=10000,
                )
        logger.info("fetched %d, stored %d", len(fetched), len(stored))

        if not fetched:
            logger.error("Bitfinex returned 0 candles — cannot validate checkpoint")
            return 2

        ok, reason = _candles_match_exactly(fetched, stored)
        if ok:
            logger.info(
                "✅ Checkpoint 1 PASS: %d candles round-tripped exactly", len(fetched)
            )
            sample = fetched[0]
            logger.info(
                "Spot-check sample: mts=%s (%s) open=%s close=%s high=%s low=%s volume=%s",
                sample.mts,
                sample.timestamp().isoformat(),
                sample.open, sample.close, sample.high, sample.low, sample.volume,
            )
            logger.info(
                "Manually verify against https://www.bitfinex.com/funding/%s "
                "(mts %s = %s)",
                args.symbol[1:] if args.symbol.startswith("f") else args.symbol,
                sample.mts,
                sample.timestamp().isoformat(),
            )
            return 0
        else:
            logger.error("❌ Checkpoint 1 FAIL: %s", reason)
            return 1

    except Exception:
        logger.exception("Operational error during backfill")
        return 3
    finally:
        await engine.dispose()


def main() -> None:
    rc = asyncio.run(_amain())
    sys.exit(rc)


if __name__ == "__main__":
    main()
