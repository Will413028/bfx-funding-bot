"""Account-serialized release lifecycle. Caller owns the transaction, never IO."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.modules.execution.audit.tables import ExecutionDecisionRow
from bfx_funding_bot.modules.execution.canary_permit import (
    CanaryPermitBlocked,
    CanaryPermitRepository,
    CanaryPermitScope,
)
from bfx_funding_bot.modules.execution.release_tables import ReleaseAuditRow, ReleaseSessionRow
from bfx_funding_bot.modules.execution.safety.tables import TradingHaltRow
from bfx_funding_bot.modules.execution.safety.trading_state import TradingState, read_current


class ReleaseBlocked(RuntimeError):  # noqa: N818
    pass


class ReleasePromotionRequired(ReleaseBlocked):
    """This build has not been proven by a canary yet.

    A state, not a fault: the writer should stay halted and keep running, not
    stop. Distinguished from its parent because the worker treats an unexpected
    failure on an unhalted account as fatal, and that is the right posture for a
    genuine runtime or proof failure -- just not for a build awaiting its first
    verification.
    """


@dataclass(frozen=True)
class ReleaseCommand:
    session_id: UUID
    symbol: str
    cell: str
    strategy: str
    amount: Decimal
    # The ceiling the human authorised. A revision between authorisation and
    # submission may move the amount, but never past what they capped.
    max_amount: Decimal
    halt_authorization: object


def _within_authorised_amount(submitted: Decimal, row: ReleaseSessionRow) -> bool:
    """Accept the same revision the reconciler is allowed to make, and no more.

    The venue minimum is USD-denominated, so its UST equivalent drifts with FX
    between authorisation and submission. The reconciler may revise up to
    RELEASE_MINIMUM_TOLERANCE above the authorised amount and logs it; requiring
    exact equality here rejected precisely those submissions, so any FX movement
    at all -- which is to say, any real session -- died as
    `session_decision_mismatch` after the offer had already been priced. Both
    sides now read the same constant.
    """
    from bfx_funding_bot.modules.execution.deployment.reconciler import (
        RELEASE_MINIMUM_TOLERANCE,
    )

    if row.minimum_amount is None:
        return False
    ceiling = min(
        row.minimum_amount * (Decimal(1) + RELEASE_MINIMUM_TOLERANCE), row.max_amount
    )
    return Decimal(0) < submitted <= row.max_amount and row.minimum_amount <= submitted <= ceiling


class ReleaseSessions:
    def __init__(self, account_id: UUID, environment: str) -> None:
        self.account_id = account_id
        self.environment = environment

    async def lock(self, session: AsyncSession) -> None:
        await acquire_transaction_lock(session, account_id=str(self.account_id),
                                       deployment_environment=self.environment)

    async def get(self, session: AsyncSession, session_id: UUID) -> ReleaseSessionRow:
        row = await session.scalar(select(ReleaseSessionRow).where(
            ReleaseSessionRow.id == session_id,
            ReleaseSessionRow.exchange_account_id == self.account_id,
            ReleaseSessionRow.deployment_environment == self.environment,
        ).execution_options(populate_existing=True))
        if row is None:
            raise ReleaseBlocked("session_not_found")
        return row

    async def trading_state(self, session: AsyncSession) -> TradingState | None:
        """Whether new offers may be placed: the trading state, not the epoch."""
        return await read_current(session, account_id=self.account_id,
                                  environment=self.environment)

    async def halt(self, session: AsyncSession) -> TradingHaltRow:
        """The legacy halt row this ceremony binds its epochs to.

        Canary permits and release sessions carry a foreign key to
        ``trading_halt.id``; they keep that binding until the ceremony is
        removed. Whether trading is stopped is ``trading_state``.
        """
        row = await session.scalar(select(TradingHaltRow).where(
            TradingHaltRow.exchange_account_id == self.account_id,
            TradingHaltRow.deployment_environment == self.environment,
        ).order_by(TradingHaltRow.id.desc()).limit(1))
        if row is None:
            raise ReleaseBlocked("persistent_halt_missing")
        return row

    def audit(self, session: AsyncSession, row: ReleaseSessionRow, *, action: str,
              actor: str, now_ms: int, evidence: dict[str, Any] | None = None) -> None:
        session.add(ReleaseAuditRow(id=uuid4(), session_id=row.id, actor=actor,
            action=action, occurred_at_ms=now_ms, evidence=evidence or {}))

    async def request(self, session: AsyncSession, *, operator: str, symbol: str,
                      cell: str, strategy: str, max_amount: Decimal,
                      expires_at_ms: int, now_ms: int) -> ReleaseSessionRow:
        if (symbol != "fUST" or not cell.strip() or not strategy.strip() or not operator.strip()
            or not max_amount.is_finite() or max_amount <= 0 or expires_at_ms <= now_ms):
            raise ReleaseBlocked("invalid_session_scope_or_expiry")
        await self.lock(session)
        # Explicit column INSERT: ORM flush would send NULL for worker-only
        # nullable fields, violating the deliberately column-scoped webapi ACL.
        row = (await session.scalars(insert(ReleaseSessionRow).values(id=uuid4(), exchange_account_id=self.account_id,
            deployment_environment=self.environment, symbol=symbol, cell=cell,
            strategy=strategy, max_amount=max_amount, expires_at_ms=expires_at_ms,
            created_at_ms=now_ms, requested_by=operator).returning(ReleaseSessionRow))).one()
        self.audit(session, row, action="request:prepare", actor=operator, now_ms=now_ms)
        return row

    async def request_action(self, session: AsyncSession, session_id: UUID, *,
                             action: str, operator: str, expected_revision: int,
                             now_ms: int) -> ReleaseSessionRow:
        await self.lock(session)
        row = await self.get(session, session_id)
        allowed = {"authorize": {"prepared"}, "validate": {"consumed", "observed"},
                   "promote": {"validated"}}
        if (row.request_revision != expected_revision
            or row.processed_revision != row.request_revision
            or row.state not in allowed.get(action, set())):
            raise ReleaseBlocked("session_request_conflict")
        row.requested_action, row.requested_by = action, operator
        row.request_revision += 1
        self.audit(session, row, action="request:" + action, actor=operator, now_ms=now_ms)
        await session.flush()
        return row

    async def prepare(self, session: AsyncSession, session_id: UUID, *,
                      binding: dict[str, Any], halt_id: int, minimum_amount: Decimal,
                      now_ms: int) -> ReleaseSessionRow:
        await self.lock(session)
        row = await self.get(session, session_id)
        halt = await self.halt(session)
        if row.state != "requested" or not halt.halted or halt.id != halt_id:
            raise ReleaseBlocked("session_prepare_conflict")
        if not 0 < minimum_amount <= row.max_amount or now_ms >= row.expires_at_ms:
            raise ReleaseBlocked("session_minimum_or_expiry")
        row.binding, row.halt_id, row.minimum_amount = binding, halt_id, minimum_amount
        row.state, row.processed_revision = "prepared", row.request_revision
        self.audit(session, row, action="prepared", actor="worker", now_ms=now_ms,
                   evidence={"binding": binding, "halt_id": halt_id, "minimum_amount": str(minimum_amount)})
        await session.flush()
        return row

    async def check_current(self, session: AsyncSession, row: ReleaseSessionRow,
                            binding: dict[str, Any], *, now_ms: int, submit: bool) -> None:
        halt = await self.halt(session)
        if not halt.halted or halt.id != row.halt_id:
            raise ReleaseBlocked("session_halt_epoch_changed")
        if row.binding != binding:
            raise ReleaseBlocked("session_runtime_or_policy_changed")
        if submit and now_ms >= row.expires_at_ms:
            raise ReleaseBlocked("session_expired")

    async def authorize(self, session: AsyncSession, session_id: UUID, *,
                        binding: dict[str, Any], now_ms: int) -> ReleaseSessionRow:
        await self.lock(session)
        row = await self.get(session, session_id)
        await self.check_current(session, row, binding, now_ms=now_ms, submit=True)
        if row.state != "prepared" or row.requested_action != "authorize" or row.request_revision <= row.processed_revision:
            raise ReleaseBlocked("session_not_authorizable")
        row.state, row.authorized_by, row.authorized_at_ms = "authorized", row.requested_by, now_ms
        row.processed_revision = row.request_revision
        self.audit(session, row, action="authorized", actor=row.requested_by, now_ms=now_ms)
        await session.flush()
        return row

    async def consume(self, session: AsyncSession, session_id: UUID, *, binding: dict[str, Any],
                      decision: ExecutionDecisionRow, attempt_id: UUID, now_ms: int) -> ReleaseSessionRow:
        await self.lock(session)
        row = await self.get(session, session_id)
        if row.state != "authorized":
            raise ReleaseBlocked("permit_already_consumed")
        await self.check_current(session, row, binding, now_ms=now_ms, submit=True)
        actual = await session.get(ExecutionDecisionRow, decision.decision_id)
        if actual is None or (
            actual.outcome != "ready" or actual.exchange_account_id != self.account_id
            or actual.deployment_environment != self.environment or actual.symbol != row.symbol
            or actual.cell_id != row.cell or actual.strategy != row.strategy
            or not _within_authorised_amount(actual.amount_usdt, row)
            or actual.config_hash != binding.get("config_digest")
            or actual.service_version != binding.get("source_revision")
        ):
            raise ReleaseBlocked("session_decision_mismatch")
        if row.halt_id is None or row.authorized_by is None or row.authorized_at_ms is None:
            raise ReleaseBlocked("session_authorization_missing")
        try:
            permit = await CanaryPermitRepository.consume_bound(session,
                scope=CanaryPermitScope(self.account_id, self.environment, row.symbol, row.cell, row.strategy, actual.amount_usdt),
                halt_id=row.halt_id, operator_id=row.authorized_by, issued_at_ms=row.authorized_at_ms,
                now_ms=now_ms, decision_id=actual.decision_id)
        except CanaryPermitBlocked as exc:
            raise ReleaseBlocked(str(exc)) from exc
        row.state, row.consumed_at_ms, row.permit_id = "consumed", now_ms, permit.permit_id
        row.decision_id, row.attempt_id, row.exact_amount = actual.decision_id, attempt_id, actual.amount_usdt
        self.audit(session, row, action="consumed", actor="worker", now_ms=now_ms,
                   evidence={"decision_id": actual.decision_id, "attempt_id": str(attempt_id),
                             "amount": str(actual.amount_usdt)})
        await session.flush()
        return row
