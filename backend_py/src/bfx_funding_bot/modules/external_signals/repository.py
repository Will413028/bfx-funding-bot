"""Dialect-aware idempotent upserts + mts bounds for external-signal tables."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql.dml import Insert as PGInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.sqlite.dml import Insert as SQLiteInsert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.external_signals.schemas import (
    LiquidationRecord,
    PerpFundingRecord,
)
from bfx_funding_bot.modules.external_signals.tables import (
    LiquidationRow,
    PerpFundingRateRow,
)

_PERP_INDEX = ["venue", "symbol", "mts"]
_PERP_SET_COLS = [
    "funding_rate", "next_funding_accrued", "next_funding_evt_mts",
    "deriv_price", "spot_price", "mark_price", "open_interest",
]
_LIQ_INDEX = ["venue", "pos_id", "mts", "is_match", "is_market_sold"]
_LIQ_SET_COLS = ["symbol", "amount", "base_price", "price_acquired"]

# asyncpg caps bind parameters per statement at 32767.
# perp: 10 cols × 3000 = 30000; liq: 9 cols × 3000 = 27000 — both under cap.
_CHUNK = 3000


async def upsert_perp_funding(
    session: AsyncSession,
    records: list[PerpFundingRecord],
) -> None:
    """Upsert by composite PK (venue, symbol, mts); latest write wins."""
    if not records:
        return
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    for i in range(0, len(records), _CHUNK):
        chunk = records[i : i + _CHUNK]
        rows = [r.model_dump() for r in chunk]
        stmt: PGInsert | SQLiteInsert
        if dialect_name == "postgresql":
            stmt = pg_insert(PerpFundingRateRow).values(rows)
        else:
            stmt = sqlite_insert(PerpFundingRateRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=_PERP_INDEX,
            set_={col: getattr(stmt.excluded, col) for col in _PERP_SET_COLS},
        )
        await session.execute(stmt)


async def upsert_liquidations(
    session: AsyncSession,
    records: list[LiquidationRecord],
) -> None:
    """Upsert by composite PK (venue, pos_id, mts, is_match, is_market_sold)."""
    if not records:
        return
    dialect_name = session.bind.dialect.name if session.bind else "postgresql"
    for i in range(0, len(records), _CHUNK):
        chunk = records[i : i + _CHUNK]
        rows = [r.model_dump() for r in chunk]
        stmt: PGInsert | SQLiteInsert
        if dialect_name == "postgresql":
            stmt = pg_insert(LiquidationRow).values(rows)
        else:
            stmt = sqlite_insert(LiquidationRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=_LIQ_INDEX,
            set_={col: getattr(stmt.excluded, col) for col in _LIQ_SET_COLS},
        )
        await session.execute(stmt)


async def get_perp_min_mts(
    session: AsyncSession, *, venue: str, symbol: str
) -> int | None:
    stmt = select(func.min(PerpFundingRateRow.mts)).where(
        PerpFundingRateRow.venue == venue,
        PerpFundingRateRow.symbol == symbol,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_perp_max_mts(
    session: AsyncSession, *, venue: str, symbol: str
) -> int | None:
    stmt = select(func.max(PerpFundingRateRow.mts)).where(
        PerpFundingRateRow.venue == venue,
        PerpFundingRateRow.symbol == symbol,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_liq_min_mts(session: AsyncSession, *, venue: str) -> int | None:
    stmt = select(func.min(LiquidationRow.mts)).where(LiquidationRow.venue == venue)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_liq_max_mts(session: AsyncSession, *, venue: str) -> int | None:
    stmt = select(func.max(LiquidationRow.mts)).where(LiquidationRow.venue == venue)
    return (await session.execute(stmt)).scalar_one_or_none()
