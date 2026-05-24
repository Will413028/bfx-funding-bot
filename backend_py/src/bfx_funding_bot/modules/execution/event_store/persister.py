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
    async def persist(self, *events: object) -> None: ...


class EventStorePersister:
    """Real persister: append events in a single committed txn."""

    def __init__(
        self,
        *,
        store: PostgresEventStore,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._store = store
        self._session_factory = session_factory

    async def persist(self, *events: object) -> None:
        async with session_scope(self._session_factory) as session:
            for event in events:
                await self._store.append(session, event)


class NoopEventPersister:
    """Null object — for chain tests / contexts that don't exercise persistence."""

    async def persist(self, *events: object) -> None:
        return None
