from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import bfx_funding_bot.modules.accounts.tables  # noqa: F401  register APIKey
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.accounts.tables import APIKey


@pytest_asyncio.fixture
async def session(sqlite_engine):
    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


def _row(user_id="u1") -> APIKey:
    return APIKey(
        user_id=user_id, label="main", api_key="PUBKEY",
        secret_ciphertext=b"\x01\x02", secret_nonce=b"\x03",
        wrapped_dek=b"\x04", dek_nonce=b"\x05", key_version=1,
    )


@pytest.mark.asyncio
async def test_insert_and_read_back(session: AsyncSession):
    session.add(_row())
    await session.commit()
    got = await session.scalar(select(APIKey).where(APIKey.user_id == "u1"))
    assert got is not None
    assert isinstance(got.id, UUID)  # client-side default=uuid4 populated the PK
    assert got.exchange_status == "unverified"  # server default
    assert got.secret_ciphertext == b"\x01\x02"
    assert got.verified_at is None


@pytest.mark.asyncio
async def test_unique_per_user(session: AsyncSession):
    session.add(_row())
    await session.commit()
    session.add(_row())  # second key for same user
    with pytest.raises(IntegrityError):
        await session.commit()
