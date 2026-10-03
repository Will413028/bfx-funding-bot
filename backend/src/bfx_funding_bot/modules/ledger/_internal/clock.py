"""Account-scoped transactional clock and query openings."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.account_identity import account_id_canonical
from bfx_funding_bot.core.writer_lock import acquire_transaction_lock, derive_transaction_lock_key
from bfx_funding_bot.modules.ledger import QueryAdmissionRefused, QueryHandle, Scope
from bfx_funding_bot.modules.ledger.tables import (
    CapitalCommandClockRow,
    LedgerObservationQueryRow,
    SubmissionAttemptJournalRow,
    TransportOutcomeJournalRow,
)


async def lock_scope(session: AsyncSession, scope: Scope) -> None:
    await acquire_transaction_lock(
        session,
        account_id=str(scope.exchange_account_id),
        deployment_environment=scope.deployment_environment,
    )


async def holds_scope_lock(session: AsyncSession, scope: Scope) -> bool:
    """Whether this session's backend holds the scope's transaction advisory lock now.

    A bigint advisory key is stored as ``classid`` (high 32 bits), ``objid``
    (low 32) and ``objsubid = 1`` in ``pg_locks``. The key is in the
    transaction namespace, so a hit is a lock ``lock_scope`` took in the
    current transaction (it is released at its end).
    """
    key = derive_transaction_lock_key(
        account_id_canonical(str(scope.exchange_account_id)), scope.deployment_environment
    )
    return bool(
        await session.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid() AND granted AND objsubid = 1 "
                "AND ((classid::bigint << 32) | objid::bigint) = :key)"
            ),
            {"key": key},
        )
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
    pending = await session.scalar(
        select(SubmissionAttemptJournalRow.attempt_id)
        .outerjoin(
            TransportOutcomeJournalRow,
            TransportOutcomeJournalRow.attempt_id == SubmissionAttemptJournalRow.attempt_id,
        )
        .where(
            SubmissionAttemptJournalRow.exchange_account_id == scope.exchange_account_id,
            SubmissionAttemptJournalRow.deployment_environment == scope.deployment_environment,
            TransportOutcomeJournalRow.attempt_id.is_(None),
        )
        .limit(1)
    )
    if pending is not None:
        raise QueryAdmissionRefused("an attempt in this scope has no outcome")
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
