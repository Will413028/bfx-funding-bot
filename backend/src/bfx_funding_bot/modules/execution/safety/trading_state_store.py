"""Durable trading state: its reads, the transition writer and the change alert.

The model -- the two states, the causes, the transition rules and ``to_state`` -- is
``execution.safety.trading_state``, which holds no I/O so the control plane may import it.
This module reads and appends the rows (PostgreSQL's trigger assigns the id under a
per-scope lock and rejects the same illegal transitions) and announces a change. Only
the account daemon and the operator tools use it (``control-plane-exposure``,
ADR 2026-10-19 D6). The failure posture is the model's: an unreadable state means no
new offers.
"""

from __future__ import annotations

import time
from typing import Final
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.modules.execution.safety.tables import TradingStateRow
from bfx_funding_bot.modules.execution.safety.trading_state import (
    ACTIVE,
    AUTO_RESUME_WINDOW_MS,
    CAUSE_AUTO,
    AutoResumeLimitReached,
    AutoResumeTooSoon,
    IllegalTradingTransition,
    TradingState,
    TransitionResult,
    restates,
    to_state,
    validate_transition,
)
from bfx_funding_bot.modules.ledger import Scope, ScopeLock
from bfx_funding_bot.modules.observability import alerts

# The trigger's SQLSTATEs (migration 8e4b2f6a1c37), mapped back to these types. BX004, a
# writer not on READ COMMITTED (2e835b6f4c12), stays unmapped: it is a fault, not a rejection.
_SQLSTATE_ERRORS: Final[dict[str, type[IllegalTradingTransition]]] = {
    "BX001": IllegalTradingTransition,
    "BX002": AutoResumeTooSoon,
    "BX003": AutoResumeLimitReached,
}


def _sqlstate(exc: BaseException) -> str | None:
    """The SQLSTATE a database error carries, through SQLAlchemy's and the driver's wrapping."""
    seen: BaseException | None = exc
    while seen is not None:
        code = getattr(seen, "sqlstate", None) or getattr(seen, "pgcode", None)
        if isinstance(code, str):
            return code
        seen = getattr(seen, "orig", None) or seen.__cause__
    return None


def _scope(stmt: Select[tuple[TradingStateRow]], *, account_id: UUID,
           environment: str) -> Select[tuple[TradingStateRow]]:
    return stmt.where(
        TradingStateRow.exchange_account_id == account_id,
        TradingStateRow.deployment_environment == environment,
    )


async def read_current(session: AsyncSession, *, account_id: UUID,
                       environment: str) -> TradingState | None:
    """Latest decision for the scope, or None if none was ever recorded."""
    row = await session.scalar(
        _scope(select(TradingStateRow), account_id=account_id, environment=environment)
        .order_by(TradingStateRow.id.desc())
        .limit(1)
    )
    return to_state(row) if row is not None else None


async def count_auto_resumes(session: AsyncSession, *, account_id: UUID, environment: str,
                             now_ms: int) -> int:
    """``ACTIVE/auto`` rows inside the rolling window ending at ``now_ms``."""
    count = await session.scalar(
        select(func.count()).select_from(TradingStateRow).where(
            TradingStateRow.exchange_account_id == account_id,
            TradingStateRow.deployment_environment == environment,
            TradingStateRow.state == ACTIVE, TradingStateRow.cause == CAUSE_AUTO,
            TradingStateRow.created_at_ms > now_ms - AUTO_RESUME_WINDOW_MS)
    )
    return count or 0


async def check_transition(
    *, current: TradingState | None, state: str, cause: str, actor: str, reason: str,
    now_ms: int = 0, auto_resumes_in_window: int = 0,
) -> None:
    """Refuse, before the database does, what its trigger would refuse.

    ``guard_trading_state_transition`` and the CHECKs are the authority; this
    only fails earlier with a clearer error. ``tests/integration/
    test_trading_state_rule_parity.py`` keeps the two in step.
    """
    validate_transition(current, state=state, cause=cause, actor=actor, reason=reason,
                        now_ms=now_ms, auto_resumes_in_window=auto_resumes_in_window)


async def append_transition(
    session: AsyncSession, *, account_id: UUID, environment: str, state: str, cause: str,
    actor: str, reason: str, now_ms: int, operator_request_id: UUID | None = None,
) -> TransitionResult:
    """Validate and append one transition inside the caller's transaction.

    The caller must already hold the account/environment transaction lock, so
    the state read here is the one the new row supersedes. ``operator_request_id``
    names the operator request this transition applies (``trading_control_requests``,
    same scope); a restated decision writes nothing and so names nothing.
    """
    current = await read_current(session, account_id=account_id, environment=environment)
    if restates(current, state=state, cause=cause):
        assert current is not None
        return TransitionResult(state=current, changed=False, previous=current)
    resumes = 0
    if state == ACTIVE and cause == CAUSE_AUTO:
        resumes = await count_auto_resumes(session, account_id=account_id,
                                           environment=environment, now_ms=now_ms)
    await check_transition(current=current, state=state, cause=cause, actor=actor,
                           reason=reason, now_ms=now_ms, auto_resumes_in_window=resumes)
    row = TradingStateRow(
        exchange_account_id=account_id,
        deployment_environment=environment,
        state=state,
        cause=cause,
        actor=actor,
        reason=reason,
        created_at_ms=now_ms,
        operator_request_id=operator_request_id,
    )
    session.add(row)
    try:
        await session.flush()
    except Exception as exc:
        mapped = _SQLSTATE_ERRORS.get(_sqlstate(exc) or "")
        if mapped is None:
            raise
        raise mapped(str(getattr(exc, "orig", exc))) from exc
    if cause == CAUSE_AUTO:
        # The trigger stamps automatic rows with the database clock.
        await session.refresh(row, ["created_at_ms"])
    return TransitionResult(state=to_state(row), changed=True, previous=current)


def announce(result: TransitionResult) -> None:
    """Alert a committed transition. Call after commit; never blocks or raises (T8)."""
    if not result.changed:
        return
    state = result.state
    alerts.emit(alerts.TRADING_STATE_CHANGED, state=state.state, cause=state.cause,
                actor=state.actor, reason=state.reason, state_id=state.id,
                previous=result.previous.state if result.previous else "none")


class TradingStateRepository:
    """Scope-bound reads and writes of the trading state for one account/environment."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        account_id: str | UUID,
        deployment_environment: str,
        scope_lock: ScopeLock,
    ) -> None:
        if not deployment_environment.strip():
            raise ValueError("deployment_environment must be non-empty")
        self._sf = session_factory
        self.account_id = account_id if isinstance(account_id, UUID) else UUID(str(account_id))
        self.environment = deployment_environment
        self._scope = Scope(self.account_id)
        # The ledger's scope lock: transitions serialise with every other money-state
        # write of the scope, and nothing outside the ledger takes the primitive (D4).
        self._scope_lock = scope_lock

    async def current(self, session: AsyncSession | None = None) -> TradingState | None:
        """Latest decision, or None if none was ever recorded.

        None is not a decision, and every trading check reads it as HALTED
        (fail closed): a scope nobody has decided about does not trade. The
        status report still shows it as "never recorded" so the two stay
        distinguishable to an operator.
        """
        if session is not None:
            return await read_current(session, account_id=self.account_id,
                                      environment=self.environment)
        async with self._sf() as owned:
            return await read_current(owned, account_id=self.account_id,
                                      environment=self.environment)

    async def transition(
        self, state: str, *, cause: str, actor: str, reason: str,
        now_ms: int | None = None,
    ) -> TransitionResult:
        """Append one transition in its own transaction, serialised per scope."""
        async with self._sf.begin() as session:
            await self._scope_lock.lock(session, self._scope)
            result = await append_transition(
                session, account_id=self.account_id, environment=self.environment,
                state=state, cause=cause, actor=actor, reason=reason,
                now_ms=now_ms if now_ms is not None else int(time.time() * 1000),
            )
        announce(result)
        return result

    async def history(self, *, limit: int = 20) -> list[TradingState]:
        async with self._sf() as session:
            rows = (await session.scalars(
                _scope(select(TradingStateRow), account_id=self.account_id,
                       environment=self.environment)
                .order_by(TradingStateRow.id.desc())
                .limit(limit)
            )).all()
        return [to_state(row) for row in rows]


__all__ = [
    "TradingStateRepository",
    "announce",
    "append_transition",
    "check_transition",
    "count_auto_resumes",
    "read_current",
]
