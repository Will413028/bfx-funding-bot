"""PG row parity and zero-write ledger hints. Requires Docker; run separately.

Mutation: a ledger sink INSERT into event_log must fail
test_ledger_hint_leaves_all_table_counts_unchanged.
"""

import json

import pytest
from sqlalchemy import func, select

from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.persister import EventStorePersister
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.execution.events import ReservationClaimed, ReservationIntent
from bfx_funding_bot.modules.ledger import (
    tables as ledger_tables,  # noqa: F401 - register all ledger tables
)
from bfx_funding_bot.modules.ledger.wiring import build_venue_hint_sink
from tests.external.bitfinex.test_venue_hint_golden import (
    ACCOUNT,
    CASES,
    CORRELATION,
    GOLDEN,
    payload_bytes,
    registry,
    scenario,
)
from tests.modules.ledger.test_venue_hints import KINDS, SCOPE, send

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CASES)
async def test_legacy_hint_actual_event_log_bytes_and_dedup(pg_session_factory, case, monkeypatch):
    persister = EventStorePersister(
        store=PostgresEventStore(deployment_environment="ci"),
        session_factory=pg_session_factory, compatibility_mode=True,
    )
    claim = registry().snapshot()["42"]
    await persister.persist(
        ReservationIntent(
            symbol="fUST", cid=7, execution_decision_id="d-hint-7",
            size_usdt=claim.size_usdt, signal_correlation_id=CORRELATION,
            account_id=ACCOUNT, is_simulated=False, occurred_at_ms=1000,
        ),
        ReservationClaimed(
            symbol="fUST", cid=7, venue_offer_id="42", size_usdt=claim.size_usdt,
            signal_correlation_id=CORRELATION, account_id=ACCOUNT,
            is_simulated=False, occurred_at_ms=1000, reservation_ref=claim.reservation_ref,
        ),
    )
    expected = json.loads(GOLDEN.read_text())[case]
    expected_type = json.loads(expected["persisted"][0])["__event_type__"]
    _, published = await scenario(
        case, monkeypatch, persister, repeat=case not in {"offer_gone", "credit_closed", "credit_closed_fallback"},
    )
    async with pg_session_factory() as session:
        rows = (await session.scalars(select(EventLogRow).where(
            EventLogRow.event_type == expected_type,
        ).order_by(EventLogRow.event_seq))).all()
    assert [payload_bytes(row.payload).decode() for row in rows] == expected["persisted"]
    assert published == expected["published"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_ledger_hint_leaves_all_table_counts_unchanged(pg_session_factory, kind):
    async def counts():
        async with pg_session_factory() as session:
            return {
                table.name: await session.scalar(select(func.count()).select_from(table))
                for table in Base.metadata.sorted_tables
            }

    before = await counts()
    assert "event_log" in before
    assert "submission_attempt_journal" in before
    now = [0.0]
    requested = []
    published = []

    class Bus:
        async def publish(self, event):
            # Every publication observes exactly the original row counts too.
            assert await counts() == before
            published.append(event)

    sink = build_venue_hint_sink(
        scope=SCOPE, request_resync=requested.append, bus=Bus(), monotonic=lambda: now[0],
    )
    await send(sink, kind)
    now[0] = 4.999
    await send(sink, kind)
    assert len(requested) == 1
    assert len(published) == 2
    assert await counts() == before
