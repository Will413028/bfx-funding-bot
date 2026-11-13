"""Protocols + dataclasses for execution pipeline.

All concrete adapters (BitfinexLiveExecutor, hard/calibrated guards) conform
to these.

AccountContext carries credentials + per-account allocation cap.  The daemon
constructs it only from the canonical ExchangeAccount UUID (serialized as a
lowercase hyphenated string for existing event contracts); there is no default
or process-global realm fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from bfx_funding_bot.external.bitfinex.credentials import Credentials
from bfx_funding_bot.external.bitfinex.order_port import FundingCancelAllResult
from bfx_funding_bot.external.bitfinex.submit_outcomes import (
    SubmitAcknowledged,
    SubmitOutcome,
    SubmitOutcomeKind,
)
from bfx_funding_bot.modules.execution.contracts import (
    GuardResult,
    ReadyToSubmit,
    ReservationRef,
)
from bfx_funding_bot.modules.strategy import DecisionPayload

__all__ = [
    "AccountContext",
    "CancelPort",
    "Credentials",
    "ExecutorPort",
    "FundingCancelAllPort",
    "GuardResult",
    "GuardRule",
    "ReadyToSubmit",
    "SubmitOutcome",
    "SubmitOutcomeKind",
    "SubmittedOrder",
    "WriterLockHandle",
]


@dataclass(frozen=True, slots=True)
class AccountContext:
    """Per-account context bound to one canonical ExchangeAccount identity."""
    account_id: str
    credentials: Credentials
    capital_cell_id: str | None = None
    # Only the command boundary supplies this; guards must reuse its replayed state.
    command_session: AsyncSession | None = None


@dataclass(frozen=True, slots=True, init=False)
class SubmittedOrder:
    """Result envelope around one typed submit outcome.

    ``status`` is a read-only label derived from the outcome for metrics,
    tracing and the ORDER_SUBMIT event; control flow reads ``outcome_kind``.
    """

    venue_offer_id: str | None
    outcome: SubmitOutcome
    reservation_ref: ReservationRef | None

    def __init__(
        self,
        *,
        outcome: SubmitOutcome,
        venue_offer_id: str | None = None,
        reservation_ref: ReservationRef | None = None,
    ) -> None:
        if isinstance(outcome, SubmitAcknowledged):
            if venue_offer_id is not None and venue_offer_id != outcome.venue_offer_id:
                raise ValueError("venue_offer_id conflicts with acknowledged outcome")
            venue_offer_id = outcome.venue_offer_id
        elif venue_offer_id is not None:
            raise ValueError(
                f"{outcome.kind.value} outcome cannot carry venue_offer_id"
            )
        object.__setattr__(self, "venue_offer_id", venue_offer_id)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "reservation_ref", reservation_ref)

    @property
    def outcome_kind(self) -> SubmitOutcomeKind:
        return self.outcome.kind

    @property
    def status(self) -> str:
        return _STATUS_LABELS[self.outcome_kind]


_STATUS_LABELS: dict[SubmitOutcomeKind, str] = {
    SubmitOutcomeKind.ACKNOWLEDGED: "submitted",
    SubmitOutcomeKind.REJECTED: "failed",
    SubmitOutcomeKind.UNKNOWN: "unknown",
    SubmitOutcomeKind.NOT_SENT: "not_sent",
}


class GuardRule(Protocol):
    """Pre-trade guard. Single-method, fail-closed convention."""
    name: str

    async def evaluate(
        self, decision: DecisionPayload, ctx: AccountContext,
    ) -> GuardResult: ...


class WriterLockHandle(Protocol):
    """Anything that can live-verify whether this process holds the writer lock."""
    async def verify_held(self) -> bool: ...


class ExecutorPort(Protocol):
    """The submit path as its caller (the deployment reconciler) sees it.

    Implemented by the observe-only middlewares and by the account command gate
    at their core. The caller hands over only the request: the gate derives the
    submit's reservation reference itself, so none can be supplied here.
    """
    async def submit(self, ready: ReadyToSubmit, ctx: AccountContext) -> SubmittedOrder: ...


class CancelPort(Protocol):
    """A managed funding-offer cancel through the account command gate.

    The gate publishes CancelRequested / CancelAcknowledged around the venue
    call; the offer's end is observed by the ledger (WS foc is only a venue
    hint), so callers never touch the ledger.
    """
    async def cancel(
        self,
        *,
        venue_offer_id: str,
        signal_correlation_id: UUID,
        account_id: str,
        ctx: AccountContext,
    ) -> None: ...


@runtime_checkable
class FundingCancelAllPort(Protocol):
    """Venue funding cancel-all for one currency -- the kill switch's only venue write.

    It bypasses the command gate on purpose: provenance, uncertainty and the
    trading state do not apply to "cancel everything", which is the one write
    that must still work when those projections are what is broken. Callers
    own the writer-lock check and the durable record of each attempt.
    """
    async def cancel_all_funding_offers(
        self, *, currency: str, ctx: AccountContext,
    ) -> FundingCancelAllResult: ...

