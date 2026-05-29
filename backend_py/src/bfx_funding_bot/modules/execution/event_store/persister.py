"""Synchronous event persistence for the A2 write path.

Each persist() call opens ONE txn (session_scope) and appends N events via
PostgresEventStore. The middleware calls persist() before the venue submit
(txn1: INTENT) and after it returns (txn2: outcome) — separate calls => separate
txns => structurally "never hold a txn across a REST call" (spec §13.4).
"""
from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore


class EventPersister(Protocol):
    async def persist(self, *events: object) -> list[bool]: ...


class EventStorePersister:
    """Real persister: append events in a single committed txn.

    Returns a list[bool] of the same length as *events, where each element
    is True if the event was newly written and False if it was silently deduped
    (same venue_offer_id + venue_seq already exists for ORDER_FILL /
    RESERVATION_RELEASED).  Callers that only care about durability (e.g.
    ReservationEmittingMiddleware, which emits fresh events) may safely ignore
    the return value.
    """

    def __init__(
        self,
        *,
        store: PostgresEventStore,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._store = store
        self._session_factory = session_factory

    async def persist(self, *events: object) -> list[bool]:
        """Append events in one txn; return per-event dedup status (True = persisted)."""
        results: list[bool] = []
        async with session_scope(self._session_factory) as session:
            for event in events:
                persisted = await self._store.append(session, event)
                results.append(persisted)
        return results


class NoopEventPersister:
    """Null object — for chain tests / contexts that don't exercise persistence.

    Always reports True (newly persisted) because dedup never fires here.
    """

    async def persist(self, *events: object) -> list[bool]:
        return [True] * len(events)
