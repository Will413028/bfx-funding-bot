from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.rest import BitfinexREST
from bfx_funding_bot.modules.candles.repository import (
    get_candles_in_range,
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
