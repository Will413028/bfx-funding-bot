"""EchoPaperExecutor — synchronous echo (submit → order_submit + order_fill same tick).

No latency / partial fill / slippage modeling. Spec section 7 (paper fidelity):
4.4 first canary reconciliation produces empirical distribution → 4.5+ optional
LatencyModel / PartialFill adapters as new ExecutorPort implementations.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any, Protocol

from bfx_funding_bot.external.bitfinex.cid import generate_cid
from bfx_funding_bot.modules.execution.contracts import ReadyToSubmit, ReservationRef
from bfx_funding_bot.modules.execution.emit import (
    emit_order_fill,
    emit_order_submit,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    Phase,
    StrategyName,
)


class _EventSink(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


def _default_date() -> date:
    """Production default: UTC date. Tests inject via date_provider for determinism."""
    return datetime.now(UTC).date()


class EchoPaperExecutor:
    """Paper executor: emits order_submit + order_fill at decision price/size."""

    def __init__(
        self,
        *,
        event_sink: _EventSink,
        phase: Phase,
        strategy: StrategyName,
        cell: str,
        date_provider: Callable[[], date] | None = None,
    ) -> None:
        self._events = event_sink
        self.phase = phase
        self.strategy = strategy
        self.cell = cell
        # date_provider DI seam: prod uses UTC date; tests inject for determinism.
        self._date_provider = date_provider or _default_date

    async def submit(
        self, ready: ReadyToSubmit, ctx: AccountContext, *, cid: int | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> SubmittedOrder:
        decision = ready.decision
        # cid is centralized by ReservationEmittingMiddleware (A2). Direct callers
        # (tests) omit it -> fall back to deterministic generation. CC2: capture
        # date once at submit entry (midnight-race immune).
        if cid is None:
            submit_date = self._date_provider()
            cid = generate_cid(decision.signal_correlation_id, submit_date)
        reference = reservation_ref or ReservationRef(
            execution_decision_id=ready.decision_id,
            cid=cid,
            signal_correlation_id=decision.signal_correlation_id,
        )
        if (
            reference.execution_decision_id != ready.decision_id
            or reference.cid != cid
            or reference.signal_correlation_id != decision.signal_correlation_id
            or reference.venue_offer_id is not None
        ):
            raise ValueError("reservation_ref conflicts with ReadyToSubmit request")
        # CC4: "paper_" prefix is invariant relied on by fill_tracker to skip
        # venue polling for simulated offers.
        offer_id = f"paper_{uuid.uuid4().hex[:12]}"

        await emit_order_submit(
            event_sink=self._events,
            phase=self.phase, strategy=self.strategy, cell=self.cell,
            ready=ready, ctx=ctx,
            cid=cid, offer_id=offer_id,
            is_simulated=True, status="submitted",
        )
        await emit_order_fill(
            event_sink=self._events,
            phase=self.phase, strategy=self.strategy, cell=self.cell,
            decision=decision, ctx=ctx,
            cid=cid, offer_id=offer_id,
            fill_size_usdt=decision.offer_amount_usdt or 0.0,
            fill_price=decision.offer_rate or 0.0,
            is_simulated=True,
        )
        return SubmittedOrder(
            cid=cid,
            venue_offer_id=offer_id,
            status="filled",
            raw_response={"offer_rate": str(decision.offer_rate)},
            reservation_ref=reference.bind_venue_offer(offer_id),
        )
