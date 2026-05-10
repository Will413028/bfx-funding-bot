from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql.dml import Insert as PGInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.sqlite.dml import Insert as SQLiteInsert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.tables import FundingCandleRow

_UPSERT_INDEX = ["symbol", "timeframe", "period_agg", "mts"]
_UPSERT_SET_COLS = ["open", "close", "high", "low", "volume"]

# asyncpg / PostgreSQL caps bind parameters per statement at 32767. With 9
# columns per row, 3000 rows = 27000 placeholders — safely under the limit.
# Bitfinex returns up to 10000 rows per page, so chunking is required.
_CANDLE_CHUNK = 3000


def _to_float_or_none(d: Decimal | None) -> float | None:
    return float(d) if d is not None else None


def _row_to_domain(row: FundingCandleRow) -> FundingCandle:
    return FundingCandle(
        symbol=row.symbol,
        timeframe=row.timeframe,
        period_agg=row.period_agg,
        mts=row.mts,
        open=Decimal(str(row.open)) if row.open is not None else None,
        close=Decimal(str(row.close)) if row.close is not None else None,
        high=Decimal(str(row.high)) if row.high is not None else None,
        low=Decimal(str(row.low)) if row.low is not None else None,
        volume=Decimal(str(row.volume)) if row.volume is not None else None,
    )


async def upsert_candles(
    session: AsyncSession,
    candles: list[FundingCandle],
) -> None:
    """Upsert candles by composite PK (symbol, timeframe, period_agg, mts).

    Postgres: ON CONFLICT DO UPDATE. Sqlite (test only): same semantics via
    sqlite dialect's insert().on_conflict_do_update.

    Chunked at _CANDLE_CHUNK rows per execute() to stay under asyncpg's
    32767 bind-parameter limit.
    """
    if not candles:
        return

    dialect_name = session.bind.dialect.name if session.bind else "postgresql"

    for i in range(0, len(candles), _CANDLE_CHUNK):
        chunk = candles[i : i + _CANDLE_CHUNK]
        rows = [
            {
                "symbol": c.symbol,
                "timeframe": c.timeframe,
                "period_agg": c.period_agg,
                "mts": c.mts,
                "open": _to_float_or_none(c.open),
                "close": _to_float_or_none(c.close),
                "high": _to_float_or_none(c.high),
                "low": _to_float_or_none(c.low),
                "volume": _to_float_or_none(c.volume),
            }
            for c in chunk
        ]

        stmt: PGInsert | SQLiteInsert
        if dialect_name == "postgresql":
            stmt = pg_insert(FundingCandleRow).values(rows)
        else:
            stmt = sqlite_insert(FundingCandleRow).values(rows)

        stmt = stmt.on_conflict_do_update(
            index_elements=_UPSERT_INDEX,
            set_={col: getattr(stmt.excluded, col) for col in _UPSERT_SET_COLS},
        )
        await session.execute(stmt)


async def get_candles_in_range(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
    start_mts: int,
    end_mts: int,
) -> list[FundingCandle]:
    """Fetch candles in [start_mts, end_mts] (inclusive) ordered by mts ASC."""
    stmt = (
        select(FundingCandleRow)
        .where(
            FundingCandleRow.symbol == symbol,
            FundingCandleRow.timeframe == timeframe,
            FundingCandleRow.period_agg == period_agg,
            FundingCandleRow.mts >= start_mts,
            FundingCandleRow.mts <= end_mts,
        )
        .order_by(FundingCandleRow.mts.asc())
    )
    result = await session.execute(stmt)
    return [_row_to_domain(row) for row in result.scalars().all()]


async def get_min_mts(
    session: AsyncSession,
    *,
    symbol: str,
    timeframe: str,
    period_agg: str,
) -> int | None:
    """Return smallest mts for the given (symbol, timeframe, period_agg) series,
    or None if no rows exist."""
    from sqlalchemy import func

    stmt = select(func.min(FundingCandleRow.mts)).where(
        FundingCandleRow.symbol == symbol,
        FundingCandleRow.timeframe == timeframe,
        FundingCandleRow.period_agg == period_agg,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
