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


def _create_all(url: str) -> None:
    from sqlalchemy import create_engine

    from bfx_funding_bot.core.db import Base

    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn)
    finally:
        engine.dispose()


@pytest_asyncio.fixture
async def pg_engine(pg_container, pg_templates) -> AsyncIterator[AsyncEngine]:
    """Per-test async engine on a fresh ``Base.metadata.create_all`` database.

    The container is session-scoped for startup cost, but migration tests are
    allowed to drive a database all the way to the irreversible Halt 1
    contract.  The container's own database is recreated before each test from
    a create_all template so that no test can leak NOT NULL/FK state (or rows)
    into a runtime integration test that intentionally exercises the additive
    ORM fixture.
    """
    import hashlib

    import bfx_funding_bot.modules.accounts.tables
    import bfx_funding_bot.modules.candles.tables
    import bfx_funding_bot.modules.execution.diagnostics.tables
    import bfx_funding_bot.modules.execution.event_store.tables
    import bfx_funding_bot.modules.external_signals.tables
    import bfx_funding_bot.modules.funding_stats.tables  # noqa: F401
    from bfx_funding_bot.core.db import Base

    # create_all builds whatever tables are registered right now; a test module
    # importing more tables later gets a template of its own.
    tables = hashlib.sha256("\n".join(sorted(Base.metadata.tables)).encode()).hexdigest()[:16]
    template = pg_templates.template(f"create_all_{tables}", _create_all)
    pg_templates.recreate(pg_container.dbname, template)

    raw_url = pg_container.get_connection_url()
    async_url = raw_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    engine = create_async_engine(async_url, pool_pre_ping=True, pool_recycle=600)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_session_factory(pg_engine: AsyncEngine):
    return async_sessionmaker(pg_engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Migrated templates on the PostgreSQL 16 container (see tests/pg_templates.py).
#
# A test that needs a migrated database clones a template instead of replaying
# the Alembic chain: ``pg_templates.template(name, build)`` builds once per
# session, ``pg_clone(name)`` hands the test its own copy and drops it after.
# ---------------------------------------------------------------------------

HEAD_TEMPLATE = "head_template"


@pytest.fixture(scope="session")
def pg_templates(pg_container):
    from tests.pg_templates import TemplateDatabases

    return TemplateDatabases(pg_container.get_connection_url().replace("+psycopg2", "+psycopg"))


@pytest.fixture
def pg_clone(pg_templates):
    """``clone(template) -> sync URL`` of a fresh copy, dropped after the test."""
    made: list[str] = []

    def clone(template: str) -> str:
        url = pg_templates.clone(template)
        made.append(url)
        return url

    yield clone
    for url in made:
        pg_templates.drop(url)


@pytest.fixture
def pg_head_url(pg_templates, pg_clone) -> str:
    """Sync (psycopg) URL of a fresh database migrated from empty to head."""
    from tests.pg_templates import upgrade_head

    return pg_clone(pg_templates.template(HEAD_TEMPLATE, upgrade_head))


@pytest_asyncio.fixture
async def pg_head_engine(pg_head_url: str) -> AsyncIterator[AsyncEngine]:
    """Async engine on a fresh database migrated from empty to head."""
    engine = create_async_engine(pg_head_url.replace("+psycopg", "+asyncpg"))
    yield engine
    await engine.dispose()


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


def _build_archive_database(url: str) -> None:
    """Migrate, seed and verify one database exactly as each test used to."""
    from sqlalchemy import create_engine

    from tests.pg_templates import alembic

    engine = create_engine(url)
    try:
        alembic(url, "upgrade", "e7b1c2d3e4f5")
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
        alembic(url, "upgrade", "head")
        with engine.begin() as conn:
            after = conn.execute(text("SELECT to_jsonb(p) FROM position_state p ORDER BY symbol")).all()
            assert after == before
            conn.exec_driver_sql("CREATE SCHEMA unrelated")
            conn.exec_driver_sql("CREATE TABLE unrelated.keep_me(id integer)")
        alembic(url, "check")
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def archive_pg():
    """Session-wide PostgreSQL 18; tests always see the container's own database."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:18-alpine") as container:
        yield container.get_connection_url().replace("+psycopg2", "+psycopg")


@pytest.fixture(scope="session")
def archive_templates(archive_pg: str):
    from tests.pg_templates import TemplateDatabases

    return TemplateDatabases(archive_pg)


@pytest.fixture(scope="session")
def _archive_template(archive_templates) -> str:
    return archive_templates.template(_ARCHIVE_TEMPLATE, _build_archive_database)


@pytest_asyncio.fixture
async def archive_db(
    archive_pg: str, archive_templates, _archive_template: str, monkeypatch: pytest.MonkeyPatch
):
    """A fresh copy of the migrated, seeded archive database for one test."""
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    archive_templates.recreate(make_url(archive_pg).database, _archive_template)
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
def _governance_template(archive_templates) -> str:
    from tests.pg_templates import upgrade_head

    return archive_templates.template(_GOVERNANCE_TEMPLATE, upgrade_head)


@pytest_asyncio.fixture
async def migrated_db(archive_templates, _governance_template: str):
    """A fresh migrated database with one exchange account: (session factory, account)."""
    from uuid import uuid4

    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount

    sync_url = archive_templates.clone(_governance_template, f"governance_{uuid4().hex[:12]}")
    engine = create_async_engine(sync_url.replace("+psycopg", "+asyncpg"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="governance-test"))
    try:
        yield factory, account
    finally:
        await engine.dispose()
        archive_templates.drop(sync_url)
