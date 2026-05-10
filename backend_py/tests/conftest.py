from collections.abc import AsyncIterator

import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


@pytest_asyncio.fixture
async def sqlite_engine() -> AsyncIterator[AsyncEngine]:
    """In-memory sqlite engine for unit tests that need a real DB.

    Uses StaticPool so all sessions share the same in-memory DB.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def sqlite_session(
    sqlite_engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(sqlite_engine, expire_on_commit=False)
    async with factory() as session:
        yield session
