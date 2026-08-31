"""PostgresEventLogQueryAdapter — query event_log for L3 smoke (3c).

Replaces AxiomSmokeQueryAdapter. Same `query_order_events(account_id, since)`
signature; reads PG event_log (read-your-writes) instead of Axiom APL round-trip.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.accounts.exchange_accounts import account_scope_clause
from bfx_funding_bot.modules.execution.event_store.serialization import _CLASS_BY_TYPE
from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow

# Matches the event_type values stored UPPERCASE in event_log (see serialization.py).
# Aligns with AxiomSmokeQueryAdapter's APL filter (which lowercases for Axiom).
_ORDER_EVENT_TYPES = ("RESERVATION_CLAIMED", "ORDER_FILL", "RESERVATION_RELEASED")

# Guard: keep _ORDER_EVENT_TYPES in sync with the authoritative event_type strings.
# If an event type is renamed in serialization.py, fail loudly at import rather than
# silently returning no rows.
_ALL_EVENT_TYPES: frozenset[str] = frozenset(_CLASS_BY_TYPE)
assert set(_ORDER_EVENT_TYPES) <= _ALL_EVENT_TYPES, (
    f"_ORDER_EVENT_TYPES drifted from serialization: "
    f"{set(_ORDER_EVENT_TYPES) - _ALL_EVENT_TYPES}"
)


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
        async with self._sf() as s:
            stmt = (
                select(EventLogRow)
                .where(
                    account_scope_clause(
                        s,
                        account_id=account_id,
                        exchange_account_column=EventLogRow.exchange_account_id,
                        legacy_account_column=EventLogRow.account_id,
                    ),
                    EventLogRow.deployment_environment == self._env,
                    EventLogRow.event_type.in_(_ORDER_EVENT_TYPES),
                    # Non-indexed range filter: acceptable for smoke's small/short-window
                    # use. Add an index on occurred_at_ms if event_log grows large.
                    EventLogRow.occurred_at_ms >= since_ms,
                )
                .order_by(EventLogRow.occurred_at_ms.asc())
            )
            rows = (await s.execute(stmt)).scalars().all()
        return [
            {
                "event_type": r.event_type,
                "account_id": (
                    str(r.exchange_account_id)
                    if r.exchange_account_id is not None
                    else r.account_id
                ),
                "occurred_at_ms": r.occurred_at_ms,
            }
            for r in rows
        ]
