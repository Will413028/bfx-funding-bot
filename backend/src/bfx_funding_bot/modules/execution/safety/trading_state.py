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
:func:`validate_transition`; this module checks them too so callers get the
error before the database does.

Who ends a halt (ADR 2026-09-26 auto-halt-resumes-when-condition-clears): an
operator ends any halt; an automatic protection ends only its own ``HALTED/auto``,
no sooner than :data:`AUTO_RESUME_MIN_HALT_MS` after it and at most
:data:`AUTO_RESUME_MAX_PER_WINDOW` times per :data:`AUTO_RESUME_WINDOW_MS`. An
operator's halt is never restated away by an automatic one, and always
supersedes one, so an operator kill is never lifted automatically.

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

from sqlalchemy import Select, func, select
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

# Mirrored by guard_trading_state_transition (migration 8e4b2f6a1c37).
AUTO_RESUME_MIN_HALT_MS: Final = 15 * 60 * 1000
AUTO_RESUME_WINDOW_MS: Final = 24 * 60 * 60 * 1000
AUTO_RESUME_MAX_PER_WINDOW: Final = 2


class IllegalTradingTransition(ValueError):  # noqa: N818 - a rejected request, not a fault
    """The requested transition would weaken a stop or break the cause contract."""


class AutoResumeTooSoon(IllegalTradingTransition):
    """An automatic resume before ``AUTO_RESUME_MIN_HALT_MS`` (SQLSTATE BX002)."""


class AutoResumeLimitReached(IllegalTradingTransition):
    """An automatic resume past the rolling limit (SQLSTATE BX003)."""


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
    now_ms: int = 0, auto_resumes_in_window: int = 0,
) -> None:
    """Raise when ``previous -> state`` is not a transition the system allows.

    The PostgreSQL insert trigger is the authority; this is the early error,
    kept in step by ``test_trading_state_rule_parity``. Code reads "no decision
    recorded" as HALTED, so the first transition it writes must be one a HALTED
    allows. ``auto_resumes_in_window`` counts ``ACTIVE/auto`` rows newer than
    ``now_ms - AUTO_RESUME_WINDOW_MS``.
    """
    if state not in STATES:
        raise IllegalTradingTransition(f"unknown trading state {state!r}")
    if cause not in CAUSES:
        raise IllegalTradingTransition(f"unknown trading state cause {cause!r}")
    if not actor.strip() or not reason.strip():
        raise IllegalTradingTransition("a trading state transition needs an actor and a reason")
    if state != ACTIVE or cause == CAUSE_OPERATOR:
        return
    if previous is None or previous.state != HALTED or previous.cause != CAUSE_AUTO:
        previous_label = "none" if previous is None else f"{previous.state}/{previous.cause}"
        raise IllegalTradingTransition(
            f"illegal trading state transition {previous_label} -> ACTIVE by {cause}")
    if now_ms - previous.created_at_ms < AUTO_RESUME_MIN_HALT_MS:
        raise AutoResumeTooSoon("auto resume before the minimum halt duration")
    if auto_resumes_in_window >= AUTO_RESUME_MAX_PER_WINDOW:
        raise AutoResumeLimitReached("auto resume limit reached for the window")


def restates(previous: TradingState | None, *, state: str, cause: str) -> bool:
    """True when writing would only repeat the current decision.

    Reasserting a halt keeps the halt that is already in force -- its cause,
    actor and reason -- rather than stacking copies of it. The exception is an
    operator halt over an automatic one: it is written, because only the
    automatic one may be lifted automatically.
    """
    if previous is None or previous.state != state:
        return False
    if state == HALTED:
        return not (cause == CAUSE_OPERATOR and previous.cause == CAUSE_AUTO)
    return previous.cause == cause


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
    "AUTO_RESUME_MAX_PER_WINDOW",
    "AUTO_RESUME_MIN_HALT_MS",
    "AUTO_RESUME_WINDOW_MS",
    "CAUSES",
    "CAUSE_AUTO",
    "CAUSE_OPERATOR",
    "HALTED",
    "STATES",
    "AutoResumeLimitReached",
    "AutoResumeTooSoon",
    "IllegalTradingTransition",
    "TradingState",
    "TradingStateRepository",
    "TransitionResult",
    "announce",
    "append_transition",
    "check_transition",
    "count_auto_resumes",
    "read_current",
    "restates",
    "to_state",
    "validate_transition",
]
