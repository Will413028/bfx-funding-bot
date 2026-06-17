import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import bfx_funding_bot.modules.accounts.user_profile  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts import config_service
from bfx_funding_bot.modules.accounts.tables import UserConfig

_CFG = {"currency": "USD", "amount": {"min": 50, "max": 10000}, "autoRenew": True}


@pytest_asyncio.fixture
async def session(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.mark.asyncio
async def test_get_absent_returns_none(session):
    assert await config_service.get_user_config(session, user_id="nobody") is None


@pytest.mark.asyncio
async def test_upsert_creates_then_get_returns_it(session):
    row = await config_service.upsert_user_config(session, user_id="user_abc", config=_CFG)
    assert row.user_id == "user_abc"
    assert row.config == _CFG
    fetched = await config_service.get_user_config(session, user_id="user_abc")
    assert fetched is not None
    assert fetched.id == row.id


@pytest.mark.asyncio
async def test_upsert_twice_updates_in_place(session):
    first = await config_service.upsert_user_config(session, user_id="user_abc", config=_CFG)
    new_cfg = {**_CFG, "currency": "UST"}
    second = await config_service.upsert_user_config(session, user_id="user_abc", config=new_cfg)
    assert second.id == first.id                     # same row, not a new insert
    assert second.config["currency"] == "UST"
    count = await session.scalar(select(func.count()).select_from(UserConfig))
    assert count == 1                                # single-config-per-user


@pytest.mark.asyncio
async def test_delete_returns_true_then_false(session):
    await config_service.upsert_user_config(session, user_id="user_abc", config=_CFG)
    assert await config_service.delete_user_config(session, user_id="user_abc") is True
    assert await config_service.delete_user_config(session, user_id="user_abc") is False


@pytest.mark.asyncio
async def test_upsert_refreshes_updated_at(session):
    # Guards the Task-2 onupdate wiring + the service refresh: editing the config
    # must advance updated_at (the FE surfaces updatedAt) while created_at holds.
    # Capture the value (not the object) — same session returns one identity-
    # mapped row, so `first` is mutated in place by the second upsert.
    first = await config_service.upsert_user_config(session, user_id="user_abc", config=_CFG)
    created, first_updated = first.created_at, first.updated_at
    await asyncio.sleep(1.05)  # sqlite current_timestamp is 1s-granular
    second = await config_service.upsert_user_config(
        session, user_id="user_abc", config={**_CFG, "currency": "UST"}
    )
    assert second.updated_at > first_updated
    assert second.created_at == created
