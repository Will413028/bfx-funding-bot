"""Durable trading state -- the authority on whether new offers may be placed.

Lending envelope ADR 2026-09-25 D4: two states.

- ``ACTIVE`` -- the writer trades inside the applied CapitalPolicy envelope;
- ``HALTED`` -- no new offer or re-post; cancels stay allowed. Entering it
  cancels offers at the venue (see ``kill_switch``): an operator kill cancels
  every offer of the currency, an automatic protection only managed ones.

The everyday per-currency stop is the policy's ``enabled`` flag, not this state.
Every transition is an appended row naming its ``cause`` (``operator`` or
``auto``), actor and reason, so "who stopped or resumed trading, when and why"
stays answerable. The current state is the highest id for the
account/environment. PostgreSQL's insert trigger assigns that id under a
per-scope lock and rejects the same illegal transitions as
:func:`validate_transition` (only an operator ends a halt); this module checks
them too so callers get the error before the database does.

Failure posture: callers must treat an unreadable state as "no new offers".
This module does not swallow exceptions; the guard decides, and fails closed.
Each call opens its own short session unless the caller passes one, because
the state is read on the submit path and must not hold a long-lived session.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.modules.execution.safety.tables import TradingStateRow
from bfx_funding_bot.modules.observability import alerts

ACTIVE: Final = "ACTIVE"
HALTED: Final = "HALTED"
STATES: Final = frozenset({ACTIVE, HALTED})

CAUSE_OPERATOR: Final = "operator"
CAUSE_AUTO: Final = "auto"
CAUSES: Final = frozenset({CAUSE_OPERATOR, CAUSE_AUTO})


class IllegalTradingTransition(ValueError):  # noqa: N818 - a rejected request, not a fault
    """The requested transition would weaken a stop or break the cause contract."""


@dataclass(frozen=True, slots=True)
class TradingState:
    id: int
    state: str
    cause: str
    actor: str
    reason: str
    created_at_ms: int
    legacy_halt_id: int | None = None

    @property
    def allows_new_offers(self) -> bool:
        return self.state == ACTIVE


@dataclass(frozen=True, slots=True)
class TransitionResult:
    state: TradingState
    # False when the request restated the current state and nothing was written.
    changed: bool
    previous: TradingState | None = None


def validate_transition(
    previous: TradingState | None, *, state: str, cause: str, actor: str, reason: str,
) -> None:
    """Raise when ``previous -> state`` is not a transition the system allows.

    The PostgreSQL insert trigger is the authority; this is the early error,
    kept in step by ``test_trading_state_rule_parity``. Code reads "no decision
    recorded" as HALTED, so the first transition it writes must be one a HALTED
    allows.
    """
    if state not in STATES:
        raise IllegalTradingTransition(f"unknown trading state {state!r}")
    if cause not in CAUSES:
        raise IllegalTradingTransition(f"unknown trading state cause {cause!r}")
    if not actor.strip() or not reason.strip():
        raise IllegalTradingTransition("a trading state transition needs an actor and a reason")
    previous_state = previous.state if previous is not None else HALTED
    if previous_state != ACTIVE and state == ACTIVE and cause != CAUSE_OPERATOR:
        raise IllegalTradingTransition(
            f"illegal trading state transition {previous_state} -> ACTIVE by {cause}")


def restates(previous: TradingState | None, *, state: str, cause: str) -> bool:
    """True when writing would only repeat the current decision.

    Reasserting a halt keeps the halt that is already in force -- its cause,
    actor and reason -- rather than stacking copies of it.
    """
    if previous is None or previous.state != state:
        return False
    return state == HALTED or previous.cause == cause


def to_state(row: TradingStateRow) -> TradingState:
    return TradingState(
        id=row.id, state=row.state, cause=row.cause, actor=row.actor, reason=row.reason,
        created_at_ms=row.created_at_ms, legacy_halt_id=row.legacy_halt_id,
    )


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


async def check_transition(
    *, current: TradingState | None, state: str, cause: str, actor: str, reason: str,
) -> None:
    """Refuse, before the database does, what its trigger would refuse.

    ``guard_trading_state_transition`` and the CHECKs are the authority; this
    only fails earlier with a clearer error. ``tests/integration/
    test_trading_state_rule_parity.py`` keeps the two in step.
    """
    validate_transition(current, state=state, cause=cause, actor=actor, reason=reason)


async def append_transition(
    session: AsyncSession, *, account_id: UUID, environment: str, state: str, cause: str,
    actor: str, reason: str, now_ms: int,
) -> TransitionResult:
    """Validate and append one transition inside the caller's transaction.

    The caller must already hold the account/environment transaction lock, so
    the state read here is the one the new row supersedes.
    """
    current = await read_current(session, account_id=account_id, environment=environment)
    if restates(current, state=state, cause=cause):
        assert current is not None
        return TransitionResult(state=current, changed=False, previous=current)
    await check_transition(current=current, state=state, cause=cause, actor=actor,
                           reason=reason)
    row = TradingStateRow(
        exchange_account_id=account_id,
        deployment_environment=environment,
        state=state,
        cause=cause,
        actor=actor,
        reason=reason,
        created_at_ms=now_ms,
    )
    session.add(row)
    await session.flush()
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
    ) -> None:
        if not deployment_environment.strip():
            raise ValueError("deployment_environment must be non-empty")
        self._sf = session_factory
        self.account_id = account_id if isinstance(account_id, UUID) else UUID(str(account_id))
        self.environment = deployment_environment

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
            await acquire_transaction_lock(
                session, account_id=str(self.account_id), deployment_environment=self.environment,
            )
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
    "ACTIVE",
    "CAUSES",
    "CAUSE_AUTO",
    "CAUSE_OPERATOR",
    "HALTED",
    "STATES",
    "IllegalTradingTransition",
    "TradingState",
    "TradingStateRepository",
    "TransitionResult",
    "announce",
    "append_transition",
    "check_transition",
    "read_current",
    "restates",
    "to_state",
    "validate_transition",
]
