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
from bfx_funding_bot.modules.execution.emit import (
    emit_order_fill,
    emit_order_submit,
)
from bfx_funding_bot.modules.execution.protocols import (
    AccountContext,
    SubmittedOrder,
)
from bfx_funding_bot.modules.marketfeed.schemas import (
    DecisionPayload,
    Phase,
    StrategyName,
)


class _AxiomProtocol(Protocol):
    async def emit(self, event: dict[str, Any]) -> None: ...


def _default_date() -> date:
    """Production default: UTC date. Tests inject via date_provider for determinism."""
    return datetime.now(UTC).date()


class EchoPaperExecutor:
    """Paper executor: emits order_submit + order_fill at decision price/size."""

    def __init__(
        self,
        *,
        axiom: _AxiomProtocol,
        phase: Phase,
        strategy: StrategyName,
        cell: str,
        date_provider: Callable[[], date] | None = None,
    ) -> None:
        self.axiom = axiom
        self.phase = phase
        self.strategy = strategy
        self.cell = cell
        # date_provider DI seam: prod uses UTC date; tests inject for determinism.
        self._date_provider = date_provider or _default_date

    async def submit(
        self, decision: DecisionPayload, ctx: AccountContext, *, cid: int | None = None,
    ) -> SubmittedOrder:
        # cid is centralized by ReservationEmittingMiddleware (A2). Direct callers
        # (tests) omit it -> fall back to deterministic generation. CC2: capture
        # date once at submit entry (midnight-race immune).
        if cid is None:
            submit_date = self._date_provider()
            cid = generate_cid(decision.signal_correlation_id, submit_date)
        # CC4: "paper_" prefix is invariant relied on by fill_tracker to skip
        # venue polling for simulated offers.
        offer_id = f"paper_{uuid.uuid4().hex[:12]}"

        await emit_order_submit(
            axiom=self.axiom,
            phase=self.phase, strategy=self.strategy, cell=self.cell,
            decision=decision, ctx=ctx,
            cid=cid, offer_id=offer_id,
            is_simulated=True, status="submitted",
        )
        await emit_order_fill(
            axiom=self.axiom,
            phase=self.phase, strategy=self.strategy, cell=self.cell,
            decision=decision, ctx=ctx,
            cid=cid, offer_id=offer_id,
            fill_size_usdt=decision.offer_amount_usdt or 0.0,
            fill_price=decision.offer_rate or 0.0,
            is_simulated=True,
        )
        return SubmittedOrder(
            cid=cid, venue_offer_id=offer_id, status="filled", raw_response=None,
        )
