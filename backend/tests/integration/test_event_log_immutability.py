"""The append path must hold against the real ledger triggers, not just the ORM.

`event_log` is append-only and `guard_capital_event()` enforces that in PL/pgSQL for
capital-bearing rows. Neither sqlite nor `Base.metadata.create_all` has that trigger, so
a writer that mutates the ledger after insert passes the whole suite and fails on the
first production append. This test runs the real migrations so the constraint is present.
"""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bfx_funding_bot.modules.execution.events import SnapshotCoverage, VenueSnapshotObserved
from tests.pg_templates import open_legacy_archive

pytestmark = pytest.mark.integration

ACCOUNT = UUID("00000000-0000-0000-0000-0000000000c1")
ENV = "ci"


def _seed(url: str) -> None:
    """``url`` is a fresh copy of the database migrated from empty to head."""
    engine = create_engine(url)
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


async def test_appending_a_capital_event_never_mutates_the_ledger(pg_head_url) -> None:
    """A capital-bearing append must not touch event_log after the insert.

    The chain link belongs beside the ledger. Writing it onto the row would be an
    UPDATE, which guard_capital_event() refuses for exactly these events -- the ones
    capital reads depend on -- so the bot could never record a snapshot again.
    """
    from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
    from bfx_funding_bot.modules.execution.event_store.tables import EventPrefixHashRow
    from bfx_funding_bot.modules.execution.event_store.writer import AccountEventWriter

    url = pg_head_url
    open_legacy_archive(url)  # the legacy writer appends; the capital trigger stays on
    _seed(url)
    engine = create_async_engine(url.replace("+psycopg", "+asyncpg"))
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
            "SELECT payload->>'capital_query_id' FROM legacy_archive.event_log WHERE event_seq = :seq"
        ), {"seq": result.event_seq})
        assert payload == capital_bearing.capital_query_id, (
            "the fixture must be capital-bearing, or the trigger never fires and "
            "this test proves nothing"
        )
    await engine.dispose()
