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
    contract.  Reset application and archive schemas before each test so that a
    migration test cannot leak NOT NULL/FK state (or rows) into a runtime
    integration test that intentionally exercises the additive ORM fixture.
    """
    raw_url = pg_container.get_connection_url()
    async_url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    engine = create_async_engine(async_url, pool_pre_ping=True, pool_recycle=600)

    async with engine.begin() as conn:
        # This engine comes only from the session-scoped synthetic container;
        # never preserve an earlier test's immutable archive across migrations.
        await conn.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        await conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        await conn.exec_driver_sql("DROP SCHEMA IF EXISTS release_archive CASCADE")
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


# ---------------------------------------------------------------------------
# Projection-archive fixtures (PostgreSQL 18).
#
# Building the archive schema means migrating to e7b1c2d3e4f5, seeding legacy
# rows, then migrating to head -- seconds per test, across ~100 test cases. It is
# done once into a template database; each test gets a byte-identical copy via
# CREATE DATABASE ... TEMPLATE. Roles are cluster-wide and so shared, as they
# already were between tests of one module.
# ---------------------------------------------------------------------------

_ARCHIVE_TEMPLATE = "archive_template"
# Matches ACCOUNT in tests/integration/test_projection_cutover_archive.py.
_ARCHIVE_ACCOUNT = "00000000-0000-0000-0000-000000000064"
_ALEMBIC_INI = __import__("pathlib").Path(__file__).resolve().parents[1] / "alembic.ini"


def _database_url(url: str, database: str) -> str:
    from sqlalchemy.engine import make_url

    return make_url(url).set(database=database).render_as_string(hide_password=False)


def _build_archive_database(url: str) -> None:
    """Migrate, seed and verify one database exactly as each test used to."""
    from alembic.config import Config
    from sqlalchemy import create_engine

    from alembic import command

    engine = create_engine(url)
    try:
        command.upgrade(Config(str(_ALEMBIC_INI)), "e7b1c2d3e4f5")
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO exchange_accounts(id,venue,label) VALUES (:id,'bitfinex','synthetic')"),
                {"id": _ARCHIVE_ACCOUNT},
            )
            conn.execute(
                text(
                    "INSERT INTO position_state(account_id,exchange_account_id,deployment_environment,symbol,reserved,last_updated_ms) VALUES (:s,:id,'ci','fUST',1.2300,123), (:s,:id,'shadow','fUSD',9.000,999)"
                ),
                {"s": _ARCHIVE_ACCOUNT, "id": _ARCHIVE_ACCOUNT},
            )
            conn.execute(
                text(
                    "INSERT INTO reconcile_observation(account_id,exchange_account_id,deployment_environment,reserved_usdt,realized_usdt,n_offers,n_credits,observed_at_ms,event_seq_fence,recorded_at) VALUES (:s,:id,'ci',1.2300,0.000,2,3,123,0,'2001-02-03T04:05:06.123456Z')"
                ),
                {"s": _ARCHIVE_ACCOUNT, "id": _ARCHIVE_ACCOUNT},
            )
            before = conn.execute(text("SELECT to_jsonb(p) FROM position_state p ORDER BY symbol")).all()
        command.upgrade(Config(str(_ALEMBIC_INI)), "head")
        with engine.begin() as conn:
            after = conn.execute(text("SELECT to_jsonb(p) FROM position_state p ORDER BY symbol")).all()
            assert after == before
            conn.exec_driver_sql("CREATE SCHEMA unrelated")
            conn.exec_driver_sql("CREATE TABLE unrelated.keep_me(id integer)")
        command.check(Config(str(_ALEMBIC_INI)))
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def archive_pg():
    """Session-wide PostgreSQL 18; tests always see the container's own database."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18-alpine") as container:
        yield container.get_connection_url().replace("+psycopg2", "+psycopg")


@pytest.fixture(scope="session")
def _archive_template(archive_pg: str) -> str:
    from sqlalchemy import create_engine

    admin = create_engine(_database_url(archive_pg, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f"CREATE DATABASE {_ARCHIVE_TEMPLATE}")
    finally:
        admin.dispose()
    with pytest.MonkeyPatch.context() as patch:
        template_url = _database_url(archive_pg, _ARCHIVE_TEMPLATE)
        patch.setenv("DATABASE_URL", template_url)
        _build_archive_database(template_url)
    return _ARCHIVE_TEMPLATE


@pytest_asyncio.fixture
async def archive_db(archive_pg: str, _archive_template: str, monkeypatch: pytest.MonkeyPatch):
    """A fresh copy of the migrated, seeded archive database for one test."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    database = make_url(archive_pg).database
    admin = create_engine(_database_url(archive_pg, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
            conn.exec_driver_sql(f'CREATE DATABASE "{database}" TEMPLATE {_archive_template}')
    finally:
        admin.dispose()
    monkeypatch.setenv("DATABASE_URL", archive_pg)
    engine = create_engine(archive_pg)
    async_engine = create_async_engine(archive_pg.replace("+psycopg", "+asyncpg"))
    try:
        yield async_sessionmaker(async_engine, expire_on_commit=False), engine
    finally:
        await async_engine.dispose()
        engine.dispose()


# ---------------------------------------------------------------------------
# Migrated governance database (PostgreSQL 18).
#
# The trading-state, deployment and operator-request rules live in PostgreSQL
# triggers and CHECK constraints (the authority; Python only fails earlier with a
# clearer message). Tests of those rules run against the real migrated schema:
# migrated once into a template, then cloned per test.
# ---------------------------------------------------------------------------

_GOVERNANCE_TEMPLATE = "governance_template"


@pytest.fixture(scope="session")
def _governance_template(archive_pg: str) -> str:
    from alembic.config import Config
    from sqlalchemy import create_engine

    from alembic import command

    admin = create_engine(_database_url(archive_pg, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f"CREATE DATABASE {_GOVERNANCE_TEMPLATE}")
    finally:
        admin.dispose()
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("DATABASE_URL", _database_url(archive_pg, _GOVERNANCE_TEMPLATE))
        command.upgrade(Config(str(_ALEMBIC_INI)), "head")
    return _GOVERNANCE_TEMPLATE


@pytest_asyncio.fixture
async def migrated_db(archive_pg: str, _governance_template: str):
    """A fresh migrated database with one exchange account: (session factory, account)."""
    from uuid import uuid4

    from sqlalchemy import create_engine

    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount

    database = f"governance_{uuid4().hex[:12]}"
    admin = create_engine(_database_url(archive_pg, "postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.exec_driver_sql(f'CREATE DATABASE "{database}" TEMPLATE {_governance_template}')
    finally:
        admin.dispose()
    url = _database_url(archive_pg, database).replace("+psycopg", "+asyncpg")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="governance-test"))
    try:
        yield factory, account
    finally:
        await engine.dispose()
        admin = create_engine(_database_url(archive_pg, "postgres"), isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as conn:
                conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        finally:
            admin.dispose()
