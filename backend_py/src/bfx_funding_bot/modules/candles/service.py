import time

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.backfill.errors import BackfillCursorStuck
from bfx_funding_bot.modules.backfill.schemas import BackfillStats, SeriesSpec
from bfx_funding_bot.modules.candles.repository import (
    get_candles_in_range,
    get_min_mts,
    upsert_candles,
)
from bfx_funding_bot.modules.candles.schemas import FundingCandle


async def backfill_candles(
    *,
    bitfinex: BitfinexREST,
    session: AsyncSession,
    symbol: str,
    timeframe: str,
    period_agg: str,
    start_ms: int,
    end_ms: int,
    limit: int = 125,
) -> tuple[list[FundingCandle], list[FundingCandle]]:
    """Fetch candles from Bitfinex, upsert to DB, read back what was stored.

    Returns (fetched, stored). Caller can compare them for round-trip integrity.
    Does NOT commit the session; caller manages the transaction (so a CLI can
    wrap multiple backfills in one tx if desired).
    """
    fetched = await bitfinex.get_funding_candles(
        symbol=symbol,
        timeframe=timeframe,
        period_agg=period_agg,
        start=start_ms,
        end=end_ms,
        limit=limit,
    )
    await upsert_candles(session, fetched)
    stored = await get_candles_in_range(
        session,
        symbol=symbol,
        timeframe=timeframe,
        period_agg=period_agg,
        start_mts=start_ms,
        end_mts=end_ms,
    )
    return fetched, stored


async def backfill_candles_to_earliest(
    *,
    client: BitfinexREST,
    session: AsyncSession,
    symbol: str,
    timeframe: str,
    period_agg: str,
    page_limit: int = 10000,
) -> BackfillStats:
    """Walking-back backfill until Bitfinex returns no more older candles.

    Resume-aware: if DB already has rows for this series, starts from
    (min_mts - 1) instead of now_ms. Caller manages session commit/rollback.
    Each batch is flushed (not committed) so subsequent get_min_mts sees it.
    """
    db_min = await get_min_mts(
        session, symbol=symbol, timeframe=timeframe, period_agg=period_agg,
    )
    end_ms = (db_min - 1) if db_min is not None else int(time.time() * 1000)

    pages = 0
    rows = 0
    while True:
        page = await client.get_funding_candles(
            symbol=symbol,
            timeframe=timeframe,
            period_agg=period_agg,
            start=0,
            end=end_ms,
            limit=page_limit,
        )
        if not page:
            break

        await upsert_candles(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        oldest_mts = min(c.mts for c in page)
        if oldest_mts >= end_ms:
            raise BackfillCursorStuck(symbol, end_ms, oldest_mts)
        end_ms = oldest_mts - 1

        if len(page) < page_limit:
            break

    final_min = await get_min_mts(
        session, symbol=symbol, timeframe=timeframe, period_agg=period_agg,
    )
    return BackfillStats(
        spec=SeriesSpec(
            kind="candles", symbol=symbol,
            timeframe=timeframe, period_agg=period_agg,
        ),
        pages=pages,
        rows=rows,
        earliest_mts=final_min if final_min is not None else end_ms,
    )
