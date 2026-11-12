from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.execution.safety.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.safety.nav_peak_store import NavPeakStore


@pytest_asyncio.fixture
async def sf(sqlite_engine) -> async_sessionmaker:
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(sqlite_engine, expire_on_commit=False)


@pytest.mark.asyncio
async def test_save_then_load_roundtrip(sf) -> None:
    store = NavPeakStore(sf, account_id="acct")
    await store.save("fUST", Decimal("1234.5"), 1000)
    assert await store.load() == {"fUST": Decimal("1234.5")}


@pytest.mark.asyncio
async def test_save_upserts_same_symbol(sf) -> None:
    store = NavPeakStore(sf, account_id="acct")
    await store.save("fUST", Decimal("100"), 1000)
    await store.save("fUST", Decimal("150"), 2000)
    await store.save("fUSD", Decimal("7"), 3000)
    assert await store.load() == {"fUST": Decimal("150"), "fUSD": Decimal("7")}


@pytest.mark.asyncio
async def test_load_is_account_scoped(sf) -> None:
    one = NavPeakStore(sf, account_id="acct")
    other = NavPeakStore(sf, account_id="other")
    await one.save("fUST", Decimal("999"), 1000)
    assert await other.load() == {}
