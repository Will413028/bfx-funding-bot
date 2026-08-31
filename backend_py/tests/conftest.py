from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool


async def ensure_auth_user(session: AsyncSession, user_id: str) -> None:
    """Seed the Better Auth principal used by PG integration tests.

    The application profile/vault tables deliberately enforce a DB-level FK to
    ``auth.user``.  Migration integration tests run in the same session-scoped
    container, so tests that exercise JIT provisioning must create the external
    auth principal just as the real Better Auth service would first do.
    """
    bind = session.bind
    if bind is None or bind.dialect.name != "postgresql":
        return
    exists = await session.scalar(text("SELECT to_regclass('auth.\"user\"')"))
    if exists is None:
        return
    await session.execute(
        text(
            'INSERT INTO auth."user" '
            '("id", "name", "email", "emailVerified", "createdAt", "updatedAt") '
            'VALUES (:id, :name, :email, false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) '
            'ON CONFLICT ("id") DO NOTHING'
        ),
        {"id": user_id, "name": user_id, "email": f"{user_id}@test.invalid"},
    )


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


# ---------------------------------------------------------------------------
# Testcontainers Postgres fixtures (session-scoped; shared across all
# integration tests regardless of their directory).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def pg_container():
    """Session-scoped Postgres container."""
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")
    with PostgresContainer("postgres:16-alpine") as container:
        yield container


@pytest_asyncio.fixture
async def pg_engine(pg_container) -> AsyncIterator[AsyncEngine]:
    """Per-test async engine pointing at an isolated testcontainer schema.

    The container is session-scoped for startup cost, but migration tests are
    allowed to drive the public schema all the way to the irreversible Halt 1
    contract.  Reset both application schemas before each test so that a
    migration test cannot leak NOT NULL/FK state (or rows) into a runtime
    integration test that intentionally exercises the additive ORM fixture.
    """
    raw_url = pg_container.get_connection_url()
    async_url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    engine = create_async_engine(async_url, pool_pre_ping=True, pool_recycle=600)

    async with engine.begin() as conn:
        await conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        await conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        await conn.exec_driver_sql("CREATE SCHEMA public")

    import bfx_funding_bot.modules.accounts.tables
    import bfx_funding_bot.modules.candles.tables
    import bfx_funding_bot.modules.execution.diagnostics.tables
    import bfx_funding_bot.modules.execution.event_store.tables
    import bfx_funding_bot.modules.external_signals.tables
    import bfx_funding_bot.modules.funding_stats.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_session_factory(pg_engine: AsyncEngine):
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """SP5: routers share one process-wide token bucket; suites reuse the same
    fake user, so drain state must not bleed across tests."""
    from bfx_funding_bot.modules.api.ratelimit import reset_shared_rate_limits

    reset_shared_rate_limits()
    yield
