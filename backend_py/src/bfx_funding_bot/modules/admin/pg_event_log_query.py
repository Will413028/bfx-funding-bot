"""PostgresEventLogQueryAdapter — query event_log for L3 smoke (3c).

Replaces AxiomSmokeQueryAdapter. Same `query_order_events(account_id, since)`
signature; reads PG event_log (read-your-writes) instead of Axiom APL round-trip.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

# Matches the event_type values stored UPPERCASE in event_log (see serialization.py).
# Aligns with AxiomSmokeQueryAdapter's APL filter (which lowercases for Axiom).
_ORDER_EVENT_TYPES = ("RESERVATION_CLAIMED", "ORDER_FILL", "RESERVATION_RELEASED")


class PostgresEventLogQueryAdapter:
    """Drop-in replacement for AxiomSmokeQueryAdapter.query_order_events.

    Queries the PG event_log table for smoke-relevant order events,
    scoped by account_id and deployment_environment (multi-tenant safe).
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        deployment_environment: str,
    ) -> None:
        self._sf = session_factory
        self._env = deployment_environment

    async def query_order_events(
        self,
        account_id: str,
        since: datetime,
    ) -> list[dict[str, Any]]:
        """Return order events for account_id since the given UTC datetime.

        Filters to RESERVATION_CLAIMED / ORDER_FILL / RESERVATION_RELEASED,
        scoped by account_id and deployment_environment.

        Returns list[dict] with at least an ``event_type`` key (UPPERCASE),
        matching the shape expected by the L3 smoke runner.
        """
        since_ms = int(since.timestamp() * 1000)
        stmt = (
            select(EventLogRow)
            .where(
                EventLogRow.account_id == account_id,
                EventLogRow.deployment_environment == self._env,
                EventLogRow.event_type.in_(_ORDER_EVENT_TYPES),
                EventLogRow.occurred_at_ms >= since_ms,
            )
            .order_by(EventLogRow.occurred_at_ms.asc())
        )
        async with self._sf() as s:
            rows = (await s.execute(stmt)).scalars().all()
        return [
            {
                "event_type": r.event_type,
                "account_id": r.account_id,
                "occurred_at_ms": r.occurred_at_ms,
            }
            for r in rows
        ]
