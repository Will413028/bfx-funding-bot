"""Durable, one-shot command boundary for the Halt 2 canary.

The normal daemon remains halted.  An operator-facing command may consume one
permit, call the already-built executor once, and must reassert the durable
halt in a ``finally`` block.  Consuming the permit is committed before the
venue call, so a process crash cannot turn into an implicit retry.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.db import session_scope
from bfx_funding_bot.modules.accounts.exchange_accounts import account_scope_clause
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit, ReservationRef
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    ExecutorPort,
    SubmittedOrder,
)
from bfx_funding_bot.modules.execution.safety.tables import TradingHaltRow
from bfx_funding_bot.modules.execution.uncertainty_tables import (
    CanaryCommandPermitRow,
    SubmissionAttemptRow,
)

__all__ = [
    "CanaryCommandPermit",
    "CanaryOneShotGate",
    "CanaryPermitBlocked",
    "CanaryPermitRepository",
    "CanaryPermitScope",
]


class CanaryPermitBlocked(RuntimeError):  # noqa: N818
    """The durable one-shot permit cannot safely be issued or consumed."""


@dataclass(frozen=True, slots=True)
class CanaryPermitScope:
    account_id: UUID
    environment: str
    symbol: str
    cell: str
    strategy: str
    amount_usdt: Decimal

    def __post_init__(self) -> None:
        if not self.environment.strip() or not self.symbol.strip():
            raise ValueError("canary permit scope requires environment and symbol")
        if not self.cell.strip() or not self.strategy.strip():
            raise ValueError("canary permit scope requires cell and strategy")
        if not self.amount_usdt.is_finite() or self.amount_usdt <= 0:
            raise ValueError("canary permit amount must be finite and positive")


@dataclass(frozen=True, slots=True)
class CanaryCommandPermit:
    permit_id: UUID
    halt_id: int
    scope: CanaryPermitScope
    operator_id: str
    state: str
    issued_at_ms: int
    consumed_at_ms: int | None
    execution_decision_id: str | None
    attempt_id: UUID | None


class CanaryPermitRepository:
    """Issue and consume permits with database row-lock serialization."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def issue(
        self,
        scope: CanaryPermitScope,
        *,
        operator_id: str,
        now_ms: int | None = None,
    ) -> CanaryCommandPermit:
        operator = _required(operator_id, field="operator_id")
        issued_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if issued_at_ms < 0:
            raise ValueError("now_ms must be non-negative")
        try:
            async with session_scope(self._session_factory) as session:
                halt_result = await session.execute(
                    select(TradingHaltRow.id, TradingHaltRow.halted)
                    .where(
                        account_scope_clause(
                            session,
                            account_id=str(scope.account_id),
                            exchange_account_column=TradingHaltRow.exchange_account_id,
                            legacy_account_column=TradingHaltRow.account_id,
                        ),
                        TradingHaltRow.deployment_environment == scope.environment,
                    )
                    .order_by(TradingHaltRow.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                halt = halt_result.first()
                if halt is None or not halt.halted:
                    raise CanaryPermitBlocked("persistent_halt_required")
                existing = await session.scalar(
                    select(CanaryCommandPermitRow).where(
                        CanaryCommandPermitRow.halt_id == halt.id,
                    )
                )
                if existing is not None:
                    raise CanaryPermitBlocked("permit_already_issued_for_halt")
                row = CanaryCommandPermitRow(
                    permit_id=uuid4(),
                    halt_id=halt.id,
                    exchange_account_id=scope.account_id,
                    deployment_environment=scope.environment,
                    symbol=scope.symbol,
                    cell=scope.cell,
                    strategy=scope.strategy,
                    amount_usdt=scope.amount_usdt,
                    operator_id=operator,
                    state="issued",
                    issued_at_ms=issued_at_ms,
                )
                session.add(row)
                await session.flush()
                return _to_permit(row)
        except CanaryPermitBlocked:
            raise
        except IntegrityError as exc:
            raise CanaryPermitBlocked("permit_issue_conflict") from exc

    async def consume(
        self,
        permit_id: UUID,
        scope: CanaryPermitScope,
        *,
        now_ms: int | None = None,
    ) -> CanaryCommandPermit:
        consumed_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if consumed_at_ms < 0:
            raise ValueError("now_ms must be non-negative")
        async with session_scope(self._session_factory) as session:
            row = await session.scalar(
                select(CanaryCommandPermitRow)
                .where(CanaryCommandPermitRow.permit_id == permit_id)
                .with_for_update()
            )
            if row is None:
                raise CanaryPermitBlocked("permit_missing")
            if row.state != "issued":
                raise CanaryPermitBlocked("permit_already_used")
            halt_result = await session.execute(
                select(TradingHaltRow.id, TradingHaltRow.halted)
                .where(TradingHaltRow.id == row.halt_id)
                .with_for_update()
            )
            halt = halt_result.first()
            current_halt_result = await session.execute(
                select(TradingHaltRow.id, TradingHaltRow.halted)
                .where(
                    account_scope_clause(
                        session,
                        account_id=str(scope.account_id),
                        exchange_account_column=TradingHaltRow.exchange_account_id,
                        legacy_account_column=TradingHaltRow.account_id,
                    ),
                    TradingHaltRow.deployment_environment == scope.environment,
                )
                .order_by(TradingHaltRow.id.desc())
                .limit(1)
            )
            current_halt = current_halt_result.first()
            if (
                halt is None
                or current_halt is None
                or current_halt.id != halt.id
                or not halt.halted
                or not current_halt.halted
            ):
                raise CanaryPermitBlocked("persistent_halt_changed")
            if not _scope_matches(row, scope):
                raise CanaryPermitBlocked("permit_scope_mismatch")
            row.state = "consumed"
            row.consumed_at_ms = consumed_at_ms
            await session.flush()
            return _to_permit(row)

    async def bind_outcome(
        self,
        permit_id: UUID,
        scope: CanaryPermitScope,
        *,
        execution_decision_id: str,
    ) -> CanaryCommandPermit:
        """Bind the consumed permit to the durable attempt created by the gate."""
        decision_id = _required(execution_decision_id, field="execution_decision_id")
        async with session_scope(self._session_factory) as session:
            row = await session.scalar(
                select(CanaryCommandPermitRow)
                .where(CanaryCommandPermitRow.permit_id == permit_id)
                .with_for_update()
            )
            if row is None:
                raise CanaryPermitBlocked("permit_missing")
            if row.state != "consumed":
                raise CanaryPermitBlocked("permit_not_consumed")
            if not _scope_matches(row, scope):
                raise CanaryPermitBlocked("permit_scope_mismatch")
            if row.execution_decision_id not in {None, decision_id}:
                raise CanaryPermitBlocked("permit_outcome_conflict")
            attempt = await session.scalar(
                select(SubmissionAttemptRow).where(
                    SubmissionAttemptRow.execution_decision_id == decision_id,
                    SubmissionAttemptRow.exchange_account_id == scope.account_id,
                    SubmissionAttemptRow.deployment_environment == scope.environment,
                    SubmissionAttemptRow.symbol == scope.symbol,
                )
            )
            if attempt is None:
                raise CanaryPermitBlocked("canary_attempt_not_durable")
            row.execution_decision_id = decision_id
            row.attempt_id = attempt.attempt_id
            await session.flush()
            return _to_permit(row)


class CanaryOneShotGate:
    """Wrap one executor call with a consumed permit and halt reassertion."""

    def __init__(
        self,
        inner: ExecutorPort,
        *,
        scope: CanaryPermitScope,
        halt_authorization: object,
        consume_permit: Callable[[], Awaitable[Any]],
        reassert_halt: Callable[[], Awaitable[None]],
        record_outcome: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._inner = inner
        self._scope = scope
        self._halt_authorization = halt_authorization
        self._consume_permit = consume_permit
        self._reassert_halt = reassert_halt
        self._record_outcome = record_outcome
        self._lock = asyncio.Lock()
        self._used = False

    async def submit(
        self,
        ready: ReadyToSubmit,
        context: AccountContext,
        *,
        cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        async with self._lock:
            if self._used:
                raise CanaryPermitBlocked("permit_already_used")
            _assert_ready_scope(ready, context, scope=self._scope)
            await self._consume_permit()
            # Mark before entering the venue boundary.  A cancellation or
            # exception after this line is still a consumed one-shot permit.
            self._used = True
            authorized_context = replace(
                context,
                canary_halt_authorization=self._halt_authorization,
            )
            try:
                result = await self._inner.submit(
                    ready,
                    authorized_context,
                    cid=cid,
                    reservation_ref=reservation_ref,
                )
                if self._record_outcome is not None:
                    await self._record_outcome(ready.decision_id)
                return result
            finally:
                # Reassertion is deliberately not best-effort: if the halt
                # cannot be written, the caller must remain failed closed.
                await self._reassert_halt()


def _required(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value.strip()


def _scope_matches(row: CanaryCommandPermitRow, scope: CanaryPermitScope) -> bool:
    return (
        row.exchange_account_id == scope.account_id
        and row.deployment_environment == scope.environment
        and row.symbol == scope.symbol
        and row.cell == scope.cell
        and row.strategy == scope.strategy
        and Decimal(str(row.amount_usdt)) == scope.amount_usdt
    )


def _assert_ready_scope(
    ready: ReadyToSubmit,
    context: AccountContext,
    *,
    scope: CanaryPermitScope,
) -> None:
    """Bind the one-shot permit to the concrete command before consuming it."""
    try:
        account_id = UUID(context.account_id)
        amount = Decimal(str(ready.decision.offer_amount_usdt))
    except (AttributeError, TypeError, ValueError, ArithmeticError) as exc:
        raise CanaryPermitBlocked("permit_command_scope_invalid") from exc
    if (
        account_id != scope.account_id
        or ready.decision.symbol != scope.symbol
        or not amount.is_finite()
        or amount != scope.amount_usdt
    ):
        raise CanaryPermitBlocked("permit_command_scope_mismatch")


def _to_permit(row: CanaryCommandPermitRow) -> CanaryCommandPermit:
    return CanaryCommandPermit(
        permit_id=row.permit_id,
        halt_id=row.halt_id,
        scope=CanaryPermitScope(
            account_id=row.exchange_account_id,
            environment=row.deployment_environment,
            symbol=row.symbol,
            cell=row.cell,
            strategy=row.strategy,
            amount_usdt=Decimal(str(row.amount_usdt)),
        ),
        operator_id=row.operator_id,
        state=row.state,
        issued_at_ms=row.issued_at_ms,
        consumed_at_ms=row.consumed_at_ms,
        execution_decision_id=row.execution_decision_id,
        attempt_id=row.attempt_id,
    )
