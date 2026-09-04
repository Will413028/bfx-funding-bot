"""Alembic environment — sync engine via psycopg.

Reads DATABASE_URL via Settings and transforms to psycopg-compatible URL
via Settings.database_url_sync. Daemon runtime uses asyncpg via
core.db._prepare_engine_kwargs (separate transform path).

Side-effect imports register tables with Base.metadata so autogenerate
sees a unified schema.
"""
# ruff: noqa: F401

import hashlib
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

# Side-effect imports: register tables with Base.metadata
import bfx_funding_bot.modules.accounts.exchange_accounts
import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.accounts.user_profile
import bfx_funding_bot.modules.candles.tables
import bfx_funding_bot.modules.execution.audit.tables
import bfx_funding_bot.modules.execution.diagnostics.tables
import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.execution.safety.tables
import bfx_funding_bot.modules.execution.uncertainty_tables
import bfx_funding_bot.modules.external_signals.tables
import bfx_funding_bot.modules.funding_stats.tables
import bfx_funding_bot.modules.lending.tracking.tables
import bfx_funding_bot.modules.live_validation.tables
import bfx_funding_bot.modules.marketfeed.tables
from alembic import context
from bfx_funding_bot.core.alembic_compare import compare_server_default, include_object
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.core.settings import Settings

config = context.config

settings = Settings()  # type: ignore[call-arg]
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Session-level advisory lock that serializes concurrent `alembic upgrade` runs
# (e.g. during VM cutover). Distinct namespace from the daemon writer lock so the
# two never false-share. blake2b -> signed 64-bit int (pg advisory-lock key type).
_MIGRATE_LOCK_KEY = int.from_bytes(
    hashlib.blake2b(b"bfx-migrate:alembic", digest_size=8).digest(), "big", signed=True
)


def do_run_migrations(connection: Connection) -> None:
    # Capture the connection's transactional default isolation level (e.g.
    # "READ COMMITTED") so we can restore it for the migration after running the
    # setup under AUTOCOMMIT. NOTE: use ``default_isolation_level`` (the DBAPI
    # session default) rather than ``get_isolation_level()`` — the latter does
    # NOT reflect the SQLAlchemy AUTOCOMMIT execution option, so it would not
    # round-trip correctly.
    original_isolation = connection.default_isolation_level
    # Run the session-scoped setup (timeouts + advisory lock) under AUTOCOMMIT so
    # they don't open a lingering outer transaction. SET and pg_try_advisory_lock
    # attach to the underlying DBAPI session, so they SURVIVE switching the
    # connection's isolation level back to transactional below.
    connection.execution_options(isolation_level="AUTOCOMMIT")
    # Bounded timeouts so a contended/blocked migration fails fast instead of
    # hanging (e.g. during VM cutover): wait at most 5s for a lock, abort any
    # single statement after 60s.
    connection.exec_driver_sql("SET lock_timeout = '5s'")
    connection.exec_driver_sql("SET statement_timeout = '60s'")
    # Serialize concurrent migrations on a session-level advisory lock; bail out
    # immediately if another migration already holds it.
    got = connection.exec_driver_sql(
        f"SELECT pg_try_advisory_lock({_MIGRATE_LOCK_KEY})"
    ).scalar()
    if not got:
        raise RuntimeError("another migration is already running (advisory lock held)")
    try:
        # Clear the SQLAlchemy-level logical transaction that autobegan on the
        # setup statements above; isolation level may not be altered while a
        # Transaction object is active. Then restore the TRANSACTIONAL isolation
        # so the migration runs ATOMICALLY (all DDL in one txn, rolled back on a
        # mid-migration failure). The session SET timeouts + advisory lock survive.
        connection.rollback()
        connection.execution_options(isolation_level=original_isolation)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=compare_server_default,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    finally:
        # Release on a non-transactional path so the unlock takes effect
        # immediately with no dangling txn before the (NullPool) connection
        # closes. rollback() clears any active SQLAlchemy txn first (required
        # before switching isolation level).
        connection.rollback()
        connection.execution_options(isolation_level="AUTOCOMMIT")
        connection.exec_driver_sql(f"SELECT pg_advisory_unlock({_MIGRATE_LOCK_KEY})")


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
