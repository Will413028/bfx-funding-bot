import time
from dataclasses import dataclass

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


@dataclass(frozen=True)
class FilledCandle:
    """Wrapper for a candle slot after LOCF reindexing.

    candle is None when the slot is beyond max_gap_hours from any source candle
    (hard tier — caller must skip signal emit and emit DEGRADED instead).
    """
    candle: FundingCandle | None
    is_stale: bool
    stale_seconds: int


def reindex_and_ffill(
    candles: list[FundingCandle],
    ref_mts: int,
    freq_ms: int = 3_600_000,
    max_gap_hours: int = 2,
) -> list[FilledCandle]:
    """Reindex sparse candle list to a full hourly grid ending at ref_mts.

    Forward-fills missing slots up to max_gap_hours of staleness. Beyond budget,
    slots remain (candle=None, is_stale=True, stale_seconds=(slot - source)/1000).

    Invariants:
    - len(output) == n hourly slots from candles[0].mts to ref_mts (or [] if candles empty)
    - output[-1] aligns to ref_mts (last slot within freq_ms tolerance)
    - For 100% dense input on freq_ms grid: 1-to-1 wrap, no ffill applied
    - Deterministic: same input → same output across N invocations
    """
    if not candles:
        return []
    sorted_candles = sorted(candles, key=lambda c: c.mts)
    if sorted_candles[-1].mts > ref_mts:
        sorted_candles = [c for c in sorted_candles if c.mts <= ref_mts]
        if not sorted_candles:
            return []

    start_mts = sorted_candles[0].mts
    n_slots = ((ref_mts - start_mts) // freq_ms) + 1
    max_gap_ms = max_gap_hours * 3_600_000

    output: list[FilledCandle] = []
    source_idx = 0
    source = sorted_candles[0]

    for slot in range(n_slots):
        slot_mts = start_mts + slot * freq_ms
        while (
            source_idx + 1 < len(sorted_candles)
            and sorted_candles[source_idx + 1].mts <= slot_mts
        ):
            source_idx += 1
            source = sorted_candles[source_idx]

        if source.mts == slot_mts:
            output.append(FilledCandle(candle=source, is_stale=False, stale_seconds=0))
            continue

        age_ms = slot_mts - source.mts
        if age_ms <= max_gap_ms:
            output.append(
                FilledCandle(candle=source, is_stale=True, stale_seconds=age_ms // 1000)
            )
        else:
            output.append(
                FilledCandle(candle=None, is_stale=True, stale_seconds=age_ms // 1000)
            )
    return output
