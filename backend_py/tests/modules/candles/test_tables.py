import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.candles.tables import (
    FundingCandleRow,
    FundingStatRow,
)


@pytest.mark.asyncio
async def test_candle_tables_create_on_sqlite(sqlite_engine: AsyncEngine) -> None:
    """Smoke: the two candle ORM tables emit DDL without errors."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    table_names = set(Base.metadata.tables.keys())
    assert "funding_candles" in table_names
    assert "funding_stats" in table_names

    pk_cols = [c.name for c in FundingCandleRow.__table__.primary_key]
    assert pk_cols == ["symbol", "timeframe", "period_agg", "mts"]

    stats_pk = [c.name for c in FundingStatRow.__table__.primary_key]
    assert stats_pk == ["symbol", "mts"]
