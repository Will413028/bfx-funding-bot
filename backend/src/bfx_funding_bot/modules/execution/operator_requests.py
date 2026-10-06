"""Operator requests: the web API queues them, the account daemon applies them.

ADR D4': the web API holds no write on execution state. Every human action that
changes it -- resuming or killing trading, adjudicating
an uncertainty -- is a row in an outbox table. The web API inserts only the
request columns (a column-scoped grant) and answers 202; the account daemon's
worker applies the request inside the account's single writer, under the
account lock, re-checks the operator in that same transaction, and records
exactly one outcome on the row:

- ``applied``  -- done; the table's own worker columns say what it produced;
- ``rejected`` -- a bounded, operator-readable code (:class:`RequestRejected`);
- ``failed``   -- a fault, named by its root exception type.

Every outbox table follows the same contract, enforced in its migration: the
request columns are immutable, ``state`` leaves ``requested`` exactly once, a
partial unique index allows one pending request per subject, rows are never
deleted. The model declares its column split as ``REQUEST_COLUMNS`` and
``WORKER_COLUMNS``, plus ``CLOSED_COLUMNS`` that no code writes any more (tests pin each
migration's grants to them).

The worker never holds a request hostage: a request whose outcome cannot be
written is marked failed in its own transaction, and one that cannot even be
marked is skipped by this process (a restart retries it), so one bad row cannot
block the queue behind it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, Protocol, get_args
from uuid import UUID

from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock

log = logging.getLogger(__name__)

RequestState = Literal["requested", "applied", "rejected", "failed"]
REQUEST_STATES: tuple[str, ...] = get_args(RequestState)
REQUESTED: RequestState = "requested"
APPLIED: RequestState = "applied"
REJECTED: RequestState = "rejected"
FAILED: RequestState = "failed"

RejectionKind = Literal["conflict", "invalid", "not_found"]
OPERATOR_NOT_AUTHORIZED = "operator_not_authorized"


class RequestRejected(RuntimeError):  # noqa: N818 - domain outcome, not a fault
    """A bounded, operator-readable reason the request cannot apply."""

    def __init__(self, code: str, *, kind: RejectionKind = "conflict") -> None:
        super().__init__(code)
        self.code = code
        self.kind = kind


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
    # The table's own worker columns (what an applied request produced).
    columns: Mapping[str, object] = field(default_factory=dict)
    # Kept for the subclass's post-commit hook (alerts, logs); never persisted.
    detail: object = None


class OperatorAuthority(Protocol):
    async def __call__(self, session: AsyncSession, *, account_id: UUID, user: str) -> bool: ...


async def operator_authorized(session: AsyncSession, *, account_id: UUID, user: str) -> bool:
    """Whether ``user`` may act as this account's operator right now.

    The one authority for every operator request the daemon applies. The
    configured sole admin operator only, then the database's own verdict
    (``public.operator_authorized``): still an admin, not banned, TOTP
    enrolled, an owner/operator member of a live account. Call it in the
    transaction that applies the request: the function takes SHARE locks, so
    the authority it read holds until that commit.
    """
    from bfx_funding_bot.core.settings import AuthSettings

    settings = AuthSettings()
    if not user or user != settings.operator_user_id or settings.operator_role != "admin":
        return False
    return bool(await session.scalar(
        text("SELECT public.operator_authorized(:account, :actor)"),
        {"account": account_id, "actor": user}))


async def insert_request(session: AsyncSession, model: Any, values: Mapping[str, object]) -> bool:
    """Queue one request from the web API; False when its pending slot is taken.

    An explicit column INSERT inside a savepoint: the web API's grant covers
    exactly ``model.REQUEST_COLUMNS``, and an ORM flush would also send the
    worker's columns. Takes no account lock -- that lock serialises the
    daemon's writer, and the web API must never stall reconcile or submit.
    """
    if set(values) != set(model.REQUEST_COLUMNS):
        raise ValueError(f"request values must be exactly {model.REQUEST_COLUMNS}")
    try:
        async with session.begin_nested():
            await session.execute(insert(model).values(**values))
    except IntegrityError:
        return False
    return True


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
        clock: Callable[[], int] | None = None,
        ownership: Callable[[], Awaitable[bool]] | None = None,
        poll_interval_s: float = 2.0,
    ) -> None:
        self.session_factory = session_factory
        self.account_id = account_id
        self.environment = environment
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
        await acquire_transaction_lock(session, account_id=str(self.account_id),
                                       deployment_environment=self.environment)

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
                for column, value in outcome.columns.items():
                    setattr(row, column, value)
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
    "APPLIED",
    "FAILED",
    "OPERATOR_NOT_AUTHORIZED",
    "REJECTED",
    "REQUESTED",
    "REQUEST_STATES",
    "NeedsPreparation",
    "OperatorAuthority",
    "OperatorRequestWorker",
    "Outcome",
    "RequestRejected",
    "RequestState",
    "insert_request",
    "operator_authorized",
    "root_cause_name",
]
