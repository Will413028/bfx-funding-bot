"""Resume-aware ingest loops for external signals (research-only).

Mirrors the candles/funding_stats walking-back pattern:
- Walk-back: cursor from (db_min) backwards until the API returns an empty
  page (earliest history reached).
- Top-up: cursor from now backwards until it crosses db_max (gap since the
  previous run is filled).
- Binance funding: forward pagination by start_time (API returns ascending).

All loops upsert + flush per page (caller owns commit — scripts wrap chunks
of pages in session_scope so long backfills persist incrementally) and
support a max_pages budget: stats.done=False means "budget exhausted, call
again to continue".

Liquidations use an *inclusive* overlap cursor (end = oldest seen mts, not
oldest-1): multiple events can share one millisecond and a page boundary may
split them; re-fetching the boundary is safe because upserts deduplicate.
A full page with zero progress escapes by decrementing 1ms.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.backfill.errors import BackfillCursorStuck
from bfx_funding_bot.modules.external_signals.repository import (
    get_liq_max_mts,
    get_liq_min_mts,
    get_perp_max_mts,
    get_perp_min_mts,
    upsert_liquidations,
    upsert_perp_funding,
)
from bfx_funding_bot.modules.external_signals.schemas import (
    LiquidationRecord,
    PerpFundingRecord,
)

_BITFINEX = "bitfinex"
_BINANCE = "binance-usdm"


class SignalsClient(Protocol):
    """Client surface the ingest loops need (see client.ExternalSignalsClient)."""

    async def get_deriv_status_hist(
        self, *, symbol: str, end: int, limit: int
    ) -> list[PerpFundingRecord]: ...

    async def get_liquidations_hist(
        self, *, end: int, limit: int
    ) -> list[LiquidationRecord]: ...

    async def get_binance_funding(
        self, *, symbol: str, start_time: int, limit: int
    ) -> list[PerpFundingRecord]: ...


@dataclass(frozen=True)
class IngestStats:
    source: str
    pages: int
    rows: int
    earliest_mts: int | None
    latest_mts: int | None
    done: bool


def _now_ms(now_ms: int | None) -> int:
    return now_ms if now_ms is not None else int(time.time() * 1000)


async def backfill_perp_funding_to_earliest(
    *,
    client: SignalsClient,
    session: AsyncSession,
    symbol: str,
    page_limit: int = 5000,
    max_pages: int | None = None,
    now_ms: int | None = None,
) -> IngestStats:
    """Walk deriv-status history backwards until Bitfinex returns no more."""
    db_min = await get_perp_min_mts(session, venue=_BITFINEX, symbol=symbol)
    end_ms = (db_min - 1) if db_min is not None else _now_ms(now_ms)

    pages = 0
    rows = 0
    done = False
    while True:
        if max_pages is not None and pages >= max_pages:
            break
        page = await client.get_deriv_status_hist(
            symbol=symbol, end=end_ms, limit=page_limit
        )
        if not page:
            done = True
            break
        await upsert_perp_funding(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        oldest = min(r.mts for r in page)
        if oldest > end_ms:
            raise BackfillCursorStuck(symbol, end_ms, oldest)
        if len(page) < page_limit:
            done = True
            break
        end_ms = oldest - 1

    return IngestStats(
        source=f"{_BITFINEX}:{symbol}",
        pages=pages,
        rows=rows,
        earliest_mts=await get_perp_min_mts(session, venue=_BITFINEX, symbol=symbol),
        latest_mts=await get_perp_max_mts(session, venue=_BITFINEX, symbol=symbol),
        done=done,
    )


async def topup_perp_funding_to_latest(
    *,
    client: SignalsClient,
    session: AsyncSession,
    symbol: str,
    page_limit: int = 5000,
    max_pages: int | None = None,
    now_ms: int | None = None,
) -> IngestStats:
    """Fill the gap between db_max and now (walks backwards from now)."""
    floor = await get_perp_max_mts(session, venue=_BITFINEX, symbol=symbol)
    end_ms = _now_ms(now_ms)

    pages = 0
    rows = 0
    done = False
    while True:
        if max_pages is not None and pages >= max_pages:
            break
        page = await client.get_deriv_status_hist(
            symbol=symbol, end=end_ms, limit=page_limit
        )
        if not page:
            done = True
            break
        await upsert_perp_funding(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        oldest = min(r.mts for r in page)
        if oldest > end_ms:
            raise BackfillCursorStuck(symbol, end_ms, oldest)
        if floor is not None and oldest <= floor:
            done = True
            break
        if len(page) < page_limit:
            done = True
            break
        end_ms = oldest - 1

    return IngestStats(
        source=f"{_BITFINEX}:{symbol}",
        pages=pages,
        rows=rows,
        earliest_mts=await get_perp_min_mts(session, venue=_BITFINEX, symbol=symbol),
        latest_mts=await get_perp_max_mts(session, venue=_BITFINEX, symbol=symbol),
        done=done,
    )


async def backfill_liquidations_to_earliest(
    *,
    client: SignalsClient,
    session: AsyncSession,
    page_limit: int = 500,
    max_pages: int | None = None,
    now_ms: int | None = None,
) -> IngestStats:
    """Walk the (all-symbol) liquidations feed backwards to earliest history."""
    db_min = await get_liq_min_mts(session, venue=_BITFINEX)
    end_ms = db_min if db_min is not None else _now_ms(now_ms)

    pages = 0
    rows = 0
    done = False
    while True:
        if max_pages is not None and pages >= max_pages:
            break
        page = await client.get_liquidations_hist(end=end_ms, limit=page_limit)
        if not page:
            done = True
            break
        await upsert_liquidations(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        oldest = min(r.mts for r in page)
        if oldest > end_ms:
            raise BackfillCursorStuck("liquidations", end_ms, oldest)
        if len(page) < page_limit:
            done = True
            break
        # Inclusive overlap cursor; escape by 1ms when a full page made no progress.
        end_ms = oldest if oldest < end_ms else end_ms - 1

    return IngestStats(
        source=f"{_BITFINEX}:liquidations",
        pages=pages,
        rows=rows,
        earliest_mts=await get_liq_min_mts(session, venue=_BITFINEX),
        latest_mts=await get_liq_max_mts(session, venue=_BITFINEX),
        done=done,
    )


async def topup_liquidations_to_latest(
    *,
    client: SignalsClient,
    session: AsyncSession,
    page_limit: int = 500,
    max_pages: int | None = None,
    now_ms: int | None = None,
) -> IngestStats:
    """Fill the liquidations gap between db_max and now."""
    floor = await get_liq_max_mts(session, venue=_BITFINEX)
    end_ms = _now_ms(now_ms)

    pages = 0
    rows = 0
    done = False
    while True:
        if max_pages is not None and pages >= max_pages:
            break
        page = await client.get_liquidations_hist(end=end_ms, limit=page_limit)
        if not page:
            done = True
            break
        await upsert_liquidations(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        oldest = min(r.mts for r in page)
        if oldest > end_ms:
            raise BackfillCursorStuck("liquidations", end_ms, oldest)
        if floor is not None and oldest <= floor:
            done = True
            break
        if len(page) < page_limit:
            done = True
            break
        end_ms = oldest if oldest < end_ms else end_ms - 1

    return IngestStats(
        source=f"{_BITFINEX}:liquidations",
        pages=pages,
        rows=rows,
        earliest_mts=await get_liq_min_mts(session, venue=_BITFINEX),
        latest_mts=await get_liq_max_mts(session, venue=_BITFINEX),
        done=done,
    )


async def topup_binance_funding_to_latest(
    *,
    client: SignalsClient,
    session: AsyncSession,
    symbol: str,
    page_limit: int = 1000,
    max_pages: int | None = None,
    start_time: int | None = None,
) -> IngestStats:
    """Forward-fill Binance realized funding from db_max+1 (ascending pages).

    start_time overrides the resume cursor (full-history sweep / gap repair —
    idempotent upserts make re-covering stored ranges safe). Note: Binance
    treats startTime=0 as absent and returns the LATEST page instead of
    history, so the empty-DB cursor starts at 1.
    """
    if start_time is not None:
        cursor = start_time
    else:
        db_max = await get_perp_max_mts(session, venue=_BINANCE, symbol=symbol)
        cursor = (db_max + 1) if db_max is not None else 1

    pages = 0
    rows = 0
    done = False
    while True:
        if max_pages is not None and pages >= max_pages:
            break
        page = await client.get_binance_funding(
            symbol=symbol, start_time=cursor, limit=page_limit
        )
        if not page:
            done = True
            break
        await upsert_perp_funding(session, page)
        await session.flush()
        pages += 1
        rows += len(page)

        newest = max(r.mts for r in page)
        if newest < cursor:
            raise BackfillCursorStuck(symbol, cursor, newest)
        if len(page) < page_limit:
            done = True
            break
        cursor = newest + 1

    return IngestStats(
        source=f"{_BINANCE}:{symbol}",
        pages=pages,
        rows=rows,
        earliest_mts=await get_perp_min_mts(session, venue=_BINANCE, symbol=symbol),
        latest_mts=await get_perp_max_mts(session, venue=_BINANCE, symbol=symbol),
        done=done,
    )
