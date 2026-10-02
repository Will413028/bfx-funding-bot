"""``port_stack``: every contract test runs once per authority, on its own database."""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ..test_ledger_schema_roles import ledger_db  # noqa: F401 - fixture re-export
from .stacks import Stack, build_stack, seed_account


@pytest_asyncio.fixture(params=["legacy", "ledger"])
async def port_stack(request, ledger_db) -> Stack:  # noqa: F811
    await seed_account(ledger_db)
    engine = create_async_engine(
        ledger_db.url.render_as_string(hide_password=False).replace("+psycopg", "+asyncpg")
    )
    try:
        yield build_stack(request.param, async_sessionmaker(engine, expire_on_commit=False))
    finally:
        await engine.dispose()
