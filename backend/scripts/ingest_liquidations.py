"""Ingest Bitfinex liquidation history into liquidations (research-only).

Source: /v2/liquidations/hist — all symbols mixed, events since ~2019-09.

WARNING — this endpoint's rate limit is brutal (empirically: a 2nd immediate
request already 429s and the cool-down lasts minutes). Defaults keep one
request per ~25s and sleep 4 min after any 429, so a full historical walk
takes hours. Run under nohup and tail the log:

    cd backend
    nohup uv run python -m scripts.ingest_liquidations > /tmp/ingest_liq.log 2>&1 &

Resume-aware + idempotent: tops up the gap between DB max and now, then walks
back from DB min until the API returns empty. Safe to interrupt and re-run;
commits every --pages-per-commit pages. Does NOT touch the live trading path.

Exit codes: 0 ok + PASS checks · 1 PASS check failed · 3 operational error.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import make_engine, make_session_factory, session_scope
from bfx_funding_bot.core.settings import Settings
from bfx_funding_bot.external.bitfinex.rate_limit import FundingRateLimiter
from bfx_funding_bot.modules.external_signals.client import ExternalSignalsClient
from bfx_funding_bot.modules.external_signals.service import (
    IngestStats,
    backfill_liquidations_to_earliest,
    topup_liquidations_to_latest,
)
from bfx_funding_bot.modules.external_signals.tables import LiquidationRow

logger = logging.getLogger("ingest_liquidations")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--page-limit", type=int, default=500)
    p.add_argument("--pages-per-commit", type=int, default=5)
    p.add_argument("--request-interval", type=float, default=25.0,
                   help="Seconds between requests (endpoint is heavily limited)")
    p.add_argument("--ratelimit-sleep", type=float, default=240.0,
                   help="Sleep after a 429 (cool-down lasts minutes)")
    return p.parse_args()


def _fmt_mts(mts: int | None) -> str:
    if mts is None:
        return "N/A"
    return datetime.fromtimestamp(mts / 1000, UTC).strftime("%Y-%m-%d %H:%M")


async def _run_chunked(
    label: str,
    session_factory: async_sessionmaker[AsyncSession],
    run_chunk: Callable[[AsyncSession], Awaitable[IngestStats]],
) -> tuple[int, int]:
    total_pages = 0
    total_rows = 0
    while True:
        async with session_scope(session_factory) as session:
            stats = await run_chunk(session)
        total_pages += stats.pages
        total_rows += stats.rows
        logger.info(
            "[%s] +%d pages (+%d rows) | total %d pages %d rows | range %s → %s%s",
            label, stats.pages, stats.rows, total_pages, total_rows,
            _fmt_mts(stats.earliest_mts), _fmt_mts(stats.latest_mts),
            " | DONE" if stats.done else "",
        )
        if stats.done:
            return total_pages, total_rows


async def _pass_check(session: AsyncSession) -> tuple[bool, list[str]]:
    lines: list[str] = []
    n = (
        await session.execute(select(func.count()).select_from(LiquidationRow))
    ).scalar_one()
    if n == 0:
        return False, ["liquidations row_count == 0"]
    lo, hi = (
        await session.execute(
            select(func.min(LiquidationRow.mts), func.max(LiquidationRow.mts))
        )
    ).one()
    n_long = (
        await session.execute(
            select(func.count()).select_from(LiquidationRow).where(
                LiquidationRow.amount > 0
            )
        )
    ).scalar_one()
    n_short = (
        await session.execute(
            select(func.count()).select_from(LiquidationRow).where(
                LiquidationRow.amount < 0
            )
        )
    ).scalar_one()
    lines.append(f"rows={n} range {_fmt_mts(lo)} → {_fmt_mts(hi)}")
    lines.append(f"long-liqs (amount>0): {n_long}, short-liqs (amount<0): {n_short}")
    both_sides = n_long > 0 and n_short > 0
    if not both_sides:
        lines.append("expected both long and short liquidation events")
    return both_sides, lines


async def _amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = _parse_args()

    settings = Settings()
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    try:
        async with httpx.AsyncClient() as http:
            client = ExternalSignalsClient(
                http=http,
                liq_limiter=FundingRateLimiter(1, args.request_interval),
                liq_rate_limit_sleep=args.ratelimit_sleep,
            )
            logger.info("→ [liquidations] top-up to latest")
            await _run_chunked(
                "liquidations:topup", session_factory,
                lambda s: topup_liquidations_to_latest(
                    client=client, session=s,
                    page_limit=args.page_limit, max_pages=args.pages_per_commit,
                ),
            )
            logger.info("→ [liquidations] walk-back to earliest")
            await _run_chunked(
                "liquidations:walkback", session_factory,
                lambda s: backfill_liquidations_to_earliest(
                    client=client, session=s,
                    page_limit=args.page_limit, max_pages=args.pages_per_commit,
                ),
            )

        print("\n=== PASS checks ===")
        async with session_scope(session_factory) as session:
            ok, lines = await _pass_check(session)
        for line in lines:
            print(f"[{'✓' if ok else '✗'}] {line}")
        return 0 if ok else 1
    except Exception:
        logger.exception("ingest_liquidations failed")
        return 3
    finally:
        await engine.dispose()


def main() -> None:
    sys.exit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
