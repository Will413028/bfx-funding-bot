"""The append path must hold against the real ledger triggers, not just the ORM.

`event_log` is append-only and `guard_capital_event()` enforces that in PL/pgSQL for
capital-bearing rows. Neither sqlite nor `Base.metadata.create_all` has that trigger, so
a writer that mutates the ledger after insert passes the whole suite and fails on the
first production append. This test runs the real migrations so the constraint is present.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved

pytestmark = pytest.mark.integration

ACCOUNT = UUID("00000000-0000-0000-0000-0000000000c1")
ENV = "ci"


def _migrate(url: str) -> None:
    engine = create_engine(url.replace("+psycopg2", "+psycopg"))
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS projection_audit CASCADE")
        conn.exec_driver_sql("DROP SCHEMA IF EXISTS auth CASCADE")
        conn.exec_driver_sql("DROP SCHEMA public CASCADE")
        conn.exec_driver_sql("CREATE SCHEMA public")
    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env=dict(os.environ, DATABASE_URL=url.replace("+psycopg2", "+psycopg")),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with engine.begin() as conn:
        # Prove the constraint this test exists for is actually installed.
        assert conn.scalar(text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE c.relname = 'event_log' AND t.tgname = 'immutable_capital_event'"
        )) == 1
        conn.execute(text(
            "INSERT INTO exchange_accounts(id, venue, label) VALUES (:id, 'bitfinex', 'trigger-fixture')"
        ), {"id": str(ACCOUNT)})
    engine.dispose()


async def test_appending_a_capital_event_never_mutates_the_ledger(pg_container) -> None:
    """A capital-bearing append must not touch event_log after the insert.

    The chain link belongs beside the ledger. Writing it onto the row would be an
    UPDATE, which guard_capital_event() refuses for exactly these events -- the ones
    capital reads depend on -- so the bot could never record a snapshot again.
    """
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.event_store.tables import EventPrefixHashRow
    from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter

    url = pg_container.get_connection_url()
    _migrate(url)
    engine = create_async_engine(url.replace("+psycopg2", "+asyncpg"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    writer = AccountEventWriter(store=PostgresEventStore(deployment_environment=ENV))

    observed = VenueSnapshotObserved(
        account_id=str(ACCOUNT), environment=ENV,
        query_started_at_ms=1000, query_finished_at_ms=1050,
        offers=(), credits=(), wallet_available={"fUST": Decimal("100")},
        coverage=SnapshotCoverage(True, True, True),
    )
    capital_bearing = replace(observed, capital_query_id=str(uuid4()), event_id=uuid4())

    async with factory.begin() as session:
        result = await writer.append(session, capital_bearing)

    async with factory() as session:
        chain = await session.get(EventPrefixHashRow, result.event_seq)
        assert chain is not None, "the append must seal its prefix"
        assert chain.prefix_hash, "a sealed link cannot be empty"
        payload = await session.scalar(text(
            "SELECT payload->>'capital_query_id' FROM event_log WHERE event_seq = :seq"
        ), {"seq": result.event_seq})
        assert payload == capital_bearing.capital_query_id, (
            "the fixture must be capital-bearing, or the trigger never fires and "
            "this test proves nothing"
        )
    await engine.dispose()
