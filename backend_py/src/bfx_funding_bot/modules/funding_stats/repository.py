from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.postgresql.dml import Insert as PGInsert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.sqlite.dml import Insert as SQLiteInsert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.funding_stats.schemas import FundingStat
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow

_UPSERT_INDEX = ["symbol", "mts"]
_UPSERT_SET_COLS = [
    "frr",
    "avg_period",
    "funding_amount",
    "funding_amount_used",
    "funding_below_threshold",
]

# asyncpg / PostgreSQL caps bind parameters per statement at 32767. With 7
# columns per row, 4000 rows = 28000 placeholders — safely under the limit.
_FUNDING_STAT_CHUNK = 4000


def _to_float_or_none(d: Decimal | None) -> float | None:
    return float(d) if d is not None else None


def _row_to_domain(row: FundingStatRow) -> FundingStat:
    return FundingStat(
        symbol=row.symbol,
        mts=row.mts,
        frr=Decimal(str(row.frr)) if row.frr is not None else None,
        avg_period=Decimal(str(row.avg_period)) if row.avg_period is not None else None,
        funding_amount=(
            Decimal(str(row.funding_amount)) if row.funding_amount is not None else None
        ),
        funding_amount_used=(
            Decimal(str(row.funding_amount_used))
            if row.funding_amount_used is not None
            else None
        ),
        funding_below_threshold=(
            Decimal(str(row.funding_below_threshold))
            if row.funding_below_threshold is not None
            else None
        ),
    )


async def upsert_funding_stats(
    session: AsyncSession,
    stats: list[FundingStat],
) -> None:
    """Upsert funding_stats rows by composite PK (symbol, mts).

    Chunked at _FUNDING_STAT_CHUNK rows per execute() to stay under
    asyncpg's 32767 bind-parameter limit.
    """
    if not stats:
        return

    dialect_name = session.bind.dialect.name if session.bind else "postgresql"

    for i in range(0, len(stats), _FUNDING_STAT_CHUNK):
        chunk = stats[i : i + _FUNDING_STAT_CHUNK]
        rows = [
            {
                "symbol": s.symbol,
                "mts": s.mts,
                "frr": _to_float_or_none(s.frr),
                "avg_period": _to_float_or_none(s.avg_period),
                "funding_amount": _to_float_or_none(s.funding_amount),
                "funding_amount_used": _to_float_or_none(s.funding_amount_used),
                "funding_below_threshold": _to_float_or_none(s.funding_below_threshold),
            }
            for s in chunk
        ]

        stmt: PGInsert | SQLiteInsert
        if dialect_name == "postgresql":
            stmt = pg_insert(FundingStatRow).values(rows)
        else:
            stmt = sqlite_insert(FundingStatRow).values(rows)

        stmt = stmt.on_conflict_do_update(
            index_elements=_UPSERT_INDEX,
            set_={col: getattr(stmt.excluded, col) for col in _UPSERT_SET_COLS},
        )
        await session.execute(stmt)


async def get_in_range(
    session: AsyncSession,
    *,
    symbol: str,
    start_mts: int,
    end_mts: int,
) -> list[FundingStat]:
    """Fetch funding_stats in [start_mts, end_mts] (inclusive) ASC by mts."""
    stmt = (
        select(FundingStatRow)
        .where(
            FundingStatRow.symbol == symbol,
            FundingStatRow.mts >= start_mts,
            FundingStatRow.mts <= end_mts,
        )
        .order_by(FundingStatRow.mts.asc())
    )
    result = await session.execute(stmt)
    return [_row_to_domain(row) for row in result.scalars().all()]


async def get_min_mts(session: AsyncSession, *, symbol: str) -> int | None:
    """Return smallest mts for symbol, or None if no rows."""
    stmt = select(func.min(FundingStatRow.mts)).where(FundingStatRow.symbol == symbol)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_frr_at_or_before_mts(
    session: AsyncSession,
    *,
    symbol: str,
    mts: int,
) -> FundingStat | None:
    """Return latest FundingStat with mts <= given mts (for FRR-as-market_rate
    in Phase 3). Returns None if nothing exists at or before that point."""
    stmt = (
        select(FundingStatRow)
        .where(FundingStatRow.symbol == symbol, FundingStatRow.mts <= mts)
        .order_by(FundingStatRow.mts.desc())
        .limit(1)
    )
    result = await session.execute(stmt)
    row = result.scalars().first()
    return _row_to_domain(row) if row is not None else None
