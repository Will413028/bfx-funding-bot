from __future__ import annotations

from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.serialization import (
    deserialize_event,
    event_type_of,
    serialize_event,
)
from bfx_funding_bot.modules.execution.event_store.tables import (
    EventLogRow,
    OfferClaimRow,
    PositionStateRow,
)
from bfx_funding_bot.modules.execution.registry_offers import ClaimRecord, RegistryState, transition

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
        row = EventLogRow(
            account_id=account_id,
            deployment_environment=self._env,
            event_type=etype,
            cid=cid,
            venue_offer_id=venue_offer_id,
            venue_seq=venue_seq,
            payload=payload,
            occurred_at_ms=occurred_at_ms,
        )
        session.add(row)
        await session.flush()  # assigns row.event_seq
        # Snapshot maintenance (same txn).
        await self._project_offer_claims(session, event, account_id, venue_offer_id)
        await self._project_position_state(
            session, etype, account_id, getattr(_ev, "size_usdt", None), row.event_seq
        )
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

    async def _project_offer_claims(
        self,
        session: AsyncSession,
        event: object,
        account_id: str,
        venue_offer_id: str | None,
    ) -> None:
        if venue_offer_id is None:
            return  # PENDING intents have no voi (Plan 3); nothing to project here.
        existing = (
            await session.execute(
                select(OfferClaimRow).where(OfferClaimRow.venue_offer_id == venue_offer_id)
            )
        ).scalars().all()
        before: dict[str, ClaimRecord] = {
            r.venue_offer_id: _row_to_claim(r) for r in existing if r.venue_offer_id is not None
        }
        now_ms: int = cast(Any, event).occurred_at_ms or 0
        after, _diags = transition(before, event, now_ms)
        rec = after.get(venue_offer_id)
        if rec is None:
            return
        await self._upsert_claim(session, rec)

    async def _upsert_claim(self, session: AsyncSession, rec: ClaimRecord) -> None:
        dialect = session.bind.dialect.name if session.bind else "postgresql"
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        values: dict[str, Any] = {
            "cid": rec.cid,
            "account_id": rec.account_id,
            "deployment_environment": self._env,
            "state": rec.state.value,
            "venue_offer_id": rec.venue_offer_id,
            "size_usdt": rec.size_usdt,
            "signal_correlation_id": str(rec.signal_correlation_id),
            "occurred_at_ms": rec.occurred_at_ms,
            "last_updated_ms": rec.last_updated_ms,
            "last_event_seq": 0,  # FSM state is the SoT for claims; position_state carries the high-water mark (Task 6)
        }
        stmt = ins(OfferClaimRow).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["cid"],
            set_={k: values[k] for k in ("state", "venue_offer_id", "last_updated_ms")},
        )
        await session.execute(stmt)
        # Expire the identity-map entry for this cid so any subsequent select() in the
        # same session re-fetches from the DB rather than returning a stale cached object.
        cached = session.identity_map.get((OfferClaimRow, (rec.cid,), None))
        if cached is not None:
            session.expire(cached)


    async def _project_position_state(
        self,
        session: AsyncSession,
        etype: str,
        account_id: str,
        size_usdt: Any,
        event_seq: int,
    ) -> None:
        size = Decimal(str(size_usdt)) if size_usdt is not None else Decimal("0")
        ps = (
            await session.execute(
                select(PositionStateRow).where(
                    PositionStateRow.account_id == account_id,
                    PositionStateRow.deployment_environment == self._env,
                )
            )
        ).scalar_one_or_none()
        if ps is None:
            ps = PositionStateRow(
                account_id=account_id,
                deployment_environment=self._env,
                reserved_usdt=Decimal("0"),
                realized_usdt=Decimal("0"),
                last_event_seq=0,
            )
            session.add(ps)
        reserved = Decimal(str(ps.reserved_usdt))
        realized = Decimal(str(ps.realized_usdt))
        if etype == "RESERVATION_CLAIMED":
            reserved += size
        elif etype == "ORDER_FILL":
            delta = min(reserved, size)
            reserved -= delta
            realized += size
        elif etype == "RESERVATION_RELEASED":
            reserved -= min(reserved, size)
        ps.reserved_usdt = reserved
        ps.realized_usdt = realized
        ps.last_event_seq = event_seq

    async def rebuild_snapshot_from_log(
        self, session: AsyncSession, *, account_id: str, deployment_environment: str
    ) -> None:
        """Delete + recompute snapshot rows for (account, env) by folding event_log."""
        await session.execute(delete(OfferClaimRow).where(
            OfferClaimRow.account_id == account_id,
            OfferClaimRow.deployment_environment == deployment_environment))
        await session.execute(delete(PositionStateRow).where(
            PositionStateRow.account_id == account_id,
            PositionStateRow.deployment_environment == deployment_environment))
        await session.flush()
        rows = (await session.execute(
            select(EventLogRow).where(
                EventLogRow.account_id == account_id,
                EventLogRow.deployment_environment == deployment_environment,
            ).order_by(EventLogRow.event_seq.asc())
        )).scalars().all()
        for r in rows:
            event = deserialize_event(r.event_type, r.payload)
            await self._project_offer_claims(session, event, account_id, r.venue_offer_id)
            await self._project_position_state(
                session, r.event_type, account_id,
                getattr(event, "size_usdt", None), r.event_seq)


def _row_to_claim(row: OfferClaimRow) -> ClaimRecord:
    return ClaimRecord(
        venue_offer_id=row.venue_offer_id or "",
        cid=row.cid,
        signal_correlation_id=UUID(row.signal_correlation_id),
        size_usdt=Decimal(str(row.size_usdt)),
        account_id=row.account_id,
        state=RegistryState(row.state),
        occurred_at_ms=row.occurred_at_ms,
        last_updated_ms=row.last_updated_ms,
    )
