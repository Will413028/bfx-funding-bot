from __future__ import annotations

from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.serialization import (
    event_type_of,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

# Event types whose re-delivery must be deduped (idempotent fills/releases).
_DEDUP_TYPES = frozenset({"ORDER_FILL", "RESERVATION_RELEASED"})


class PostgresEventStore:
    """Append-only event log + (later tasks) transactional snapshot maintenance."""

    def __init__(self, *, deployment_environment: str) -> None:
        self._env = deployment_environment

    async def append(self, session: AsyncSession, event: object) -> bool:
        """Append one domain event in the caller's txn. Returns False if deduped (skipped).

        Caller commits (e.g. via session_scope). Does NOT commit here.
        """
        etype = event_type_of(event)
        # Cast to Any so attribute access works on the dynamically-typed event object.
        _ev: Any = cast(Any, event)
        account_id: str = _ev.account_id
        venue_offer_id: str | None = getattr(_ev, "venue_offer_id", None)
        venue_seq: int | None = getattr(_ev, "venue_seq", None)
        cid: int | None = getattr(_ev, "cid", None)
        occurred_at_ms: int = _ev.occurred_at_ms or 0

        if etype in _DEDUP_TYPES and await self._already_logged(
            session, account_id, etype, venue_offer_id, venue_seq
        ):
            return False

        payload: dict[str, Any] = serialize_event(event)
        session.add(EventLogRow(
            account_id=account_id,
            deployment_environment=self._env,
            event_type=etype,
            cid=cid,
            venue_offer_id=venue_offer_id,
            venue_seq=venue_seq,
            payload=payload,
            occurred_at_ms=occurred_at_ms,
        ))
        # Snapshot maintenance is added in later tasks (offer_claims, then position_state).
        return True

    async def _already_logged(
        self, session: AsyncSession, account_id: str, event_type: str,
        venue_offer_id: str | None, venue_seq: int | None,
    ) -> bool:
        stmt = (
            select(EventLogRow.event_seq)
            .where(
                EventLogRow.account_id == account_id,
                EventLogRow.deployment_environment == self._env,
                EventLogRow.event_type == event_type,
                EventLogRow.venue_offer_id == venue_offer_id,
                EventLogRow.venue_seq == venue_seq,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).first() is not None
