"""Alembic environment — async, reads DATABASE_URL from bfx_funding_bot.core.settings.

Imports tables from each module to populate Base.metadata for autogenerate.
When adding a new module with tables.py, add the import here.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.core.settings import Settings

# Side-effect imports: register tables with Base.metadata
import bfx_funding_bot.modules.candles.tables  # noqa: F401
import bfx_funding_bot.modules.accounts.tables  # noqa: F401

config = context.config

settings = Settings()  # type: ignore[call-arg]
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(object, name, type_, reflected, compare_to):
    """Filter out legacy Atlas revision-tracking table from autogenerate.

    `atlas_schema_revisions` exists on Neon from the previous Atlas era.
    Per Q2 stack decision Alembic now owns schema; the table is left
    untouched as historical audit trail but excluded from drift detection.
    """
    if type_ == "table" and name == "atlas_schema_revisions":
        return False
    return True


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


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


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
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
