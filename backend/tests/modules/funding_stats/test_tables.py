import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.funding_stats.tables import FundingStatRow


@pytest.mark.asyncio
async def test_funding_stat_table_creates_on_sqlite(sqlite_engine: AsyncEngine) -> None:
    """Smoke: FundingStatRow ORM table emits DDL without errors."""
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    table_names = set(Base.metadata.tables.keys())
    assert "funding_stats" in table_names

    pk_cols = [c.name for c in FundingStatRow.__table__.primary_key]
    assert pk_cols == ["symbol", "mts"]
