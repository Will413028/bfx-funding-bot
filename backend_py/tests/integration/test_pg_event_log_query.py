"""PostgresEventLogQueryAdapter — L3 smoke read-your-writes (3c)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from bfx_funding_bot.modules.admin.pg_event_log_query import PostgresEventLogQueryAdapter
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)

pytestmark = pytest.mark.integration

_SCID = UUID("22222222-2222-2222-2222-222222222222")


@pytest.mark.asyncio
async def test_query_returns_claimed_and_fill_for_account(pg_session_factory) -> None:
    env = "ci-query-t1"
    acct = "SMOKE_T1"
    store = PostgresEventStore(deployment_environment=env)
    since = datetime.now(UTC) - timedelta(minutes=1)

    async with pg_session_factory() as s:
        await store.append(
            s,
            ReservationClaimed(
                cid=5001,
                venue_offer_id="vq1",
                size_usdt=Decimal("10"),
                signal_correlation_id=_SCID,
                account_id=acct,
                is_simulated=True,
                venue_seq=1,
                occurred_at_ms=int(since.timestamp() * 1000) + 1000,
            symbol="fUST"),
        )
        await store.append(
            s,
            OrderFilled(
                cid=5001,
                venue_offer_id="vq1",
                credit_id="cq1",
                size_usdt=Decimal("4"),
                fill_rate=0.0,
                signal_correlation_id=_SCID,
                account_id=acct,
                is_simulated=True,
                venue_seq=2,
                occurred_at_ms=int(since.timestamp() * 1000) + 2000,
            symbol="fUST"),
        )
        await s.commit()

    adapter = PostgresEventLogQueryAdapter(
        session_factory=pg_session_factory, deployment_environment=env
    )
    rows = await adapter.query_order_events(acct, since)
    types = {r["event_type"] for r in rows}
    assert "RESERVATION_CLAIMED" in types
    assert "ORDER_FILL" in types


@pytest.mark.asyncio
async def test_query_scopes_by_account_and_env(pg_session_factory) -> None:
    """Seed events for account A; query account B → must return none of A's events.
    Also verify different deployment_environment is isolated.
    """
    env = "ci-query-t2"
    acct_a = "SMOKE_SCOPE_A"
    acct_b = "SMOKE_SCOPE_B"
    store = PostgresEventStore(deployment_environment=env)
    since = datetime.now(UTC) - timedelta(minutes=1)

    # Seed events only for acct_a
    async with pg_session_factory() as s:
        await store.append(
            s,
            ReservationClaimed(
                cid=5002,
                venue_offer_id="vq2",
                size_usdt=Decimal("7"),
                signal_correlation_id=_SCID,
                account_id=acct_a,
                is_simulated=True,
                venue_seq=1,
                occurred_at_ms=int(since.timestamp() * 1000) + 500,
            symbol="fUST"),
        )
        await store.append(
            s,
            ReservationReleased(
                cid=5002,
                venue_offer_id="vq2",
                size_usdt=Decimal("7"),
                reason="venue_cancel",
                signal_correlation_id=_SCID,
                account_id=acct_a,
                is_simulated=True,
                venue_seq=2,
                occurred_at_ms=int(since.timestamp() * 1000) + 1000,
            symbol="fUST"),
        )
        await s.commit()

    # Query for acct_b → should find nothing
    adapter = PostgresEventLogQueryAdapter(
        session_factory=pg_session_factory, deployment_environment=env
    )
    rows_b = await adapter.query_order_events(acct_b, since)
    assert rows_b == [], f"Expected empty for {acct_b}, got {rows_b}"

    # Query for acct_a in a different env → should find nothing
    other_env_adapter = PostgresEventLogQueryAdapter(
        session_factory=pg_session_factory, deployment_environment="ci-other-env"
    )
    rows_other = await other_env_adapter.query_order_events(acct_a, since)
    assert rows_other == [], f"Expected empty for other env, got {rows_other}"

    # Sanity: acct_a in correct env should have results
    rows_a = await adapter.query_order_events(acct_a, since)
    types_a = {r["event_type"] for r in rows_a}
    assert "RESERVATION_CLAIMED" in types_a
    assert "RESERVATION_RELEASED" in types_a
