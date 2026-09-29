"""Account-scoped transactional clock and query openings."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.modules.ledger import QueryHandle, Scope
from bfx_funding_bot.modules.ledger.tables import (
    CapitalCommandClockRow,
    LedgerObservationQueryRow,
)


async def lock_scope(session: AsyncSession, scope: Scope) -> None:
    await acquire_transaction_lock(
        session,
        account_id=str(scope.exchange_account_id),
        deployment_environment=scope.deployment_environment,
    )


async def bump_locked(session: AsyncSession, scope: Scope) -> int:
    await session.execute(
        insert(CapitalCommandClockRow)
        .values(
            exchange_account_id=scope.exchange_account_id,
            deployment_environment=scope.deployment_environment,
            revision=0,
        )
        .on_conflict_do_nothing(index_elements=["exchange_account_id", "deployment_environment"])
    )
    revision = await session.scalar(
        update(CapitalCommandClockRow)
        .where(
            CapitalCommandClockRow.exchange_account_id == scope.exchange_account_id,
            CapitalCommandClockRow.deployment_environment == scope.deployment_environment,
        )
        .values(revision=CapitalCommandClockRow.revision + 1)
        .returning(CapitalCommandClockRow.revision)
    )
    assert revision is not None
    return revision


async def bump_clock(session: AsyncSession, scope: Scope) -> int:
    await lock_scope(session, scope)
    return await bump_locked(session, scope)


async def begin_query(session: AsyncSession, scope: Scope, started_at_ms: int) -> QueryHandle:
    await lock_scope(session, scope)
    latest = await session.scalar(
        select(func.max(LedgerObservationQueryRow.query_revision)).where(
            LedgerObservationQueryRow.exchange_account_id == scope.exchange_account_id,
            LedgerObservationQueryRow.deployment_environment == scope.deployment_environment,
        )
    )
    revision = await session.scalar(
        select(CapitalCommandClockRow.revision).where(
            CapitalCommandClockRow.exchange_account_id == scope.exchange_account_id,
            CapitalCommandClockRow.deployment_environment == scope.deployment_environment,
        )
    )
    handle = QueryHandle(uuid4(), (latest or 0) + 1, revision or 0, started_at_ms)
    session.add(
        LedgerObservationQueryRow(
            query_id=handle.query_id,
            exchange_account_id=scope.exchange_account_id,
            deployment_environment=scope.deployment_environment,
            query_revision=handle.query_revision,
            start_revision=handle.start_revision,
            started_at_ms=started_at_ms,
        )
    )
    await session.flush()
    return handle
