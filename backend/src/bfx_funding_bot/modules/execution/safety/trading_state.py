"""Durable trading state -- the authority on whether new offers may be placed.

ADR 2026-09-25 D4 separates the trading state from releases:

- ``ACTIVE``   -- the writer trades normally (optionally inside a probation
  period, whose fields are reserved here for T5);
- ``REDUCING`` -- a pause: cancels are allowed, no new offer and no re-post;
- ``HALTED``   -- a stop: as REDUCING, and entering it cancels the venue's
  funding offers (see ``kill_switch``).

Every transition is an appended row naming its ``cause`` (``operator``,
``kill_switch``, ``auto``, ``material_deploy``), actor and reason, so "who
stopped or resumed trading, when and why" stays answerable. The current state
is the highest id for the account/environment. PostgreSQL's insert trigger
assigns that id under a per-scope lock and rejects the same illegal transitions
as :func:`validate_transition`; this module checks them too so SQLite fixtures
and callers get the error before the database does.

Failure posture: callers must treat an unreadable state as "no new offers".
This module does not swallow exceptions; the guard decides, and fails closed.
Each call opens its own short session unless the caller passes one, because
the state is read on the submit path and must not hold a long-lived session.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bfx_funding_bot.core.writer_lock import acquire_transaction_lock
from bfx_funding_bot.modules.execution.safety.tables import TradingStateRow
from bfx_funding_bot.modules.observability import alerts

ACTIVE: Final = "ACTIVE"
REDUCING: Final = "REDUCING"
HALTED: Final = "HALTED"

CAUSE_OPERATOR: Final = "operator"
CAUSE_KILL_SWITCH: Final = "kill_switch"
CAUSE_AUTO: Final = "auto"
CAUSE_MATERIAL_DEPLOY: Final = "material_deploy"

# Which causes may put the writer in which state. Mirrors ck_trading_state_cause.
CAUSES_BY_STATE: Final[dict[str, frozenset[str]]] = {
    ACTIVE: frozenset({CAUSE_OPERATOR, CAUSE_AUTO}),
    REDUCING: frozenset({CAUSE_OPERATOR, CAUSE_MATERIAL_DEPLOY}),
    HALTED: frozenset({CAUSE_OPERATOR, CAUSE_KILL_SWITCH, CAUSE_AUTO}),
}


class IllegalTradingTransition(ValueError):  # noqa: N818 - a rejected request, not a fault
    """The requested transition would weaken a stop or break the cause contract."""


@dataclass(frozen=True, slots=True)
class Probation:
    multiplier: Decimal
    started_at_ms: int


@dataclass(frozen=True, slots=True)
class TradingState:
    id: int
    state: str
    cause: str
    actor: str
    reason: str
    created_at_ms: int
    probation: Probation | None = None
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
    probation: Probation | None = None,
) -> None:
    """Raise when ``previous -> state`` is not a transition the system allows.

    The same rules are enforced by the PostgreSQL insert trigger; keep the two
    in step (``8e4f33517b10_add_trading_state``). One difference is deliberate:
    code reads "no decision recorded" as HALTED, so the first transition it
    writes must be one a HALTED allows; the trigger accepts any first row so
    the migration can carry an existing pause over.
    """
    allowed = CAUSES_BY_STATE.get(state)
    if allowed is None:
        raise IllegalTradingTransition(f"unknown trading state {state!r}")
    if cause not in allowed:
        raise IllegalTradingTransition(f"cause {cause!r} cannot put trading in {state}")
    if not actor.strip() or not reason.strip():
        raise IllegalTradingTransition("a trading state transition needs an actor and a reason")
    if probation is not None and (
        state != ACTIVE
        or not probation.multiplier.is_finite()
        or not Decimal(0) < probation.multiplier <= Decimal(1)
        or probation.started_at_ms < 0
    ):
        raise IllegalTradingTransition("probation applies only to ACTIVE, with 0 < multiplier <= 1")
    # No recorded decision is read as HALTED (fail closed), so it leaves only
    # the way a HALTED does.
    previous_state = previous.state if previous is not None else HALTED
    previous_cause = previous.cause if previous is not None else None
    if previous_state == HALTED and state == REDUCING:
        # A halt ends only in an operator's resume; a pause would let the
        # cheaper exit apply to a stop that was never proven safe to lift.
        raise IllegalTradingTransition("illegal trading state transition HALTED -> REDUCING")
    if previous_state != ACTIVE and state == ACTIVE and cause != CAUSE_OPERATOR:
        raise IllegalTradingTransition(
            f"illegal trading state transition {previous_state} -> ACTIVE by {cause}"
        )
    if (previous_state == REDUCING and previous_cause == CAUSE_MATERIAL_DEPLOY
            and state == REDUCING and cause != CAUSE_MATERIAL_DEPLOY):
        raise IllegalTradingTransition(
            "illegal trading state transition: material deploy approval cannot be relabelled"
        )


def restates(previous: TradingState | None, *, state: str, cause: str,
             probation: Probation | None) -> bool:
    """True when writing would only repeat the current decision.

    Reasserting a halt keeps the halt that is already in force -- its cause,
    actor and reason -- rather than stacking copies of it.
    """
    if previous is None or previous.state != state:
        return False
    if state == HALTED:
        return True
    return previous.cause == cause and previous.probation == probation


def to_state(row: TradingStateRow) -> TradingState:
    probation = (
        Probation(multiplier=Decimal(str(row.probation_multiplier)),
                  started_at_ms=int(row.probation_started_at_ms))
        if row.probation_multiplier is not None and row.probation_started_at_ms is not None
        else None
    )
    return TradingState(
        id=row.id, state=row.state, cause=row.cause, actor=row.actor, reason=row.reason,
        created_at_ms=row.created_at_ms, probation=probation, legacy_halt_id=row.legacy_halt_id,
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


async def append_transition(
    session: AsyncSession, *, account_id: UUID, environment: str, state: str, cause: str,
    actor: str, reason: str, now_ms: int, probation: Probation | None = None,
) -> TransitionResult:
    """Validate and append one transition inside the caller's transaction.

    The caller must already hold the account/environment transaction lock, so
    the state read here is the one the new row supersedes.
    """
    current = await read_current(session, account_id=account_id, environment=environment)
    if restates(current, state=state, cause=cause, probation=probation):
        assert current is not None
        return TransitionResult(state=current, changed=False, previous=current)
    validate_transition(current, state=state, cause=cause, actor=actor, reason=reason,
                        probation=probation)
    row = TradingStateRow(
        exchange_account_id=account_id,
        deployment_environment=environment,
        state=state,
        cause=cause,
        actor=actor,
        reason=reason,
        created_at_ms=now_ms,
        probation_multiplier=probation.multiplier if probation is not None else None,
        probation_started_at_ms=probation.started_at_ms if probation is not None else None,
    )
    session.add(row)
    await session.flush()
    return TransitionResult(state=to_state(row), changed=True, previous=current)


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
        now_ms: int | None = None, probation: Probation | None = None,
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
                probation=probation,
            )
        if result.changed:  # committed; alerting never blocks or raises (T8)
            alerts.emit(alerts.TRADING_STATE_CHANGED, state=state, cause=cause, actor=actor,
                        reason=reason, state_id=result.state.id,
                        previous=result.previous.state if result.previous else "none")
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
    "CAUSES_BY_STATE",
    "CAUSE_AUTO",
    "CAUSE_KILL_SWITCH",
    "CAUSE_MATERIAL_DEPLOY",
    "CAUSE_OPERATOR",
    "HALTED",
    "REDUCING",
    "IllegalTradingTransition",
    "Probation",
    "TradingState",
    "TradingStateRepository",
    "TransitionResult",
    "append_transition",
    "read_current",
    "restates",
    "to_state",
    "validate_transition",
]
