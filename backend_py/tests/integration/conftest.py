"""Integration test fixtures — testcontainers Postgres + minimal daemon."""
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)
from testcontainers.postgres import PostgresContainer


@pytest.fixture(scope="session")
def pg_container():
    """Session-scoped Postgres container."""
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest_asyncio.fixture
async def pg_engine(pg_container) -> AsyncIterator[AsyncEngine]:
    """Per-test async engine pointing at testcontainer Postgres.

    The testcontainer URL is postgresql+psycopg2://... — we swap the driver
    to asyncpg so SQLAlchemy async sessions work.
    """
    raw_url = pg_container.get_connection_url()
    # raw_url like postgresql+psycopg2://test:test@host:port/test
    async_url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    engine = create_async_engine(async_url, pool_pre_ping=True, pool_recycle=600)

    # Create tables via Base.metadata (faster than alembic for tests)
    # Side-effect imports to populate Base.metadata
    import bfx_funding_bot.modules.accounts.tables
    import bfx_funding_bot.modules.candles.tables
    import bfx_funding_bot.modules.funding_stats.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_session_factory(pg_engine):
    return async_sessionmaker(pg_engine, expire_on_commit=False)
