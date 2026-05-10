from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.candles.schemas import FundingCandle
from bfx_funding_bot.modules.candles.tables import FundingCandleRow


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
    """
    if not candles:
        return

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
        for c in candles
    ]

    dialect_name = session.bind.dialect.name if session.bind else "postgresql"

    if dialect_name == "postgresql":
        stmt = pg_insert(FundingCandleRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "timeframe", "period_agg", "mts"],
            set_={
                "open": stmt.excluded.open,
                "close": stmt.excluded.close,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "volume": stmt.excluded.volume,
            },
        )
    else:
        stmt = sqlite_insert(FundingCandleRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "timeframe", "period_agg", "mts"],
            set_={
                "open": stmt.excluded.open,
                "close": stmt.excluded.close,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "volume": stmt.excluded.volume,
            },
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
