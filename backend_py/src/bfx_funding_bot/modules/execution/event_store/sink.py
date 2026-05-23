from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.execution.event_store.store import PostgresEventStore
from bfx_funding_bot.modules.execution.events import (
    OrderFilled,
    ReservationClaimed,
    ReservationReleased,
)


class PostgresEventSink:
    """Bus subscriber: persists SoT domain events to the Postgres event store.

    One txn per event (event_log row + snapshot update via PostgresEventStore.append).
    Mirrors AxiomEventSink's handler shape so it slots into the same bus subscriptions.
    """

    def __init__(
        self, *, store: PostgresEventStore, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        self._store = store
        self._session_factory = session_factory

    async def on_reservation_claimed(self, event: ReservationClaimed) -> None:
        await self._persist(event)

    async def on_order_filled(self, event: OrderFilled) -> None:
        await self._persist(event)

    async def on_reservation_released(self, event: ReservationReleased) -> None:
        await self._persist(event)

    async def _persist(self, event: object) -> None:
        async with session_scope(self._session_factory) as session:
            await self._store.append(session, event)
