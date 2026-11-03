"""The operator-request worker: applies one outbox table's requests in the account daemon.

The request side -- the row's states, the bounded rejection and the web API's insert --
is ``execution.operator_requests``; its docstring states the outbox contract this worker
completes: apply under the account lock, re-check the operator in that transaction, and
record exactly one outcome. Only the account daemon composes a worker; the control plane
must not be able to (``control-plane-exposure``, ADR 2026-10-19 D6).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.operator_requests import (
    FAILED,
    OPERATOR_NOT_AUTHORIZED,
    REJECTED,
    REQUESTED,
    RequestRejected,
    RequestState,
)
from bfx_funding_bot.modules.ledger import Scope, ScopeLock

log = logging.getLogger(__name__)


class NeedsPreparation(Exception):  # noqa: N818 - control flow, not a fault
    """Applying needs something observed outside the transaction first.

    Raised by :meth:`OperatorRequestWorker.apply` before it writes anything;
    the worker rolls back, runs :meth:`OperatorRequestWorker.prepare` with no
    lock or transaction held, and applies again with its result.
    """


@dataclass(frozen=True, slots=True)
class Outcome:
    state: RequestState
    reason: str | None = None
    # Kept for the subclass's post-commit hook (alerts, logs); never persisted.
    detail: object = None


class OperatorAuthority(Protocol):
    async def __call__(self, session: AsyncSession, *, account_id: UUID, user: str) -> bool: ...


async def operator_authorized(session: AsyncSession, *, account_id: UUID, user: str) -> bool:
    """Whether ``user`` may act as this account's operator right now.

    The one authority for every operator request the daemon applies: the
    database's verdict (``public.operator_authorized``), an owner/operator
    member of a live account. Membership rows are written by the table owner
    only, and the migration that made this the sole check asserted a single
    authorized user (quality plan 9d-1). Call it in the transaction that
    applies the request: the function takes SHARE locks, so the authority it
    read holds until that commit.
    """
    if not user:
        return False
    return bool(await session.scalar(
        text("SELECT public.operator_authorized(:account, :actor)"),
        {"account": account_id, "actor": user}))


def root_cause_name(exc: BaseException) -> str:
    seen: set[int] = set()
    current: BaseException = exc
    while current.__cause__ is not None and id(current.__cause__) not in seen:
        seen.add(id(current))
        current = current.__cause__
    return type(current).__name__


class OperatorRequestWorker[RowT, PreparedT](ABC):
    """Applies one outbox table's requests inside the account's single writer."""

    #: The outbox model: ``request_id``, scope, ``requested_by``, ``created_at_ms``,
    #: ``state``, ``processed_at_ms``, ``outcome_reason`` and its own columns.
    model: ClassVar[Any]
    #: Log and error prefix.
    name: ClassVar[str]

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        account_id: UUID,
        environment: str,
        authority: OperatorAuthority,
        scope_lock: ScopeLock,
        clock: Callable[[], int] | None = None,
        ownership: Callable[[], Awaitable[bool]] | None = None,
        poll_interval_s: float = 2.0,
    ) -> None:
        self.session_factory = session_factory
        self.account_id = account_id
        self.environment = environment
        self.ledger_scope = Scope(account_id, environment)
        # The ledger's lock, the one every money-state write of this scope is under;
        # nothing outside the ledger takes the primitive (ADR 2026-10-19 D4).
        self.scope_lock = scope_lock
        # Required, not defaulted: a worker that forgot to check who asked
        # would apply a revoked operator's request.
        self.authority = authority
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.ownership = ownership
        self.poll_interval_s = poll_interval_s
        # Requests this process could neither apply nor mark failed. Skipped so
        # one bad row cannot starve the queue; a restart retries them.
        self._stuck: set[UUID] = set()

    # -------------------------------------------------------------- subclass

    @abstractmethod
    async def apply(self, session: AsyncSession, row: RowT, prepared: PreparedT | None) -> Outcome:
        """Apply ``row`` in ``session`` (a savepoint, lock held, operator checked).

        Return the applied outcome, raise :class:`RequestRejected` for a
        bounded refusal, or :class:`NeedsPreparation` before writing anything.
        """

    async def prepare(self, row_id: UUID) -> PreparedT:
        """Observe what :meth:`apply` asked for, outside any lock or transaction."""
        raise NotImplementedError

    async def idle(self) -> None:  # noqa: B027 - optional hook
        """Housekeeping when the queue is empty."""

    async def committed(self, row: RowT, outcome: Outcome) -> None:  # noqa: B027 - optional hook
        """After the outcome is committed, outside every lock (alerts, follow-up
        work such as a kill's venue cancel-all). Errors are logged, never raised."""

    def queue_order(self) -> tuple[Any, ...]:
        """Which waiting request goes first; oldest by default."""
        return (self.model.created_at_ms, self.model.request_id)

    def failure_reason(self, exc: BaseException) -> str:
        return f"{self.name}_failed:{root_cause_name(exc)}"

    # ---------------------------------------------------------------- driver

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            processed = False
            try:
                processed = await self.tick()
            except Exception:
                # A failed tick leaves the request queued (its transaction rolled
                # back) and must not take the writer down with it.
                log.exception("%s_tick_failed", self.name)
            if processed:
                continue
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self.poll_interval_s)

    async def tick(self) -> bool:
        """Settle at most one queued request, oldest first. Returns whether one was taken."""
        if self.ownership is not None and not await self.ownership():
            raise RuntimeError(f"{self.name}_writer_ownership_lost")
        request_id = await self._oldest_pending()
        if request_id is None:
            await self.idle()
            return False
        await self.process(request_id)
        return True

    async def process(self, request_id: UUID) -> str:
        """Settle one request; returns its state afterwards."""
        try:
            return await self._settle(request_id, prepared=None)
        except NeedsPreparation:
            pass
        try:
            prepared = await self.prepare(request_id)
        except RequestRejected as exc:
            return await self._fail(request_id, exc.code)
        except Exception as exc:
            log.exception("%s_prepare_failed request_id=%s", self.name, request_id)
            return await self._fail(request_id, f"prepare_failed:{root_cause_name(exc)}")
        return await self._settle(request_id, prepared=prepared)

    def _scoped(self, stmt: Any) -> Any:
        return stmt.where(self.model.exchange_account_id == self.account_id,
                          self.model.deployment_environment == self.environment)

    async def _oldest_pending(self) -> UUID | None:
        query = self._scoped(select(self.model.request_id)).where(
            self.model.state == REQUESTED,
        ).order_by(*self.queue_order()).limit(1)
        if self._stuck:
            query = query.where(self.model.request_id.not_in(self._stuck))
        async with self.session_factory() as session:
            request_id: UUID | None = await session.scalar(query)
            return request_id

    async def _lock(self, session: AsyncSession) -> None:
        await self.scope_lock.lock(session, self.ledger_scope)

    async def _settle(self, request_id: UUID, *, prepared: PreparedT | None) -> str:
        row: Any = None
        try:
            async with self.session_factory.begin() as session:
                await self._lock(session)
                row = await session.scalar(self._scoped(select(self.model)).where(
                    self.model.request_id == request_id,
                ).execution_options(populate_existing=True))
                if row is None:
                    return "missing"
                if row.state != REQUESTED:
                    return str(row.state)
                now = self.clock()
                try:
                    # Savepoint: a rejected or failed apply leaves nothing
                    # behind but the outcome recorded on the request.
                    async with session.begin_nested():
                        # Accepted while its operator was authorized; applying
                        # it needs them to still be, in this transaction.
                        if not await self.authority(session, account_id=self.account_id,
                                                    user=row.requested_by):
                            raise RequestRejected(OPERATOR_NOT_AUTHORIZED)
                        outcome = await self.apply(session, row, prepared)
                except NeedsPreparation:
                    raise
                except RequestRejected as exc:
                    outcome = Outcome(REJECTED, exc.code)
                except Exception as exc:
                    reason = self.failure_reason(exc)
                    log.exception("%s_failed request_id=%s reason=%s", self.name, request_id, reason)
                    outcome = Outcome(FAILED, reason)
                row.state, row.processed_at_ms = outcome.state, now
                row.outcome_reason = None if outcome.reason is None else outcome.reason[:500]
                await session.flush()
        except NeedsPreparation:
            raise
        except Exception as exc:
            reason = f"outcome_write_failed:{root_cause_name(exc)}"
            log.exception("%s_outcome_unwritten request_id=%s reason=%s", self.name, request_id, reason)
            return await self._fail(request_id, reason)
        log.info("%s_processed request_id=%s state=%s reason=%s", self.name, request_id,
                 outcome.state, outcome.reason)
        await self._after_commit(row, outcome)
        return outcome.state

    async def _after_commit(self, row: Any, outcome: Outcome) -> None:
        try:
            await self.committed(row, outcome)
        except Exception:
            log.exception("%s_after_commit_failed request_id=%s", self.name, row.request_id)

    async def _fail(self, request_id: UUID, reason: str) -> str:
        """Mark a request failed; when even that cannot be written, skip it.

        Skipped by this process only (a restart retries it), so one request
        whose outcome cannot be recorded never blocks the queue behind it.
        """
        try:
            row = await self._mark_failed(request_id, reason)
        except Exception:
            log.exception("%s_request_stuck request_id=%s", self.name, request_id)
            self._stuck.add(request_id)
            return REQUESTED
        if row is None:  # an earlier commit landed after all; its outcome stands
            return "settled"
        log.error("%s_failed request_id=%s reason=%s", self.name, request_id, reason)
        await self._after_commit(row, Outcome(FAILED, reason))
        return FAILED

    async def _mark_failed(self, request_id: UUID, reason: str) -> Any:
        """Record ``failed`` in its own transaction, only while still ``requested``."""
        async with self.session_factory.begin() as session:
            await self._lock(session)
            return await session.scalar(self._scoped(update(self.model)).where(
                self.model.request_id == request_id, self.model.state == REQUESTED,
            ).values(state=FAILED, processed_at_ms=self.clock(), outcome_reason=reason[:500])
                .returning(self.model))


__all__ = [
    "NeedsPreparation",
    "OperatorAuthority",
    "OperatorRequestWorker",
    "Outcome",
    "operator_authorized",
    "root_cause_name",
]
