"""Operator trading control: resume and kill requests, applied by the daemon.

Lending envelope ADR 2026-09-25 D4: the web API only inserts a ``resume`` or
``kill`` request (the operator-request outbox); the
:class:`TradingControlWorker` applies it under the account lock after
re-checking the operator, and records one outcome on the row; the trading
state it writes and the kill's cancel-all audit name the request
(``operator_request_id``). A resume ends a
HALTED (whoever caused it) and never starts a probation: every offer is bounded
by the CapitalPolicy envelope instead. A kill writes HALTED and, after that
commit, runs the venue cancel-all; it rejects every request still waiting to
resume, and every waiting currency enable (``capital_policy_requests``).
Releases never touch the trading state.

The cancel-all runs after the commit, in this process, so a crash or a stop
there (or a failure before its first audit row) would leave a kill whose venue
half never ran: the planner pulls only managed offers it may cancel, never a
hand-placed or auto-renewed one, nor one in an uncertain scope. The durable
rows are the work item: while that kill's HALTED is still in force, an idle
worker sees no finished cancel-all since it and runs it (:meth:`idle`).
"""
from __future__ import annotations

import logging
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.modules.execution.capital_tables import CapitalPolicyRequestRow
from bfx_funding_bot.modules.execution.operator_requests import (
    APPLIED,
    REJECTED,
    REQUESTED,
    OperatorRequestWorker,
    Outcome,
    RequestRejected,
)
from bfx_funding_bot.modules.execution.safety.tables import (
    FundingCancelAllAuditRow,
    TradingControlRequestRow,
    TradingStateRow,
)
from bfx_funding_bot.modules.execution.safety.trading_state import (
    ACTIVE,
    CAUSE_OPERATOR,
    HALTED,
    IllegalTradingTransition,
    TransitionResult,
    announce,
    append_transition,
    read_current,
)
from bfx_funding_bot.modules.observability import alerts

log = logging.getLogger(__name__)

# Alert events (modules/observability/alerts); trading_state adds
# trading_state_changed for every committed transition.
TRADING_CONTROL_APPLIED = "trading_control_applied"
TRADING_CONTROL_REJECTED = "trading_control_rejected"
TRADING_CONTROL_FAILED = "trading_control_failed"
# An applied kill had no finished cancel-all (the process died or failed after the
# commit); the idle worker runs it now.
KILL_CANCEL_ALL_CAUGHT_UP = "kill_cancel_all_caught_up"


class _Kill(Protocol):
    async def engage(self, *, cause: str, actor: str, reason: str,
                     when_already_halted: str = "retry",
                     operator_request_id: UUID | None = None) -> Any: ...

    async def currencies(self) -> tuple[tuple[str, ...], str | None]: ...


class TradingControlWorker(OperatorRequestWorker[TradingControlRequestRow, None]):
    """Applies resume/kill requests.

    A kill writes HALTED (cause operator) in the request's transaction, then --
    after commit, outside every lock -- runs the kill switch, which repeats
    nothing but the venue cancel-all (audited and alerted like /admin/halt).
    Asking again retries an incomplete cancel-all, as /admin/halt does.
    """

    model = TradingControlRequestRow
    name = "trading_control"
    # Bound by the daemon once the kill switch exists (it is built later).
    kill_switch: _Kill | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Kills whose cancel-all this process already caught up: once per
        # process, so an audit that cannot be written never loops the venue.
        self._caught_up: set[UUID] = set()

    def queue_order(self) -> tuple[Any, ...]:
        """A kill never waits behind another request."""
        return (case((TradingControlRequestRow.action == "kill", 0), else_=1),
                TradingControlRequestRow.created_at_ms, TradingControlRequestRow.request_id)

    async def apply(self, session: AsyncSession, row: TradingControlRequestRow,
                    prepared: None) -> Outcome:
        try:
            result, note = await self._decide(session, row)
        except IllegalTradingTransition as exc:
            raise RequestRejected(f"illegal_transition: {exc}") from exc
        return Outcome(APPLIED, note, detail=result)

    async def idle(self) -> None:
        """Run the cancel-all of a kill whose venue half never finished."""
        if self.kill_switch is None:
            return
        row = await self._kill_without_cancel_all()
        if row is None or row.request_id in self._caught_up:
            return
        self._caught_up.add(row.request_id)
        log.critical("kill_cancel_all_caught_up request=%s", row.request_id)
        alerts.emit(KILL_CANCEL_ALL_CAUGHT_UP, level=alerts.CRITICAL,
                    request_id=str(row.request_id), by=row.requested_by)
        await self._cancel_all(row)

    async def _kill_without_cancel_all(self) -> TradingControlRequestRow | None:
        """The latest applied kill, if its HALTED is in force and no cancel-all finished since.

        In force: the account is HALTED and no ACTIVE row follows the HALTED the
        kill wrote, or restated (a later HALTED is then someone else's stop).
        Finished: since the kill, every currency a cancel-all covers now has a
        terminal audit row last, whoever ran it (/admin/halt counts); a
        ``requested`` row left last means the venue call was cut off.
        """
        assert self.kill_switch is not None
        async with self.session_factory() as session:
            current = await read_current(session, account_id=self.account_id,
                                         environment=self.environment)
            # Fast path only: an ACTIVE account also fails the resume check below.
            if current is None or current.state != HALTED:
                return None
            kill: TradingControlRequestRow | None = await session.scalar(self._scoped(
                select(TradingControlRequestRow)).where(
                TradingControlRequestRow.action == "kill",
                TradingControlRequestRow.state == APPLIED,
            ).order_by(TradingControlRequestRow.processed_at_ms.desc(),
                       TradingControlRequestRow.request_id.desc()).limit(1))
            if kill is None or kill.processed_at_ms is None:
                return None
            states = select(TradingStateRow.id).where(
                TradingStateRow.exchange_account_id == self.account_id,
                TradingStateRow.deployment_environment == self.environment)
            # Ordered by id, not time: a resume may share the kill's millisecond.
            halted_id = await session.scalar(states.where(
                TradingStateRow.operator_request_id == kill.request_id))
            if halted_id is None:  # a restated kill: the HALTED it found in force
                halted_id = await session.scalar(states.where(
                    TradingStateRow.state == HALTED,
                    TradingStateRow.created_at_ms <= kill.processed_at_ms,
                ).order_by(TradingStateRow.id.desc()).limit(1))
            resumed = await session.scalar(states.where(
                TradingStateRow.state == ACTIVE,
                TradingStateRow.id > (halted_id or 0),
            ).limit(1))
            if resumed is not None:
                return None
            audit = (await session.execute(select(
                FundingCancelAllAuditRow.currency, FundingCancelAllAuditRow.phase,
            ).where(
                FundingCancelAllAuditRow.exchange_account_id == self.account_id,
                FundingCancelAllAuditRow.deployment_environment == self.environment,
                FundingCancelAllAuditRow.occurred_at_ms >= kill.processed_at_ms,
            ).order_by(FundingCancelAllAuditRow.id))).tuples().all()
        last: dict[str, str] = dict(audit)  # each currency's latest phase
        if "requested" in last.values():
            return kill
        covered, _ = await self.kill_switch.currencies()
        return kill if set(covered) - last.keys() else None

    async def committed(self, row: TradingControlRequestRow, outcome: Outcome) -> None:
        """Tell the operator, then finish a kill at the venue (outside every lock)."""
        fields = {"request_id": str(row.request_id), "action": row.action, "by": row.requested_by}
        if outcome.state == APPLIED:
            result = outcome.detail if isinstance(outcome.detail, TransitionResult) else None
            alerts.emit(TRADING_CONTROL_APPLIED, level=alerts.WARNING, outcome=outcome.reason,
                        state_id=result.state.id if result is not None else "unchanged", **fields)
            if result is not None:
                announce(result)
            if row.action == "kill":
                await self._cancel_all(row)
        elif outcome.state == REJECTED:
            alerts.emit(TRADING_CONTROL_REJECTED, level=alerts.WARNING, reason=outcome.reason,
                        **fields)
        else:
            alerts.emit(TRADING_CONTROL_FAILED, level=alerts.CRITICAL, reason=outcome.reason,
                        **fields)

    async def _cancel_all(self, row: TradingControlRequestRow) -> None:
        if self.kill_switch is None:
            log.critical("trading_control_kill_without_kill_switch request=%s", row.request_id)
            alerts.emit(TRADING_CONTROL_FAILED, level=alerts.CRITICAL, request_id=str(row.request_id),
                        action=row.action, reason="kill switch not wired: venue offers not cancelled")
            return
        # HALTED is already committed; the kill switch restates it and runs
        # the venue cancel-all, recording and alerting its outcome.
        await self.kill_switch.engage(cause=CAUSE_OPERATOR, actor=row.requested_by,
                                      reason=f"kill: {row.reason}", when_already_halted="retry",
                                      operator_request_id=row.request_id)

    async def _decide(self, session: AsyncSession,
                      row: TradingControlRequestRow) -> tuple[TransitionResult, str]:
        now = self.clock()
        if row.action == "kill":
            result = await append_transition(
                session, account_id=self.account_id, environment=self.environment, state=HALTED,
                cause=CAUSE_OPERATOR, actor=row.requested_by, reason=f"kill: {row.reason}",
                now_ms=now, operator_request_id=row.request_id)
            # Whatever else was waiting was asked before the stop; none of it
            # may undo the stop after it (a queued resume above all).
            superseded = (await session.execute(
                update(TradingControlRequestRow).where(
                    TradingControlRequestRow.exchange_account_id == self.account_id,
                    TradingControlRequestRow.deployment_environment == self.environment,
                    TradingControlRequestRow.state == REQUESTED,
                    TradingControlRequestRow.request_id != row.request_id,
                ).values(state=REJECTED, processed_at_ms=now, outcome_reason="superseded_by_kill")
                .returning(TradingControlRequestRow.request_id))).scalars().all()
            # A waiting enable would widen what trading does after the resume;
            # the operator re-decides it. A waiting disable only narrows it and
            # still applies.
            superseded = [*superseded, *(await session.execute(
                update(CapitalPolicyRequestRow).where(
                    CapitalPolicyRequestRow.exchange_account_id == self.account_id,
                    CapitalPolicyRequestRow.deployment_environment == self.environment,
                    CapitalPolicyRequestRow.state == REQUESTED,
                    CapitalPolicyRequestRow.action == "enable",
                ).values(state=REJECTED, processed_at_ms=now, outcome_reason="superseded_by_kill")
                .returning(CapitalPolicyRequestRow.request_id))).scalars().all()]
            if superseded:
                log.warning("trading_control_superseded_by_kill kill=%s superseded=%s",
                            row.request_id, [str(r) for r in superseded])
            return result, "HALTED; venue cancel-all follows (funding_cancel_all_audit)"
        current = await read_current(session, account_id=self.account_id,
                                     environment=self.environment)
        if current is not None and current.state == ACTIVE:
            raise RequestRejected("already_active")
        result = await append_transition(
            session, account_id=self.account_id, environment=self.environment, state=ACTIVE,
            cause=CAUSE_OPERATOR, actor=row.requested_by, reason=f"resumed: {row.reason}",
            now_ms=now, operator_request_id=row.request_id)
        return result, "resumed"


__all__ = [
    "KILL_CANCEL_ALL_CAUGHT_UP",
    "TRADING_CONTROL_APPLIED",
    "TRADING_CONTROL_FAILED",
    "TRADING_CONTROL_REJECTED",
    "TradingControlWorker",
]
