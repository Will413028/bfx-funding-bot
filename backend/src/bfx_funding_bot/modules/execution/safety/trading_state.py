"""Durable trading state -- the authority on whether new offers may be placed.

ADR 2026-09-25 D4 separates the trading state from releases:

- ``ACTIVE``   -- the writer trades normally, or inside a probation (reduced
  limits, ADR D3) until one passes;
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
from dataclasses import dataclass, replace
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

PROBATION_STARTED: Final = "probation_started"
PROBATION_LIFTED: Final = "probation_lifted"

# Which causes may put the writer in which state. Mirrors ck_trading_state_cause.
CAUSES_BY_STATE: Final[dict[str, frozenset[str]]] = {
    ACTIVE: frozenset({CAUSE_OPERATOR, CAUSE_AUTO}),
    REDUCING: frozenset({CAUSE_OPERATOR, CAUSE_MATERIAL_DEPLOY}),
    HALTED: frozenset({CAUSE_OPERATOR, CAUSE_KILL_SWITCH, CAUSE_AUTO}),
}


class IllegalTradingTransition(ValueError):  # noqa: N818 - a rejected request, not a fault
    """The requested transition would weaken a stop or break the cause contract."""


class NotAnOperatorPause(ValueError):  # noqa: N818 - a rejected request, not a fault
    """Only an operator's pause is lifted by :meth:`TradingStateRepository.resume_pause`."""


@dataclass(frozen=True, slots=True)
class Probation:
    """Reduced limits after an approval or an automatic halt (ADR D3).

    ``floor`` is the per-currency venue minimum (native units, submit margin
    included) observed when the probation started: the probation cell limit is
    never below one minimum offer. Kept as sorted pairs so the value is
    hashable and compares by content.
    """

    multiplier: Decimal
    started_at_ms: int
    floor: tuple[tuple[str, Decimal], ...] = ()

    def floor_for(self, symbol: str) -> Decimal:
        return dict(self.floor).get(symbol, Decimal(0))

    @classmethod
    def starting(cls, *, multiplier: Decimal, started_at_ms: int,
                 floor: dict[str, Decimal]) -> Probation:
        return cls(multiplier=multiplier, started_at_ms=started_at_ms,
                   floor=tuple(sorted(floor.items())))

    def restarted(self, *, started_at_ms: int) -> Probation:
        """The same limits, counted again from ``started_at_ms``."""
        return replace(self, started_at_ms=started_at_ms)


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
        or any(not amount.is_finite() or amount < 0 for _, amount in probation.floor)
    ):
        raise IllegalTradingTransition(
            "probation applies only to ACTIVE, with 0 < multiplier <= 1 and non-negative floors")
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
                  started_at_ms=int(row.probation_started_at_ms),
                  floor=tuple(sorted((str(symbol), Decimal(str(amount)))
                                     for symbol, amount in (row.probation_floor or {}).items())))
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


async def unfinished_probation(session: AsyncSession, *, account_id: UUID,
                               environment: str) -> Probation | None:
    """The latest probation that no lift has ended, or None.

    ADR D3 invariant: until a probation passes, exposure stays within it. A
    pause, an operator's stop or a standard deploy does not end one; only the
    lift -- an ACTIVE without a probation, written from inside it -- does.
    """
    started = await session.scalar(
        _scope(select(TradingStateRow), account_id=account_id, environment=environment)
        .where(TradingStateRow.probation_multiplier.is_not(None))
        .order_by(TradingStateRow.id.desc())
        .limit(1)
    )
    if started is None:
        return None
    lifted = await session.scalar(
        _scope(select(TradingStateRow), account_id=account_id, environment=environment)
        .where(TradingStateRow.id > started.id, TradingStateRow.state == ACTIVE,
               TradingStateRow.probation_multiplier.is_(None))
        .limit(1)
    )
    return None if lifted is not None else to_state(started).probation


def _is_lift(current: TradingState | None, *, state: str, cause: str,
             probation: Probation | None) -> bool:
    return (current is not None and current.state == ACTIVE and current.probation is not None
            and state == ACTIVE and cause == CAUSE_AUTO and probation is None)


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
    if (state == ACTIVE and probation is None
            and not _is_lift(current, state=state, cause=cause, probation=probation)
            and await unfinished_probation(session, account_id=account_id,
                                           environment=environment) is not None):
        # Needs the history, not only the previous row; the insert trigger
        # guard_trading_state_probation (0218f9ab59a2) enforces the same.
        raise IllegalTradingTransition(
            "illegal trading state transition: a probation has not passed, so ACTIVE must "
            "stay inside it (ADR D3)")
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
        probation_floor=(
            {symbol: str(amount) for symbol, amount in probation.floor}
            if probation is not None else None
        ),
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
    if state.probation is not None:
        alerts.emit(PROBATION_STARTED, level=alerts.WARNING, state_id=state.id,
                    multiplier=str(state.probation.multiplier),
                    floor={symbol: str(amount) for symbol, amount in state.probation.floor},
                    started_at_ms=state.probation.started_at_ms)


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
        announce(result)
        return result

    async def resume_pause(self, *, actor: str, reason: str,
                           now_ms: int | None = None) -> TransitionResult:
        """Lift an operator's pause, back inside an unfinished probation if any.

        Checked under the scope lock, so a stop recorded after the caller last
        read the state is never lifted by mistake. ACTIVE already: nothing to do.
        """
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        async with self._sf.begin() as session:
            await acquire_transaction_lock(
                session, account_id=str(self.account_id), deployment_environment=self.environment,
            )
            current = await read_current(session, account_id=self.account_id,
                                         environment=self.environment)
            if current is not None and current.state == ACTIVE:
                return TransitionResult(state=current, changed=False, previous=current)
            if current is None or current.state != REDUCING or current.cause != CAUSE_OPERATOR:
                raise NotAnOperatorPause(
                    "not an operator's pause" if current is None
                    else f"{current.state} by {current.cause} is not an operator's pause")
            owed = await unfinished_probation(session, account_id=self.account_id,
                                              environment=self.environment)
            result = await append_transition(
                session, account_id=self.account_id, environment=self.environment,
                state=ACTIVE, cause=CAUSE_OPERATOR, actor=actor, reason=reason, now_ms=now,
                probation=owed.restarted(started_at_ms=now) if owed is not None else None,
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
    "CAUSES_BY_STATE",
    "CAUSE_AUTO",
    "CAUSE_KILL_SWITCH",
    "CAUSE_MATERIAL_DEPLOY",
    "CAUSE_OPERATOR",
    "HALTED",
    "PROBATION_LIFTED",
    "PROBATION_STARTED",
    "REDUCING",
    "IllegalTradingTransition",
    "NotAnOperatorPause",
    "Probation",
    "TradingState",
    "TradingStateRepository",
    "TransitionResult",
    "announce",
    "append_transition",
    "read_current",
    "restates",
    "to_state",
    "unfinished_probation",
    "validate_transition",
]
