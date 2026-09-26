"""Operator trading control: resume and kill requests, applied by the daemon.

Lending envelope ADR 2026-09-25 D4: the web API only inserts a ``resume`` or
``kill`` request (the operator-request outbox); the
:class:`TradingControlWorker` applies it under the account lock after
re-checking the operator, and records one outcome on the row. A resume ends a
HALTED (whoever caused it) and never starts a probation: every offer is bounded
by the CapitalPolicy envelope instead. A kill writes HALTED and, after that
commit, runs the venue cancel-all; it rejects every request still waiting to
resume, and every waiting currency enable (``capital_policy_requests``).
Releases never touch the trading state.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from sqlalchemy import case, update
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
from bfx_funding_bot.modules.execution.safety.tables import TradingControlRequestRow
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


class _Kill(Protocol):
    async def engage(self, *, cause: str, actor: str, reason: str,
                     when_already_halted: str = "retry") -> Any: ...


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
        return Outcome(APPLIED, note, columns={"trading_state_id": result.state.id},
                       detail=result)

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
                                      reason=f"kill: {row.reason}", when_already_halted="retry")

    async def _decide(self, session: AsyncSession,
                      row: TradingControlRequestRow) -> tuple[TransitionResult, str]:
        now = self.clock()
        if row.action == "kill":
            result = await append_transition(
                session, account_id=self.account_id, environment=self.environment, state=HALTED,
                cause=CAUSE_OPERATOR, actor=row.requested_by, reason=f"kill: {row.reason}",
                now_ms=now)
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
            now_ms=now)
        return result, "resumed"


__all__ = [
    "TRADING_CONTROL_APPLIED",
    "TRADING_CONTROL_FAILED",
    "TRADING_CONTROL_REJECTED",
    "TradingControlWorker",
]
