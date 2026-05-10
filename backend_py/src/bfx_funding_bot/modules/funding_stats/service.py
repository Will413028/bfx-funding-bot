import time

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.backfill.errors import BackfillCursorStuck
from bfx_funding_bot.modules.backfill.schemas import BackfillStats, SeriesSpec
from bfx_funding_bot.modules.funding_stats.repository import (
    get_min_mts,
    upsert_funding_stats,
)


async def backfill_funding_stats_to_earliest(
    *,
    client: BitfinexREST,
    session: AsyncSession,
    symbol: str,
    page_limit: int = 10000,
) -> BackfillStats:
    """Walking-back backfill of funding_stats until Bitfinex returns empty.

    Resume-aware: if DB already has rows for this symbol, starts from
    (min_mts - 1) instead of now_ms. Caller manages session commit/rollback.
    """
    db_min = await get_min_mts(session, symbol=symbol)
    end_ms = (db_min - 1) if db_min is not None else int(time.time() * 1000)

    pages = 0
    rows = 0
    while True:
        page = await client.get_funding_stats(
            symbol=symbol, end=end_ms, limit=page_limit,
        )
        if not page:
            break

        await upsert_funding_stats(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        oldest_mts = min(s.mts for s in page)
        if oldest_mts >= end_ms:
            raise BackfillCursorStuck(symbol, end_ms, oldest_mts)
        end_ms = oldest_mts - 1

        if len(page) < page_limit:
            break

    final_min = await get_min_mts(session, symbol=symbol)
    return BackfillStats(
        spec=SeriesSpec(kind="funding_stats", symbol=symbol),
        pages=pages,
        rows=rows,
        earliest_mts=final_min if final_min is not None else end_ms,
    )
