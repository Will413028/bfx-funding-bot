import time

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.funding_stats.repository import (
    get_max_mts,
    get_min_mts,
    upsert_funding_stats,
)
from bfx_funding_bot.modules.market import BackfillCursorStuck, BackfillStats, SeriesSpec


async def backfill_funding_stats_to_earliest(
    *,
    client: BitfinexREST,
    session: AsyncSession,
    symbol: str,
    page_limit: int = 250,
) -> BackfillStats:
    """Walking-back backfill of funding_stats until Bitfinex returns empty.

    Resume-aware: if DB already has rows for this symbol, starts from
    (min_mts - 1) instead of now_ms. Caller manages session commit/rollback.

    Note: Bitfinex caps /v2/funding/stats/{Symbol}/hist at limit=250 (500+
    returns HTTP 500), so page_limit defaults to 250 here.
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


async def backfill_funding_stats_to_latest(
    *,
    client: BitfinexREST,
    session: AsyncSession,
    symbol: str,
    page_limit: int = 250,
    now_ms: int | None = None,
) -> BackfillStats:
    """Forward fill: page back from now until reaching the latest stored row.

    Brings funding_stats current after a gap. Idempotent (upsert). Pages
    backward from `now_ms` (defaults to wall clock) and STOPS once a page's
    oldest mts has reached/passed the stored max (db_max), so it only fetches
    the [db_max, now] gap. Caller manages session commit/rollback.

    Note: returns a BackfillStats whose `earliest_mts` field carries the
    *latest* (max) stored mts after the run — the field name reflects the
    backward-fill variant; for forward fill it acts as the newest watermark.
    """
    db_max = await get_max_mts(session, symbol=symbol)
    end_ms = now_ms if now_ms is not None else int(time.time() * 1000)

    pages = 0
    rows = 0
    while True:
        page = await client.get_funding_stats(symbol=symbol, end=end_ms, limit=page_limit)
        if not page:
            break
        await upsert_funding_stats(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        oldest_mts = min(s.mts for s in page)
        if db_max is not None and oldest_mts <= db_max:
            break  # reached the data we already have — gap filled
        if oldest_mts >= end_ms:
            raise BackfillCursorStuck(symbol, end_ms, oldest_mts)
        end_ms = oldest_mts - 1
        if len(page) < page_limit:
            break

    final_max = await get_max_mts(session, symbol=symbol)
    return BackfillStats(
        spec=SeriesSpec(kind="funding_stats", symbol=symbol),
        pages=pages,
        rows=rows,
        earliest_mts=final_max if final_max is not None else end_ms,
    )
