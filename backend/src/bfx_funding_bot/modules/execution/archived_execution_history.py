"""The execution history below the switch: the frozen legacy ``event_log``, read-only.

The ledger's execution history continues into this archive below its watermark (plan Q4);
the event log is frozen since the switch (``e8f9a0b1c2d3``), so nothing here writes.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.event_store.tables import EventLogRow
from bfx_funding_bot.modules.ledger import (
    ExecutionCursorError,
    ExecutionEventView,
    ExecutionPage,
    Scope,
)


class ArchivedExecutionHistory:
    """The scope's ``event_log``, newest first; the cursor is the last row's ``event_seq``."""

    async def list_executions(
        self,
        session: AsyncSession,
        scope: Scope,
        *,
        before: str | None,
        limit: int,
        event_type: str | None,
    ) -> ExecutionPage:
        stmt = select(EventLogRow).where(
            EventLogRow.exchange_account_id == scope.exchange_account_id,
            EventLogRow.deployment_environment == scope.deployment_environment,
        )
        if before is not None:
            if not before.isascii() or not before.isdigit():
                raise ExecutionCursorError(before)
            stmt = stmt.where(EventLogRow.event_seq < int(before))
        if event_type is not None:
            stmt = stmt.where(EventLogRow.event_type == event_type)
        rows = (
            await session.execute(stmt.order_by(EventLogRow.event_seq.desc()).limit(limit + 1))
        ).scalars().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        events = []
        for row in rows:
            payload = row.payload or {}
            amount = payload.get("amount") or payload.get("size_usdt")
            rate = payload.get("rate") if payload.get("rate") is not None else payload.get("fill_rate")
            events.append(ExecutionEventView(
                event_key=str(row.event_seq),
                event_type=row.event_type,
                occurred_at_ms=row.occurred_at_ms,
                symbol=payload.get("symbol"),
                venue_offer_id=row.venue_offer_id,
                cid=row.cid,
                amount=str(amount) if amount is not None else None,
                rate=float(rate) if rate is not None else None,
            ))
        return ExecutionPage(
            tuple(events), str(rows[-1].event_seq) if has_more and rows else None
        )
