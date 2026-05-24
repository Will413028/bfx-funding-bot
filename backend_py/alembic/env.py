"""Alembic environment — sync engine via psycopg.

Reads DATABASE_URL via Settings and transforms to psycopg-compatible URL
via Settings.database_url_sync. Daemon runtime uses asyncpg via
core.db._prepare_engine_kwargs (separate transform path).

Side-effect imports register tables with Base.metadata so autogenerate
sees a unified schema.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import Connection

# Side-effect imports: register tables with Base.metadata
import bfx_funding_bot.modules.accounts.tables
import bfx_funding_bot.modules.candles.tables
import bfx_funding_bot.modules.execution.diagnostics.tables
import bfx_funding_bot.modules.execution.event_store.tables
import bfx_funding_bot.modules.funding_stats.tables  # noqa: F401
from alembic import context
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.core.settings import Settings

config = context.config

settings = Settings()  # type: ignore[call-arg]
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):
    """Filter out legacy Atlas revision-tracking table from autogenerate."""
    return not (type_ == "table" and name == "atlas_schema_revisions")


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


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
