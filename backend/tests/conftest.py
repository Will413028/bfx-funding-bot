import logging
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from functools import cache
from pathlib import Path

import pytest
import pytest_asyncio
from hypothesis import is_hypothesis_test
from hypothesis import settings as hypothesis_settings
from pytest_postgresql.factories import postgresql_proc
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

# Registers the whole schema on Base.metadata before any test module is collected, so a
# create_all never sees a table whose foreign-key target another module owns go missing.
import bfx_funding_bot.apps.schema  # noqa: F401
from tests import pg_local

# Property-test budgets (the `property` marker in pyproject.toml) live only in profiles:
# CI runs Hypothesis's default profile (100 examples), the nightly workflow passes
# --hypothesis-profile=nightly. A test never pins max_examples (tests/test_property_budget.py),
# or the nightly budget would not reach it.
hypothesis_settings.register_profile("nightly", max_examples=1000)


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
# PostgreSQL 18 fixtures: one local server per pytest process (see tests/pg_local.py).
#
# ``pg_container`` and ``archive_pg`` used to be a postgres:16 and a postgres:18 testcontainer;
# both now resolve to this one server, so their names and URL shapes are unchanged. Only tests
# marked ``docker`` start a container (``docker_pg_container``).
# ---------------------------------------------------------------------------

pg_local.install_executor()


def _initial_pg_bin() -> Path | None:
    try:
        return pg_local.find_pg_bin()
    except pg_local.PgBinNotFoundError:
        return None  # reported by ``pg_server``, so tests that need no database still run


_PG_BIN = _initial_pg_bin()
bfx_pg_proc = postgresql_proc(
    executable=str((_PG_BIN or Path("/nonexistent-postgresql-bin")) / "pg_ctl"),
    host="127.0.0.1", port=None, user="test", password="test", dbname="test",
    postgres_options=pg_local.SERVER_OPTIONS,
)


@pytest.fixture(scope="session")
def pg_server(request: pytest.FixtureRequest) -> pg_local.LocalPostgres:
    """The process's PostgreSQL, checked to be the production major before and after start."""
    import psycopg

    try:
        expected = pg_local.dockerfile_pg_major()
        bin_dir = pg_local.find_pg_bin()
        if bin_dir is None:
            pytest.fail(pg_local.missing_bin_message(expected), pytrace=False)
        for tool in ("pg_ctl", "pg_dump", "pg_restore"):
            pg_local.check_major(pg_local.tool_major(bin_dir, tool), expected, f"{bin_dir}/{tool}")
    except (pg_local.PgBinNotFoundError, pg_local.PgVersionMismatchError) as error:
        pytest.fail(str(error), pytrace=False)
    proc = request.getfixturevalue("bfx_pg_proc")
    with psycopg.connect(host=proc.host, port=proc.port, user=proc.user, password=proc.password,
                         dbname="postgres", autocommit=True) as connection:
        running = int(connection.execute("SHOW server_version_num").fetchone()[0]) // 10000
        data_directory = Path(connection.execute("SHOW data_directory").fetchone()[0])
    try:
        pg_local.check_major(running, expected, "the running server")
    except pg_local.PgVersionMismatchError as error:
        pytest.fail(str(error), pytrace=False)
    temp_root = Path(proc.datadir).resolve().parent
    assert data_directory.resolve().is_relative_to(temp_root), (data_directory, temp_root)
    return pg_local.LocalPostgres(
        bin_dir=bin_dir, host=proc.host, port=proc.port, username=proc.user,
        password=proc.password, dbname=proc.dbname, data_directory=data_directory.resolve(),
        socket_directory=Path(proc.socket_directory), temp_root=temp_root,
    )


@pytest.fixture(scope="session")
def pg_container(pg_server: pg_local.LocalPostgres) -> pg_local.LocalPostgres:
    """The former postgres:16 testcontainer: the per-process local server."""
    return pg_server


# ---------------------------------------------------------------------------
# Docker-only tests. A test marked ``docker`` drives a real daemon (containers, networks,
# `docker exec psql`); it is skipped when none is reachable. CI sets BFX_REQUIRE_DOCKER=1, which
# turns a missing daemon into a failure so those tests can never silently stop running there.
# ---------------------------------------------------------------------------


@cache
def docker_available() -> bool:
    try:
        completed = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


@pytest.hookimpl(tryfirst=True)  # mark before `-m property` selects
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    # Every Hypothesis test is a property test, so `-m property` (nightly) finds all of them.
    for item in items:
        if is_hypothesis_test(getattr(item, "obj", None)):
            item.add_marker(pytest.mark.property)
    docker_items = [item for item in items if item.get_closest_marker("docker")]
    if not docker_items or docker_available():
        return
    if os.environ.get("BFX_REQUIRE_DOCKER") == "1":
        raise pytest.UsageError(
            f"BFX_REQUIRE_DOCKER=1 but no Docker daemon is reachable; {len(docker_items)} "
            "selected tests are marked `docker`."
        )
    skip = pytest.mark.skip(reason="no reachable Docker daemon (marked `docker`)")
    for item in docker_items:
        item.add_marker(skip)


_DOCKER_ENDPOINT_VARIABLES = (
    "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
)


@pytest.fixture(autouse=True)
def _no_ambient_docker_endpoint(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a test marked ``docker`` may see the developer's Docker endpoint.

    The cutover operations guard refuses any non-default DOCKER_HOST (it reads os.environ even
    when a fake runner stands in for the daemon), so those tests failed on a machine whose
    Docker lives behind DOCKER_HOST (Colima, remote contexts). Nothing else needs it.
    """
    if request.node.get_closest_marker("docker") is None:
        for name in _DOCKER_ENDPOINT_VARIABLES:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def docker_pg_container() -> Iterator[object]:
    """The pinned production image (deploy/vm/postgres/Dockerfile), for ``docker`` tests only."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(pg_local.postgres_image_reference()) as container:
        yield container


@pytest.fixture(scope="session")
def docker_pg_templates(docker_pg_container):
    from tests.pg_templates import TemplateDatabases

    return TemplateDatabases(
        docker_pg_container.get_connection_url().replace("+psycopg2", "+psycopg")
    )


# ---------------------------------------------------------------------------
# Logging guard: alembic's fileConfig() once disabled every logger of the process, which made
# an unrelated later caplog test fail. A test that leaves logging changed now fails by name:
# the global disable level, and each logger's level, disabled flag and propagation (a caplog
# record reaches the root handler only through all three).
# ---------------------------------------------------------------------------


def _logging_state() -> tuple[int, dict[str, tuple[int, bool, bool]]]:
    loggers = {
        name: (logger.level, logger.disabled, logger.propagate)
        for name, logger in list(logging.root.manager.loggerDict.items())
        if isinstance(logger, logging.Logger)
    }
    loggers[""] = (logging.root.level, logging.root.disabled, logging.root.propagate)
    return logging.root.manager.disable, loggers


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """For a test whose code configures logging by design (a CLI's ``main``)."""
    disable = logging.root.manager.disable
    loggers = [
        (logger, logger.level, logger.disabled, logger.propagate)
        for logger in [logging.root, *logging.root.manager.loggerDict.values()]
        if isinstance(logger, logging.Logger)
    ]
    yield
    logging.disable(disable)
    for logger, level, disabled, propagate in loggers:
        logger.setLevel(level)
        logger.disabled, logger.propagate = disabled, propagate


@pytest.fixture(autouse=True)
def _logging_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    disable, loggers = _logging_state()
    yield
    disable_after, loggers_after = _logging_state()
    problems = []
    if disable_after != disable:
        problems.append(
            f"logging.disable {logging.getLevelName(disable)} -> {logging.getLevelName(disable_after)}"
        )
    # Loggers that existed before the test: one a library creates mid-test is configured its way.
    # uvicorn's own loggers are reset by every uvicorn.Config (healthz), always to the same state.
    changed = sorted(
        name or "root" for name, before in loggers.items()
        if loggers_after[name] != before and name.partition(".")[0] != "uvicorn"
    )
    if changed:
        problems.append(f"loggers changed (level/disabled/propagate): {', '.join(changed[:5])}")
    if problems:
        pytest.fail(f"{request.node.nodeid} left logging changed ({'; '.join(problems)})", pytrace=False)


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

    from bfx_funding_bot.core.db import Base

    # Every process registers the whole schema at collection (apps.schema, imported
    # above); the name still tracks the table set so a stale template is never reused.
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


@pytest.fixture(autouse=True)
def _fresh_alert_sink() -> Iterator[None]:
    """Operator alerts go through one process-wide sink with a token bucket and a
    10-minute de-duplication window; a test must never see the budget or the
    de-duplication state an earlier test left. Each test starts on a fresh
    log-only sink, and the previous sink is put back afterwards."""
    from bfx_funding_bot.modules.observability import alerts

    previous = alerts.install(alerts.AlertSink(transport=None))
    yield
    alerts.install(previous)


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
def _governance_template(pg_templates) -> str:
    from tests.pg_templates import upgrade_head

    return pg_templates.template(_GOVERNANCE_TEMPLATE, upgrade_head)


@pytest_asyncio.fixture
async def migrated_db(pg_templates, _governance_template: str):
    """A fresh migrated database with one exchange account: (session factory, account)."""
    from uuid import uuid4

    from bfx_funding_bot.modules.accounts.tables import ExchangeAccount

    sync_url = pg_templates.clone(_governance_template, f"governance_{uuid4().hex[:12]}")
    engine = create_async_engine(sync_url.replace("+psycopg", "+asyncpg"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account = uuid4()
    async with factory.begin() as session:
        session.add(ExchangeAccount(id=account, venue="bitfinex", label="governance-test"))
    try:
        yield factory, account
    finally:
        await engine.dispose()
        pg_templates.drop(sync_url)
