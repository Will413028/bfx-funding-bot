from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.funding_stats.repository import (
    get_frr_at_or_before_mts,
    get_in_range,
    get_min_mts,
    upsert_funding_stats,
)
from bfx_funding_bot.modules.funding_stats.schemas import FundingStat


@pytest.fixture
async def setup_schema(sqlite_engine: AsyncEngine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make(symbol: str, mts: int, frr: str = "5.8e-7") -> FundingStat:
    return FundingStat(
        symbol=symbol,
        mts=mts,
        frr=Decimal(frr),
        avg_period=Decimal("2.3"),
        funding_amount=Decimal("45000000"),
        funding_amount_used=Decimal("21000000"),
        funding_below_threshold=Decimal("1200000"),
    )


@pytest.mark.asyncio
async def test_upsert_then_get_in_range_round_trips(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    rows = [
        _make("fUSD", 1700000000000, frr="5.8e-7"),
        _make("fUSD", 1700003600000, frr="6.1e-7"),
    ]
    await upsert_funding_stats(sqlite_session, rows)
    await sqlite_session.commit()

    fetched = await get_in_range(
        sqlite_session, symbol="fUSD",
        start_mts=1700000000000, end_mts=1700003600000,
    )
    assert len(fetched) == 2
    assert fetched[0].mts == 1700000000000
    assert fetched[1].mts == 1700003600000
    assert fetched[0].frr is not None
    assert abs(fetched[0].frr - Decimal("5.8e-7")) < Decimal("1e-12")


@pytest.mark.asyncio
async def test_upsert_replaces_existing_pk(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    original = _make("fUSD", 1700000000000, frr="5.8e-7")
    updated = original.model_copy(update={"frr": Decimal("7.0e-7")})

    await upsert_funding_stats(sqlite_session, [original])
    await upsert_funding_stats(sqlite_session, [updated])
    await sqlite_session.commit()

    fetched = await get_in_range(
        sqlite_session, symbol="fUSD",
        start_mts=1700000000000, end_mts=1700000000000,
    )
    assert len(fetched) == 1
    assert fetched[0].frr is not None
    assert abs(fetched[0].frr - Decimal("7.0e-7")) < Decimal("1e-12")


@pytest.mark.asyncio
async def test_upsert_empty_list_is_noop(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_funding_stats(sqlite_session, [])
    await sqlite_session.commit()
    fetched = await get_in_range(
        sqlite_session, symbol="fUSD", start_mts=0, end_mts=10**13,
    )
    assert fetched == []


@pytest.mark.asyncio
async def test_get_min_mts_returns_none_for_empty_symbol(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    result = await get_min_mts(sqlite_session, symbol="fUSD")
    assert result is None


@pytest.mark.asyncio
async def test_get_min_mts_returns_smallest(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_funding_stats(
        sqlite_session,
        [
            _make("fUSD", 1700003600000),
            _make("fUSD", 1700000000000),
            _make("fUSD", 1700007200000),
            _make("fUST", 1500000000000),  # different symbol
        ],
    )
    await sqlite_session.commit()
    assert await get_min_mts(sqlite_session, symbol="fUSD") == 1700000000000
    assert await get_min_mts(sqlite_session, symbol="fUST") == 1500000000000


@pytest.mark.asyncio
async def test_get_frr_at_or_before_mts_returns_latest_le(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_funding_stats(
        sqlite_session,
        [
            _make("fUSD", 1700000000000, frr="5.0e-7"),
            _make("fUSD", 1700003600000, frr="6.0e-7"),
            _make("fUSD", 1700007200000, frr="7.0e-7"),
        ],
    )
    await sqlite_session.commit()

    # exact match
    fs = await get_frr_at_or_before_mts(
        sqlite_session, symbol="fUSD", mts=1700003600000,
    )
    assert fs is not None
    assert fs.mts == 1700003600000
    assert fs.frr is not None
    assert abs(fs.frr - Decimal("6.0e-7")) < Decimal("1e-12")

    # between two entries — return earlier
    fs = await get_frr_at_or_before_mts(
        sqlite_session, symbol="fUSD", mts=1700005000000,
    )
    assert fs is not None
    assert fs.mts == 1700003600000

    # before all entries
    fs = await get_frr_at_or_before_mts(
        sqlite_session, symbol="fUSD", mts=1500000000000,
    )
    assert fs is None


@pytest.mark.asyncio
async def test_get_frr_at_or_before_mts_isolated_per_symbol(
    sqlite_session: AsyncSession,
    setup_schema: None,
) -> None:
    await upsert_funding_stats(
        sqlite_session,
        [
            _make("fUSD", 1700000000000, frr="5.0e-7"),
            _make("fUST", 1700003600000, frr="9.9e-7"),
        ],
    )
    await sqlite_session.commit()

    fs = await get_frr_at_or_before_mts(
        sqlite_session, symbol="fUSD", mts=1700010000000,
    )
    assert fs is not None
    assert fs.mts == 1700000000000  # 不會抓到 fUST 那筆
