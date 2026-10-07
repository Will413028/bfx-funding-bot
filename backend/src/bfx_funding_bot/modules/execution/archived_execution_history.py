"""The execution history below the switch: the archived legacy ``event_log``, read-only.

The ledger's execution history continues into this archive below its watermark (plan Q4).
The event log is frozen since the switch (``e8f9a0b1c2d3``) and lives in ``legacy_archive``
since ``c2d3e4f5a6b7``, where the web API may read exactly the columns ``EVENT_LOG`` names
(the migration's ``WEBAPI_EVENT_COLUMNS``). The table is declared here, on its own metadata,
rather than through the legacy event store's ORM: this is the archive's one runtime reader.
"""

from __future__ import annotations

from sqlalchemy import JSON, BigInteger, Column, MetaData, Table, Text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.ledger import (
    ExecutionCursorError,
    ExecutionEventView,
    ExecutionPage,
    Scope,
)

# The frozen legacy tables' schema (migration ``c2d3e4f5a6b7``); migrations own it.
SCHEMA = "legacy_archive"

EVENT_LOG = Table(
    "event_log",
    MetaData(),
    Column("event_seq", BigInteger, primary_key=True),
    Column("exchange_account_id", PG_UUID(as_uuid=True)),
    Column("deployment_environment", Text),
    Column("event_type", Text),
    Column("occurred_at_ms", BigInteger),
    Column("venue_offer_id", Text),
    Column("cid", BigInteger),
    Column("payload", JSON().with_variant(JSONB, "postgresql")),
    schema=SCHEMA,
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
        log = EVENT_LOG.c
        stmt = select(*EVENT_LOG.c).where(
            log.exchange_account_id == scope.exchange_account_id,
            log.deployment_environment == scope.deployment_environment,
        )
        if before is not None:
            if not before.isascii() or not before.isdigit():
                raise ExecutionCursorError(before)
            stmt = stmt.where(log.event_seq < int(before))
        if event_type is not None:
            stmt = stmt.where(log.event_type == event_type)
        rows = (await session.execute(stmt.order_by(log.event_seq.desc()).limit(limit + 1))).all()
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
