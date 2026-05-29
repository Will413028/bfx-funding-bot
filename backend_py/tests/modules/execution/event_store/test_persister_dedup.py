"""EventStorePersister.persist() dedup-status return — unit tests.

Audit finding I1: persist() must propagate the per-event dedup status from
store.append() so callers can distinguish newly-persisted (True) from
silently-deduped (False) events.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import bfx_funding_bot.modules.execution.event_store.tables  # noqa: F401
from bfx_funding_bot.core.db import Base
from bfx_funding_bot.modules.execution.event_store.persister import (
    EventStorePersister,
    NoopEventPersister,
)
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import OrderFilled, ReservationReleased

_SCID = UUID("22222222-2222-2222-2222-222222222222")


async def _create_all(session: AsyncSession) -> None:
    bind = session.bind
    assert bind is not None
    async with bind.begin() as conn:  # type: ignore[union-attr]
        await conn.run_sync(Base.metadata.create_all)


def _fill(cid: int = 1, venue_offer_id: str = "v1", venue_seq: int = 10) -> OrderFilled:
    return OrderFilled(
        cid=cid,
        venue_offer_id=venue_offer_id,
        credit_id=None,
        size_usdt=Decimal("50"),
        fill_rate=0.0003,
        signal_correlation_id=_SCID,
        account_id="acct",
        is_simulated=False,
        venue_seq=venue_seq,
        occurred_at_ms=2000,
    )


def _release(cid: int = 2, venue_offer_id: str = "v2", venue_seq: int = 20) -> ReservationReleased:
    return ReservationReleased(
        cid=cid,
        venue_offer_id=venue_offer_id,
        size_usdt=Decimal("30"),
        reason="venue_cancel",
        signal_correlation_id=_SCID,
        account_id="acct",
        is_simulated=False,
        venue_seq=venue_seq,
        occurred_at_ms=3000,
    )


async def _make_persister(
    sqlite_session: AsyncSession,
) -> tuple[EventStorePersister, async_sessionmaker[AsyncSession]]:
    await _create_all(sqlite_session)
    store = PostgresEventStore(deployment_environment="ci")
    # Build a session factory that reuses the same sqlite_session's engine.
    bind = sqlite_session.bind
    assert bind is not None
    factory = async_sessionmaker(bind, expire_on_commit=False)  # type: ignore[arg-type]
    return EventStorePersister(store=store, session_factory=factory), factory


async def test_persist_new_event_returns_true(sqlite_session: AsyncSession) -> None:
    """A freshly-persisted event returns [True]."""
    persister, _ = await _make_persister(sqlite_session)
    result = await persister.persist(_fill())
    assert result == [True]


async def test_persist_duplicate_event_returns_false(sqlite_session: AsyncSession) -> None:
    """Re-delivering the same (venue_offer_id, venue_seq) ORDER_FILL returns [False]."""
    persister, _ = await _make_persister(sqlite_session)
    fill = _fill(cid=10, venue_offer_id="dup1", venue_seq=99)
    first = await persister.persist(fill)
    second = await persister.persist(fill)  # same (voi, venue_seq)
    assert first == [True]
    assert second == [False]


async def test_persist_multiple_events_mixed_dedup(sqlite_session: AsyncSession) -> None:
    """persist(*new_event, *dup_event) returns [True, False] in order."""
    persister, _ = await _make_persister(sqlite_session)
    fill = _fill(cid=20, venue_offer_id="m1", venue_seq=50)
    release = _release(cid=21, venue_offer_id="m2", venue_seq=60)

    # Persist the release first so it becomes a dup on the second call.
    await persister.persist(release)

    # Now: fill is new (True), release is dup (False).
    result = await persister.persist(fill, release)
    assert result == [True, False]


async def test_persist_reservation_released_dedup(sqlite_session: AsyncSession) -> None:
    """RESERVATION_RELEASED is also in _DEDUP_TYPES; same-seq re-delivery → False."""
    persister, _ = await _make_persister(sqlite_session)
    ev = _release(cid=30, venue_offer_id="r1", venue_seq=77)
    assert await persister.persist(ev) == [True]
    assert await persister.persist(ev) == [False]


async def test_noop_persister_always_returns_true_list() -> None:
    """NoopEventPersister returns [True] per event (no dedup ever fires)."""
    noop = NoopEventPersister()
    fill = _fill()
    result = await noop.persist(fill)
    assert result == [True]

    result2 = await noop.persist(fill, fill)
    assert result2 == [True, True]


async def test_persist_no_events_returns_empty_list(sqlite_session: AsyncSession) -> None:
    """persist() with zero events returns []."""
    persister, _ = await _make_persister(sqlite_session)
    result = await persister.persist()
    assert result == []
