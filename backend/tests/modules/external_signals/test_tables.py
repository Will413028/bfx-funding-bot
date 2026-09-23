"""Table DDL smoke tests for external_signals (perp_funding_rates, liquidations)."""
from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.external_signals.tables import (
    LiquidationRow,
    PerpFundingRateRow,
)


@pytest.mark.asyncio
async def test_tables_create_on_sqlite(sqlite_engine: AsyncEngine) -> None:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    table_names = set(Base.metadata.tables.keys())
    assert "perp_funding_rates" in table_names
    assert "liquidations" in table_names


def test_perp_funding_composite_pk() -> None:
    pk_cols = [c.name for c in PerpFundingRateRow.__table__.primary_key]
    assert pk_cols == ["venue", "symbol", "mts"]


def test_liquidations_composite_pk() -> None:
    pk_cols = [c.name for c in LiquidationRow.__table__.primary_key]
    assert pk_cols == ["venue", "pos_id", "mts", "is_match", "is_market_sold"]
