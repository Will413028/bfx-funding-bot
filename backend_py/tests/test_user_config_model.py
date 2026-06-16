import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.tables  # registers models with Base.metadata
import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401 (registers UserProfile)
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import UserConfig


@pytest_asyncio.fixture
async def session(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.mark.asyncio
async def test_userconfig_inserts_with_text_user_id_and_autogen_id(session):
    row = UserConfig(user_id="user_abc", config={"currency": "USD"})
    session.add(row)
    await session.flush()
    await session.refresh(row)
    assert isinstance(row.id, uuid.UUID)        # client-side default fired
    assert row.user_id == "user_abc"            # TEXT, not coerced to UUID
    assert row.config == {"currency": "USD"}
    assert row.created_at is not None
